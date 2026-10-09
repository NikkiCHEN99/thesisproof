"""投资命题验证台 —— Streamlit 前端。

输入投资命题 -> 点击「开始验证」 -> 调用 orchestrator.verify_thesis 走完整验证链路，
然后以三栏布局呈现：子问题 / 证据 / 冲突与结论。

结果区提供三个动作：
- 「保存为研究任务」：把当前结果落盘到 ``./saved_tasks/``（storage.py）
- 「继续追问」：把 {命题、子问题、证据卡、结论} 作为上下文，
  连同用户的新问题交给 ``llm_client.ask_followup`` 回答，
  历史存在 ``st.session_state``，回答里的 [E1] 编号可回查到具体证据卡。
- 「以此为基础比较」：把当前结果临时放进 ``st.session_state`` 的对比面板 A 侧。

左侧边栏列出已保存的研究任务，可选中并重新加载到页面。

页面顶部的「📊 命题对比」面板（Expander）可从已保存任务中挑两条并排对比：
命题、结论、冲突数量、支持/反对/无法验证证据数量，全部用 ``st.metric`` 呈现。
"""

from datetime import date, timedelta

import streamlit as st

from llm_client import ask_followup
from orchestrator import build_result_context, verify_thesis
from storage import list_tasks, load_task, save_task

st.set_page_config(
    page_title="投资命题验证台",
    page_icon="📊",
    layout="wide",
)

