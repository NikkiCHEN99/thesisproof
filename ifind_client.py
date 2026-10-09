"""iFind 新闻 / 公告检索封装。

对 ``call(server_type, tool_name, params)`` 做一层薄封装，把三层嵌套的 MCP 响应
拆解成扁平的「结果列表」，供 thesisproof 应用直接消费。

调用实现优先取**项目内置**的 ``ifind_call.py``（token 走环境变量 / Streamlit Secrets，
不依赖本机 skill 路径，本地与线上同一份代码）；只有在缺少该模块时才回退到
本机安装的 iFinD skill 目录。加载来源打印在每次调用的调试行里（``source=``）。

实测的真实返回结构::

    call() -> {
        "ok": True,
        "status_code": 200,
        "data": {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": "<json 字符串>"}]
            },
        },
    }

``text`` 解析后是::

    search_news   -> {"code": 1, "msg": "success", "data": {"data": "<json 字符串>"}}
    search_notice -> {"code": 1, "msg": "success", "data": "<json 字符串>"}
    无结果时      -> {"code": 1, "msg": "success", "data": {"answer": "...结果为空。"}}

两种形状都要再解析一次，才拿到真正的结果列表；无结果时按空列表返回::

    [{"资讯标题": ..., "资讯内容": ..., "日期": ..., "URL": ...}, ...]
"""

from __future__ import annotations

import json
import sys
import time

# iFinD 接口用 verify=False 请求 https，屏蔽一下证书告警，避免刷屏
try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # pragma: no cover - 缺 urllib3 不影响主流程
    pass

# 仅在回退到本机 skill 时才需要这个路径；默认走内置实现，不依赖它
IFIND_SKILL_PATH = "/Users/nikkiwithnicci/.workbuddy/skills/ifind-finance-data"

# ---------------------------------------------------------------------------
# 1) 加载 iFinD 调用实现
#    内置 ifind_call（首选，可部署）→ 本机 skill（回退，仅本地可用）
# ---------------------------------------------------------------------------
try:
    from ifind_call import call as _call

    _CALL_SOURCE = "builtin"
    _IMPORT_ERROR = None
except Exception as _builtin_exc:  # noqa: BLE001
    try:
        if IFIND_SKILL_PATH not in sys.path:
            sys.path.append(IFIND_SKILL_PATH)
        from call import call as _call  # type: ignore[no-redef]

        _CALL_SOURCE = "skill"
        _IMPORT_ERROR = None
    except Exception as _skill_exc:  # noqa: BLE001
        # 延迟容错：即使都不可用，本模块仍可被 import，调用时返回空列表
        _call = None
        _CALL_SOURCE = None
        _IMPORT_ERROR = _skill_exc
        _BUILTIN_ERROR = _builtin_exc
else:
    _BUILTIN_ERROR = None

# 配置类错误（token 未配置）不重试 —— 这类问题重试无效，只会白等退避时间
try:
    from ifind_call import IFindConfigError as _ConfigError
except Exception:  # noqa: BLE001 - 走 skill 回退时没有这个类

    class _ConfigError(RuntimeError):  # type: ignore[no-redef]
        """占位类型：skill 回退路径不会抛它，因此永远匹配不到。"""

