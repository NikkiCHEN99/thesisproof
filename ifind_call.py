"""iFinD MCP 调用实现（项目内置版）。

背景：原先 `ifind_client.py` 直接 `sys.path.append` 本机 skill 目录再 `from call import call`，
那个绝对路径只在本机存在，部署到 Streamlit Cloud 后会**静默降级为空结果**
（不报错，但页面上一条证据都没有）。本模块把同样的调用逻辑内置进项目，
认证 token 改从环境变量 / Streamlit Secrets 读取，因此**本地与线上共用同一份实现**。

调用方式是 iFinD 官方的远程 MCP（HTTP + JSON-RPC）：

    POST https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-news-mcp

token 的解析优先级（由 `config.get_secret` 统一处理）：

1. 环境变量 ``IFIND_MCP_KEY``（本地来自 ``.env``；线上来自 Streamlit Secrets）
2. 与本模块同级的 ``mcp_config.json`` 的 ``auth_token`` 字段（老用法兼容）
3. ``~/.workbuddy/skills/ifind-finance-data/mcp_config.json``（本机装了 skill 时的兜底）

调用示例::

    from ifind_call import call
    call("news", "search_news", {"query": "...", "time_start": "...", "time_end": "...", "size": 5})
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import requests

from config import ROOT, get_secret

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
SERVERS = {
    "stock": f"{BASE}/hexin-ifind-ds-stock-mcp",
    "fund": f"{BASE}/hexin-ifind-ds-fund-mcp",
    "edb": f"{BASE}/hexin-ifind-ds-edb-mcp",
    "news": f"{BASE}/hexin-ifind-ds-news-mcp",
    "bond": f"{BASE}/hexin-ifind-ds-bond-mcp",
    "global_stock": f"{BASE}/hexin-ifind-ds-global-stock-mcp",
    "index": f"{BASE}/hexin-ifind-ds-index-mcp",
    "future": f"{BASE}/hexin-ifind-ds-futures-mcp",
}

AUTH_ENV_KEY = "IFIND_MCP_KEY"
TOKEN = "auth_token"  # mcp_config.json 里的字段名

# 回退查找 mcp_config.json 的位置：项目根目录 → 本机安装的 skill 目录
_CONFIG_FALLBACKS = (
    ROOT / "mcp_config.json",
    Path.home() / ".workbuddy" / "skills" / "ifind-finance-data" / "mcp_config.json",
)

BLOCKED_KEYS = {"__proto__", "prototype", "constructor"}


class IFindConfigError(RuntimeError):
    """配置类错误（如 token 未配置）。

    与网络/限流错误区分开：这类错误**重试没有意义**，
    上层（``ifind_client._search``）据此跳过退避重试、直接失败，
    否则每个来源都要白等 3+6 秒。
    """

# 会话与请求计数器（模块级状态，进程内共享）
_sessions: dict = {}
_req_ids: dict = {}
_tool_sets: dict = {}
_lock = threading.Lock()  # 保护 _init：并发冷启动时避免重复 initialize

_token_cache = None


# ---------------------------------------------------------------------------
# token 解析
# ---------------------------------------------------------------------------
def _token_from_config_file() -> tuple:
    """从回退位置的 mcp_config.json 里取 token，返回 ``(token, 文件路径)``。"""
    for path in _CONFIG_FALLBACKS:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 读不了就试下一个
            continue

        token = data.get(TOKEN) if isinstance(data, dict) else None
        if token:
            return str(token), path
    return None, None


def get_auth_token() -> str:
    """取认证 token（首次调用后缓存）。取不到时抛 ``RuntimeError``。"""
    global _token_cache
    if _token_cache:
        # 环境变量里的值优先，若中途变了则以新值为准
        env_token = get_secret(AUTH_ENV_KEY)
        if env_token:
            _token_cache = env_token
        return _token_cache

    env_token = get_secret(AUTH_ENV_KEY)
    if env_token:
        _token_cache = env_token
        return _token_cache

    file_token, path = _token_from_config_file()
    if file_token:
        print(f"[ifind_call] 未配置 {AUTH_ENV_KEY}，回退读取本地配置文件：{path}")
        _token_cache = file_token
        return _token_cache

    locations = "、".join(str(p) for p in _CONFIG_FALLBACKS)
    hint = f"，或提供 mcp_config.json（查找位置：{locations}）" if locations else ""
    raise IFindConfigError(
        f"未找到 iFinD 认证 token：请配置环境变量 {AUTH_ENV_KEY}"
        f"（本地写在 .env，线上写在 Streamlit Secrets）{hint}"
    )


def token_source() -> str:
    """返回 token 的来源描述，用于排障（**不返回 token 本身**）。"""
    env_token = get_secret(AUTH_ENV_KEY)
    if env_token:
        return f"{AUTH_ENV_KEY}（环境变量 / .env / Streamlit Secrets）"
    _, path = _token_from_config_file()
    if path:
        return f"配置文件 {path}"
    return "未配置"


# ---------------------------------------------------------------------------
# HTTP / JSON-RPC
# ---------------------------------------------------------------------------
def _next_id(t):
    _req_ids[t] = _req_ids.get(t, 0) + 1
    return _req_ids[t]


def _headers(t=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": get_auth_token(),
    }
    if t in _sessions:
        h["Mcp-Session-Id"] = _sessions[t]
    return h


def _post(t, payload, timeout=60):
    resp = requests.post(
        SERVERS[t],
        json=payload,
        headers=_headers(t),
        verify=False,  # 与官方 skill 保持一致（该域名证书链在本机不被信任）
        timeout=timeout,
    )
    data = None
    if resp.text.strip():
        try:
            data = resp.json()
        except Exception:
            data = resp.text
    return resp, data


def _validate_params(params):
    if not isinstance(params, dict):
        raise TypeError("input must be a JSON object")

    def walk(value):
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in BLOCKED_KEYS:
                    raise TypeError("input contains blocked field")
                walk(item)
            return
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("input contains invalid number")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError("input contains unsupported value type")

    walk(params)
    json.dumps(params, allow_nan=False)


def _init(t):
    """建立 MCP 会话。加锁避免并发冷启动时重复 initialize（会互相覆盖 session id）。"""
    if t in _sessions:
        return

    with _lock:
        if t in _sessions:  # 双检：可能已被其他线程初始化
            return

        payload = {
            "jsonrpc": "2.0",
            "id": _next_id(t),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "http-client", "version": "1.0.0"},
            },
        }

        resp, data = _post(t, payload, timeout=30)
        resp.raise_for_status()

        session_id = resp.headers.get("Mcp-Session-Id")
        if not session_id:
            raise RuntimeError(f"initialize 成功但未返回 Mcp-Session-Id: {data}")

        _sessions[t] = session_id

    notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    requests.post(
        SERVERS[t],
        json=notify,
        headers=_headers(t),
        verify=False,
        timeout=10,
    )


def _load_tool_set(server_type):
    if server_type in _tool_sets:
        return _tool_sets[server_type]

    res = list_tools(server_type)
    tools = res.get("data", {}).get("result", {}).get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("Invalid tools/list response")

    tool_set = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool.get("name")
    }
    _tool_sets[server_type] = tool_set
    return tool_set


def call(server_type, tool_name, params):
    """调用一个 MCP 工具，返回与官方 skill 完全一致的结构。

    返回 ``{"ok": bool, "status_code": int, "data"|"error": ...}``；
    与官方实现一样：HTTP 层失败会 ``raise_for_status()`` 抛出，
    token 缺失会在 ``get_auth_token()`` 处抛 ``RuntimeError``。
    """
    if server_type not in SERVERS:
        raise ValueError(f"unknown server_type: {server_type}")

    _validate_params(params)
    allowed_tools = _load_tool_set(server_type)
    if tool_name not in allowed_tools:
        raise ValueError(f"toolName not allowed for server_type {server_type}: {tool_name}")

    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": params,
        },
    }

    resp, data = _post(server_type, payload)

    if isinstance(data, dict) and "error" in data:
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": data["error"],
            "raw": data,
        }

    resp.raise_for_status()
    return {
        "ok": True,
        "status_code": resp.status_code,
        "data": data,
    }


def list_tools(server_type):
    """列出一个 MCP server 下的工具（首次调用会先 initialize）。"""
    if server_type not in SERVERS:
        raise ValueError(f"unknown server_type: {server_type}")

    _init(server_type)

    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "tools/list",
        "params": {},
    }

    resp, data = _post(server_type, payload)

    if isinstance(data, dict) and "error" in data:
        return {
            "ok": False,
            "status_code": resp.status_code,
            "error": data["error"],
            "raw": data,
        }

    resp.raise_for_status()

    return {
        "ok": True,
        "status_code": resp.status_code,
        "data": data,
    }


if __name__ == "__main__":
    # 自测：python ifind_call.py —— 只打印 token 来源，不输出 token 明文
    print(f"[ifind_call] token 来源：{token_source()}")

    res = call(
        "news",
        "search_news",
        {"query": "人工智能行业动态", "time_start": "2025-01-01", "time_end": "2026-01-01", "size": 1},
    )
    print(f"[ifind_call] ok={res.get('ok')} status={res.get('status_code')}")
