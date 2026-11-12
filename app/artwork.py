from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    DEFAULT_MASK_MAP,
    IMAGE_EXTENSIONS,
    THUMB_DIR_NAME,
    THUMB_EXTENSION,
    THUMB_RUNTIME_EXTENSION,
)
from .database import Database
from .media_tools import is_jpeg

PREVIEW_SOURCE_VERSION = "source-artwork-v1"


def remove_cached_thumbnails(data_dir: Path, values: Iterable[str]) -> int:
    """Delete only generated thumbnail files, never library media or artwork."""
    thumb_dir = (Path(data_dir) / THUMB_DIR_NAME).resolve()
    removed = 0
    for value in values:
        raw = Path(str(value))
        if raw.parent.name.casefold() != THUMB_DIR_NAME:
            continue
        for suffix in {THUMB_EXTENSION, THUMB_RUNTIME_EXTENSION}:
            candidate = (thumb_dir / raw.name).with_suffix(suffix).resolve()
            if candidate.parent != thumb_dir or not candidate.is_file():
                continue
            try:
                candidate.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _logical_source(path: Path, masked_ext: str = ".txt") -> Path | None:
    masked_ext = masked_ext.lower()
    if path.suffix.lower() == masked_ext and path.is_file():
        return path
    if path.suffix.lower() == masked_ext and not path.exists():
        exposed = path.with_suffix(".jpg")
        if exposed.is_file():
            return path
    if path.suffix.lower() == ".jpg" and not path.exists():
        masked_path = path.with_suffix(masked_ext)
        if masked_path.is_file():
            return masked_path
    return None


class ArtworkRuntime:
    """Temporarily expose library artwork as JPEG names while the app is open."""

    def __init__(self, db: Database):
        self.db = db
        self.data_dir = Path(db.path).parent
        self.journal = self.data_dir / "artwork-journal.json"
        self._entries: dict[str, Path] = {}
        self._recover()

    def _recover(self) -> None:
        if not self.journal.exists():
            return
        try:
            rows = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            self.journal.unlink(missing_ok=True)
            return
        remaining = []
        for row in rows if isinstance(rows, list) else []:
            try:
                original = Path(row["original"])
                exposed = Path(row["exposed"])
            except (KeyError, TypeError):
                continue
            if exposed.is_file() and not original.exists():
                try:
                    exposed.replace(original)
                except OSError:
                    self._entries[str(exposed)] = original
                    remaining.append(
                        {"original": str(original), "exposed": str(exposed)}
                    )
        if remaining:
            self.journal.write_text(
                json.dumps(remaining, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            self.journal.unlink(missing_ok=True)

    def _desired_sources(self) -> set[Path]:
        desired: set[Path] = set()
        mask_map = self.db.mask_map()
        masked_ext = str(mask_map.get(".jpg", ".txt")).lower()
        image_extensions = IMAGE_EXTENSIONS | {masked_ext}
        for video in self.db.videos():
            values: list[Any] = [video.get("poster_path"), video.get("thumb_path")]
            values.extend(video.get("sidecars") or [])
            for value in values:
                if not value:
                    continue
                path = Path(str(value))
                if path.suffix.lower() not in image_extensions:
                    continue
                source = self._entries.get(str(path.resolve())) or _logical_source(
                    path,
                    masked_ext,
                )
                if not source or source.suffix.lower() != masked_ext:
                    continue
                image = source if source.is_file() else source.with_suffix(".jpg")
                if image.is_file() and is_jpeg(image):
                    desired.add(source.resolve())
        return desired

    def _save_journal(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._entries:
            self.journal.unlink(missing_ok=True)
            return
        payload = [
            {"original": str(original), "exposed": str(exposed)}
            for exposed, original in sorted(self._entries.items())
        ]
        self.journal.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def cleanup_legacy_cache(self) -> None:
        if self.db.setting("preview_source_version") == PREVIEW_SOURCE_VERSION:
            return
        data_root = self.data_dir.resolve()
        for name in (THUMB_DIR_NAME, "thumbnails"):
            directory = (self.data_dir / name).resolve()
            if directory.parent == data_root and directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
        self.db.set_setting("preview_source_version", PREVIEW_SOURCE_VERSION)

    def refresh(self) -> int:
        desired = self._desired_sources()
        desired_keys = {str(path).casefold() for path in desired}
        changed = 0

        for exposed_text, original in list(self._entries.items()):
            if str(original).casefold() in desired_keys:
                continue
            exposed = Path(exposed_text)
            if exposed.is_file() and not original.exists():
                try:
                    exposed.replace(original)
                except OSError:
                    continue
            self._entries.pop(exposed_text, None)
            changed += 1

        plan: dict[str, tuple[Path, Path]] = {}
        for original in desired:
            exposed = original.with_suffix(".jpg")
            if exposed.exists() and original.exists():
                continue
            if exposed.is_file() or original.is_file():
                plan[str(exposed)] = (exposed, original)
        if plan:
            # Persist intent before renaming so an abnormal exit is recoverable.
            self._entries.update(
                {str(exposed): original for exposed, original in plan.values()}
            )
            self._save_journal()
        for exposed, original in plan.values():
            if original.is_file() and not exposed.exists():
                try:
                    original.replace(exposed)
                    changed += 1
                except OSError:
                    self._entries.pop(str(exposed), None)
        self._save_journal()
        return changed

    def reset_after_mask(self) -> None:
        self._entries.clear()
        self.journal.unlink(missing_ok=True)

    def restore(self) -> int:
        restored = 0
        for exposed_text, original in list(self._entries.items()):
            exposed = Path(exposed_text)
            if exposed.is_file() and not original.exists():
                try:
                    exposed.replace(original)
                    restored += 1
                except OSError:
                    continue
            self._entries.pop(exposed_text, None)
        self._save_journal()
        return restored