# 限流重试策略：iFinD 轻量级账号并发 2 也会触发 429，需要退避重试
SEARCH_MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试
RATE_LIMIT_BACKOFF_SECONDS = 3  # 退避基数：第 n 次重试等待 n * 基数 秒


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
class IFindError(RuntimeError):
    """iFinD 接口返回的错误（区别于「调用成功但没有结果」）。"""

    def __init__(self, message: str, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


def _is_meta_note(item) -> bool:
    """判断是否为接口附带的纯说明性条目（只含「备注」字段，不是真实结果）。"""
    return isinstance(item, dict) and set(item.keys()) <= {"备注"}


def _extract_items(resp) -> list:
    """从 call() 的三层嵌套响应中取出结果列表，失败时抛异常。"""
    # 第一层：call() 的统一包装
    if not isinstance(resp, dict):
        raise ValueError(f"响应不是 dict：{type(resp)}")
    if not resp.get("ok"):
        raise RuntimeError(f"call() 返回失败：{resp.get('error', resp)}")

    # 第二层：JSON-RPC 外壳
    payload = (resp.get("data") or {}).get("result") or {}
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError(f"响应缺少 result.content：{str(resp)[:300]}")

    text = content[0].get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"content[0].text 不是字符串：{type(text)}")

    # 接口级错误：result.isError=True，text 是 "call failed, status: 429, ..." 之类的说明
    # 这类响应不是 JSON，必须在下面 json.loads 之前拦掉，否则会被误报为「解析失败」
    if payload.get("isError"):
        limited = ("429" in text) or ("请求过于频繁" in text) or ("降低频率" in text)
        raise IFindError(f"iFinD 返回错误：{text[:200]}", rate_limited=limited)

    # 第三层：iFinD 业务包装
    inner = json.loads(text)
    if inner.get("code") != 1:
        raise RuntimeError(f"iFinD 业务错误 code={inner.get('code')} msg={inner.get('msg')}")

    # 注意：不同工具的 data 形状不一致，实测有两种
    #   search_news   -> {"data": {"data": "<json 字符串>"}}
    #   search_notice -> {"data": "<json 字符串>"}
    node = inner.get("data")
    # 无结果时接口返回 {"answer": "tool_id=... 工具调用结果为空。"}，按「空列表」处理而非报错
    if node is None:
        return []
    if isinstance(node, str):
        items = json.loads(node)
    elif isinstance(node, dict):
        if "data" not in node:
            return []
        raw = node.get("data")
        if isinstance(raw, str):
            items = json.loads(raw)
        elif isinstance(raw, list):
            items = raw
        else:
            raise RuntimeError(f"data.data 类型未知：{type(raw)}")
    elif isinstance(node, list):
        items = node
    else:
        raise RuntimeError(f"data 字段类型未知：{type(node)}")

    if not isinstance(items, list):
        raise RuntimeError(f"结果不是列表：{type(items)}")
    return items


