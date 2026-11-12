from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "d3d11va")

if getattr(sys, "frozen", False):
    # ctranslate2 用 LoadLibrary 找 cublas64_12.dll，打包后要显式把 _internal
    # 加进 DLL 搜索目录，否则 GPU 转写会报找不到 cublas。
    for _folder in {
        Path(sys.executable).parent,
        Path(getattr(sys, "_MEIPASS", sys.executable)),
    }:
        if _folder.is_dir():
            try:
                os.add_dll_directory(str(_folder))
            except (AttributeError, OSError):
                pass

if len(sys.argv) >= 2 and sys.argv[1] == "--transcribe-worker":
    from app.transcribe_child import main as transcribe_worker_main

    raise SystemExit(transcribe_worker_main(sys.argv[2:]))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from app.constants import (
    APP_TITLE,
    DB_PATH,
    LOG_DIR,
    initialize_data_dir,
)
from app.theme import apply_theme
from app.ui.icons import svg_icon
from app.ui.main_window import MainWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地视频资源管理器")
    parser.add_argument("--screenshot", type=Path, help="启动后保存窗口截图并退出")
    return parser.parse_args()


def install_exception_logger() -> None:
    def handle(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "crash.log").write_text(text, encoding="utf-8")
        except OSError:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle


def main() -> int:
    initialize_data_dir()
    install_exception_logger()
    args = parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("LocalVideoLibrary")
    app.setWindowIcon(svg_icon("app.svg"))
    apply_theme(app)

    window = MainWindow(DB_PATH)
    window.show()
    if args.screenshot:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

        def grab() -> None:
            window.grab().save(str(args.screenshot), "PNG")
            app.quit()

        QTimer.singleShot(1400, grab)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