# ---------------------------------------------------------------------------
# 全局样式：底部固定合规声明
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      .compliance-footer {
        position: fixed;
        left: 0; right: 0; bottom: 0;
        z-index: 9999;
        padding: 8px 16px;
        text-align: center;
        font-size: 12px;
        letter-spacing: .3px;
        color: #6b7280;
        background: rgba(255, 255, 255, .96);
        border-top: 1px solid #e5e7eb;
        backdrop-filter: blur(6px);
      }
      /* 验证结果存在时改用内联声明（见页面末尾），跟随内容滚动，
         不与 st.chat_input 的固定浮动容器（高约 90~130px，随版本不同）重叠 */
      .compliance-inline {
        margin-top: 1.25rem;
        padding: 10px 16px;
        text-align: center;
        font-size: 12px;
        letter-spacing: .3px;
        color: #6b7280;
        border-top: 1px solid #e5e7eb;
      }
      /* 给固定底栏留出空间，避免遮挡内容 */
      .block-container { padding-bottom: 4rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("投资命题验证台")
st.caption("拆解命题 → 检索公告/新闻 → 分类证据 → 检测数字冲突 → 生成结论")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
STATUS_LABEL = {
    "support": "支持",
    "oppose": "反对",
    "unverifiable": "无法验证",
}
STATUS_COLOR = {
    "support": "green",
    "oppose": "red",
    "unverifiable": "gray",
}
VERDICT_LABEL = {
    "support": "支持",
    "partial_support": "部分支持",
    "oppose": "反对",
    "insufficient": "证据不足",
    "conflict": "存在冲突",
}


def sub_question_status(item: dict) -> tuple:
    """把单个子问题下的证据聚合成一个状态标签。"""
    counts = {"support": 0, "oppose": 0, "unverifiable": 0}
    for ev in item.get("evidence") or []:
        if ev.get("status") in counts:
            counts[ev["status"]] += 1

    if counts["oppose"]:
        return "存在反驳", "red", counts
    if counts["support"]:
        return "有支持", "green", counts
    return "无法验证", "gray", counts


def render_evidence_card(ev: dict) -> None:
    """单条证据卡：编号 / 来源 / 标题 / 日期 / 理由 / 片段 / 数字。

    开头展示的 `E3` 编号与「继续追问」回答里的 [E3] 引用一一对应。
    """
    status = ev.get("status", "unverifiable")
    color = STATUS_COLOR.get(status, "gray")
    with st.container(border=True):
        eid = ev.get("eid") or ""
        badge = f"`{eid}` · " if eid else ""
        st.markdown(
            f":{color}[**{STATUS_LABEL.get(status, status)}**] · {badge}"
            f"{ev.get('source_type', '')}"
        )
        st.markdown(f"**{(ev.get('title') or '（无标题）')[:60]}**")
        if ev.get("date"):
            st.caption(f"日期：{ev['date']}")
        if ev.get("reason"):
            st.markdown(f":{color}[{ev['reason']}]")

        snippet = (ev.get("snippet") or "").strip()
        if len(snippet) > 120:
            st.write(snippet[:120] + "…")
            with st.expander("展开完整片段"):
                st.write(snippet)
        elif snippet:
            st.write(snippet)

        for claim in ev.get("numeric_claims") or []:
            if isinstance(claim, dict) and claim.get("metric"):
                st.markdown(f"`{claim.get('metric')} = {claim.get('value')}{claim.get('unit') or ''}`")

        if ev.get("url"):
            st.markdown(f"[查看原文]({ev['url']})")


# ---------------------------------------------------------------------------
# 会话状态：追问历史 / 上下文 + 对比面板 A、B 两侧，跨 rerun 保留
# ---------------------------------------------------------------------------
st.session_state.setdefault("followups", [])  # [{"role", "text", "citations", "refused", "insufficient"}]
st.session_state.setdefault("followup_context", "")  # 本次验证结果压成的追问上下文
st.session_state.setdefault("compare_a", None)  # {"thesis", "source", "result"}
st.session_state.setdefault("compare_b", None)


# ---------------------------------------------------------------------------
# 命题对比：把一次验证结果抽成可比较的结构化指标
# ---------------------------------------------------------------------------
VERDICT_COLOR = {"support": "green", "partial_support": "orange", "oppose": "red"}
COMPARE_EMPTY_LABEL = "— 未选择 —"


def result_stats(result: dict) -> dict:
    """抽出用于并排对比的指标：结论、冲突数、三类证据数、子问题数。"""
    result = result if isinstance(result, dict) else {}
    evidence = [ev for ev in (result.get("evidence") or []) if isinstance(ev, dict)]

    counts = {"support": 0, "oppose": 0, "unverifiable": 0}
    for ev in evidence:
        status = ev.get("status")
        counts[status if status in counts else "unverifiable"] += 1

    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    v_key = verdict.get("verdict") or "insufficient"
    return {
        "verdict": v_key,
        "verdict_label": VERDICT_LABEL.get(v_key, v_key),
        "summary": verdict.get("summary") or "—",
        "conflicts": len(result.get("conflicts") or []),
        "sub_questions": len(result.get("sub_questions") or []),
        "evidence_total": len(evidence),
        "support": counts["support"],
        "oppose": counts["oppose"],
        "unverifiable": counts["unverifiable"],
        "subject": result.get("subject") or "未识别",
        "time_range": result.get("time_range") or "未指定",
    }


def _slot_from_record(record: dict, source: str) -> dict:
    """把 storage.load_task 的返回包装成对比面板的一侧。"""
    return {
        "thesis": record.get("thesis") or "",
        "source": source,
        "result": record.get("result") or {},
    }


def task_option_labels(tasks: list) -> list:
    """任务下拉框的选项标签：首项为「未选择」，其余为「命题 ｜ 保存时间」。

    直接以标签字符串作为选项值（不用 ``format_func``）：Streamlit 的
    ``format_func`` + 非字符串 options 组合在回读选中值时会错位，
    同一份标签同时用于「选中值 → 任务」的反查。
    """
    labels = [COMPARE_EMPTY_LABEL]
    seen: dict = {}
    for task in tasks:
        base = f"{(task.get('thesis') or '（无命题）')[:20]} ｜ {task.get('saved_at') or '—'}"
        seen[base] = seen.get(base, 0) + 1
        labels.append(base if seen[base] == 1 else f"{base}（{seen[base]}）")
    return labels


def _resolve_task_index(labels: list, picked) -> int:
    """把下拉框选中值解析成 tasks 下标；「未选择」或无效值返回 -1。"""
    if not isinstance(picked, str) or picked not in labels:
        return -1
    return labels.index(picked) - 1


def _apply_compare_pick(side: str) -> None:
    """下拉框变化回调：把选中的已保存任务读进 A / B 侧（选「未选择」则清空该侧）。"""
    tasks = list_tasks()
    idx = _resolve_task_index(task_option_labels(tasks), st.session_state.get(f"compare_pick_{side}"))
    slot_key = f"compare_{side}"

    if idx < 0:
        st.session_state[slot_key] = None
        return

    meta = tasks[idx]
    try:
        record = load_task(meta["filename"])
    except Exception as exc:  # noqa: BLE001 - 文件缺失 / 损坏只提示，不让页面崩掉
        st.session_state[slot_key] = None
        st.session_state["flash_error"] = f"读取对比任务失败：{type(exc).__name__}: {exc}"
        return

    st.session_state[slot_key] = _slot_from_record(
        record, source=meta.get("saved_at") or meta["filename"]
    )


def _pick_compare_a() -> None:
    _apply_compare_pick("a")


def _pick_compare_b() -> None:
    _apply_compare_pick("b")


def _use_current_as_compare() -> None:
    """「以此为基础比较」回调：把当前验证结果临时放到对比面板 A 侧。"""
    current = st.session_state.get("result")
    if not isinstance(current, dict) or not current:
        st.session_state["flash_error"] = "还没有验证结果，先验证一条命题再试。"
        return

    st.session_state["compare_a"] = {
        "thesis": st.session_state.get("result_thesis") or current.get("thesis") or "",
        "source": "当前验证结果",
        "result": current,
    }
    # A 侧来自「当前结果」时不指向任何已保存任务，把下拉框同步回空
    st.session_state["compare_pick_a"] = COMPARE_EMPTY_LABEL
    st.session_state["flash"] = "已把当前结果放到顶部「命题对比」的 A 侧，再选一个任务作为 B 侧即可并排对比。"


def _clear_compare_a() -> None:
    st.session_state["compare_a"] = None
    st.session_state["compare_pick_a"] = COMPARE_EMPTY_LABEL


def _clear_compare_b() -> None:
    st.session_state["compare_b"] = None
    st.session_state["compare_pick_b"] = COMPARE_EMPTY_LABEL


def render_compare_panel(slot_a: dict, slot_b: dict) -> None:
    """A / B 并排对比：命题、结论、冲突数量、三类证据数量（st.metric + Δ）。"""
    sa = result_stats(slot_a.get("result"))
    sb = result_stats(slot_b.get("result"))

    if sa["verdict"] != sb["verdict"]:
        st.warning(
            f"结论不一致：A = {sa['verdict_label']}，B = {sb['verdict_label']}"
            f"（冲突 {sa['conflicts']} vs {sb['conflicts']}）"
        )
    else:
        st.success(f"结论一致：两侧均为「{sa['verdict_label']}」")

    left, right = st.columns(2, gap="medium")

    def _side(col, slot: dict, stat: dict, other: dict, name: str) -> None:
        with col:
            st.markdown(f"#### 任务 {name}")
            st.markdown(f"**命题**：{slot.get('thesis') or '（无命题）'}")
            st.caption(
                f"来源：{slot.get('source') or '—'}　｜　主体：{stat['subject']}"
                f"　｜　时间范围：{stat['time_range']}"
            )
            st.markdown(
                f"**结论**：:{VERDICT_COLOR.get(stat['verdict'], 'gray')}"
                f"[{stat['verdict_label']}]"
            )
            st.caption((stat["summary"] or "—")[:200])

            row1 = st.columns(2)
            row1[0].metric(
                "冲突数量", stat["conflicts"],
                delta=stat["conflicts"] - other["conflicts"], delta_color="off",
            )
            row1[1].metric(
                "子问题", stat["sub_questions"],
                delta=stat["sub_questions"] - other["sub_questions"], delta_color="off",
            )

            row2 = st.columns(3)
            row2[0].metric(
                "支持", stat["support"],
                delta=stat["support"] - other["support"], delta_color="off",
            )
            row2[1].metric(
                "反对", stat["oppose"],
                delta=stat["oppose"] - other["oppose"], delta_color="off",
            )
            row2[2].metric(
                "无法验证", stat["unverifiable"],
                delta=stat["unverifiable"] - other["unverifiable"], delta_color="off",
            )
            st.caption(
                f"证据总数 {stat['evidence_total']}"
                f"（Δ {stat['evidence_total'] - other['evidence_total']:+d}）"
            )

    _side(left, slot_a, sa, sb, "A")
    _side(right, slot_b, sb, sa, "B")
    st.caption("Δ 为该指标相对另一侧的差值，只表示数量多寡，不含优劣判断。")


# 已保存任务列表：侧边栏与对比面板共用（只读一次，避免重复扫描目录）
try:
    saved_tasks = list_tasks()
except Exception as exc:  # noqa: BLE001 - 磁盘异常不应拖垮页面
    saved_tasks = []
    _saved_tasks_error = f"读取任务列表失败：{type(exc).__name__}: {exc}"
else:
    _saved_tasks_error = ""

# ---------------------------------------------------------------------------
# 页面顶部：命题对比（Expander）
# ---------------------------------------------------------------------------
with st.expander(
    "📊 命题对比",
    expanded=bool(st.session_state.get("compare_a") or st.session_state.get("compare_b")),
):
    st.caption(
        "从已保存的研究任务里各选一条并排对比；也可以先在下方验证一条命题，"
        "再点结果区的「🔀 以此为基础比较」把当前结果放到 A 侧，然后挑 B 侧。"
    )

    if not saved_tasks:
        st.caption("还没有已保存的任务。先在下方验证一条命题，再点「💾 保存为研究任务」。")
    else:
        options = task_option_labels(saved_tasks)
        pick_a, pick_b = st.columns(2, gap="medium")
        with pick_a:
            st.selectbox(
                "对比 A（左栏）", options=options, key="compare_pick_a", on_change=_pick_compare_a
            )
        with pick_b:
            st.selectbox(
                "对比 B（右栏）", options=options, key="compare_pick_b", on_change=_pick_compare_b
            )

    slot_a = st.session_state.get("compare_a")
    slot_b = st.session_state.get("compare_b")

    if slot_a or slot_b:
        chip_a, chip_b = st.columns(2, gap="medium")
        for col, slot, name, clearer in (
            (chip_a, slot_a, "A", _clear_compare_a),
            (chip_b, slot_b, "B", _clear_compare_b),
        ):
            with col:
                if slot:
                    st.markdown(f"`{name}` **{(slot.get('thesis') or '（无命题）')[:28]}**")
                    st.caption(f"来源：{slot.get('source') or '—'}")
                    st.button(f"移除 {name} 侧", key=f"compare_clear_{name}", on_click=clearer)
                else:
                    st.caption(f"{name} 侧待选择")

    if slot_a and slot_b:
        st.divider()
        if (slot_a.get("thesis") or "") == (slot_b.get("thesis") or "") and (
            slot_a.get("source") or ""
        ) == (slot_b.get("source") or ""):
            st.info("两侧选的是同一条任务，各项指标应完全一致。")
        render_compare_panel(slot_a, slot_b)
    elif slot_a or slot_b:
        st.info("已选好一侧，再选另一侧即可并排对比。")


# ---------------------------------------------------------------------------
# 输入区
# ---------------------------------------------------------------------------
thesis = st.text_input("投资命题", placeholder="请输入投资命题")

col_start, col_end = st.columns(2)
with col_start:
    time_start = st.date_input("开始日期", value=date.today() - timedelta(days=730))
with col_end:
    time_end = st.date_input("结束日期", value=date.today())

if st.button("开始验证", type="primary"):
    query = thesis.strip()
    if not query:
        st.warning("请先输入一条投资命题，再开始验证。")
    else:
        with st.spinner("验证中…（拆解命题 → 检索公告/新闻 → 分类证据 → 检测冲突 → 生成结论）"):
            st.session_state["result"] = verify_thesis(
                query, time_start=str(time_start), time_end=str(time_end)
            )
            st.session_state["result_thesis"] = query
        # 换了一条命题：追问历史与上下文都作废，避免串台
        st.session_state["followups"] = []
        st.session_state["followup_context"] = build_result_context(
            st.session_state["result"]
        )

result = st.session_state.get("result")


# ---------------------------------------------------------------------------
# 交互回调：Streamlit 约定 —— 写盘 / 换结果这类「先改状态再重绘」的动作放回调里，
# 回调在脚本重跑前执行，因此侧边栏能当轮看到新保存的任务。
# 不要用 st.button + st.rerun()：1.30 下按钮的触发态会在显式 rerun 后残留，导致无限重跑。
# ---------------------------------------------------------------------------
def _save_current_result() -> None:
    """「保存为研究任务」回调：把当前结果写入 ./saved_tasks/。"""
    current = st.session_state.get("result") or {}
    thesis_text = st.session_state.get("result_thesis") or current.get("thesis") or ""
    try:
        filename = save_task(thesis_text, current)
    except Exception as exc:  # noqa: BLE001 - 磁盘异常只提示，不让页面崩掉
        st.session_state["flash_error"] = f"保存失败：{type(exc).__name__}: {exc}"
    else:
        st.session_state["flash"] = f"保存成功：{filename}"
        print(f"[app] 保存研究任务 {filename}")


def _load_selected_task() -> None:
    """侧边栏「加载」回调：把选中任务重新载入页面。"""
    tasks = list_tasks()
    idx = _resolve_task_index(task_option_labels(tasks), st.session_state.get("task_pick"))

    if idx < 0 or idx >= len(tasks):
        st.session_state["flash_error"] = "所选任务已失效（可能已被删除），请重新选择。"
        return

    try:
        record = load_task(tasks[idx]["filename"])
    except Exception as exc:  # noqa: BLE001 - 文件丢失 / 损坏时给出提示
        st.session_state["flash_error"] = f"加载失败：{type(exc).__name__}: {exc}"
        return

    st.session_state["result"] = record["result"]
    st.session_state["result_thesis"] = record["thesis"]
    # 换了一条命题：追问历史与上下文都作废，避免串台
    st.session_state["followups"] = []
    st.session_state["followup_context"] = build_result_context(record["result"])
    st.session_state["flash"] = f"已加载研究任务：{record['filename']}"


def _clear_followups() -> None:
    """「清空追问记录」回调。"""
    st.session_state["followups"] = []


# ---------------------------------------------------------------------------
# 侧边栏：我的研究任务（storage.list_tasks / load_task）
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("📂 我的研究任务")

    if _saved_tasks_error:
        st.error(_saved_tasks_error)

    if not saved_tasks:
        st.caption("还没有保存的任务。验证一条命题后，点结果区下方的「💾 保存为研究任务」。")
    else:
        st.selectbox(
            "选择任务",
            options=task_option_labels(saved_tasks),
            key="task_pick",
        )
        col_load, col_meta = st.columns([1, 1])
        with col_load:
            st.button("加载", type="primary", on_click=_load_selected_task)
        with col_meta:
            st.caption(f"共 {len(saved_tasks)} 个")
        st.caption("任务文件存放在 `./saved_tasks/`（云端实例重启后会丢失）。")

# 提示条：保存成功 / 加载结果（回调写入，这里消费掉）
_flash = st.session_state.pop("flash", None)
if _flash:
    st.success(_flash)
_flash_error = st.session_state.pop("flash_error", None)
if _flash_error:
    st.error(_flash_error)

if result:
    st.divider()
    st.markdown(
        f"**验证对象**：{st.session_state.get('result_thesis', '')}"
        f"　｜　主体：`{result.get('subject') or '未识别'}`"
        f"　｜　时间范围：{result.get('time_range') or '未指定'}"
    )

    if result.get("errors"):
        with st.expander(f"⚠ 过程中有 {len(result['errors'])} 条错误记录"):
            for err in result["errors"]:
                st.markdown(f"- {err}")

    left, middle, right = st.columns([1, 1.7, 1.15], gap="medium")

    # ---------------- 左栏：子问题列表 + 状态 ----------------
    with left:
        st.subheader(f"子问题（{len(result['sub_questions'])}）")
        if not result["sub_questions"]:
            st.info("没有拆解出子问题。")
        for item in result["sub_questions"]:
            label, color, counts = sub_question_status(item)
            with st.container(border=True):
                st.markdown(f":{color}[**{label}**] · `{item.get('id')}`")
                st.markdown(f"**{item.get('question')}**")
                st.caption(
                    f"维度：{item.get('dimension') or '—'}　｜　"
                    f"证据类型：{item.get('evidence_type') or '—'}"
                )
                st.caption(
                    f"支持 {counts['support']}　反对 {counts['oppose']}　"
                    f"无法验证 {counts['unverifiable']}"
                )

    # ---------------- 中栏：证据卡三栏 ----------------
    with middle:
        st.subheader("证据")

        grouped = {"support": [], "oppose": [], "unverifiable": []}
        for ev in result["evidence"]:
            grouped.setdefault(ev.get("status", "unverifiable"), []).append(ev)

        show_all = st.toggle("显示全部证据", value=False)
        limit = 99 if show_all else 5

        ev_cols = st.columns(3, gap="small")
        for col, status in zip(ev_cols, ["support", "oppose", "unverifiable"]):
            items = grouped.get(status) or []
            with col:
                st.markdown(
                    f":{STATUS_COLOR[status]}[**{STATUS_LABEL[status]}（{len(items)}）**]"
                )
                if not items:
                    st.caption("—")
                for ev in items[:limit]:
                    render_evidence_card(ev)
                if len(items) > limit:
                    st.caption(f"另有 {len(items) - limit} 条未显示，可打开「显示全部证据」")

    # ---------------- 右栏：冲突标记 + 结论卡 ----------------
    with right:
        st.subheader(f"冲突标记（{len(result['conflicts'])}）")
        if not result["conflicts"]:
            st.caption("未检测到同一指标的数字冲突。")
        for conflict in result["conflicts"]:
            with st.container(border=True):
                st.markdown(f":red[**⚠ 冲突｜{conflict.get('metric')}**]")
                st.markdown(f":red[{conflict.get('note', '')}]")
                for v in conflict.get("values") or []:
                    st.markdown(
                        f":red[- {v.get('value')}{v.get('unit') or ''}"
                        f"（{v.get('source_type')} {v.get('date')}）]"
                    )

        st.divider()
        st.subheader("结论")
        verdict = result.get("verdict") or {}
        v_key = verdict.get("verdict", "insufficient")
        v_color = {"support": "green", "partial_support": "orange", "oppose": "red"}.get(
            v_key, "gray"
        )
        with st.container(border=True):
            st.markdown(f":{v_color}[### {VERDICT_LABEL.get(v_key, v_key)}]")
            st.markdown(verdict.get("summary") or "—")

            conditions = verdict.get("change_conditions") or []
            if conditions:
                st.markdown("**结论变化条件**")
                for cond in conditions:
                    st.markdown(f"- {cond}")

    # ---------------- 结论卡 / 冲突卡之后：保存为研究任务 / 以此为基础比较 ----------------
    st.divider()
    col_save, col_compare, col_note = st.columns([1, 1.1, 2.6], gap="medium")
    with col_save:
        st.button("💾 保存为研究任务", on_click=_save_current_result)
    with col_compare:
        st.button("🔀 以此为基础比较", on_click=_use_current_as_compare)
    with col_note:
        st.caption(
            "保存后在左侧「📂 我的研究任务」中可随时重新加载；"
            "点「以此为基础比较」会把当前结果放到顶部「📊 命题对比」的 A 侧，"
            "再从下拉框挑另一条任务作为 B 侧并排对比。"
        )

    # ---------------- 继续追问 ----------------
    st.divider()
    st.subheader("继续追问")
    st.caption(
        "基于本次验证的命题、子问题、证据卡与结论作答，回答中的 `E1` 编号可回查到上方证据卡；"
        "仅做事实梳理，不提供涨跌预测或买卖建议。"
    )

    # 证据编号 -> 证据对象，用于把 [E3] 这类引用还原成具体来源
    ev_by_id = {
        ev.get("eid"): ev
        for ev in (result.get("evidence") or [])
        if isinstance(ev, dict) and ev.get("eid")
    }

    # 追问上下文按需构建并缓存（避免每次 rerun 重算）
    if not st.session_state.get("followup_context"):
        st.session_state["followup_context"] = build_result_context(result)

    def render_assistant(msg: dict) -> None:
        """助手气泡：回答正文 + 引用证据编号 + 引用原文。"""
        if msg.get("refused"):
            st.markdown(f":orange[{msg['text']}]")
            st.caption("合规提示：涉及涨跌预测或操作建议的提问，一律只做事实梳理。")
            return

        st.markdown(msg["text"])

        citations = [c for c in (msg.get("citations") or []) if c in ev_by_id]
        if citations:
            st.caption("引用证据：" + "　".join(f"`{c}`" for c in citations))
            with st.expander(f"查看引用的 {len(citations)} 条证据原文"):
                for cid in citations:
                    ev = ev_by_id[cid]
                    st.markdown(
                        f"**`{cid}`** ｜ {STATUS_LABEL.get(ev.get('status'), '')} ｜ "
                        f"{ev.get('source_type')} ｜ {ev.get('date') or '—'}"
                    )
                    st.markdown(f"{ev.get('title') or '（无标题）'}")
                    snippet = (ev.get("snippet") or "").strip()
                    if snippet:
                        st.caption(snippet[:300] + ("…" if len(snippet) > 300 else ""))
                    if ev.get("url"):
                        st.markdown(f"[查看原文]({ev['url']})")
                    st.divider()

        if msg.get("insufficient"):
            st.caption("提示：该问题未被本次检索到的证据覆盖，回答已按「事实梳理」范围收口。")

    # 历史追问（来自 st.session_state，刷新/重跑不丢）
    for msg in st.session_state["followups"]:
        with st.chat_message(msg.get("role", "assistant")):
            if msg.get("role") == "user":
                st.markdown(msg.get("text", ""))
            else:
                render_assistant(msg)

    # 追问输入框（固定在页面底部）
    prompt = st.chat_input("针对结论继续追问…")
    if prompt:
        question = prompt.strip()
        history = list(st.session_state["followups"])

        st.session_state["followups"].append({"role": "user", "text": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("正在基于本次证据回答…"):
                try:
                    answer = ask_followup(
                        st.session_state["followup_context"], question, history=history
                    )
                except Exception as exc:  # noqa: BLE001 - 页面不能因为追问挂掉
                    answer = {
                        "refused": False,
                        "answer": f"回答失败：{type(exc).__name__}: {exc}",
                        "citations": [],
                        "insufficient": True,
                    }
            render_assistant(
                {
                    "role": "assistant",
                    "text": answer.get("answer", ""),
                    "citations": answer.get("citations") or [],
                    "refused": bool(answer.get("refused")),
                    "insufficient": bool(answer.get("insufficient")),
                }
            )

        st.session_state["followups"].append(
            {
                "role": "assistant",
                "text": answer.get("answer", ""),
                "citations": answer.get("citations") or [],
                "refused": bool(answer.get("refused")),
                "insufficient": bool(answer.get("insufficient")),
            }
        )

    if st.session_state["followups"]:
        st.button("清空追问记录", on_click=_clear_followups)

    # 追问输入框是固定在视口底部的浮动容器（不同 Streamlit 版本高度不同），
    # 给页面底部留出足够空间，保证滚动到最底时内联合规声明不会被输入框盖住
    st.markdown(
        "<style>.block-container { padding-bottom: 11rem; }</style>",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# 合规声明：未验证时固定在视口底部；验证后内联在页面末尾（追问输入框上方），
# 避免固定横条压住 st.chat_input 的浮动容器
# ---------------------------------------------------------------------------
COMPLIANCE_TEXT = "本产品仅用于投资研究辅助，不构成投资建议。"
if result:
    st.markdown(f'<div class="compliance-inline">{COMPLIANCE_TEXT}</div>', unsafe_allow_html=True)
else:
    st.markdown(f'<div class="compliance-footer">{COMPLIANCE_TEXT}</div>', unsafe_allow_html=True)
