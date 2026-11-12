from __future__ import annotations

import hashlib
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from .constants import (
    THUMB_DIR,
    THUMB_DIR_NAME,
    THUMB_EXTENSION,
    THUMB_RUNTIME_EXTENSION,
    IMAGE_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from .database import Database
from .media_tools import (
    collect_sidecars,
    capture_at_from_filename,
    find_nfo,
    find_poster,
    find_preview,
    identity_key,
    is_jpeg,
    logical_extension,
    parse_nfo,
    probe_video,
    thumbnail_for,
)

PROBE_VERSION = "3"

# 程序自己的缓存/备份目录以及系统目录，扫库时一律跳过
SKIPPED_DIRECTORY_NAMES = {
    ".media-library",
    ".thumbnails",
    "$RECYCLE.BIN",
    "System Volume Information",
}
_FILE_ATTRIBUTE_HIDDEN = 0x2


def _is_hidden_directory(entry: os.DirEntry[str]) -> bool:
    """点开头或以隐藏属性存在的目录一律跳过（与资源管理器观感一致）。"""
    if entry.name.startswith("."):
        return True
    if entry.name in SKIPPED_DIRECTORY_NAMES:
        return True
    try:
        attributes = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _FILE_ATTRIBUTE_HIDDEN)


def iter_candidate_files(
    root: Path,
    recursive: bool = True,
    extensions: set[str] | None = None,
) -> list[Path]:
    allowed_extensions = extensions or VIDEO_EXTENSIONS
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not recursive or _is_hidden_directory(entry):
                                continue
                            stack.append(Path(entry.path))
                        elif (
                            entry.is_file(follow_symlinks=False)
                            and not entry.name.startswith(".")
                            and Path(entry.name).suffix.lower() in allowed_extensions
                        ):
                            found.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
        if not recursive:
            break
    return found


