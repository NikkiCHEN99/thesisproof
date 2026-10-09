"""投资命题验证编排器。

``verify_thesis(thesis)`` 把一条投资命题走完整条验证链路：

    1. parse_thesis 拆解子问题
    2. 并发检索：每个子问题各调 search_notice / search_news
    3. 证据分类：support / oppose / unverifiable
    4. 冲突检测：同一指标出现多个不同数字
    5. LLM 汇总结论（verdict / summary / change_conditions）
    6. 返回结构化结果

来源层状态（每个子问题 × 每个来源一张卡，见 ``sources``）::

    ok      检索成功且有结果
    missing 检索成功但结果为空      -> 记录「<来源>层无证据」
    failed  重试 1 次后仍失败        -> 记录「<来源>层失败」

失败兜底：
    - 全部来源都失败 -> 不调 LLM，直接 ``insufficient`` / ``数据获取失败``
    - 所有来源都无证据 -> 不调 LLM，直接 ``insufficient`` / ``未检索到相关证据``
    - 结论 LLM 返回非法 JSON -> 重试 1 次，仍失败 -> ``insufficient`` / ``结论生成失败``
    - 命题涉及预测涨跌 / 买卖时机 / 目标价 -> 合规拒绝，
      ``verdict.refused=true``、summary 固定为「仅做事实梳理」

合规约束（禁止涨跌/买卖/推荐用语、禁止承诺收益、结论五选一、
变化条件用「若……则结论转弱/转强/存疑」）定义在 ``llm_client.COMPLIANCE_RULES``，
提示词本体在 ``llm_client.CONCLUSION_PROMPT``。
预测类命题由两道闸门拦截：``llm_client.detect_prediction_intent``（命题级前置，确定性）
+ 结论 LLM 的 ``refused`` 字段。

并发限制：iFinD 检索用 ``asyncio.Semaphore(2)``；LLM 调用用独立信号量。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from datetime import date, timedelta

import llm_client
from ifind_client import search_news, search_notice

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------
IFIND_CONCURRENCY = 2  # 需求指定：iFinD 并发上限 2
LLM_CONCURRENCY = 4  # LLM 并发上限
SEARCH_SIZE = 4  # 每个子问题、每个来源取几条
SNIPPET_CHARS = 350  # 送进 LLM 的片段截断长度
MAX_ITEMS_PER_SQ = 8  # 每个子问题最多送几条证据给 LLM
DEFAULT_LOOKBACK_DAYS = 730  # 默认回溯天数
VALUE_TOLERANCE = 1e-4  # 相对容差 0.01%：吸收四舍五入，但保留 0.1% 级的真实差异

VALID_STATUS = {"support", "oppose", "unverifiable"}
VALID_VERDICT = {"partial_support", "support", "oppose", "insufficient", "conflict"}

# 来源层（公告层 / 新闻层）状态
SOURCE_STATUS = {"ok", "missing", "failed"}
SOURCE_LABEL = {"公告": "公告层", "新闻": "新闻层"}
SOURCE_ATTEMPTS = 2  # 首次 + 失败重试 1 次
SOURCE_RETRY_WAIT = 5.0  # 重试前等待秒数（iFinD 429 提示「请求过于频繁」，留足间隔）

VERDICT_ATTEMPTS = 2  # 结论生成：首次 + 非法 JSON 重试 1 次

# 兜底结论模板（refused 表示是否触发合规拒绝）
FALLBACK_VERDICT = {
    "refused": False,
    "verdict": "insufficient",
    "summary": "结论生成失败",
    "change_conditions": [],
}
DATA_FAILED_VERDICT = {
    "refused": False,
    "verdict": "insufficient",
    "summary": "数据获取失败",
    "change_conditions": [],
}
NO_EVIDENCE_VERDICT = {
    "refused": False,
    "verdict": "insufficient",
    "summary": "未检索到相关证据",
    "change_conditions": [],
}
# 合规拒绝：命题涉及预测涨跌 / 买卖时机 / 目标价时使用，summary 固定为「仅做事实梳理」
REFUSAL_VERDICT = {
    "refused": True,
    "verdict": "insufficient",
    "summary": llm_client.REFUSAL_SUMMARY,
    "change_conditions": [],
}

# LLM 有时会直接回中文选项，统一映射回英文 key
VERDICT_ALIAS = {
    "支持": "support",
    "部分支持": "partial_support",
    "反对": "oppose",
    "证据不足": "insufficient",
    "存在冲突": "conflict",
}

ORCH_SYSTEM = (
    "你是一个严谨的投研证据审核员，只输出合法 JSON，不要输出任何解释文字。"
    "只做事实判定：禁止出现「涨/跌/买入/卖出/推荐」等价格方向或操作用语，禁止承诺收益。"
)

# ---------------------------------------------------------------------------
# 提示词模板（统一用 __XXX__ 占位 + str.replace，避免 str.format 误吃 JSON 大括号）
#
# 注意：结论提示词（含合规红线）已集中到 ``llm_client.CONCLUSION_PROMPT``，
# 通过 ``llm_client.build_conclusion_prompt(...)`` 渲染。
# ---------------------------------------------------------------------------
CLASSIFY_PROMPT = """你在做投资命题的证据审核。

