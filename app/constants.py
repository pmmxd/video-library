from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "VideoLibrary"
APP_TITLE = "影库"
APP_VERSION = "0.4.33"
ALL_LIBRARIES = 0

DEFAULT_MASK_MAP = {
    ".mp4": ".dll",
    ".m4v": ".m4vx",
    ".mov": ".qt",
    ".mkv": ".mkvx",
    ".avi": ".avix",
    ".wmv": ".wmvx",
    ".flv": ".flvx",
    ".webm": ".webmx",
    ".ts": ".dat",
    ".m2ts": ".m2tx",
    ".jpg": ".txt",
}
MASK_MAP = DEFAULT_MASK_MAP
UNMASK_MAP = {value: key for key, value in DEFAULT_MASK_MAP.items()}
MASKABLE_EXTENSIONS = frozenset(DEFAULT_MASK_MAP)

VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
    ".m2ts",
    ".dll",
    ".dat",
    ".m4vx",
    ".qt",
    ".mkvx",
    ".avix",
    ".wmvx",
    ".flvx",
    ".webmx",
    ".m2tx",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".txt"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def legacy_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def _data_dir() -> Path:
    override = os.environ.get("VIDEO_MANAGER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return application_root() / "data"


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "library.db"
THUMB_DIR_NAME = ".thumbnails"
THUMB_DIR = DATA_DIR / THUMB_DIR_NAME
THUMB_EXTENSION = ".vthumb"
THUMB_RUNTIME_EXTENSION = ".jpg"
LOG_DIR = DATA_DIR / "logs"


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", application_root())).resolve()
    return application_root()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def resolve_data_path(value: str | Path | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else DATA_DIR / path


def runtime_thumbnail_path(value: str | Path | None) -> Path | None:
    path = resolve_data_path(value)
    if path and path.suffix.lower() == THUMB_EXTENSION:
        runtime_path = path.with_suffix(THUMB_RUNTIME_EXTENSION)
        if runtime_path.exists():
            return runtime_path
    return path


def runtime_artwork_path(value: str | Path | None) -> Path | None:
    path = runtime_thumbnail_path(value)
    if not path:
        return None
    if not path.exists() and path.suffix.lower() != ".jpg":
        exposed_path = path.with_suffix(".jpg")
        if exposed_path.exists():
            return exposed_path
    if path.suffix.lower() == ".txt":
        runtime_path = path.with_suffix(".jpg")
        if runtime_path.exists():
            return runtime_path
    elif path.suffix.lower() == ".jpg" and not path.exists():
        original_path = path.with_suffix(".txt")
        if original_path.exists():
            return original_path
    return path


def enable_thumbnail_runtime_names(directory: Path = THUMB_DIR) -> None:
    for source in directory.glob(f"*{THUMB_EXTENSION}"):
        target = source.with_suffix(THUMB_RUNTIME_EXTENSION)
        if target.exists():
            continue
        try:
            source.replace(target)
        except OSError:
            continue


def restore_thumbnail_file_names(directory: Path = THUMB_DIR) -> None:
    for source in directory.glob(f"*{THUMB_RUNTIME_EXTENSION}"):
        target = source.with_suffix(THUMB_EXTENSION)
        try:
            source.replace(target)
        except OSError:
            continue


def initialize_data_dir() -> bool:
    if os.environ.get("VIDEO_MANAGER_DATA_DIR"):
        ensure_data_dirs()
        return False
    legacy = legacy_data_dir()
    if DB_PATH.exists() or not (legacy / "library.db").exists():
        ensure_data_dirs()
        return False
    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_DIR.exists():
        shutil.move(str(legacy), str(DATA_DIR))
    else:
        for item in legacy.iterdir():
            target = DATA_DIR / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
        try:
            legacy.rmdir()
        except OSError:
            pass
    ensure_data_dirs()
    return True


def find_tool(name: str, known_paths: tuple[str, ...] = ()) -> str | None:
    configured = os.environ.get(f"{name.upper()}_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
    for root in (bundled_root(), application_root()):
        local = root / "tools" / f"{name}.exe"
        if local.exists():
            return str(local)
    found = shutil.which(name)
    if found:
        return found
    for candidate in known_paths:
        path = Path(candidate)
        if path.exists():
            return str(path)
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
EXIFTOOL = find_tool("exiftool")
LOSSLESSCUT = find_tool("LosslessCut") or ""
