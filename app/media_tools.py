from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .constants import (
    EXIFTOOL,
    FFMPEG,
    FFPROBE,
    IMAGE_EXTENSIONS,
    DEFAULT_MASK_MAP,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from .security import hide_directory

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 抽帧用硬件解码：实测 10 分钟以上的视频，cuda 比纯 CPU 快约 2.3 倍。
# 首次失败自动退回 CPU 并记住结果，避免每帧都白试一次。
_HWACCEL_FLAG = ["-hwaccel", "cuda"]
_HWACCEL_ARGS: list[str] | None = list(_HWACCEL_FLAG)

# 这些元数据键说明视频可能带旋转矩阵，必须交给 ffprobe 读 display matrix
_AV_ROTATION_HINTS = (
    "com.android",
    "com.apple",
    "com.xiaomi",
    "creation_time",
    "location",
)


def _hwaccel_args() -> list[str]:
    return _HWACCEL_ARGS or []


def _disable_hwaccel() -> None:
    global _HWACCEL_ARGS
    _HWACCEL_ARGS = []



def read_head(path: Path, size: int = 512) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def is_jpeg(path: Path) -> bool:
    return read_head(path, 3) == b"\xff\xd8\xff"


def sniff_media(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS and is_jpeg(path):
        return ".jpg"
    head = read_head(path, 512)
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".mp4"
    if ext in {".ts", ".m2ts", ".dat"} and head and head[0] == 0x47:
        return ".ts"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return ".avi"
    if head.startswith(b"FLV"):
        return ".flv"
    if head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return ".wmv"
    if ext in VIDEO_EXTENSIONS and ext not in {".dll", ".dat"}:
        return ext
    return None


def is_video_candidate(path: Path) -> bool:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    detected = sniff_media(path)
    return detected is not None and detected != ".jpg"


def identity_key(path: Path) -> str:
    return f"{str(path.parent.resolve()).casefold()}|{path.stem.casefold()}"


def parse_nfo(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    candidates = [path]
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        candidates.append(backup)
    root = None
    for candidate in candidates:
        try:
            root = ET.parse(candidate).getroot()
            break
        except (ET.ParseError, OSError, UnicodeError):
            continue
    if root is None:
        return {}

    def text(*names: str) -> str:
        for name in names:
            value = root.findtext(name)
            if value and value.strip():
                return value.strip()
        return ""

    def all_text(name: str) -> list[str]:
        values = []
        for node in root.findall(name):
            if node.text and node.text.strip():
                values.append(node.text.strip())
        return values

    genres = [
        node.text.strip()
        for node in root.findall("genre")
        if node.text and node.text.strip()
    ]
    if not genres:
        genre = text("genre")
        genres = [item.strip() for item in re.split(r"[/,，]", genre) if item.strip()]
    try:
        rating = float(text("rating")) if text("rating") else None
    except ValueError:
        rating = None
    runtime = 0
    runtime_value = text("runtime", "duration")
    match = re.search(r"\d+", runtime_value)
    if match:
        runtime = int(match.group()) * 60_000

    unique_ids = []
    for node in root.findall("uniqueid"):
        value = (node.text or "").strip()
        if value:
            unique_ids.append(
                {
                    "type": node.get("type") or "",
                    "value": value,
                    "default": node.get("default", "").lower() == "true",
                }
            )
    primary_id = next(
        (item["value"] for item in unique_ids if item["default"]),
        next(
            (
                item["value"]
                for item in unique_ids
                if item["type"].casefold() in {"num", "id", "tmdb", "imdb"}
            ),
            unique_ids[0]["value"] if unique_ids else "",
        ),
    )
    actors = []
    for node in root.findall("actor"):
        name = (node.findtext("name") or "").strip()
        if not name:
            continue
        actors.append(
            {
                "name": name,
                "role": (node.findtext("role") or "").strip(),
                "thumb": (node.findtext("thumb") or "").strip(),
            }
        )
    raw: dict[str, Any] = {}
    for node in root:
        if node.tag in {"uniqueid", "actor", "set"}:
            continue
        value = (node.text or "").strip()
        if not value:
            continue
        if node.tag in raw:
            if not isinstance(raw[node.tag], list):
                raw[node.tag] = [raw[node.tag]]
            raw[node.tag].append(value)
        else:
            raw[node.tag] = value
    return {
        "title": text("title"),
        "original_title": text("originaltitle"),
        "year": text("year", "premiered")[:4],
        "plot": text("plot", "outline"),
        "tagline": text("tagline"),
        "genres": genres,
        "nfo_tags": all_text("tag"),
        "studio": text("studio", "maker"),
        "rating": rating,
        "duration_ms": runtime,
        "premiered": text("premiered", "releasedate"),
        "country": text("country"),
        "content_set": text("set/name"),
        "mpaa": text("mpaa"),
        "directors": all_text("director"),
        "actors": actors,
        "nfo_id": primary_id,
        "nfo_ids": unique_ids,
        "nfo_raw": raw,
    }


def find_nfo(video_path: Path) -> Path | None:
    candidates = (
        video_path.with_suffix(".nfo"),
        video_path.parent / "movie.nfo",
        video_path.parent / "tvshow.nfo",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _image_suffixes(mask_map: dict[str, str] | None = None) -> tuple[str, ...]:
    values = mask_map or DEFAULT_MASK_MAP
    return tuple(
        dict.fromkeys(
            (".jpg", ".jpeg", ".png", ".webp", values.get(".jpg", ".txt"), ".txt")
        )
    )


def _image_candidates(
    video_path: Path,
    mask_map: dict[str, str] | None = None,
) -> list[Path]:
    stem = video_path.stem
    bases = (
        "poster",
        "folder",
        "fanart",
        "thumb",
        "landscape",
        f"{stem}-poster",
        f"{stem}-fanart",
        stem,
    )
    suffixes = _image_suffixes(mask_map)
    result: list[Path] = []
    for base in bases:
        for suffix in suffixes:
            path = video_path.parent / f"{base}{suffix}"
            if path.exists() and path not in result:
                result.append(path)
    return result


def find_poster(
    video_path: Path,
    mask_map: dict[str, str] | None = None,
) -> Path | None:
    for candidate in _image_candidates(video_path, mask_map):
        if is_jpeg(candidate):
            return candidate
    return None


def _screenshot_candidates(
    video_path: Path,
    mask_map: dict[str, str] | None = None,
) -> list[Path]:
    folder = video_path.parent / ".screenshot"
    if not folder.is_dir():
        return []
    try:
        candidates = sorted(
            (
                child
                for child in folder.iterdir()
                if child.is_file()
                and child.suffix.lower() in _image_suffixes(mask_map)
                and is_jpeg(child)
            ),
            key=lambda child: child.name.casefold(),
        )
    except OSError:
        return []
    return candidates


def find_preview(
    video_path: Path,
    screenshots: list[Path] | None = None,
    mask_map: dict[str, str] | None = None,
) -> Path | None:
    """Prefer the library's landscape artwork over a generated video frame."""
    stem = video_path.stem
    for base in ("fanart", "landscape", "thumb", f"{stem}-fanart"):
        for suffix in _image_suffixes(mask_map):
            candidate = video_path.parent / f"{base}{suffix}"
            if candidate.exists() and is_jpeg(candidate):
                return candidate
    shots = (
        _screenshot_candidates(video_path, mask_map)
        if screenshots is None
        else screenshots
    )
    for candidate in shots:
        if candidate.is_file() and is_jpeg(candidate):
            return candidate
    return find_poster(video_path, mask_map)


def collect_sidecars(
    video_path: Path,
    nfo: Path | None,
    poster: Path | None,
    subtitles: list[Path] | None = None,
    screenshots: list[Path] | None = None,
    mask_map: dict[str, str] | None = None,
) -> list[Path]:
    result: list[Path] = []
    if nfo:
        result.append(nfo)
        backup = nfo.with_suffix(nfo.suffix + ".bak")
        if backup.exists():
            result.append(backup)
    if poster:
        result.append(poster)
    for candidate in _image_candidates(video_path, mask_map):
        if candidate not in result:
            result.append(candidate)
    shots = (
        _screenshot_candidates(video_path, mask_map)
        if screenshots is None
        else screenshots
    )
    for candidate in shots:
        if candidate.is_file() and is_jpeg(candidate) and candidate not in result:
            result.append(candidate)
    for child in subtitles if subtitles is not None else video_path.parent.iterdir():
        if not child.is_file():
            continue
        child_stem = child.stem.casefold()
        video_stem = video_path.stem.casefold()
        if child_stem != video_stem and not child_stem.startswith(f"{video_stem}."):
            continue
        if child.suffix.lower() in SUBTITLE_EXTENSIONS and child not in result:
            result.append(child)
    return result


def _hidden_run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _parse_fps(value: str | None) -> float | None:
    if not value or "/" not in value:
        try:
            return float(value) if value else None
        except ValueError:
            return None
    left, right = value.split("/", 1)
    try:
        denominator = float(right)
        return round(float(left) / denominator, 3) if denominator else None
    except ValueError:
        return None


def _parse_rating(value: Any) -> float | None:
    """文件元数据里的评分（ExifTool/QuickTime 的 Rating），0 视为无评分。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    return max(0.0, min(5.0, number)) or None


def _normalize_capture_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(
        r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[T ](\d{2}):(\d{2}):(\d{2})",
        text,
    )
    if match:
        return (
            f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            f"T{match.group(4)}:{match.group(5)}:{match.group(6)}"
        )
    return text[:19]


def capture_at_from_filename(path: Path) -> str:
    match = re.search(
        r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_ T]?"
        r"(\d{2})(\d{2})(\d{2})(?!\d)",
        path.stem,
    )
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}"


def _display_dimensions(stream: dict[str, Any]) -> tuple[int, int]:
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    rotation = _rotation_degrees(stream)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def _rotation_degrees(stream: dict[str, Any]) -> int:
    rotation_value: Any = (stream.get("tags") or {}).get("rotate")
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("rotation") is not None:
            rotation_value = side_data["rotation"]
            break
    try:
        rotation = int(round(float(rotation_value)))
    except (TypeError, ValueError):
        rotation = 0
    return rotation % 360


def _parse_exif_duration(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(float(value) * 1000)
    text = str(value)
    if ":" in text:
        parts = text.split(":")
        try:
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + float(part)
            return int(seconds * 1000)
        except ValueError:
            return 0
    return 0


def _probe_with_exiftool(path: Path) -> dict[str, Any]:
    if not EXIFTOOL:
        return {}
    try:
        result = _exiftool_argfile(
            [
                "-j",
                "-fast",
                "-n",
                "-charset",
                "filename=UTF8",
                "-FileType",
                "-MIMEType",
                "-Duration",
                "-ImageWidth",
                "-ImageHeight",
                "-Rotation",
                "-VideoFrameRate",
                "-CreateDate",
                "-DateTimeOriginal",
                "-MediaCreateDate",
                "-Make",
                "-Model",
                "-LensModel",
                "-GPSCoordinates",
                "-Rating",
                str(path),
            ]
        )
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        item = payload[0] if payload else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return {}
    width = int(item.get("ImageWidth") or 0)
    height = int(item.get("ImageHeight") or 0)
    try:
        rotation = int(round(float(item.get("Rotation") or 0)))
    except (TypeError, ValueError):
        rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "duration_ms": _parse_exif_duration(item.get("Duration")),
        "width": width,
        "height": height,
        "fps": _parse_fps(str(item.get("VideoFrameRate") or "")),
        "codec": None,
        "rotation": rotation % 360,
        "capture_at": _normalize_capture_at(
            item.get("CreateDate")
            or item.get("DateTimeOriginal")
            or item.get("MediaCreateDate")
        ),
        "camera_make": str(item.get("Make") or ""),
        "camera_model": str(item.get("Model") or ""),
        "lens": str(item.get("LensModel") or ""),
        "location": str(item.get("GPSCoordinates") or ""),
        "rating": _parse_rating(item.get("Rating")),
    }


def _probe_with_av(path: Path) -> dict[str, Any] | None:
    """进程内用 PyAV 探测，比起一个 ffprobe 子进程快约 27 倍（实测 0.012s vs 0.33s）。

    手机/相机拍的视频靠 display matrix 记录旋转方向，PyAV 17 读不出这个矩阵，
    这类文件交给 ffprobe 处理，避免竖屏视频的宽高被算反。
    """
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path), "r") as container:
            video = next(
                (item for item in container.streams if item.type == "video"), None
            )
            if video is None:
                return None
            metadata = {
                str(key).lower(): str(value)
                for key, value in (container.metadata or {}).items()
            }
            if any(hint in key for key in metadata for hint in _AV_ROTATION_HINTS):
                return None
            duration = container.duration
            width = video.codec_context.width or 0
            height = video.codec_context.height or 0
            sar = video.codec_context.sample_aspect_ratio
            if (
                sar
                and sar.numerator
                and sar.denominator
                and sar.numerator != sar.denominator
            ):
                width = int(round(width * sar.numerator / sar.denominator))
            rate = video.average_rate or video.guessed_rate
            data = {
                "duration_ms": int(duration / 1000) if duration else 0,
                "bitrate_bps": int(container.bit_rate or 0),
                "width": width,
                "height": height,
                "codec": video.codec_context.name,
                "fps": float(rate) if rate else 0.0,
                "rotation": 0,
                "capture_at": "",
                "camera_make": "",
                "camera_model": "",
                "lens": "",
                "location": "",
                "plot": "",
                "rating": _parse_rating(metadata.get("rating")),
            }
    except Exception:  # noqa: BLE001 - 任何容器异常都退回 ffprobe
        return None
    if not data["duration_ms"] or not data["width"] or not data["height"]:
        return None
    return data


def _exiftool_argfile(lines: list[str]) -> subprocess.CompletedProcess[bytes]:
    """用英文临时目录里的 UTF-8 argfile 调用 ExifTool。

    argfile 自身的路径也不能含中文：那样 ``-@`` 的参数同样会被 Windows
    代码页损坏，ExifTool 会报 "Invalid filename encoding"。
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="vl-exiftool-",
        suffix=".args",
        delete=False,
        encoding="utf-8",
        newline="\n",
    )
    try:
        handle.write("\n".join(lines))
        handle.close()
        return _hidden_run(
            [EXIFTOOL, "-charset", "exiftool=UTF8", "-@", handle.name],
            timeout=60,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def write_video_rating(path: Path, rating: float | None) -> tuple[bool, str]:
    """把评分写进视频文件自身的元数据（ExifTool 的 Rating，0 表示清除）。"""
    if not EXIFTOOL:
        return False, "未找到 ExifTool"
    value = str(int(round(float(rating)))) if rating else "0"
    try:
        result = _exiftool_argfile(
            [
                "-charset",
                "filename=UTF8",
                "-overwrite_original",
                f"-Rating={value}",
                f"-XMP:Rating={value}",
                str(path),
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        return False, detail or f"退出码 {result.returncode}"
    return True, detail


def write_video_tags(path: Path, tags: list[str]) -> tuple[bool, str]:
    """把标签写进视频文件的 QuickTime:Genre 字段。

    多个标签用「; 」连接：Windows 资源管理器的“流派”列读的就是这个字段，
    ExifTool、VLC 这类工具也按同一个键读取。

    中文标签不能直接走命令行参数：Windows 下 ExifTool 会按系统代码页解释，
    写入即损坏。改用 UTF-8 的 argfile 传参。
    """
    if not EXIFTOOL:
        return False, "未找到 ExifTool"
    cleaned = [tag.strip() for tag in tags if tag.strip()]
    lines = [
        "-charset",
        "filename=UTF8",
        "-overwrite_original",
        f"-Genre={'; '.join(cleaned)}",
        str(path),
    ]
    try:
        result = _exiftool_argfile(lines)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        return False, detail or f"退出码 {result.returncode}"
    return True, detail


def probe_video(path: Path) -> dict[str, Any]:
    quick = _probe_with_av(path)
    if quick is not None:
        if not quick.get("bitrate_bps") and quick.get("duration_ms"):
            try:
                quick["bitrate_bps"] = int(
                    path.stat().st_size * 8 / (quick["duration_ms"] / 1000)
                )
            except (OSError, ZeroDivisionError):
                quick["bitrate_bps"] = 0
        return quick
    data: dict[str, Any] = {}
    if FFPROBE:
        try:
            result = _hidden_run(
                [
                    FFPROBE,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,bit_rate:"
                    "format_tags=creation_time,location,make,model,encoder,description,rating:"
                    "stream=codec_type,codec_name,width,height,r_frame_rate,"
                    "display_aspect_ratio:stream_tags=rotate,creation_time,location:"
                    "stream_side_data=rotation",
                    "-of",
                    "json",
                    str(path),
                ],
                timeout=25,
            )
            payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
            streams = payload.get("streams") or []
            video_stream = next(
                (item for item in streams if item.get("codec_type") == "video"), {}
            )
            duration = (payload.get("format") or {}).get("duration")
            bitrate = (payload.get("format") or {}).get("bit_rate")
            format_tags = (payload.get("format") or {}).get("tags") or {}
            stream_tags = video_stream.get("tags") or {}
            width, height = _display_dimensions(video_stream)
            data = {
                "duration_ms": int(float(duration) * 1000) if duration else 0,
                "bitrate_bps": int(bitrate) if bitrate else 0,
                "width": width,
                "height": height,
                "codec": video_stream.get("codec_name"),
                "fps": _parse_fps(video_stream.get("r_frame_rate")),
                "rotation": _rotation_degrees(video_stream),
                "capture_at": _normalize_capture_at(
                    format_tags.get("creation_time") or stream_tags.get("creation_time")
                ),
                "camera_make": str(
                    format_tags.get("make")
                    or format_tags.get("com.apple.quicktime.make")
                    or ""
                ),
                "camera_model": str(
                    format_tags.get("model")
                    or format_tags.get("com.apple.quicktime.model")
                    or ""
                ),
                "lens": str(format_tags.get("lens_model") or ""),
                "location": str(
                    format_tags.get("location") or stream_tags.get("location") or ""
                ),
                "plot": str(format_tags.get("description") or ""),
                "rating": _parse_rating(
                    format_tags.get("rating") or stream_tags.get("rating")
                ),
            }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            data = {}
    if not data or not data.get("duration_ms"):
        fallback = _probe_with_exiftool(path)
        for key, value in fallback.items():
            if value and not data.get(key):
                data[key] = value
    if not data.get("bitrate_bps") and data.get("duration_ms"):
        try:
            data["bitrate_bps"] = int(
                path.stat().st_size * 8 / (data["duration_ms"] / 1000)
            )
        except (OSError, ZeroDivisionError):
            data["bitrate_bps"] = 0
    return data


def thumbnail_for(
    video_path: Path,
    output_path: Path,
    duration_ms: int,
    poster_path: Path | None,
) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    hide_directory(output_path.parent)
    if poster_path and is_jpeg(poster_path):
        image = QImage(str(poster_path))
        if not image.isNull():
            image = image.scaled(
                640,
                360,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            return image.save(str(output_path), "JPEG", 88)
    if not FFMPEG:
        return False
    seek_seconds = max(1.0, min((duration_ms / 1000) * 0.12, 180.0))
    base = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        *_hwaccel_args(),
        "-ss",
        f"{seek_seconds:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2",
        "-q:v",
        "4",
        "-an",
        "-sn",
        "-dn",
        "-y",
        str(output_path),
    ]
    try:
        result = _hidden_run(base, timeout=30)
        if result.returncode != 0 and _hwaccel_args():
            # 硬件解码不可用（无独显/驱动不支持/编解码器不支持），退回 CPU 解码
            _disable_hwaccel()
            result = _hidden_run([item for item in base if item not in _HWACCEL_FLAG], timeout=30)
        return result.returncode == 0 and output_path.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def _mask_inverse(mask_map: dict[str, str] | None = None) -> dict[str, str]:
    values = mask_map or DEFAULT_MASK_MAP
    return {target.lower(): source.lower() for source, target in values.items()}


def logical_extension(
    path: Path,
    mask_map: dict[str, str] | None = None,
) -> str:
    detected = sniff_media(path)
    ext = path.suffix.lower()
    inverse = _mask_inverse(mask_map)
    logical = inverse.get(ext)
    if logical and detected and detected != ".jpg":
        return logical
    if logical in {".ts", ".m2ts"} and read_head(path, 1) == b"\x47":
        return logical
    return detected or ""


def masked_extension(
    logical_ext: str,
    mask_map: dict[str, str] | None = None,
) -> str:
    values = mask_map or DEFAULT_MASK_MAP
    return values.get(logical_ext.lower(), logical_ext.lower())


def exposed_extension(
    physical_ext: str,
    mask_map: dict[str, str] | None = None,
) -> str:
    return _mask_inverse(mask_map).get(physical_ext.lower(), physical_ext.lower())


def existing_video_path(
    video: dict[str, Any],
    mask_map: dict[str, str] | None = None,
) -> Path | None:
    raw_path = str(video.get("path") or "").strip()
    if not raw_path:
        return None
    source = Path(raw_path)
    if source.is_file():
        return source
    values = mask_map or DEFAULT_MASK_MAP
    inverse = _mask_inverse(values)
    suffixes = [
        source.suffix.lower(),
        str(video.get("physical_ext") or "").lower(),
        str(video.get("logical_ext") or "").lower(),
        values.get(str(video.get("logical_ext") or "").lower(), ""),
        inverse.get(str(video.get("physical_ext") or "").lower(), ""),
    ]
    for suffix in dict.fromkeys(item for item in suffixes if item):
        candidate = source.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    allowed_extensions = VIDEO_EXTENSIONS | set(values.values())
    try:
        matches = [
            item
            for item in source.parent.iterdir()
            if item.is_file()
            and item.stem.casefold() == source.stem.casefold()
            and item.suffix.lower() in allowed_extensions
        ]
    except OSError:
        return None
    return sorted(matches, key=lambda item: item.name.casefold())[0] if matches else None


def format_duration(duration_ms: int) -> str:
    seconds = max(0, int(duration_ms // 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_timecode(duration_ms: int) -> str:
    seconds = max(0, int(duration_ms // 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_timecode(value: str) -> int | None:
    text = value.strip()
    if not text:
        return 0
    try:
        if ":" not in text:
            return max(0, int(float(text) * 1000))
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            return None
        numbers = [float(part) for part in parts]
        if len(numbers) == 2:
            minutes, seconds = numbers
            total = minutes * 60 + seconds
        else:
            hours, minutes, seconds = numbers
            total = hours * 3600 + minutes * 60 + seconds
        return max(0, int(total * 1000))
    except ValueError:
        return None


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_bitrate(bitrate_bps: int) -> str:
    if bitrate_bps <= 0:
        return "码率未知"
    mbps = bitrate_bps / 1_000_000
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{bitrate_bps / 1000:.0f} Kbps"


def resolution_label(width: int, height: int) -> str:
    short_side = min(width, height)
    if short_side >= 2160:
        return "4K"
    if short_side >= 1440:
        return "1440p"
    if short_side >= 1080:
        return "1080p"
    if short_side >= 720:
        return "720p"
    return f"{width}×{height}" if width and height else "未知"


def progress_fraction(position_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 0.0
    return max(0.0, min(1.0, position_ms / duration_ms))


def nearest_keyframe_seconds(duration_ms: int) -> int:
    return max(5, min(30, math.ceil(duration_ms / 1000 / 120)))
