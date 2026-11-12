from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QFile, QThread, Signal

from .constants import (
    DATA_DIR,
    DEFAULT_MASK_MAP,
    FFMPEG,
    SUBTITLE_EXTENSIONS,
)
from .database import Database
from .media_tools import existing_video_path, identity_key, is_jpeg, logical_extension

EXPOSURE_JOURNAL = DATA_DIR / "expose-journal.json"


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def ensure_library_path(path: Path, library: dict[str, Any]) -> None:
    if not is_within(path, Path(library["root_path"])):
        raise ValueError(f"路径不在资源库范围内: {path}")


def record_temporary_exposure(masked_path: Path, exposed_path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPOSURE_JOURNAL.write_text(
        json.dumps(
            {"masked": str(masked_path), "exposed": str(exposed_path)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def clear_temporary_exposure() -> None:
    try:
        EXPOSURE_JOURNAL.unlink(missing_ok=True)
    except OSError:
        pass


def recover_temporary_exposure(db: Database) -> int:
    if not EXPOSURE_JOURNAL.exists():
        return 0
    try:
        record = json.loads(EXPOSURE_JOURNAL.read_text(encoding="utf-8"))
        masked = Path(record["masked"])
        exposed = Path(record["exposed"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        clear_temporary_exposure()
        return 0
    roots = [Path(library["root_path"]) for library in db.libraries()]
    if not roots or not any(
        is_within(masked, root) and is_within(exposed, root) for root in roots
    ):
        db.record_operation(
            "recover",
            str(exposed),
            str(masked),
            "failed",
            "恢复路径不在当前资源库范围内",
        )
        clear_temporary_exposure()
        return 0
    if exposed.exists() and not masked.exists():
        try:
            exposed.rename(masked)
            db.record_operation("recover", str(exposed), str(masked), "success")
            count = 1
        except OSError as exc:
            db.record_operation("recover", str(exposed), str(masked), "failed", str(exc))
            count = 0
    else:
        count = 0
    if masked.exists():
        clear_temporary_exposure()
    return count


def _move_file(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(str(source))
    if target.exists():
        raise FileExistsError(str(target))
    try:
        source.rename(target)
    except OSError:
        if target.exists():
            raise
        shutil.move(str(source), str(target))


_RECYCLE_SCRIPT = """
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName Microsoft.VisualBasic
$paths = @({items})
for ($i = 0; $i -lt $paths.Count; $i++) {{
    $p = $paths[$i]
    if (-not (Test-Path -LiteralPath $p)) {{
        Write-Output "$i`tMISSING"
        continue
    }}
    try {{
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $p, 'OnlyErrorDialogs', 'SendToRecycleBin')
        Write-Output "$i`tOK"
    }} catch {{
        Write-Output "$i`tFAIL $($_.Exception.Message)"
    }}
}}
"""


def _recycle_bin_via_shell(paths: list[Path]) -> dict[int, str]:
    """批量移入回收站，返回 {序号: "OK"|"FAIL ..."|"MISSING"}。

    QFile.moveToTrash 与 SHFileOperationW 在文件被其他进程短暂占用时会静默失败，
    这里改用系统自带的 VisualBasic 文件 API（与资源管理器同一路径），可靠性最高。
    """
    if not paths:
        return {}
    items = ", ".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    script = _RECYCLE_SCRIPT.format(items=items)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return {index: "FAIL 无法调用系统删除接口" for index in range(len(paths))}
    results: dict[int, str] = {index: "FAIL 未知错误" for index in range(len(paths))}
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            results[int(parts[0])] = parts[1].strip()
    return results


def _sidecar_paths(
    video: dict[str, Any],
    mask_map: dict[str, str] | None = None,
) -> list[Path]:
    try:
        return [
            _resolved_image_path(Path(item), mask_map)
            for item in json.loads(video.get("sidecars_json") or "[]")
        ]
    except (TypeError, json.JSONDecodeError):
        return []


def _resolved_image_path(
    path: Path,
    mask_map: dict[str, str] | None = None,
) -> Path:
    values = mask_map or DEFAULT_MASK_MAP
    masked_ext = values.get(".jpg", ".txt").lower()
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".txt", masked_ext}
    if path.suffix.lower() not in image_extensions:
        return path
    for candidate in (
        path,
        path.with_suffix(masked_ext),
        path.with_suffix(".jpg"),
        path.with_suffix(".txt"),
    ):
        if candidate.is_file():
            return candidate
    return path


def _same_stem_sidecar(path: Path, stem: str) -> bool:
    return path.is_file() and path.stem.casefold() == stem.casefold()


def _rollback(pairs: list[tuple[Path, Path]]) -> None:
    for source, target in reversed(pairs):
        try:
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
        except OSError:
            pass


def rename_video(
    db: Database,
    video: dict[str, Any],
    library: dict[str, Any],
    new_stem: str,
) -> tuple[Path, list[tuple[Path, Path]]]:
    mask_map = db.mask_map()
    new_stem = new_stem.strip()
    if not new_stem or any(char in new_stem for char in '<>:"/\\|?*'):
        raise ValueError("文件名无效")
    old_path = existing_video_path(video) or Path(video["path"])
    ensure_library_path(old_path, library)
    new_path = old_path.with_name(f"{new_stem}{old_path.suffix}")
    ensure_library_path(new_path, library)
    if new_path.exists():
        raise FileExistsError(f"目标文件已存在: {new_path.name}")

    moves: list[tuple[Path, Path]] = [(old_path, new_path)]
    for sidecar in _sidecar_paths(video, mask_map):
        if not sidecar.exists() or not _same_stem_sidecar(sidecar, video["stem"]):
            continue
        target = sidecar.with_name(f"{new_stem}{sidecar.suffix}")
        if target.exists():
            raise FileExistsError(f"关联文件目标已存在: {target.name}")
        moves.append((sidecar, target))

    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            _move_file(source, target)
            completed.append((source, target))
    except Exception:
        _rollback(completed)
        raise

    updated_sidecars = []
    for sidecar in _sidecar_paths(video, mask_map):
        target = next(
            (new for old, new in completed if old == sidecar),
            sidecar,
        )
        updated_sidecars.append(str(target))
    poster_path = video.get("poster_path")
    poster = (
        _resolved_image_path(Path(poster_path), mask_map)
        if poster_path
        else None
    )
    if poster:
        renamed = next(
            (new for old, new in completed if old == poster),
            None,
        )
        if renamed:
            poster_path = str(renamed)
        else:
            poster_path = str(poster)
    nfo_path = video.get("nfo_path")
    if nfo_path:
        renamed = next(
            (new for old, new in completed if str(old) == nfo_path),
            None,
        )
        if renamed:
            nfo_path = str(renamed)
    subtitle_path = video.get("subtitle_path")
    if subtitle_path:
        renamed = next(
            (new for old, new in completed if str(old) == subtitle_path),
            None,
        )
        if renamed:
            subtitle_path = str(renamed)

    db.update_video(
        video["id"],
        path=str(new_path),
        identity_key=identity_key(new_path),
        relative_path=str(new_path.resolve().relative_to(Path(library["root_path"]).resolve())),
        file_name=new_path.name,
        stem=new_stem,
        title=new_stem,
        physical_ext=new_path.suffix.lower(),
        sidecars_json=json.dumps(updated_sidecars, ensure_ascii=False),
        poster_path=poster_path,
        nfo_path=nfo_path,
        subtitle_path=subtitle_path,
    )
    for old, new in completed:
        db.record_operation("rename", str(old), str(new), "success")
    return new_path, completed


def move_video(
    db: Database,
    video: dict[str, Any],
    library: dict[str, Any],
    target_dir: Path,
) -> tuple[Path, list[tuple[Path, Path]]]:
    mask_map = db.mask_map()
    target_dir = target_dir.resolve()
    ensure_library_path(target_dir, library)
    if not target_dir.is_dir():
        raise NotADirectoryError(str(target_dir))
    old_path = existing_video_path(video) or Path(video["path"])
    ensure_library_path(old_path, library)
    if old_path.parent.resolve() == target_dir:
        raise ValueError("视频已经在这个目录中")
    new_path = target_dir / old_path.name
    if new_path.exists():
        raise FileExistsError(f"目标位置已有同名文件: {new_path.name}")

    moves: list[tuple[Path, Path]] = [(old_path, new_path)]
    sibling_videos = [
        item
        for item in old_path.parent.iterdir()
        if item.is_file()
        and item.suffix.lower() in {".mp4", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".ts", ".m2ts", ".dat", ".dll", ".mov", ".m4v"}
    ]
    single_video_folder = len(sibling_videos) <= 1
    for sidecar in _sidecar_paths(video, mask_map):
        if not sidecar.exists():
            continue
        same_stem = _same_stem_sidecar(sidecar, video["stem"])
        folder_metadata = sidecar.name.casefold() in {
            "movie.nfo",
            "movie.nfo.bak",
            "poster.jpg",
            "poster.txt",
            "fanart.jpg",
            "fanart.txt",
            "folder.jpg",
            "folder.txt",
        }
        try:
            relative = sidecar.resolve().relative_to(old_path.parent.resolve())
        except ValueError:
            continue
        nested_screenshot = (
            len(relative.parts) > 1
            and relative.parts[0].casefold() == ".screenshot"
        )
        if not same_stem and not (
            single_video_folder and (folder_metadata or nested_screenshot)
        ):
            continue
        target = target_dir / (sidecar.name if same_stem else relative)
        if target.exists():
            raise FileExistsError(f"目标位置已有同名关联文件: {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        moves.append((sidecar, target))

    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            _move_file(source, target)
            completed.append((source, target))
    except Exception:
        _rollback(completed)
        raise

    for source, _ in completed:
        if source.parent != old_path.parent:
            try:
                source.parent.rmdir()
            except OSError:
                pass

    updated_sidecars = []
    for sidecar in _sidecar_paths(video, mask_map):
        target = next(
            (new for old, new in completed if old == sidecar),
            sidecar,
        )
        updated_sidecars.append(str(target))
    poster_path = video.get("poster_path")
    poster = (
        _resolved_image_path(Path(poster_path), mask_map)
        if poster_path
        else None
    )
    if poster:
        renamed = next(
            (new for old, new in completed if old == poster),
            None,
        )
        if renamed:
            poster_path = str(renamed)
        else:
            poster_path = str(poster)
    nfo_path = video.get("nfo_path")
    if nfo_path:
        renamed = next(
            (new for old, new in completed if str(old) == nfo_path),
            None,
        )
        if renamed:
            nfo_path = str(renamed)
    subtitle_path = video.get("subtitle_path")
    if subtitle_path:
        renamed = next(
            (new for old, new in completed if str(old) == subtitle_path),
            None,
        )
        if renamed:
            subtitle_path = str(renamed)
    db.update_video(
        video["id"],
        path=str(new_path),
        identity_key=identity_key(new_path),
        relative_path=str(new_path.relative_to(Path(library["root_path"]))),
        file_name=new_path.name,
        stem=new_path.stem,
        sidecars_json=json.dumps(updated_sidecars, ensure_ascii=False),
        poster_path=poster_path,
        nfo_path=nfo_path,
        subtitle_path=subtitle_path,
    )
    for old, new in completed:
        db.record_operation("move", str(old), str(new), "success")
    return new_path, completed


def delete_video(
    db: Database,
    video: dict[str, Any],
    library: dict[str, Any],
    include_sidecars: bool,
) -> tuple[int, list[str]]:
    mask_map = db.mask_map()
    video_path = existing_video_path(video) or Path(video["path"])
    paths: list[Path] = [video_path]
    if include_sidecars:
        paths.extend(_sidecar_paths(video, mask_map))
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    failures: list[str] = []
    deleted = 0
    video_deleted = False
    missing: list[Path] = []
    pending: list[Path] = []
    moved: set[Path] = set()
    for path in unique:
        ensure_library_path(path, library)
        if not path.exists():
            missing.append(path)
        elif QFile.moveToTrash(str(path)):
            moved.add(path)
        else:
            pending.append(path)
    results = _recycle_bin_via_shell(pending)
    for index, path in enumerate(pending):
        if results.get(index) == "OK":
            moved.add(path)
    pending_detail = {path: results.get(index, "回收站操作失败") for index, path in enumerate(pending)}
    for path in unique:
        if path in moved:
            deleted += 1
            if path == video_path:
                video_deleted = True
            db.record_operation("trash", str(path), None, "success")
        elif path in missing:
            if path == video_path:
                video_deleted = True
                db.record_operation(
                    "trash",
                    str(path),
                    None,
                    "success",
                    "文件不存在，已移除数据库记录",
                )
        else:
            detail = pending_detail.get(path, "回收站操作失败")
            failures.append(f"{path.name}: {detail}")
            db.record_operation("trash", str(path), None, "failed", detail)
    if video_deleted:
        db.delete_video_record(video["id"])
    return deleted, failures


def _desired_extension(
    path: Path,
    enable: bool,
    logical_ext: str | None = None,
    mask_map: dict[str, str] | None = None,
) -> str:
    values = mask_map or DEFAULT_MASK_MAP
    ext = (logical_ext or logical_extension(path, values)).lower()
    if enable:
        return values.get(ext, ext)
    inverse = {target.lower(): source.lower() for source, target in values.items()}
    return inverse.get(path.suffix.lower(), ext)


class MaskWorker(QThread):
    progress = Signal(int, int, str)
    finished_mask = Signal(bool)

    def __init__(
        self,
        db_path: str,
        library_id: int,
        enable: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.library_id = library_id
        self.enable = enable
        self.failures: list[str] = []

    def run(self) -> None:
        db = Database(self.db_path)
        library = db.library(self.library_id)
        if not library:
            db.close()
            self.finished_mask.emit(False)
            return
        root = Path(library["root_path"])
        mask_map = db.mask_map()
        videos = db.videos(library_id=self.library_id)
        plan: list[tuple[dict[str, Any] | None, Path, Path, str | None]] = []
        seen: set[str] = set()

        for video in videos:
            path = Path(video["path"])
            target_ext = _desired_extension(
                path,
                self.enable,
                video["logical_ext"],
                mask_map,
            )
            if path.suffix.lower() != target_ext:
                plan.append((video, path, path.with_suffix(target_ext), None))
                seen.add(str(path).casefold())
            for sidecar in _sidecar_paths(video, mask_map):
                if str(sidecar).casefold() in seen or not sidecar.exists():
                    continue
                if not is_jpeg(sidecar):
                    continue
                target_ext = _desired_extension(
                    sidecar,
                    self.enable,
                    ".jpg",
                    mask_map,
                )
                if sidecar.suffix.lower() != target_ext:
                    plan.append((None, sidecar, sidecar.with_suffix(target_ext), video["id"]))
                    seen.add(str(sidecar).casefold())

        total = len(plan)
        completed = 0
        sidecar_changes: dict[int, dict[str, Path]] = {}
        for video, source, target, sidecar_video_id in plan:
            if self.isInterruptionRequested():
                break
            try:
                ensure_library_path(source, library)
                ensure_library_path(target, library)
                if target == source or target.exists():
                    if target.exists() and target != source:
                        raise FileExistsError("目标文件已存在")
                elif not source.exists():
                    raise FileNotFoundError("源文件不存在")
                else:
                    _move_file(source, target)
                if video is not None:
                    db.update_video(
                        video["id"],
                        path=str(target),
                        physical_ext=target.suffix.lower(),
                    )
                elif sidecar_video_id is not None:
                    sidecar_changes.setdefault(sidecar_video_id, {})[str(source)] = target
                db.record_operation(
                    "mask" if self.enable else "unmask",
                    str(source),
                    str(target),
                    "success",
                )
                completed += 1
            except Exception as exc:
                self.failures.append(f"{source.name}: {exc}")
                db.record_operation(
                    "mask" if self.enable else "unmask",
                    str(source),
                    str(target),
                    "failed",
                    str(exc),
                )
            self.progress.emit(total, completed, source.name)

        for video_id, changes in sidecar_changes.items():
            video = db.video(video_id)
            if not video:
                continue
            sidecars = []
            for sidecar in _sidecar_paths(video, mask_map):
                sidecars.append(changes.get(str(sidecar), sidecar))
            poster = (
                _resolved_image_path(Path(video["poster_path"]), mask_map)
                if video.get("poster_path")
                else None
            )
            if poster and str(poster) in changes:
                poster = changes[str(poster)]
            db.update_video(
                video_id,
                sidecars_json=json.dumps([str(item) for item in sidecars], ensure_ascii=False),
                poster_path=str(poster) if poster else None,
            )

        if not self.isInterruptionRequested():
            db.update_library_mask(self.library_id, self.enable)
        db.close()
        self.finished_mask.emit(not self.failures)


class ExportWorker(QThread):
    progress = Signal(int, str)
    finished_export = Signal(int, int, object)

    def __init__(
        self,
        items: list[dict[str, Any]],
        destination: Path,
        parent=None,
    ):
        super().__init__(parent)
        self.items = items
        self.destination = destination
        self.failures: list[str] = []

    @staticmethod
    def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
        candidate = directory / f"{stem}{suffix}"
        index = 2
        while candidate.exists():
            candidate = directory / f"{stem} ({index}){suffix}"
            index += 1
        return candidate

    @staticmethod
    def _copy_file(source: Path, target: Path) -> None:
        staging = target.with_name(f".{target.name}.exporting")
        try:
            with source.open("rb") as source_handle, staging.open("wb") as target_handle:
                while chunk := source_handle.read(8 * 1024 * 1024):
                    if QThread.currentThread().isInterruptionRequested():
                        raise InterruptedError
                    target_handle.write(chunk)
            os.replace(staging, target)
            shutil.copystat(source, target)
        except Exception:
            if staging.exists():
                partial = ExportWorker._unique_path(
                    target.parent,
                    target.stem,
                    f"{target.suffix}.partial",
                )
                try:
                    staging.replace(partial)
                except OSError:
                    pass
            raise

    def run(self) -> None:
        copied_videos = 0
        copied_subtitles = 0
        cancelled = False
        total = len(self.items)
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(self.items, 1):
                if self.isInterruptionRequested():
                    cancelled = True
                    break
                source = Path(str(item.get("source") or ""))
                if not source.is_file():
                    self.failures.append(f"{source.name or source}：视频不存在")
                    continue
                logical_ext = str(item.get("logical_ext") or source.suffix)
                if not logical_ext.startswith("."):
                    logical_ext = f".{logical_ext}"
                target = self._unique_path(
                    self.destination,
                    str(item.get("stem") or source.stem),
                    logical_ext,
                )
                try:
                    self._copy_file(source, target)
                    copied_videos += 1
                except InterruptedError:
                    cancelled = True
                    break
                except OSError as exc:
                    self.failures.append(f"{source.name}：{exc}")
                    continue

                seen: set[str] = set()
                for raw_subtitle in item.get("subtitles") or []:
                    if self.isInterruptionRequested():
                        cancelled = True
                        break
                    subtitle = Path(str(raw_subtitle))
                    key = str(subtitle.resolve()).casefold()
                    if (
                        key in seen
                        or not subtitle.is_file()
                        or subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS
                    ):
                        continue
                    seen.add(key)
                    if subtitle.name.startswith(source.stem):
                        relative_name = subtitle.name[len(source.stem) :]
                    else:
                        relative_name = subtitle.suffix
                    subtitle_target = self._unique_path(
                        self.destination,
                        target.stem,
                        relative_name,
                    )
                    try:
                        self._copy_file(subtitle, subtitle_target)
                        copied_subtitles += 1
                    except InterruptedError:
                        cancelled = True
                        break
                    except OSError as exc:
                        self.failures.append(f"{subtitle.name}：{exc}")
                self.progress.emit(
                    min(99, int(index * 100 / max(1, total))),
                    f"{index}/{total}  {source.name}",
                )
                if cancelled or self.isInterruptionRequested():
                    cancelled = True
                    break
        except OSError as exc:
            self.failures.append(f"目标目录不可用：{exc}")
        if cancelled:
            self.failures.append("导出已取消")
        self.finished_export.emit(
            copied_videos,
            copied_subtitles,
            list(self.failures),
        )


class TrimWorker(QThread):
    finished_trim = Signal(bool, str, str)

    def __init__(
        self,
        db_path: str,
        source: Path,
        output: Path,
        start_ms: int,
        end_ms: int,
        mode: str,
        parent=None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.source = source
        self.output = output
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.mode = mode

    def run(self) -> None:
        db = Database(self.db_path)
        if not FFMPEG:
            db.record_operation(
                "trim",
                str(self.source),
                str(self.output),
                "failed",
                "ffmpeg not found",
            )
            db.close()
            self.finished_trim.emit(False, "", "找不到 ffmpeg")
            return
        if self.output.exists():
            db.record_operation(
                "trim",
                str(self.source),
                str(self.output),
                "failed",
                "输出文件已存在",
            )
            db.close()
            self.finished_trim.emit(False, "", "输出文件已存在")
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        duration = max(1, self.end_ms - self.start_ms) / 1000
        args = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-ss",
            f"{self.start_ms / 1000:.3f}",
            "-i",
            str(self.source),
            "-t",
            f"{duration:.3f}",
        ]
        if self.mode == "copy":
            args.extend(["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"])
        else:
            args.extend(
                [
                    "-map",
                    "0:v:0?",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                ]
            )
            if self.output.suffix.lower() in {".mp4", ".mov", ".m4v"}:
                args.extend(["-movflags", "+faststart"])
        args.append(str(self.output))
        process = None
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            while process.poll() is None:
                if self.isInterruptionRequested():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    db.record_operation(
                        "trim",
                        str(self.source),
                        str(self.output),
                        "cancelled",
                    )
                    db.close()
                    self.finished_trim.emit(False, "", "裁剪已取消")
                    return
                time.sleep(0.1)
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            success = process.returncode == 0 and self.output.exists()
            detail = "" if success else stderr.strip()[-1500:] or "ffmpeg 执行失败"
            db.record_operation(
                "trim",
                str(self.source),
                str(self.output),
                "success" if success else "failed",
                detail,
            )
            db.close()
            self.finished_trim.emit(success, str(self.output) if success else "", detail)
        except Exception as exc:
            if process and process.poll() is None:
                process.kill()
            db.record_operation(
                "trim",
                str(self.source),
                str(self.output),
                "failed",
                str(exc),
            )
            db.close()
            self.finished_trim.emit(False, "", str(exc))
