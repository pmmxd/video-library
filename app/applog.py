"""轻量文件日志：只记录关键事件，写失败时静默，绝不干扰主流程。"""

from __future__ import annotations

import time

from .constants import LOG_DIR

_MAX_BYTES = 2 * 1024 * 1024


def log_file():
    return LOG_DIR / "videolibrary.log"


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = log_file()
        if target.exists() and target.stat().st_size > _MAX_BYTES:
            target.replace(target.with_name("videolibrary.1.log"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
        pass