def _search(
    tool_name: str,
    query: str,
    time_start: str,
    time_end: str,
    size: int,
    errors: list = None,
    max_attempts: int = SEARCH_MAX_ATTEMPTS,
) -> list:
    """公共实现：拼参数、调用、解析、打印调试信息、异常兜底。

    限流（429）与网络异常都会退避重试；配置类错误（token 未配置）**不重试**，
    直接失败（重试对配置问题无效，只会白等退避时间）。
    最终失败仍返回空列表（绝不向上抛异常），但如果传入了 ``errors`` 列表，
    会把错误信息追加进去，便于上层区分「没有结果」和「调用失败」。
    """
    tag = f"[ifind:{tool_name}]"
    print(
        f"{tag} 请求参数 query={query!r} time_start={time_start} time_end={time_end} "
        f"size={size} source={_CALL_SOURCE}"
    )

    if _call is None:
        msg = (
            f"{tag} 加载失败：既无法导入内置 ifind_call（{_BUILTIN_ERROR}），"
            f"也无法导入 skill 的 call()（{_IMPORT_ERROR}）"
        )
        print(msg)
        if errors is not None:
            errors.append(msg)
        return []

    params = {
        "query": query,
        "time_start": time_start,
        "time_end": time_end,
        "size": size,
    }

    last_err = None
    resp = None  # 网络异常时 _call 可能没走到赋值，必须预置，否则错误分支会 UnboundLocalError
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _call("news", tool_name, params)
            items = _extract_items(resp)
        except Exception as exc:  # noqa: BLE001 - 对外统一降级为空列表
            last_err = exc

            # 配置类错误（如 token 未配置）：重试无意义，直接失败
            if isinstance(exc, _ConfigError):
                msg = f"{tag} 配置错误，不重试：{exc}"
                print(msg)
                if errors is not None:
                    errors.append(msg)
                return []

            rate_limited = isinstance(exc, IFindError) and exc.rate_limited
            if attempt < max_attempts:
                # 限流与网络抖动都是瞬时的，退避后重试
                wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
                kind = "触发限流" if rate_limited else f"{type(exc).__name__}"
                print(f"{tag} {kind}，{wait}s 后重试（{attempt}/{max_attempts - 1}）")
                time.sleep(wait)
                continue
            print(f"{tag} 最终失败：{type(exc).__name__}: {exc}")
            if resp is not None:
                print(f"{tag} 原始响应摘要：{str(resp)[:500]}")
            break
        else:
            # 剔除接口附带的「备注」说明条目
            items = [it for it in items if not _is_meta_note(it)]
            print(f"{tag} 成功：status={resp.get('status_code')}，返回 {len(items)} 条结果")
            return items

    msg = f"{tag} 最终失败：{type(last_err).__name__}: {last_err}"
    if errors is not None:
        errors.append(msg)
    return []


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def search_notice(
    query, time_start, time_end, size=5, errors=None, max_attempts=SEARCH_MAX_ATTEMPTS
):
    """检索上市公司公告。

    参数
    ----
    query : str
        查询内容，可同时包含报告元数据要求，例如
        ``"光迅科技2024年度报告 光模块技术"``。
    time_start / time_end : str
        时间范围，形如 ``"2025-01-01"``。
    size : int
        返回条数上限，默认 5。
    errors : list, 可选
        传入列表时，调用失败会把错误信息追加进去。用于上层区分
        「检索成功但没有结果」与「调用失败」。
    max_attempts : int, 可选
        最多尝试次数（含首次），默认 ``SEARCH_MAX_ATTEMPTS``。
        上层若自带重试策略，可传 ``1`` 避免两层重试叠加。

    返回
    ----
    list[dict]
        公告结果列表（含 公告标题 / 内容 / 日期 / URL 等字段）；
        任何失败都会打印错误并返回空列表 ``[]``（限流 429 会自动退避重试）。
    """
    return _search("search_notice", query, time_start, time_end, size, errors, max_attempts)


def search_news(
    query, time_start, time_end, size=5, errors=None, max_attempts=SEARCH_MAX_ATTEMPTS
):
    """检索财经新闻资讯。

    参数
    ----
    query : str
        查询内容，例如 ``"人工智能行业动态"``。
    time_start / time_end : str
        时间范围，形如 ``"2025-01-01"``。
    size : int
        返回条数上限，默认 5。
    errors : list, 可选
        传入列表时，调用失败会把错误信息追加进去。用于上层区分
        「检索成功但没有结果」与「调用失败」。
    max_attempts : int, 可选
        最多尝试次数（含首次），默认 ``SEARCH_MAX_ATTEMPTS``。

    返回
    ----
    list[dict]
        新闻结果列表（含 资讯标题 / 资讯内容 / 日期 / URL 等字段）；
        任何失败都会打印错误并返回空列表 ``[]``（限流 429 会自动退避重试）。
    """
    return _search("search_news", query, time_start, time_end, size, errors, max_attempts)


if __name__ == "__main__":
    # 简单自测：python ifind_client.py
    print("=== search_news 自测 ===")
    news = search_news("人工智能行业动态", "2025-01-01", "2026-01-01", size=2)
    print(f"news 条数：{len(news)}")
    if news:
        print(json.dumps(news[0], ensure_ascii=False, indent=2)[:400])

    print("\n=== search_notice 自测 ===")
    notices = search_notice("年度报告 光模块", "2025-01-01", "2026-01-01", size=2)
    print(f"notice 条数：{len(notices)}")
    if notices:
        print(json.dumps(notices[0], ensure_ascii=False, indent=2)[:400])
