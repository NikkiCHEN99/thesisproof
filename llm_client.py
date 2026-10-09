"""DeepSeek LLM 客户端。

本模块集中管理**所有提示词**（与业务数据流解耦）：

- ``PROMPT_TEMPLATE``   命题拆解（``parse_thesis``）
- ``CONCLUSION_PROMPT`` 结论生成（由 ``build_conclusion_prompt`` 渲染，供 orchestrator 调用）
- ``COMPLIANCE_RULES``  合规红线：禁止价格方向/操作用语、禁止承诺收益、
  涉及预测涨跌一律拒绝并回复「仅做事实梳理」

API Key 通过 python-dotenv 从项目根目录的 ``.env`` 读取（环境变量 ``DEEPSEEK_API_KEY``）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

import requests

# 显式指定与本文件同级的 .env：
# load_dotenv() 不带参数时，在交互式/`python -c` 环境下会退回「当前工作目录」查找，
# 从别处启动进程就会读不到 Key。这里固定按文件位置解析，避免踩这个坑。
load_dotenv(Path(__file__).resolve().parent / ".env")

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
TIMEOUT = 60
MAX_ATTEMPTS = 2  # 首次 + 「失败重试 1 次」

PROMPT_TEMPLATE = """你是一个投资研究助手。用户给出投资命题，拆解为3-6个可验证子问题。
输出 JSON：
{
"clarified_thesis": "澄清后命题",
"subject": "主体",
"time_range": "时间范围",
"sub_questions": [
{"id": "q1", "question": "子问题", "dimension": "维度", "evidence_type": "公告/新闻/财务"}
]
}
只输出 JSON，不要其他文字。
命题：__THESIS__"""

# 第 2 次尝试时追加的强约束，避免上次的输出格式问题重复出现
RETRY_SUFFIX = "\n\n注意：上次的输出无法被解析为 JSON。请直接输出 JSON 对象本身，不要包含任何解释文字或 Markdown 代码块。"

# ---------------------------------------------------------------------------
# 合规约束（面向用户输出内容的统一红线）
# ---------------------------------------------------------------------------
COMPLIANCE_RULES = """【合规红线，必须遵守】
1. 禁止输出「涨」「跌」「买入」「卖出」「推荐」等价格方向或投资操作用语，
   也不得用同义替换（如「看多」「加仓」「目标价」「上行空间」）。
2. 禁止承诺或暗示收益，如「必然上涨」「稳赚」「翻倍」「建议买入」。
3. 结论只能是下列五选一：支持 / 部分支持 / 反对 / 证据不足 / 存在冲突。
4. 「变化条件」必须写成「若……，则结论转弱 / 转强 / 存疑」的句式，只描述可观测事实。
5. 命题或证据若涉及预测涨跌、买卖时机、目标价，一律不得给出预测：直接拒绝，
   summary 固定回复「仅做事实梳理」。"""

REFUSAL_SUMMARY = "仅做事实梳理"  # 触发合规拒绝时固定使用的回复

CONCLUSION_SYSTEM = (
    "你是一个严谨的投研证据审核员，只输出合法 JSON，不要输出任何解释文字。"
    "你只做事实梳理，不提供任何投资建议。"
)

CONCLUSION_PROMPT = """你是投资研究负责人，需要对一条投资命题给出**基于证据的事实性梳理结论**。

━━━ 第一步：合规判断（必须最先执行，优先于一切分析）━━━
若原始命题或澄清后命题涉及**预测涨跌、买卖时机、目标价、仓位操作**，
则禁止给出任何预测或操作判断：直接拒绝并**立即结束**，只输出下一行这个 JSON，
不要附加任何方向性判断、目标价或操作建议：
{"refused": true, "verdict": "insufficient", "summary": "仅做事实梳理", "change_conditions": []}

若命题不属于上述情形，才继续第二步。

━━━ 第二步：事实性结论梳理 ━━━
原始命题：__THESIS__
澄清后命题：__CLARIFIED__

