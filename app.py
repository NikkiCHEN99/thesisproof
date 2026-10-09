"""投资命题验证台 —— Streamlit 前端。

输入投资命题 -> 点击「开始验证」 -> 调用 orchestrator.verify_thesis 走完整验证链路，
然后以三栏布局呈现：子问题 / 证据 / 冲突与结论。
"""

from datetime import date, timedelta

import streamlit as st

from orchestrator import verify_thesis

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
    """单条证据卡：来源 / 标题 / 日期 / 理由 / 片段 / 数字。"""
    status = ev.get("status", "unverifiable")
    color = STATUS_COLOR.get(status, "gray")
    with st.container(border=True):
        st.markdown(f":{color}[**{STATUS_LABEL.get(status, status)}**] · {ev.get('source_type', '')}")
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

result = st.session_state.get("result")

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

# ---------------------------------------------------------------------------
# 底部固定合规声明（始终显示）
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="compliance-footer">本产品仅用于投资研究辅助，不构成投资建议。</div>',
    unsafe_allow_html=True,
)
