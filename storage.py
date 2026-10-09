"""研究任务的本地持久化。

把一次 ``verify_thesis`` 的结果落盘成 JSON，供页面侧边栏挑选、重新加载。

- 存放目录：``<项目根>/saved_tasks/``（不存在会自动创建）
- 文件名：``<命题 slug>_<YYYYmmdd_HHMMSS>.json``，例：``贵州茅台2024年盈利改善来自主营业务_20261009_184205.json``
- 落盘结构::

      {
        "schema": 1,
        "thesis": "原始命题",
        "saved_at": "2026-10-09 18:42:05",
        "saved_ts": 1791568925.12,
        "result": { ... verify_thesis 的完整返回 ... }
      }

**已知边界（云端）**：Streamlit Community Cloud 的容器文件系统是临时的，
应用重启 / 重新部署后 ``saved_tasks/`` 里的文件会丢失，且多用户实例之间不共享。
本地运行不受影响。真正的多端持久化需要接数据库（见 README「未做事项」）。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

# 存放目录固定跟随本文件所在的项目根，避免受启动时工作目录影响
TASK_DIR = Path(__file__).resolve().parent / "saved_tasks"

SCHEMA_VERSION = 1
SLUG_MAX_CHARS = 40  # 文件名里命题部分的最大长度

# 文件名安全化：只保留中英文、数字、下划线与连字符
_UNSAFE_CHARS = re.compile(r"[^\w\u4e00-\u9fff-]+")
_FILENAME_ONLY = re.compile(r"^[\w\u4e00-\u9fff.\-]+$")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _slug(thesis: str) -> str:
    """把命题压成可安全用作文件名的短串。"""
    text = (thesis or "").strip() or "未命名命题"
    slug = _UNSAFE_CHARS.sub("_", text).strip("_")
    return (slug[:SLUG_MAX_CHARS] or "未命名命题")


def _task_path(filename: str) -> Path:
    """把任务文件名解析成绝对路径，并挡掉目录穿越。"""
    name = (filename or "").strip()
    if not name or not _FILENAME_ONLY.match(name) or "/" in name or "\\" in name:
        raise ValueError(f"非法的任务文件名：{filename!r}")

    path = (TASK_DIR / name).resolve()
    if path.parent != TASK_DIR.resolve():
        raise ValueError(f"任务文件不在 saved_tasks/ 目录内：{filename!r}")
    return path


def ensure_dir() -> Path:
    """确保存放目录存在，返回该目录。"""
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    return TASK_DIR


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def save_task(thesis: str, result: dict) -> str:
    """把一次验证结果保存为 JSON 文件。

    参数
    ----
    thesis : str
        命题原文（用于生成文件名，并作为记录里的可读标题）。
    result : dict
        ``orchestrator.verify_thesis`` 的返回结果。

    返回
    ----
    str
        保存后的**文件名**（不含目录），例如
        ``"贵州茅台2024年盈利改善来自主营业务_20261009_184205.json"``。

    写入采用「先写临时文件再原子替换」，避免中途失败留下半个 JSON。
    """
    ensure_dir()

    now = datetime.now()
    filename = f"{_slug(thesis)}_{now:%Y%m%d_%H%M%S}.json"
    path = _task_path(filename)

    # 同一秒内重复保存时，补一个序号避免覆盖
    seq = 1
    while path.exists():
        filename = f"{_slug(thesis)}_{now:%Y%m%d_%H%M%S}_{seq}.json"
        path = _task_path(filename)
        seq += 1

    payload = {
        "schema": SCHEMA_VERSION,
        "thesis": thesis or "",
        "saved_at": f"{now:%Y-%m-%d %H:%M:%S}",
        "saved_ts": time.time(),
        "result": result if isinstance(result, dict) else {},
    }

    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        # default=str 兜底：个别字段若是 Decimal / datetime 也能落盘，不会中断保存
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    tmp_path.replace(path)

    print(f"[storage] 已保存研究任务：{path}（{path.stat().st_size} 字节）")
    return filename


def list_tasks() -> list:
    """列出所有已保存任务，按保存时间倒序。

    返回
    ----
    list[dict]
        每项形如::

            {"filename": "...json",       # 文件名（传给 load_task）
             "thesis": "贵州茅台2024年盈利改善来自主营业务",
             "saved_at": "2026-10-09 18:42:05",
             "saved_ts": 1791568925.12,   # 排序用的时间戳
             "sub_questions": 6,          # 子问题数量（读不出则为 0）
             "verdict": "partial_support"} # 结论 key（读不出则为 ""）

        目录不存在时返回空列表；单个文件损坏时跳过并打印告警，不影响其他任务。
    """
    if not TASK_DIR.exists():
        return []

    tasks: list = []
    for path in TASK_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # noqa: BLE001 - 单个坏文件不应影响整体列表
            print(f"[storage] 跳过无法解析的任务文件 {path.name}：{type(exc).__name__}: {exc}")
            continue

        if not isinstance(payload, dict):
            continue

        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
        tasks.append(
            {
                "filename": path.name,
                "thesis": payload.get("thesis") or "",
                "saved_at": payload.get("saved_at") or "",
                "saved_ts": payload.get("saved_ts") or 0,
                "sub_questions": len(result.get("sub_questions") or []),
                "verdict": verdict.get("verdict") or "",
            }
        )

    tasks.sort(key=lambda t: t.get("saved_ts") or 0, reverse=True)
    return tasks


def load_task(filename: str) -> dict:
    """加载指定任务。

    参数
    ----
    filename : str
        ``list_tasks()`` / ``save_task()`` 返回的文件名。

    返回
    ----
    dict
        ``{"filename": ..., "thesis": ..., "saved_at": ..., "saved_ts": ...,
        "result": {...}}``；直接取 ``record["result"]`` 即可重新渲染页面。

    异常
    ----
    FileNotFoundError
        文件不存在（例如云端实例重启后丢失）。
    ValueError
        文件名非法或越出 ``saved_tasks/`` 目录。
    """
    path = _task_path(filename)
    if not path.exists():
        raise FileNotFoundError(f"任务文件不存在：{filename}")

    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if not isinstance(payload, dict):
        raise ValueError(f"任务文件内容不是 JSON 对象：{filename}")

    result = payload.get("result")
    if not isinstance(result, dict):
        result = {}

    return {
        "filename": path.name,
        "thesis": payload.get("thesis") or "",
        "saved_at": payload.get("saved_at") or "",
        "saved_ts": payload.get("saved_ts") or 0,
        "result": result,
    }
