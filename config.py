"""统一配置读取：环境变量 / ``.env`` / Streamlit Secrets。

本项目的密钥有两种运行环境：

- **本地**：项目根目录的 ``.env``（已被 ``.gitignore`` 忽略）
- **线上（Streamlit Community Cloud）**：平台侧配置的 Secrets

为了让同一份代码在两处都能拿到密钥，``get_secret()`` 按下面的优先级取值：

1. **真实环境变量** —— 包括 Streamlit Cloud 上配置的顶层 Secrets：
   Streamlit 会把顶层且值为 ``str`` / ``int`` / ``float`` 的 secret 写入 ``os.environ``
   （见 ``streamlit/runtime/secrets.py`` 的 ``_maybe_set_environment_variable``）。
2. **``.env``** —— 由 python-dotenv 在导入本模块时注入 ``os.environ``，仅本地开发用。
3. **``st.secrets``** —— 显式兜底，不依赖上面那条「隐式提升」的加载时序。

调用方统一用 ``get_secret("KEY")``，不要直接读 ``os.environ``。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（本文件所在目录），所有相对路径都以此为准，
# 避免依赖「当前工作目录」——Streamlit Cloud 与本地启动方式不同，cwd 不一样。
ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

# 显式按文件位置加载 .env：
# load_dotenv() 不带参数时，在交互式 / `python -c` 环境下会退回「当前工作目录」查找，
# 从别处启动进程就会静默读不到 Key。这里固定用绝对路径。
# override=False（默认）：真实环境变量优先，不会被 .env 覆盖。
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
except Exception as _dotenv_exc:  # pragma: no cover - 缺 python-dotenv 时仍可用环境变量/Secrets
    _DOTENV_ERROR = _dotenv_exc
else:
    _DOTENV_ERROR = None

# None = 尚未探测；False = streamlit 不可用；否则为模块对象
_STREAMLIT_MODULE = None


def _streamlit_module():
    """惰性导入 streamlit；不可用时返回 ``False``（结果会被缓存）。"""
    global _STREAMLIT_MODULE
    if _STREAMLIT_MODULE is None:
        try:
            import streamlit as st
        except Exception:  # noqa: BLE001 - 没装 streamlit 也要能跑纯脚本
            _STREAMLIT_MODULE = False
        else:
            _STREAMLIT_MODULE = st
    return _STREAMLIT_MODULE


def _from_streamlit_secrets(name: str):
    """从一个顶层 Secret 取值；不在 Streamlit 运行时 / 未配置 / 非标量一律返回 ``None``。"""
    st = _streamlit_module()
    if not st:
        return None
    try:
        value = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - 没有 secrets.toml 等，按「未配置」处理
        return None

    # 嵌套的 TOML 段（dict）不能当密钥用，直接忽略
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return str(value)


def get_secret(name: str, default=None):
    """按 环境变量 → ``.env`` → Streamlit Secrets 的顺序取配置值。"""
    value = os.environ.get(name)
    if value:
        return value

    value = _from_streamlit_secrets(name)
    if value:
        return value

    return default


def has_secret(name: str) -> bool:
    """该配置项是否已配置（等价于 ``get_secret(name) is not None``）。"""
    return get_secret(name) is not None


if __name__ == "__main__":
    # 自测：python config.py —— 只打印掩码，不输出明文
    def _mask(v):
        if not v:
            return "（未配置）"
        return f"{v[:6]}…{v[-4:]}（len={len(v)}）"

    print(f"ROOT            = {ROOT}")
    print(f".env 是否存在   = {ENV_FILE.exists()}")
    print(f"python-dotenv   = {'可用' if _DOTENV_ERROR is None else f'加载失败：{_DOTENV_ERROR}'}")
    print(f"streamlit       = {'可用' if _streamlit_module() else '不可用'}")
    print()
    for key in ("DEEPSEEK_API_KEY", "FUYAO_API_KEY", "IFIND_MCP_KEY"):
        print(f"{key:18}= {_mask(get_secret(key))}")