各子问题的证据判定：
__DIGEST__

检测到的数字冲突：
__CONFLICTS__

请从下列五个选项中择一给出 verdict（值只写英文 key，含义见中文）：
- support（支持）：主要子问题都被证据支持
- partial_support（部分支持）：部分支持，另有部分无法验证或存在反驳
- oppose（反对）：证据整体与命题相矛盾
- insufficient（证据不足）：证据不足，无法判断
- conflict（存在冲突）：存在关键数字冲突且无法调和

change_conditions 的每一条都必须形如：
- 「若2025年主营收入增速低于5%，则结论转弱」
- 「若非经常性损益占比超过10%，则结论存疑」
- 「若毛利率连续两个季度回升，则结论转强」

__COMPLIANCE__

只输出 JSON（两种形态二选一，不要输出其他内容）：

【普通情形】
{"refused": false, "verdict": "partial_support", "summary": "一句话事实性结论（不超过60字，不含操作建议）",
 "change_conditions": ["若<可观测事实>，则结论转弱", "若<可观测事实>，则结论转强"]}

【预测/操作类命题】
{"refused": true, "verdict": "insufficient", "summary": "仅做事实梳理", "change_conditions": []}"""

# 明确指向「股价方向」或「交易动作」的说法。
# 刻意不收裸的「涨 / 跌」二字：否则「产品涨价」「营收增长」这类事实性表述会被误判。
_PREDICTION_PATTERNS = [
    # 价格方向预测
    "会涨", "会跌", "能涨", "能跌", "要涨", "要跌", "将涨", "将跌",
    "涨吗", "跌吗", "上涨吗", "下跌吗", "会不会涨", "会不会跌", "能否涨", "能否跌",
    "涨到", "跌到", "股价涨", "股价跌", "上涨空间", "下跌空间", "上行空间", "下行空间",
    "目标价", "后市", "走势预测", "股价预测", "预测涨", "预测跌",
    # 买卖 / 仓位操作
    "买入", "卖出", "加仓", "减仓", "建仓", "清仓", "抄底", "止盈", "止损",
    "满仓", "空仓", "该买", "该卖", "能不能买", "该不该买", "值得买", "值得投", "可以买",
    "看多", "看空", "做多", "做空", "操作建议", "推荐买", "推荐卖",
]

# 「股价 + 方向」的宽匹配，覆盖「股价下周会涨」「股价会不会跌」这类问法
_PREDICTION_REGEXES = [
    re.compile(r"股价[^\s。；;，,？?！!]{0,8}(?:涨|跌|反弹|回调|走强|走弱)"),
    re.compile(r"股票[^\s。；;，,？?！!]{0,8}(?:涨|跌|反弹|回调|走强|走弱)"),
]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def detect_prediction_intent(text: str) -> list:
    """检测命题是否在要求「预测涨跌 / 买卖时机 / 目标价 / 仓位操作」。

    返回命中的关键词列表（未命中则为空列表）。

    这是合规拒绝的**第一道闸**（确定性判断）：命中即直接拒绝，不再检索、不再生成结论。
    实测发现仅靠提示词不可靠 —— 模型会只把 summary 写成「仅做事实梳理：…」，
    却依然给出 `partial_support` 与方向性判断。因此这里补一层代码兜底。
    """
    if not text or not isinstance(text, str):
        return []

    hits = [kw for kw in _PREDICTION_PATTERNS if kw in text]
    for pattern in _PREDICTION_REGEXES:
        matched = pattern.search(text)
        if matched:
            hits.append(matched.group(0))

    return list(dict.fromkeys(hits))


def build_conclusion_prompt(thesis: str, clarified: str, digest: str, conflicts: str) -> str:
    """渲染结论提示词。

    统一入口：合规红线（``COMPLIANCE_RULES``）始终会被注入，
    避免调用方漏带约束。占位符一律用 ``str.replace``，
    不用 ``str.format``（模板含 JSON 大括号会被误判为占位符）。
    """
    return (
        CONCLUSION_PROMPT.replace("__THESIS__", thesis or "")
        .replace("__CLARIFIED__", clarified or thesis or "")
        .replace("__DIGEST__", digest or "")
        .replace("__CONFLICTS__", conflicts or "无")
        .replace("__COMPLIANCE__", COMPLIANCE_RULES)
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """从模型输出里稳妥地取出 JSON 对象。

    容错顺序：去掉 ``` 代码块围栏 -> 直接 parse -> 截取首尾大括号再 parse。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型返回内容为空")

    s = text.strip()

    fence = _FENCE_RE.search(s)
    if fence:
        s = fence.group(1).strip()

    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # 兜底：模型可能在 JSON 前后夹带了说明文字
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise
        obj = json.loads(s[start : end + 1])

    if not isinstance(obj, dict):
        raise ValueError(f"期望 JSON 对象，实际得到 {type(obj).__name__}")
    return obj


def _chat(prompt: str, system: str = None) -> str:
    """调一次 DeepSeek chat，返回 message.content 文本。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("未找到 DEEPSEEK_API_KEY，请检查 .env 文件")

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system or "你是一个投资研究助手，只输出合法 JSON，不要输出其他文字。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(
        API_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()

    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def chat_json(
    prompt: str,
    system: str = None,
    tag: str = "[llm:chat_json]",
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """通用调用：把 prompt 发给 DeepSeek，返回解析后的 JSON 对象。

    与 ``parse_thesis`` 共用同一套容错策略：JSON 解析失败带强约束重试 1 次
    （共 ``MAX_ATTEMPTS`` 次尝试），最终失败返回空 dict ``{}``。

    ``max_attempts`` 可覆盖尝试次数（含首次）；上层若自带重试策略，
    传 ``1`` 即可避免两层重试叠加。
    """
    attempts = max(1, int(max_attempts))
    last_err = None
    for attempt in range(1, attempts + 1):
        p = prompt + (RETRY_SUFFIX if attempt > 1 else "")
        try:
            print(f"{tag} 第 {attempt}/{attempts} 次调用 {MODEL}")
            return _extract_json(_chat(p, system=system))
        except Exception as exc:  # noqa: BLE001 - 对外统一降级为空 dict
            last_err = exc
            print(f"{tag} 第 {attempt} 次失败：{type(exc).__name__}: {exc}")

    print(f"{tag} 重试后仍失败，返回空 dict；最后一次错误：{last_err}")
    return {}


def parse_thesis(thesis: str) -> dict:
    """把投资命题拆解为 3-6 个可验证子问题。

    返回形如::

        {
            "clarified_thesis": "...",
            "subject": "...",
            "time_range": "...",
            "sub_questions": [
                {"id": "q1", "question": "...", "dimension": "...",
                 "evidence_type": "公告/新闻/财务"},
                ...
            ],
        }

    任何失败（缺 Key / 网络异常 / JSON 解析失败）都会打印错误并返回空 dict ``{}``。
    JSON 解析失败会带强约束重试 1 次（共 2 次尝试）。
    """
    tag = "[llm:parse_thesis]"

    if not thesis or not thesis.strip():
        print(f"{tag} 命题为空，直接返回空 dict")
        return {}

    thesis = thesis.strip()
    print(f"{tag} 命题={thesis!r}")

    # 用 replace 而非 str.format：模板里含 JSON 示例的大括号，format 会误判为占位符
    prompt = PROMPT_TEMPLATE.replace("__THESIS__", thesis)
    result = chat_json(prompt, tag=tag)

    if not result:
        return {}

    subs = result.get("sub_questions")
    print(
        f"{tag} 成功：主体={result.get('subject')!r}，"
        f"拆解出 {len(subs) if isinstance(subs, list) else 0} 个子问题"
    )
    return result


if __name__ == "__main__":
    demo = "新能源车渗透率提升将带动上游锂电材料公司利润持续增长"
    print(json.dumps(parse_thesis(demo), ensure_ascii=False, indent=2))