def content_hash(path: Path, chunk_size: int = 64 * 1024) -> str:
    """Fast SHA-256 fingerprint for large media: size, head and tail."""
    stat = path.stat()
    digest = hashlib.sha256(str(stat.st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(chunk_size))
        if stat.st_size > chunk_size:
            handle.seek(max(chunk_size, stat.st_size - chunk_size))
            digest.update(handle.read(chunk_size))
    return digest.hexdigest()


def relocation_matches(
    candidates: list[Path],
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Match moved/renamed files to stale records when size and mtime are unique."""
    normalized_paths = {
        str(path.resolve()).casefold(): path for path in candidates
    }
    current_keys = {identity_key(path) for path in candidates}
    record_keys = {record["identity_key"] for record in records}
    stale = [
        record
        for record in records
        if str(Path(record["path"]).resolve()).casefold() not in normalized_paths
        and record["identity_key"] not in current_keys
    ]
    by_signature: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for record in stale:
        signature = (int(record["size_bytes"]), int(record["mtime_ns"]))
        by_signature.setdefault(signature, []).append(record)

    candidate_signatures: dict[tuple[int, int], list[Path]] = {}
    candidate_stats: dict[str, tuple[int, int]] = {}
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = (stat.st_size, stat.st_mtime_ns)
        candidate_signatures.setdefault(signature, []).append(path)
        candidate_stats[str(path.resolve()).casefold()] = signature

    matches: dict[str, dict[str, Any]] = {}
    unresolved: list[Path] = []
    for path in candidates:
        if identity_key(path) in record_keys:
            continue
        path_key = str(path.resolve()).casefold()
        signature = candidate_stats.get(path_key)
        if signature is None:
            continue
        old_rows = by_signature.get(signature, [])
        new_paths = candidate_signatures.get(signature, [])
        if len(old_rows) == 1 and len(new_paths) == 1:
            matches[path_key] = old_rows[0]
        else:
            unresolved.append(path)

    by_hash: dict[str, list[dict[str, Any]]] = {}
    for record in stale:
        if record.get("content_hash"):
            by_hash.setdefault(str(record["content_hash"]), []).append(record)
    if by_hash:
        candidate_hashes: dict[str, list[Path]] = {}
        for path in unresolved:
            candidate_hashes.setdefault(content_hash(path), []).append(path)
        for digest, paths in candidate_hashes.items():
            old_rows = by_hash.get(digest, [])
            if len(old_rows) == 1 and len(paths) == 1:
                matches[str(paths[0].resolve()).casefold()] = old_rows[0]
    return matches


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    message = Signal(str)
    finished_scan = Signal(int, int, bool)

    def __init__(
        self,
        db_path: str,
        library_id: int,
        parent=None,
        directories: list[str] | None = None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.library_id = library_id
        self.directories = list(directories or [])

    def run(self) -> None:
        db = Database(self.db_path)
        scan_token = uuid.uuid4().hex
        library = db.library(self.library_id)
        if not library:
            db.close()
            self.finished_scan.emit(0, 0, False)
            return
        root = Path(library["root_path"])
        shooting_project = str(library.get("kind") or "") == "shooting"
        mask_map = db.mask_map()
        candidate_extensions = VIDEO_EXTENSIONS | set(mask_map.values())
        image_extensions = IMAGE_EXTENSIONS | {mask_map.get(".jpg", ".txt")}
        db.begin_scan(self.library_id, scan_token)
        if self.directories:
            candidates = []
            seen_candidates: set[str] = set()
            for directory in self.directories:
                for candidate in iter_candidate_files(
                    Path(directory),
                    recursive=False,
                    extensions=candidate_extensions,
                ):
                    key = str(candidate.resolve()).casefold()
                    if key not in seen_candidates:
                        seen_candidates.add(key)
                        candidates.append(candidate)
        else:
            candidates = iter_candidate_files(
                root,
                extensions=candidate_extensions,
            )
        refresh_probe = (
            db.setting(f"scan_probe_version:{self.library_id}") != PROBE_VERSION
        )
        total = len(candidates)
        subtitles_by_dir: dict[Path, list[Path]] = {}
        screenshots_by_dir: dict[Path, list[Path]] = {}
        for candidate in candidates:
            parent = candidate.parent
            if parent in subtitles_by_dir:
                continue
            try:
                subtitles_by_dir[parent] = [
                    child
                    for child in parent.iterdir()
                    if child.is_file()
                    and child.suffix.lower() in SUBTITLE_EXTENSIONS
                ]
            except OSError:
                subtitles_by_dir[parent] = []
            try:
                screenshot_dir = parent / ".screenshot"
                screenshots_by_dir[parent] = sorted(
                    (
                        child
                        for child in screenshot_dir.iterdir()
                        if child.is_file()
                        and child.suffix.lower() in image_extensions
                        and is_jpeg(child)
                    ),
                    key=lambda child: child.name.casefold(),
                )
            except OSError:
                screenshots_by_dir[parent] = []
        imported = 0
        errors = 0
        cancelled = False
        existing_by_key = {
            video["identity_key"]: video
            for video in db.videos(library_id=self.library_id)
        }
        relocated = relocation_matches(candidates, list(existing_by_key.values()))

        def process(path: Path) -> dict[str, Any] | None:
            try:
                logical_ext = logical_extension(path, mask_map)
                if logical_ext in {".jpg", ""}:
                    return None
                stat = path.stat()
                key = identity_key(path)
                existing = existing_by_key.get(key) or relocated.get(
                    str(path.resolve()).casefold()
                )
                nfo_path = find_nfo(path)
                nfo_data = parse_nfo(nfo_path)
                poster = find_poster(path, mask_map)
                screenshots = screenshots_by_dir.get(path.parent, [])
                preview = find_preview(path, screenshots, mask_map)
                sidecars = collect_sidecars(
                    path,
                    nfo_path,
                    poster,
                    subtitles_by_dir.get(path.parent),
                    screenshots,
                    mask_map,
                )
                same_file = (
                    existing
                    and existing["size_bytes"] == stat.st_size
                    and existing["mtime_ns"] == stat.st_mtime_ns
                )
                unchanged = (
                    same_file
                    and existing["bitrate_bps"] > 0
                    and existing["width"] > 0
                    and existing["height"] > 0
                )
                if unchanged and not refresh_probe:
                    probe = {
                        "duration_ms": existing["duration_ms"],
                        "bitrate_bps": existing["bitrate_bps"],
                        "width": existing["width"],
                        "height": existing["height"],
                        "codec": existing["codec"],
                        "fps": existing["fps"],
                        "rotation": existing.get("rotation") or 0,
                        "capture_at": existing.get("capture_at") or "",
                        "camera_make": existing.get("camera_make") or "",
                        "camera_model": existing.get("camera_model") or "",
                        "lens": existing.get("lens") or "",
                        "location": existing.get("location") or "",
                        "plot": existing.get("plot") or "",
                    }
                else:
                    probe = probe_video(path)
                nfo_capture = (
                    f"{nfo_data['premiered']}T00:00:00"
                    if nfo_data.get("premiered")
                    else ""
                )
                name_capture = capture_at_from_filename(path)
                capture_at = probe.get("capture_at") or (
                    name_capture or nfo_capture
                    if shooting_project
                    else nfo_capture or name_capture
                ) or datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                file_hash = (
                    str(existing.get("content_hash") or "")
                    if existing and same_file
                    else ""
                ) or content_hash(path)

                thumb_value = str(preview.resolve()) if preview else None
                if not preview:
                    cache_key = hashlib.sha1(
                        f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode(
                            "utf-8", errors="replace"
                        )
                    ).hexdigest()
                    thumb_path = THUMB_DIR / f"{cache_key}{THUMB_RUNTIME_EXTENSION}"
                    if not thumb_path.exists():
                        thumbnail_for(
                            path,
                            thumb_path,
                            int(probe.get("duration_ms") or 0),
                            None,
                        )
                    if thumb_path.exists():
                        thumb_value = str(
                            Path(THUMB_DIR_NAME)
                            / thumb_path.with_suffix(THUMB_EXTENSION).name
                        )
                nfo_title = str(nfo_data.get("title") or path.stem)
                renamed = bool(
                    existing
                    and str(existing.get("stem") or "") != path.stem
                )
                preserve_file_title = bool(
                    existing
                    and str(existing.get("title") or "") == path.stem
                    and nfo_title != path.stem
                )

                return {
                    "library_id": self.library_id,
                    "path": str(path.resolve()),
                    "identity_key": key,
                    "relative_path": str(path.resolve().relative_to(root.resolve())),
                    "file_name": path.name,
                    "stem": path.stem,
                    "logical_ext": logical_ext,
                    "physical_ext": path.suffix.lower(),
                    "title": (
                        path.stem
                        if renamed or preserve_file_title
                        else nfo_title
                    ),
                    "original_title": nfo_data.get("original_title"),
                    "year": nfo_data.get("year") or capture_at[:4],
                    "plot": nfo_data.get("plot") or probe.get("plot") or "",
                    "genres": nfo_data.get("genres"),
                    "tagline": nfo_data.get("tagline"),
                    "premiered": nfo_data.get("premiered"),
                    "country": nfo_data.get("country"),
                    "content_set": nfo_data.get("content_set"),
                    "mpaa": nfo_data.get("mpaa"),
                    "actors": nfo_data.get("actors"),
                    "directors": nfo_data.get("directors"),
                    "nfo_id": nfo_data.get("nfo_id"),
                    "nfo_ids": nfo_data.get("nfo_ids"),
                    "nfo_tags": nfo_data.get("nfo_tags"),
                    "nfo_raw": nfo_data.get("nfo_raw"),
                    "studio": nfo_data.get("studio"),
                    "rating": probe.get("rating"),
                    "duration_ms": int(
                        probe.get("duration_ms") or nfo_data.get("duration_ms") or 0
                    ),
                    "bitrate_bps": int(probe.get("bitrate_bps") or 0),
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "rotation": int(probe.get("rotation") or 0),
                    "capture_at": capture_at,
                    "camera_make": probe.get("camera_make") or "",
                    "camera_model": probe.get("camera_model") or "",
                    "lens": probe.get("lens") or "",
                    "location": probe.get("location") or "",
                    "source_kind": (
                        "shooting"
                        if shooting_project
                        else "nfo"
                        if nfo_path
                        else "plain"
                    ),
                    "codec": probe.get("codec"),
                    "fps": probe.get("fps"),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "nfo_path": str(nfo_path) if nfo_path else None,
                    "poster_path": str(poster) if poster else None,
                    "thumb_path": thumb_value,
                    "sidecars": [str(item) for item in sidecars],
                    "content_hash": file_hash,
                    "_existing_id": int(existing["id"]) if existing else None,
                }
            except Exception:
                return None

        self.message.emit(f"正在扫描 {total} 个候选文件")
        # ffprobe/ffmpeg 都是独立进程，并发能线性吃掉抽帧耗时；按 CPU 线程数的 2/3 开，
        # 上限 16，留一部分算力给界面
        worker_count = min(16, max(4, (os.cpu_count() or 4) * 2 // 3))
        last_emit = 0.0
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(process, path): path
                for path in candidates
                if not self.isInterruptionRequested()
            }
            for index, future in enumerate(as_completed(futures), start=1):
                if self.isInterruptionRequested():
                    cancelled = True
                    self.message.emit("扫描已取消")
                    break
                path = futures[future]
                try:
                    item = future.result()
                    if item:
                        db.upsert_video(item, scan_token)
                        imported += 1
                    now = time.monotonic()
                    if index == total or now - last_emit >= 0.15:
                        self.progress.emit(index, total, path.name)
                        last_emit = now
                except Exception:
                    errors += 1

        if not cancelled:
            db.finish_scan(
                self.library_id,
                scan_token,
                scope_paths=self.directories or None,
            )
            db.set_setting(f"scan_probe_version:{self.library_id}", PROBE_VERSION)
        db.close()
        self.finished_scan.emit(imported, errors, not cancelled)