子问题：__QUESTION__
｜验证维度：__DIMENSION__
｜期望证据类型：__EVIDENCE_TYPE__

下面是从公告/新闻中检索到的证据片段，请**逐条**判断它对上述子问题属于哪一类：

- support（支持）：片段含有明确数字或事实，方向与子问题一致，能支撑子问题成立
- oppose（矛盾）：片段含有明确数字或事实，方向与子问题相矛盾
- unverifiable（无法验证）：没有明确数字、表述模糊、或与子问题无关

同时抽取片段中与子问题**直接相关**的关键数字指标，例如
「2024年茅台酒营业收入：1459.28 亿元」。
没有关键数字就返回空数组。metric 必须写清年份与口径；同一指标在不同片段里必须使用
**完全一致**的 metric 字符串，否则无法比对出冲突。

只输出 JSON：
{
"evidence": [
  {"index": 0, "status": "support", "reason": "30字内理由",
   "numeric_claims": [{"metric": "指标名", "value": 1459.28, "unit": "亿元"}]}
]
}

证据片段：
__EVIDENCE__"""


# ---------------------------------------------------------------------------
# 数值 / 指标工具
# ---------------------------------------------------------------------------
_MONEY_SCALE = {
    "元": 1.0,
    "千元": 1e3,
    "万元": 1e4,
    "万": 1e4,
    "百万元": 1e6,
    "亿元": 1e8,
    "亿": 1e8,
}
_PERCENT_UNITS = {"%", "％", "个百分点", "pct"}

_PUNCT_RE = re.compile(r"[\s，。、；：（）()【】\[\]「」\"'·,.:;!?%％\-—_/\\|]+")


def _to_float(value) -> float | None:
    """尽量把模型给出的值转成 float；转不了返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("，", "").replace("%", "").replace("％", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _norm_metric(metric: str) -> str:
    """指标名归一化：去掉空白与标点，便于同一指标的字面比对。"""
    return _PUNCT_RE.sub("", str(metric or "")).lower()


def _canonical_value(value: float, unit: str):
    """把数值换算到统一量纲，避免「亿元 vs 万元 vs 元」被误判成冲突。

    返回 ``(换算后的值, 量纲标签)``。相同量纲标签的数值可以直接比对；
    认不出的单位（如「吨」「万辆」）按原单位单独成组，只在同单位之间比较。
    """
    u = str(unit or "").strip().replace(" ", "")
    if u in _MONEY_SCALE:
        return value * _MONEY_SCALE[u], "money"
    if u in _PERCENT_UNITS:
        return value, "percent"
    return value, f"unit:{u}"


def detect_conflicts(evidence: list) -> list:
    """检测「同一指标多个不同数字」。

    按 (归一化指标名, 量纲) 分组，组内数值超出相对容差即视为冲突。
    """
    buckets: dict = {}

    for ev in evidence:
        for claim in ev.get("numeric_claims") or []:
            if not isinstance(claim, dict):
                continue
            metric = claim.get("metric")
            value = _to_float(claim.get("value"))
            if not metric or value is None:
                continue

            norm_value, unit_key = _canonical_value(value, claim.get("unit"))
            key = (_norm_metric(metric), unit_key)

            bucket = buckets.setdefault(key, {"metric": metric, "values": []})
            bucket["values"].append(
                {
                    "value": value,
                    "unit": claim.get("unit") or "",
                    "norm_value": norm_value,
                    "sub_question_id": ev.get("sub_question_id"),
                    "source_type": ev.get("source_type"),
                    "title": ev.get("title"),
                    "date": ev.get("date"),
                }
            )

    conflicts = []
    for (_, _), bucket in buckets.items():
        values = bucket["values"]
        if len(values) < 2:
            continue

        base = values[0]["norm_value"]
        differs = False
        for v in values[1:]:
            denom = max(abs(base), abs(v["norm_value"])) or 1.0
            if abs(v["norm_value"] - base) / denom > VALUE_TOLERANCE:
                differs = True
                break
        if not differs:
            continue

        distinct = sorted({round(v["norm_value"], 6) for v in values})
        conflicts.append(
            {
                "metric": bucket["metric"],
                "distinct_count": len(distinct),
                "values": [
                    {
                        "value": v["value"],
                        "unit": v["unit"],
                        "sub_question_id": v["sub_question_id"],
                        "source_type": v["source_type"],
                        "title": v["title"],
                        "date": v["date"],
                    }
                    for v in values
                ],
                "note": f"同一指标出现 {len(distinct)} 个不同数值",
            }
        )

    return conflicts


# ---------------------------------------------------------------------------
# 异步执行辅助
# ---------------------------------------------------------------------------
def _run_async(coro):
    """在同步函数里安全地跑协程。

    若当前线程已有运行中的事件循环（Jupyter / 某些 Streamlit 运行方式），
    直接在原线程 ``asyncio.run`` 会报错，这里改用子线程执行。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


async def _guarded(sem: asyncio.Semaphore, fn, *args, **kwargs):
    """在信号量保护下把同步阻塞函数丢到线程池执行。"""
    async with sem:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _format_evidence_block(items: list) -> str:
    lines = []
    for i, ev in enumerate(items):
        snippet = (ev.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS] + "…"
        lines.append(
            f"[{i}] 来源：{ev.get('source_type')}｜标题：{ev.get('title')}｜日期：{ev.get('date')}\n"
            f"片段：{snippet}"
        )
    return "\n\n".join(lines)


def _normalize_item(source_type: str, sq_id: str, raw: dict) -> dict:
    """把 iFinD 返回的中文键条目归一化成统一的证据条目。"""
    return {
        "sub_question_id": sq_id,
        "source_type": source_type,
        "title": raw.get("title") or raw.get("公告标题") or raw.get("资讯标题") or "",
        "date": raw.get("date") or raw.get("日期") or "",
        "url": raw.get("url") or raw.get("URL") or "",
        "snippet": raw.get("snippet")
        or raw.get("公告片段内容")
        or raw.get("资讯内容")
        or "",
        "status": "unverifiable",
        "reason": "",
        "numeric_claims": [],
    }


async def _fetch_source(
    source_type: str,
    fn,
    sq_id: str,
    query: str,
    time_start: str,
    time_end: str,
    size: int,
    sem: asyncio.Semaphore,
) -> dict:
    """检索单个来源（公告 / 新闻），返回 ``{"card": ..., "items": [...]}``。

    ``card`` 是该来源的「证据卡」，含 ``status``：

        ok      检索成功且有结果
        missing 检索成功但结果为空   -> note「<来源>层无证据」
        failed  重试后仍失败         -> note「<来源>层失败」

    重试策略：失败（接口报错 / 网络异常）后重试 1 次，仍失败才判 failed。
    这里向 ifind_client 传 ``max_attempts=1``，让它单次尝试，
    由本层独占「重试 1 次」的策略，避免两层重试叠加成 6 次请求。
    """
    label = SOURCE_LABEL[source_type]
    tag = f"[orchestrator] {sq_id} {label}"
    print(f"{tag} 检索 query={query!r}")

    err_msgs: list = []
    for attempt in range(1, SOURCE_ATTEMPTS + 1):
        errs: list = []
        try:
            raw_items = await _guarded(
                sem, fn, query, time_start, time_end, size, errors=errs, max_attempts=1
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，单个来源失败不影响整体流程
            errs.append(f"{type(exc).__name__}: {exc}")
            raw_items = []

        if not errs:
            # 接口层面成功（结果可能为空）
            items = [
                _normalize_item(source_type, sq_id, raw)
                for raw in (raw_items or [])
                if isinstance(raw, dict)
            ]
            if items:
                print(f"{tag} → ok（{len(items)} 条）")
                return {
                    "card": {
                        "status": "ok",
                        "note": f"{label}检索到 {len(items)} 条证据",
                        "count": len(items),
                    },
                    "items": items,
                }
            print(f"{tag} → missing（{label}无证据）")
            return {
                "card": {"status": "missing", "note": f"{label}无证据", "count": 0},
                "items": [],
            }

        err_msgs = errs
        print(f"{tag} 第 {attempt}/{SOURCE_ATTEMPTS} 次失败：{errs[-1]}")
        if attempt < SOURCE_ATTEMPTS:
            print(f"{tag} 失败，{SOURCE_RETRY_WAIT}s 后重试 1 次")
            await asyncio.sleep(SOURCE_RETRY_WAIT)

    print(f"{tag} → failed（{label}失败，重试后仍失败）")
    return {
        "card": {
            "status": "failed",
            "note": f"{label}失败",
            "count": 0,
            "error": err_msgs[-1] if err_msgs else "",
        },
        "items": [],
    }


def _summarize_sources(sources: list, errors: list) -> dict:
    """汇总来源层状态，并把「无证据 / 失败」记录进 messages 与 errors。"""
    counts = {"ok": 0, "missing": 0, "failed": 0}
    messages = []

    for card in sources:
        status = card.get("status")
        if status in counts:
            counts[status] += 1
        if status not in ("missing", "failed"):
            continue
        text = f"{card.get('sub_question_id')} {card.get('note')}"
        messages.append(text)
        if status == "failed":
            detail = card.get("error")
            errors.append(f"{text}：{detail}" if detail else text)

    total = len(sources)
    return {
        **counts,
        "total": total,
        "all_failed": total > 0 and counts["failed"] == total,
        "messages": messages,
    }


async def _fetch_one(
    sq: dict, subject: str, time_start: str, time_end: str, size: int, sem
) -> dict:
    """并发检索单个子问题的公告与新闻，返回来源状态卡 + 候选证据。"""
    sq_id = sq.get("id")
    question = sq.get("question") or ""
    query = f"{subject} {question}".strip() if subject else question

    notice_res, news_res = await asyncio.gather(
        _fetch_source("公告", search_notice, sq_id, query, time_start, time_end, size, sem),
        _fetch_source("新闻", search_news, sq_id, query, time_start, time_end, size, sem),
    )

    cards = []
    items = []
    for source_type, res in (("公告", notice_res), ("新闻", news_res)):
        cards.append({"sub_question_id": sq_id, "source_type": source_type, **res["card"]})
        items.extend(res["items"])

    status_txt = "、".join(f"{c['source_type']}={c['status']}" for c in cards)
    print(f"[orchestrator] {sq_id} 候选证据 {len(items)} 条（{status_txt}）")
    return {"sub_question": sq, "items": items[:MAX_ITEMS_PER_SQ], "sources": cards}


async def _classify_one(sq: dict, items: list, sem, errors: list) -> list:
    """用 LLM 给单个子问题的证据打 support / oppose / unverifiable。"""
    if not items:
        return []

    prompt = (
        CLASSIFY_PROMPT.replace("__QUESTION__", sq.get("question") or "")
        .replace("__DIMENSION__", sq.get("dimension") or "未指定")
        .replace("__EVIDENCE_TYPE__", sq.get("evidence_type") or "未指定")
        .replace("__EVIDENCE__", _format_evidence_block(items))
    )

    tag = f"[llm:classify:{sq.get('id')}]"
    result = await _guarded(sem, llm_client.chat_json, prompt, ORCH_SYSTEM, tag)

    if not isinstance(result, dict) or not isinstance(result.get("evidence"), list):
        errors.append(f"{sq.get('id')} 证据分类失败，默认全部 unverifiable")
        print(f"[orchestrator] {sq.get('id')} 分类失败，保留 unverifiable")
        return items

    by_index = {}
    for row in result["evidence"]:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            by_index[idx] = row

    for i, ev in enumerate(items):
        row = by_index.get(i)
        if not row:
            continue
        status = row.get("status")
        ev["status"] = status if status in VALID_STATUS else "unverifiable"
        ev["reason"] = row.get("reason") or ""
        claims = row.get("numeric_claims")
        ev["numeric_claims"] = claims if isinstance(claims, list) else []

    counts = {}
    for ev in items:
        counts[ev["status"]] = counts.get(ev["status"], 0) + 1
    print(f"[orchestrator] {sq.get('id')} 分类完成 {counts}")
    return items


def _build_digest(sub_questions: list, evidence: list) -> str:
    """把子问题 + 证据判定压成给最终结论用的摘要。"""
    grouped: dict = {}
    for ev in evidence:
        grouped.setdefault(ev["sub_question_id"], []).append(ev)

    lines = []
    for sq in sub_questions:
        lines.append(f"[{sq.get('id')}] {sq.get('question')}（维度：{sq.get('dimension') or '未指定'}）")
        items = grouped.get(sq.get("id")) or []
        if not items:
            lines.append("  - 无检索结果")
            continue
        for ev in items:
            claim_txt = ""
            if ev.get("numeric_claims"):
                claim_txt = "｜数字：" + "；".join(
                    f"{c.get('metric')}={c.get('value')}{c.get('unit') or ''}"
                    for c in ev["numeric_claims"]
                    if isinstance(c, dict)
                )
            lines.append(
                f"  - [{ev['status']}] {ev.get('title')}（{ev.get('source_type')} {ev.get('date')}）"
                f"{claim_txt}"
            )
    return "\n".join(lines)


def _truthy(value) -> bool:
    """把 LLM 返回的各种「真值」写法统一成 bool（模型有时会回字符串 "true"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是"}
    return bool(value)


def _normalize_verdict(raw) -> dict | None:
    """把 LLM 返回的结论规范化。

    流程：
      1. 合规拒绝（``refused=true``）-> 固定为「仅做事实梳理」，verdict 归为 insufficient；
      2. verdict 支持英文 key 或五个中文选项（``VERDICT_ALIAS`` 映射）；
      3. ``change_conditions`` 只做清洗，未用「若……则结论转弱/转强/存疑」句式仅提示不改写。

    非法（非 dict / 为空 / verdict 不在枚举内）时返回 ``None``，由调用方决定是否重试。
    """
    if not isinstance(raw, dict) or not raw:
        return None

    # 1) 合规拒绝：refused=true，或 summary 已按合规要求回复「仅做事实梳理」
    #    （实测模型有时只把 summary 写成「仅做事实梳理：…」却仍给 verdict，这里一并纠正）
    summary = str(raw.get("summary") or "").strip()
    if _truthy(raw.get("refused")) or llm_client.REFUSAL_SUMMARY in summary:
        print(f"[orchestrator] LLM 触发合规拒绝 → summary={llm_client.REFUSAL_SUMMARY!r}")
        return dict(REFUSAL_VERDICT)

    # 2) verdict 归一化
    key = raw.get("verdict")
    if isinstance(key, str):
        key = VERDICT_ALIAS.get(key.strip(), key.strip())
    if key not in VALID_VERDICT:
        return None

    # 3) 变化条件清洗（句式不符只提示，不擅自改写）
    conditions = []
    conds = raw.get("change_conditions")
    if isinstance(conds, list):
        for cond in conds:
            text = str(cond).strip()
            if not text:
                continue
            if not text.startswith("若"):
                print(f"[orchestrator] 提示：变化条件未用「若…则结论转弱/转强/存疑」句式：{text[:40]}")
            conditions.append(text)

    return {
        "refused": False,
        "verdict": key,
        "summary": str(raw.get("summary") or "").strip(),
        "change_conditions": conditions,
    }


async def _generate_verdict(prompt: str, sem, errors: list) -> dict:
    """调 LLM 生成最终结论。

    非法 JSON 会重试 1 次（共 ``VERDICT_ATTEMPTS`` 次尝试）；仍失败则返回
    「结论生成失败」兜底模板（``verdict=insufficient``）。
    这里向 ``chat_json`` 传 ``max_attempts=1``，由本层独占「重试 1 次」策略。
    系统提示词用 ``llm_client.CONCLUSION_SYSTEM``（含「只做事实梳理」约束）。
    """
    for attempt in range(1, VERDICT_ATTEMPTS + 1):
        raw = await _guarded(
            sem,
            llm_client.chat_json,
            prompt,
            llm_client.CONCLUSION_SYSTEM,
            "[llm:verdict]",
            max_attempts=1,
        )
        verdict = _normalize_verdict(raw)
        if verdict is not None:
            if attempt > 1:
                print("[orchestrator] 结论重试后成功")
            return verdict

        print(
            f"[orchestrator] 结论第 {attempt}/{VERDICT_ATTEMPTS} 次返回非法/不可用："
            f"{str(raw)[:200]}"
        )
        if attempt < VERDICT_ATTEMPTS:
            print("[orchestrator] 结论非法 JSON，重试 1 次")

    errors.append("结论生成失败：LLM 连续两次未返回合法 JSON，已使用兜底结论")
    print("[orchestrator] 结论生成失败 → insufficient / 结论生成失败")
    return dict(FALLBACK_VERDICT)


async def _verify_async(thesis: str, time_start: str, time_end: str, size: int) -> dict:
    errors: list = []

    # ---- 1. 拆解 ----
    parsed = await asyncio.to_thread(llm_client.parse_thesis, thesis)
    if not parsed:
        errors.append("命题拆解失败（parse_thesis 返回空）")
        return _empty_result(thesis, errors, None, time_start, time_end)

    sub_questions = [s for s in (parsed.get("sub_questions") or []) if isinstance(s, dict)]
    if not sub_questions:
        errors.append("命题拆解结果里没有子问题")
        return _empty_result(thesis, errors, parsed, time_start, time_end)

    subject = parsed.get("subject") or ""
    print(f"[orchestrator] 拆解出 {len(sub_questions)} 个子问题，主体={subject!r}")

    # ---- 2. 并发检索（iFinD 限流 Semaphore(2)）----
    ifind_sem = asyncio.Semaphore(IFIND_CONCURRENCY)
    fetched = await asyncio.gather(
        *[
            _fetch_one(sq, subject, time_start, time_end, size, ifind_sem)
            for sq in sub_questions
        ]
    )

    # 汇总来源层状态卡，并把「无证据 / 失败」记录进 errors
    sources = [card for f in fetched for card in f["sources"]]
    retrieval = _summarize_sources(sources, errors)
    for sq, f in zip(sub_questions, fetched):
        sq["sources"] = f["sources"]
    print(
        f"[orchestrator] 来源层状态 ok={retrieval['ok']} missing={retrieval['missing']} "
        f"failed={retrieval['failed']}（共 {retrieval['total']} 张卡）"
    )

    # ---- 2.5 全部来源失败 → 直接「数据获取失败」，不再调用 LLM ----
    if retrieval["all_failed"]:
        errors.append("全部来源检索失败：verdict=insufficient / 数据获取失败")
        print("[orchestrator] 全部来源失败，短路返回 insufficient / 数据获取失败")
        return _assemble(
            thesis, parsed, time_start, time_end,
            sub_questions, sources, retrieval, [], [], dict(DATA_FAILED_VERDICT), errors,
        )

    # ---- 3. 证据分类 ----
    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
    classified = await asyncio.gather(
        *[
            _classify_one(f["sub_question"], f["items"], llm_sem, errors)
            for f in fetched
        ]
    )

    # 把证据回填到子问题，并生成扁平列表
    evidence = []
    for sq, items in zip(sub_questions, classified):
        sq["evidence"] = items
        evidence.extend(items)

    # ---- 3.5 所有来源都无证据 → 无需调 LLM ----
    if not evidence:
        errors.append("所有来源均无证据：verdict=insufficient / 未检索到相关证据")
        print("[orchestrator] 无任何证据，短路返回 insufficient / 未检索到相关证据")
        return _assemble(
            thesis, parsed, time_start, time_end,
            sub_questions, sources, retrieval, evidence, [], dict(NO_EVIDENCE_VERDICT), errors,
        )

    # ---- 4. 冲突检测 ----
    conflicts = detect_conflicts(evidence)
    print(f"[orchestrator] 检测到 {len(conflicts)} 处数字冲突")

    # ---- 5. 结论（非法 JSON 重试 1 次，仍失败走兜底模板）----
    conflict_text = (
        "\n".join(
            f"- {c['metric']}：" + "；".join(
                f"{v['value']}{v['unit']}（{v['source_type']} {v['date']}）" for v in c["values"]
            )
            for c in conflicts
        )
        or "无"
    )

    final_prompt = llm_client.build_conclusion_prompt(
        thesis=thesis,
        clarified=parsed.get("clarified_thesis") or thesis,
        digest=_build_digest(sub_questions, evidence),
        conflicts=conflict_text,
    )

    verdict = await _generate_verdict(final_prompt, llm_sem, errors)
    print(f"[orchestrator] 结论：{verdict['verdict']}｜{verdict['summary']}")

    # ---- 6. 组装 ----
    return _assemble(
        thesis, parsed, time_start, time_end,
        sub_questions, sources, retrieval, evidence, conflicts, verdict, errors,
    )


def _assemble(
    thesis: str,
    parsed: dict,
    time_start: str,
    time_end: str,
    sub_questions: list,
    sources: list,
    retrieval: dict,
    evidence: list,
    conflicts: list,
    verdict: dict,
    errors: list,
) -> dict:
    """统一组装返回结构（保证各条短路路径的字段完全一致）。"""
    return {
        "thesis": thesis,
        "clarified_thesis": (parsed or {}).get("clarified_thesis") or "",
        "subject": (parsed or {}).get("subject") or "",
        "time_range": (parsed or {}).get("time_range") or "",
        "time_window": {"time_start": time_start, "time_end": time_end},
        "sub_questions": sub_questions,
        "sources": sources,
        "retrieval_summary": retrieval,
        "evidence": evidence,
        "conflicts": conflicts,
        "verdict": verdict,
        "errors": errors,
    }


_EMPTY_RETRIEVAL = {
    "ok": 0,
    "missing": 0,
    "failed": 0,
    "total": 0,
    "all_failed": False,
    "messages": [],
}


def _empty_result(
    thesis: str, errors: list, parsed: dict = None, time_start: str = None, time_end: str = None
) -> dict:
    """命题拆解阶段就失败时的空结果。"""
    return _assemble(
        thesis,
        parsed,
        time_start,
        time_end,
        [],
        [],
        dict(_EMPTY_RETRIEVAL),
        [],
        [],
        {
            "refused": False,
            "verdict": "insufficient",
            "summary": "未能完成验证：命题拆解失败。",
            "change_conditions": [],
        },
        errors,
    )


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def verify_thesis(thesis: str, time_start: str = None, time_end: str = None, size: int = SEARCH_SIZE) -> dict:
    """验证一条投资命题，返回结构化结果。

    参数
    ----
    thesis : str
        投资命题原文。
    time_start / time_end : str, 可选
        检索时间范围，形如 ``"2025-01-01"``。默认回溯 ``DEFAULT_LOOKBACK_DAYS`` 天到今天。
    size : int
        每个子问题、每个来源检索多少条，默认 ``SEARCH_SIZE``。

    返回
    ----
    dict
        ``{thesis, clarified_thesis, subject, time_range, time_window,
        sub_questions, sources, retrieval_summary, evidence, conflicts,
        verdict, errors}``

        - ``sub_questions``：拆解结果，每项附 ``sources`` 与 ``evidence``
        - ``sources``：来源层证据卡（每个子问题 × 每个来源一张），
          含 ``status`` = ``ok`` / ``missing`` / ``failed``
        - ``retrieval_summary``：``{ok, missing, failed, total, all_failed, messages}``
        - ``evidence``：扁平证据列表，含 ``status`` (support/oppose/unverifiable)
          与 ``numeric_claims``
        - ``conflicts``：同一指标不同数字的冲突项
        - ``verdict``：``{refused, verdict, summary, change_conditions}``；
          ``refused=true`` 表示命题涉及预测涨跌/买卖时机/目标价，已合规拒绝，
          此时 summary 固定为「仅做事实梳理」
        - ``errors``：过程中非致命的错误记录，便于排查
    """
    time_end = time_end or date.today().isoformat()
    time_start = time_start or (date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()

    if not thesis or not thesis.strip():
        return _empty_result("", ["命题为空"], None, time_start, time_end)

    thesis = thesis.strip()

    # ---- 0. 合规前置闸门：涉及预测涨跌 / 买卖时机 / 目标价 / 仓位操作 -> 直接拒绝 ----
    hits = llm_client.detect_prediction_intent(thesis)
    if hits:
        print(f"[orchestrator] 合规拒绝：命题涉及预测涨跌/操作建议，命中 {hits}，不执行检索")
        return _assemble(
            thesis, None, time_start, time_end,
            [], [], dict(_EMPTY_RETRIEVAL), [], [], dict(REFUSAL_VERDICT), [],
        )

    print(f"[orchestrator] 开始验证 命题={thesis!r} 时间范围={time_start}~{time_end}")
    return _run_async(_verify_async(thesis, time_start, time_end, size))


if __name__ == "__main__":
    import json

    demo = "贵州茅台2024年盈利改善来自主营业务"
    print(json.dumps(verify_thesis(demo), ensure_ascii=False, indent=2))
