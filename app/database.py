from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cities import location_label
from .constants import (
    DEFAULT_MASK_MAP,
    MASKABLE_EXTENSIONS,
    THUMB_DIR_NAME,
    THUMB_EXTENSION,
    THUMB_RUNTIME_EXTENSION,
)
from .security import create_password_record, verify_password


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'auto',
    mask_enabled INTEGER NOT NULL DEFAULT 0,
    isolated INTEGER NOT NULL DEFAULT 0,
    scan_complete INTEGER NOT NULL DEFAULT 1,
    last_scan_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    color TEXT NOT NULL DEFAULT '#E5B567',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    identity_key TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    stem TEXT NOT NULL,
    logical_ext TEXT NOT NULL,
    physical_ext TEXT NOT NULL,
    title TEXT NOT NULL,
    original_title TEXT,
    year TEXT,
    plot TEXT,
    genres TEXT NOT NULL DEFAULT '[]',
    studio TEXT,
    rating REAL,
    premiered TEXT,
    country TEXT,
    content_set TEXT,
    mpaa TEXT,
    tagline TEXT,
    actors_json TEXT NOT NULL DEFAULT '[]',
    directors_json TEXT NOT NULL DEFAULT '[]',
    nfo_id TEXT,
    nfo_ids_json TEXT NOT NULL DEFAULT '[]',
    nfo_tags_json TEXT NOT NULL DEFAULT '[]',
    nfo_raw_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    bitrate_bps INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    rotation INTEGER NOT NULL DEFAULT 0,
    capture_at TEXT,
    camera_make TEXT NOT NULL DEFAULT '',
    camera_model TEXT NOT NULL DEFAULT '',
    lens TEXT NOT NULL DEFAULT '',
    location TEXT,
    source_kind TEXT,
    codec TEXT,
    fps REAL,
    content_hash TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    nfo_path TEXT,
    subtitle_path TEXT,
    poster_path TEXT,
    thumb_path TEXT,
    sidecars_json TEXT NOT NULL DEFAULT '[]',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    favorite INTEGER NOT NULL DEFAULT 0,
    watched INTEGER NOT NULL DEFAULT 0,
    last_position_ms INTEGER NOT NULL DEFAULT 0,
    last_played_at TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    scan_token TEXT,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(library_id, identity_key)
);

CREATE INDEX IF NOT EXISTS idx_videos_library ON videos(library_id);
CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category_id);
CREATE INDEX IF NOT EXISTS idx_videos_played ON videos(last_played_at DESC);
CREATE TABLE IF NOT EXISTS video_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_group_members (
    group_id INTEGER NOT NULL REFERENCES video_groups(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL UNIQUE REFERENCES videos(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, video_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS hidden_tags (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    hidden_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_aliases (
    source_name TEXT PRIMARY KEY COLLATE NOCASE,
    target_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_path TEXT,
    target_path TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _capture_at_from_name(name: str) -> str:
    match = re.search(
        r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_ T]?"
        r"(\d{2})(\d{2})(\d{2})(?!\d)",
        Path(name).stem,
    )
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        self.conn.commit()
        self._migrate_categories_to_libraries()
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_library_top_name
            ON categories(library_id, name COLLATE NOCASE)
            WHERE parent_id IS NULL
            """
        )
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_library_child_name
            ON categories(library_id, parent_id, name COLLATE NOCASE)
            WHERE parent_id IS NOT NULL
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_capture ON videos(capture_at)"
        )
        self.conn.commit()

    def _migrate_columns(self) -> None:
        migrations = {
            "libraries": {
                "isolated": "INTEGER NOT NULL DEFAULT 0",
                "scan_complete": "INTEGER NOT NULL DEFAULT 1",
            },
            "videos": {
                "bitrate_bps": "INTEGER NOT NULL DEFAULT 0",
                "rotation": "INTEGER NOT NULL DEFAULT 0",
                "capture_at": "TEXT",
                "camera_make": "TEXT NOT NULL DEFAULT ''",
                "camera_model": "TEXT NOT NULL DEFAULT ''",
                "lens": "TEXT NOT NULL DEFAULT ''",
                "location": "TEXT",
                "source_kind": "TEXT",
                "premiered": "TEXT",
                "country": "TEXT",
                "content_set": "TEXT",
                "mpaa": "TEXT",
                "tagline": "TEXT",
                "actors_json": "TEXT NOT NULL DEFAULT '[]'",
                "directors_json": "TEXT NOT NULL DEFAULT '[]'",
                "nfo_id": "TEXT",
                "nfo_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "nfo_tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "nfo_raw_json": "TEXT NOT NULL DEFAULT '{}'",
                "subtitle_path": "TEXT",
                "content_hash": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

    def _migrate_categories_to_libraries(self) -> None:
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(categories)").fetchall()
        }
        if "library_id" in existing:
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DROP TABLE IF EXISTS categories_new")
            self.conn.execute(
                """
                CREATE TABLE categories_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id INTEGER NOT NULL
                        REFERENCES libraries(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    parent_id INTEGER REFERENCES categories_new(id) ON DELETE CASCADE,
                    color TEXT NOT NULL DEFAULT '#E5B567',
                    created_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute("UPDATE videos SET category_id = NULL")
            self.conn.execute("DROP TABLE categories")
            self.conn.execute("ALTER TABLE categories_new RENAME TO categories")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            try:
                self.conn.execute("DROP TABLE IF EXISTS categories_new")
            except sqlite3.Error:
                pass
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def add_library(
        self,
        name: str,
        root_path: str | Path,
        kind: str = "auto",
        mask_enabled: bool = False,
    ) -> int:
        root = str(Path(root_path).expanduser().resolve())
        cursor = self.conn.execute(
            """
            INSERT INTO libraries(
                name, root_path, kind, mask_enabled, scan_complete, created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (name.strip(), root, kind, int(mask_enabled), utc_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def libraries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT l.*,
                   COUNT(v.id) AS video_count,
                   SUM(CASE WHEN v.missing = 0 THEN 1 ELSE 0 END) AS available_count
            FROM libraries l
            LEFT JOIN videos v ON v.library_id = l.id
            GROUP BY l.id
            ORDER BY l.created_at, l.name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def library(self, library_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM libraries WHERE id = ?", (library_id,)
        ).fetchone()
        return dict(row) if row else None

    def remove_library(self, library_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT thumb_path FROM videos WHERE library_id = ? AND thumb_path IS NOT NULL",
            (library_id,),
        ).fetchall()
        try:
            self.conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (f"library_access_password:{library_id}",),
            )
            self.conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return [str(row["thumb_path"]) for row in rows if row["thumb_path"]]

    def update_library_mask(self, library_id: int, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE libraries SET mask_enabled = ? WHERE id = ?",
            (int(enabled), library_id),
        )
        self.conn.commit()

    def rename_library(self, library_id: int, name: str) -> None:
        self.conn.execute(
            "UPDATE libraries SET name = ? WHERE id = ?",
            (name.strip(), library_id),
        )
        self.conn.commit()

    def update_library_isolated(self, library_id: int, isolated: bool) -> None:
        self.conn.execute(
            "UPDATE libraries SET isolated = ? WHERE id = ?",
            (int(isolated), library_id),
        )
        self.conn.commit()

    def update_library_kind(self, library_id: int, kind: str) -> None:
        self.conn.execute(
            "UPDATE libraries SET kind = ? WHERE id = ?",
            (kind, library_id),
        )
        self.conn.commit()

    def update_library_kind(self, library_id: int, kind: str) -> None:
        self.conn.execute(
            "UPDATE libraries SET kind = ? WHERE id = ?",
            (kind, library_id),
        )
        self.conn.commit()

    def setting(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    @staticmethod
    def _validated_mask_map(values: dict[str, Any] | None) -> dict[str, str]:
        source = values if isinstance(values, dict) else {}
        result: dict[str, str] = {}
        used: set[str] = set()
        for logical_ext in sorted(MASKABLE_EXTENSIONS):
            raw_target = source.get(
                logical_ext,
                DEFAULT_MASK_MAP[logical_ext],
            )
            target = str(raw_target or "").strip().lower()
            if (
                not re.fullmatch(r"\.[a-z0-9_-]{1,16}", target)
                or target in MASKABLE_EXTENSIONS
                or target in used
            ):
                raise ValueError(f"伪装后缀无效或重复：{target or '(空)'}")
            result[logical_ext] = target
            used.add(target)
        return result

    def mask_map(self) -> dict[str, str]:
        try:
            values = json.loads(self.setting("mask_extension_map") or "{}")
        except json.JSONDecodeError:
            values = {}
        try:
            return self._validated_mask_map(values)
        except ValueError:
            return dict(DEFAULT_MASK_MAP)

    def set_mask_map(self, values: dict[str, Any]) -> None:
        clean = self._validated_mask_map(values)
        self.set_setting(
            "mask_extension_map",
            json.dumps(clean, ensure_ascii=False, sort_keys=True),
        )

    def has_isolated_reveal_password(self) -> bool:
        return bool(self.setting("isolated_reveal_password"))

    def set_isolated_reveal_password(self, password: str) -> None:
        self.set_setting(
            "isolated_reveal_password",
            create_password_record(password),
        )

    def verify_isolated_reveal_password(self, password: str) -> bool:
        return verify_password(
            password,
            self.setting("isolated_reveal_password"),
        )

    def has_library_access_password(self, library_id: int) -> bool:
        return bool(self.setting(f"library_access_password:{library_id}"))

    def set_library_access_password(self, library_id: int, password: str) -> None:
        self.set_setting(
            f"library_access_password:{library_id}",
            create_password_record(password),
        )

    def verify_library_access_password(
        self,
        library_id: int,
        password: str,
    ) -> bool:
        return verify_password(
            password,
            self.setting(f"library_access_password:{library_id}"),
        )

    def begin_scan(self, library_id: int, token: str) -> None:
        self.conn.execute(
            "UPDATE libraries SET last_scan_at = ?, scan_complete = 0 WHERE id = ?",
            (utc_now(), library_id),
        )
        self.conn.commit()

    def finish_scan(
        self,
        library_id: int,
        token: str,
        scope_paths: list[str] | None = None,
    ) -> None:
        scopes = (
            [Path(path).resolve() for path in scope_paths]
            if scope_paths is not None
            else None
        )
        rows = self.conn.execute(
            """
            SELECT id, path FROM videos
            WHERE library_id = ? AND COALESCE(scan_token, '') <> ?
            """,
            (library_id, token),
        ).fetchall()
        stale_ids = []
        preserved_ids = []
        for row in rows:
            path = Path(row["path"]).resolve()
            if scopes is not None and not any(
                path == scope or path.is_relative_to(scope)
                for scope in scopes
            ):
                continue
            if path.exists():
                preserved_ids.append(int(row["id"]))
            else:
                stale_ids.append(int(row["id"]))
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            self.conn.execute(
                f"DELETE FROM videos WHERE id IN ({placeholders})",
                stale_ids,
            )
            self.conn.execute(
                "DELETE FROM video_groups WHERE id NOT IN ("
                "SELECT group_id FROM video_group_members "
                "GROUP BY group_id HAVING COUNT(*) >= 2)"
            )
        if preserved_ids:
            placeholders = ",".join("?" for _ in preserved_ids)
            self.conn.execute(
                f"UPDATE videos SET scan_token = ? WHERE id IN ({placeholders})",
                (token, *preserved_ids),
            )
        self.conn.execute(
            "UPDATE libraries SET scan_complete = 1 WHERE id = ?",
            (library_id,),
        )
        self.conn.commit()

    def existing_video_for(self, library_id: int, identity_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM videos WHERE library_id = ? AND identity_key = ?",
            (library_id, identity_key),
        ).fetchone()
        return dict(row) if row else None

    def upsert_video(self, data: dict[str, Any], scan_token: str) -> int:
        now = utc_now()
        existing_id = data.get("_existing_id")
        if existing_id:
            existing = self.conn.execute(
                "SELECT * FROM videos WHERE id = ? AND library_id = ?",
                (existing_id, data["library_id"]),
            ).fetchone()
        else:
            existing = self.conn.execute(
                """
                SELECT * FROM videos
                WHERE path = ? COLLATE NOCASE
                   OR (library_id = ? AND identity_key = ?)
                LIMIT 1
                """,
                (data["path"], data["library_id"], data["identity_key"]),
            ).fetchone()

        values = {
            "library_id": data["library_id"],
            "path": data["path"],
            "identity_key": data["identity_key"],
            "relative_path": data.get("relative_path", ""),
            "file_name": data["file_name"],
            "stem": data["stem"],
            "logical_ext": data["logical_ext"],
            "physical_ext": data["physical_ext"],
            "title": data.get("title") or data["stem"],
            "original_title": data.get("original_title"),
            "year": data.get("year"),
            "plot": data.get("plot"),
            "genres": json.dumps(data.get("genres") or [], ensure_ascii=False),
            "studio": data.get("studio"),
            "rating": data.get("rating"),
            "premiered": data.get("premiered"),
            "country": data.get("country"),
            "content_set": data.get("content_set"),
            "mpaa": data.get("mpaa"),
            "tagline": data.get("tagline"),
            "actors_json": json.dumps(data.get("actors") or [], ensure_ascii=False),
            "directors_json": json.dumps(data.get("directors") or [], ensure_ascii=False),
            "nfo_id": data.get("nfo_id"),
            "nfo_ids_json": json.dumps(data.get("nfo_ids") or [], ensure_ascii=False),
            "nfo_tags_json": json.dumps(data.get("nfo_tags") or [], ensure_ascii=False),
            "nfo_raw_json": json.dumps(data.get("nfo_raw") or {}, ensure_ascii=False),
            "duration_ms": int(data.get("duration_ms") or 0),
            "bitrate_bps": int(data.get("bitrate_bps") or 0),
            "width": int(data.get("width") or 0),
            "height": int(data.get("height") or 0),
            "rotation": int(data.get("rotation") or 0),
            "capture_at": data.get("capture_at"),
            "camera_make": data.get("camera_make") or "",
            "camera_model": data.get("camera_model") or "",
            "lens": data.get("lens") or "",
            "location": data.get("location"),
            "source_kind": data.get("source_kind"),
            "codec": data.get("codec"),
            "fps": data.get("fps"),
            "content_hash": data.get("content_hash"),
            "size_bytes": int(data.get("size_bytes") or 0),
            "mtime_ns": int(data.get("mtime_ns") or 0),
            "nfo_path": data.get("nfo_path"),
            "poster_path": data.get("poster_path"),
            "thumb_path": data.get("thumb_path"),
            "sidecars_json": json.dumps(data.get("sidecars") or [], ensure_ascii=False),
            "missing": 0,
            "scan_token": scan_token,
            "updated_at": now,
        }
        if values["rating"] is None and existing is not None:
            # 文件元数据里没有评分时保留用户手动评的星，别在重扫时被清掉
            values["rating"] = existing["rating"]

        if existing:
            video_id = int(existing["id"])
            assignments = ", ".join(f"{key} = ?" for key in values)
            self.conn.execute(
                f"UPDATE videos SET {assignments} WHERE id = ?",
                (*values.values(), video_id),
            )
        else:
            values["added_at"] = now
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            cursor = self.conn.execute(
                f"INSERT INTO videos({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            video_id = int(cursor.lastrowid)
        self.conn.commit()
        return video_id

    def update_video(self, video_id: int, **fields: Any) -> None:
        allowed = {
            "path",
            "identity_key",
            "relative_path",
            "file_name",
            "stem",
            "logical_ext",
            "physical_ext",
            "title",
            "poster_path",
            "thumb_path",
            "sidecars_json",
            "nfo_path",
            "subtitle_path",
            "category_id",
            "favorite",
            "watched",
            "rating",
            "last_position_ms",
            "last_played_at",
            "missing",
            "capture_at",
            "rotation",
            "camera_make",
            "camera_model",
            "lens",
            "location",
            "source_kind",
        }
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        self.conn.execute(
            f"UPDATE videos SET {assignments} WHERE id = ?",
            (*changes.values(), video_id),
        )
        self.conn.commit()

    def delete_video_record(self, video_id: int) -> None:
        self.delete_video_records([video_id])

    def delete_video_records(self, video_ids: Iterable[int]) -> int:
        ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"DELETE FROM videos WHERE id IN ({placeholders})",
            ids,
        )
        self.conn.execute(
            "DELETE FROM video_groups WHERE id NOT IN ("
            "SELECT group_id FROM video_group_members "
            "GROUP BY group_id HAVING COUNT(*) >= 2)"
        )
        self.conn.execute(
            "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM video_tags)"
        )
        self.conn.commit()
        return max(0, int(cursor.rowcount))

    def repair_thumbnail_paths(self, thumb_dir: Path) -> int:
        rows = self.conn.execute(
            "SELECT id, thumb_path FROM videos WHERE thumb_path IS NOT NULL"
        ).fetchall()
        repaired = 0
        for row in rows:
            raw = str(row["thumb_path"] or "")
            if not raw:
                continue
            candidate = (thumb_dir / Path(raw).name).with_suffix(THUMB_EXTENSION)
            runtime = candidate.with_suffix(THUMB_RUNTIME_EXTENSION)
            relative = (
                str(Path(THUMB_DIR_NAME) / candidate.name)
                if candidate.exists() or runtime.exists()
                else None
            )
            if raw != relative:
                self.conn.execute(
                    "UPDATE videos SET thumb_path = ? WHERE id = ?",
                    (relative, row["id"]),
                )
                repaired += 1
        self.conn.commit()
        return repaired

    def video(self, video_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return self._video_dict(row) if row else None

    def video_by_path(self, path: str | Path) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM videos WHERE path = ? COLLATE NOCASE",
            (str(Path(path)),),
        ).fetchone()
        return self._video_dict(row) if row else None

    def video_paths_for_library(self, library_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT path FROM videos
            WHERE library_id = ? AND missing = 0
            """,
            (library_id,),
        ).fetchall()
        return [str(row["path"]) for row in rows]

    def missing_videos(
        self,
        library_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if library_ids is not None and not library_ids:
            return []
        params: list[Any] = []
        where = "WHERE missing = 1"
        if library_ids is not None:
            placeholders = ",".join("?" for _ in library_ids)
            where += f" AND library_id IN ({placeholders})"
            params.extend(library_ids)
        rows = self.conn.execute(
            f"SELECT * FROM videos {where} ORDER BY id",
            params,
        ).fetchall()
        return [self._video_dict(row) for row in rows]

    def top_level_folders(self, library_id: int) -> list[dict[str, Any]]:
        library = self.library(library_id)
        if not library:
            return []
        root = Path(library["root_path"])
        if not root.is_dir():
            return []
        skipped = {".media-library", ".thumbnails", "$RECYCLE.BIN", "System Volume Information"}
        names: set[str] = set()
        try:
            for child in root.iterdir():
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and child.name not in skipped
                ):
                    names.add(child.name)
        except OSError:
            return []
        counts: dict[str, int] = {}
        for row in self.conn.execute(
            """
            SELECT relative_path FROM videos
            WHERE library_id = ? AND missing = 0
            """,
            (library_id,),
        ).fetchall():
            relative = str(row["relative_path"] or "").replace("/", "\\")
            folder, separator, _remainder = relative.partition("\\")
            if separator and folder:
                counts[folder] = counts.get(folder, 0) + 1
        return [
            {"name": name, "video_count": counts.get(name, 0)}
            for name in sorted(names, key=str.casefold)
        ]

    def _video_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key, field, fallback in (
            ("genres", "genres", []),
            ("sidecars", "sidecars_json", []),
            ("actors", "actors_json", []),
            ("directors", "directors_json", []),
            ("nfo_ids", "nfo_ids_json", []),
            ("nfo_tags", "nfo_tags_json", []),
            ("nfo_raw", "nfo_raw_json", {}),
        ):
            try:
                data[key] = json.loads(data.get(field) or json.dumps(fallback))
            except (TypeError, json.JSONDecodeError):
                data[key] = fallback
        capture_at = str(data.get("capture_at") or "").strip()
        name_capture = _capture_at_from_name(str(data.get("file_name") or ""))
        if not capture_at:
            if str(data.get("source_kind") or "") == "shooting" and name_capture:
                capture_at = name_capture
            elif data.get("premiered"):
                capture_at = f"{data['premiered']}T00:00:00"
            elif name_capture:
                capture_at = name_capture
        if not capture_at and int(data.get("mtime_ns") or 0):
            capture_at = datetime.fromtimestamp(
                int(data["mtime_ns"]) / 1_000_000_000
            ).isoformat(timespec="seconds")
        data["capture_at"] = capture_at
        data["capture_date"] = capture_at[:10] if capture_at else ""
        data["capture_time"] = capture_at[11:19] if len(capture_at) >= 19 else ""
        data["year_month"] = capture_at[:7] if capture_at else ""
        return data

    def videos(
        self,
        library_id: int | None = None,
        category_id: int | None = None,
        mode: str = "all",
        search: str = "",
        sort: str = "added",
        descending: bool | None = None,
        tag: str | None = None,
        library_ids: list[int] | None = None,
        nfo_filters: dict[str, str] | None = None,
        orientation: str = "all",
        metadata_filters: dict[str, str] | None = None,
        folder: str | None = None,
        expanded_groups: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if library_ids is not None:
            if not library_ids:
                return []
            placeholders = ",".join("?" for _ in library_ids)
            clauses.append(f"library_id IN ({placeholders})")
            params.extend(library_ids)
        elif library_id is not None:
            clauses.append("library_id = ?")
            params.append(library_id)
        if category_id is not None:
            clauses.append("category_id = ?")
            params.append(category_id)
        if mode == "uncategorized":
            clauses.append("category_id IS NULL")
        elif mode == "favorite":
            clauses.append("favorite = 1")
        elif mode == "continue":
            clauses.append("last_position_ms > 5000 AND watched = 0")
        elif mode == "recent":
            clauses.append("datetime(added_at) >= datetime('now', '-7 days')")
        elif mode == "missing":
            clauses.append("missing = 1")
        if folder:
            clauses.append(
                r"(instr(REPLACE(relative_path, '/', '\'), '\') > 0 "
                r"AND substr(REPLACE(relative_path, '/', '\'), 1, "
                r"instr(REPLACE(relative_path, '/', '\'), '\') - 1) = ?)"
            )
            params.append(folder)
        if orientation == "landscape":
            clauses.append("width > 0 AND height > 0 AND width >= height")
        elif orientation == "portrait":
            clauses.append("width > 0 AND height > width")
        if search.strip():
            needle = f"%{search.strip()}%"
            clauses.append(
                "(title LIKE ? OR file_name LIKE ? OR relative_path LIKE ? "
                "OR COALESCE(plot, '') LIKE ? OR genres LIKE ? "
                "OR COALESCE(nfo_id, '') LIKE ? OR nfo_tags_json LIKE ? "
                "OR actors_json LIKE ? OR directors_json LIKE ? "
                "OR COALESCE(premiered, '') LIKE ? OR COALESCE(country, '') LIKE ? "
                "OR COALESCE(content_set, '') LIKE ? OR COALESCE(mpaa, '') LIKE ? "
                "OR COALESCE(tagline, '') LIKE ? OR COALESCE(capture_at, '') LIKE ? "
                "OR COALESCE(camera_make, '') LIKE ? OR COALESCE(camera_model, '') LIKE ? "
                "OR COALESCE(lens, '') LIKE ? OR COALESCE(location, '') LIKE ?)"
            )
            params.extend([needle] * 19)
        if tag:
            clauses.append(
                """
                (
                    EXISTS (
                        SELECT 1 FROM video_tags vt
                        JOIN tags t ON t.id = vt.tag_id
                        WHERE vt.video_id = videos.id AND t.name = ? COLLATE NOCASE
                          AND NOT EXISTS (
                              SELECT 1 FROM hidden_tags ht
                              WHERE ht.name = t.name COLLATE NOCASE
                          )
                    )
                    OR EXISTS (
                        SELECT 1 FROM json_each(videos.nfo_tags_json) AS nfo_tag
                        WHERE (
                            nfo_tag.value = ? COLLATE NOCASE
                            OR EXISTS (
                                SELECT 1 FROM tag_aliases ta
                                WHERE ta.source_name = nfo_tag.value COLLATE NOCASE
                                  AND ta.target_name = ? COLLATE NOCASE
                            )
                        )
                          AND NOT EXISTS (
                              SELECT 1 FROM hidden_tags ht
                              WHERE ht.name = nfo_tag.value COLLATE NOCASE
                                 OR ht.name = (
                                     SELECT ta.target_name
                                     FROM tag_aliases ta
                                     WHERE ta.source_name = nfo_tag.value COLLATE NOCASE
                                 )
                          )
                    )
                )
                """
            )
            params.extend([tag, tag, tag])
        if nfo_filters:
            direct_fields = {
                "year": "year",
                "premiered": "premiered",
                "country": "country",
                "studio": "studio",
                "content_set": "content_set",
                "mpaa": "mpaa",
            }
            for key, column in direct_fields.items():
                value = (nfo_filters.get(key) or "").strip()
                if value:
                    clauses.append(f"COALESCE({column}, '') = ? COLLATE NOCASE")
                    params.append(value)
            json_fields = {
                "genre": ("genres", "value"),
                "nfo_tag": ("nfo_tags_json", "value"),
                "director": ("directors_json", "value"),
                "actor": ("actors_json", "name"),
            }
            for key, (column, value_expression) in json_fields.items():
                value = (nfo_filters.get(key) or "").strip()
                if not value:
                    continue
                if key == "nfo_tag":
                    clauses.append(
                        """
                        EXISTS (
                            SELECT 1 FROM json_each(videos.nfo_tags_json) AS json_item
                            WHERE (
                                json_item.value = ? COLLATE NOCASE
                                OR EXISTS (
                                    SELECT 1 FROM tag_aliases ta
                                    WHERE ta.source_name = json_item.value COLLATE NOCASE
                                      AND ta.target_name = ? COLLATE NOCASE
                                )
                            )
                        )
                        """
                    )
                    params.extend([value, value])
                    continue
                expression = (
                    f"json_extract(json_item.value, '$.{value_expression}')"
                    if value_expression == "name"
                    else "json_item.value"
                )
                clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1 FROM json_each(videos.{column}) AS json_item
                        WHERE {expression} = ? COLLATE NOCASE
                    )
                    """
                )
                params.append(value)
        if metadata_filters:
            date_expression = (
                "COALESCE(NULLIF(capture_at, ''), NULLIF(premiered, ''), "
                "CASE "
                "WHEN file_name GLOB '[12][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
                "THEN substr(file_name, 1, 10) "
                "WHEN file_name GLOB '[12][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*' "
                "THEN substr(file_name, 1, 4) || '-' || substr(file_name, 5, 2) || '-' || substr(file_name, 7, 2) "
                "END, "
                "datetime(mtime_ns / 1000000000.0, 'unixepoch', 'localtime'))"
            )
            capture_year = (metadata_filters.get("capture_year") or "").strip()
            if capture_year:
                clauses.append(f"substr({date_expression}, 1, 4) = ?")
                params.append(capture_year)
            capture_month = (metadata_filters.get("capture_month") or "").strip()
            if capture_month:
                clauses.append(f"substr({date_expression}, 1, 7) = ?")
                params.append(capture_month)
            camera = (metadata_filters.get("camera") or "").strip()
            if camera:
                clauses.append(
                    "(COALESCE(NULLIF(camera_model, ''), camera_make, '') = ? COLLATE NOCASE)"
                )
                params.append(camera)
            lens = (metadata_filters.get("lens") or "").strip()
            if lens:
                clauses.append("COALESCE(lens, '') = ? COLLATE NOCASE")
                params.append(lens)
            location = (metadata_filters.get("location") or "").strip()
            if location:
                # location 元数据是经纬度，筛选按最近城市匹配
                city_ids = [
                    int(row["id"])
                    for row in self.conn.execute("SELECT id, location FROM videos")
                    if location_label(row["location"]) == location
                ]
                self.conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS filter_location (id INTEGER PRIMARY KEY)"
                )
                self.conn.execute("DELETE FROM filter_location")
                self.conn.executemany(
                    "INSERT INTO filter_location (id) VALUES (?)",
                    [(video_id,) for video_id in city_ids],
                )
                clauses.append("videos.id IN (SELECT id FROM filter_location)")
            resolution = (metadata_filters.get("resolution") or "").strip()
            resolution_rules = {
                "4K": "MIN(width, height) >= 2160",
                "1440p": "MIN(width, height) >= 1440 AND MIN(width, height) < 2160",
                "1080p": "MIN(width, height) >= 1080 AND MIN(width, height) < 1440",
                "720p": "MIN(width, height) >= 720 AND MIN(width, height) < 1080",
                "较低分辨率": "MIN(width, height) > 0 AND MIN(width, height) < 720",
            }
            if resolution in resolution_rules:
                clauses.append(resolution_rules[resolution])
            size = (metadata_filters.get("size") or "").strip()
            size_rules = {
                "小于 100 MB": "size_bytes < 104857600",
                "100 MB - 1 GB": "size_bytes >= 104857600 AND size_bytes < 1073741824",
                "1 - 5 GB": "size_bytes >= 1073741824 AND size_bytes < 5368709120",
                "5 GB 以上": "size_bytes >= 5368709120",
            }
            if size in size_rules:
                clauses.append(size_rules[size])
            duration = (metadata_filters.get("duration") or "").strip()
            duration_rules = {
                "30 秒以内": "duration_ms > 0 AND duration_ms < 30000",
                "30 秒 - 5 分钟": "duration_ms >= 30000 AND duration_ms < 300000",
                "5 - 30 分钟": "duration_ms >= 300000 AND duration_ms < 1800000",
                "30 分钟以上": "duration_ms >= 1800000",
            }
            if duration in duration_rules:
                clauses.append(duration_rules[duration])
            rating = (metadata_filters.get("rating") or "").strip()
            if rating == "未评级":
                clauses.append("COALESCE(rating, 0) < 0.5")
            elif rating.endswith(" 星"):
                try:
                    wanted = int(rating.removesuffix(" 星"))
                except ValueError:
                    wanted = 0
                if wanted:
                    clauses.append("CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) = ?")
                    params.append(wanted)
            tag_value = (metadata_filters.get("tag") or "").strip()
            if tag_value:
                clauses.append(
                    """
                    (
                        EXISTS (
                            SELECT 1 FROM video_tags vt
                            JOIN tags t ON t.id = vt.tag_id
                            WHERE vt.video_id = videos.id AND t.name = ? COLLATE NOCASE
                              AND NOT EXISTS (
                                  SELECT 1 FROM hidden_tags ht
                                  WHERE ht.name = t.name COLLATE NOCASE
                              )
                        )
                        OR EXISTS (
                            SELECT 1 FROM json_each(videos.nfo_tags_json) AS nfo_tag
                            WHERE (
                                nfo_tag.value = ? COLLATE NOCASE
                                OR EXISTS (
                                    SELECT 1 FROM tag_aliases ta
                                    WHERE ta.source_name = nfo_tag.value COLLATE NOCASE
                                      AND ta.target_name = ? COLLATE NOCASE
                                )
                            )
                              AND NOT EXISTS (
                                  SELECT 1 FROM hidden_tags ht
                                  WHERE ht.name = nfo_tag.value COLLATE NOCASE
                                     OR ht.name = (
                                         SELECT ta.target_name
                                         FROM tag_aliases ta
                                         WHERE ta.source_name = nfo_tag.value COLLATE NOCASE
                                     )
                              )
                        )
                    )
                    """
                )
                params.extend([tag_value, tag_value, tag_value])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_specs = {
            "title": ("title COLLATE NOCASE", "ASC"),
            "name": ("file_name COLLATE NOCASE", "ASC"),
            "capture_time": (
                "COALESCE(NULLIF(capture_at, ''), NULLIF(premiered, ''), "
                "datetime(mtime_ns / 1000000000.0, 'unixepoch', 'localtime'))",
                "DESC",
            ),
            "duration": ("duration_ms", "DESC"),
            "recent": ("COALESCE(last_played_at, '')", "DESC"),
            "size": ("size_bytes", "DESC"),
            "bitrate": ("bitrate_bps", "DESC"),
            "file_time": ("mtime_ns", "DESC"),
            "premiered": ("COALESCE(premiered, '')", "DESC"),
            "added": ("added_at", "DESC"),
        }
        expression, natural_direction = order_specs.get(
            sort,
            order_specs["added"],
        )
        direction = (
            natural_direction
            if descending is None
            else ("DESC" if descending else "ASC")
        )
        order = f"{expression} {direction}, id {direction}"
        rows = self.conn.execute(
            f"SELECT * FROM videos {where} ORDER BY {order}", params
        ).fetchall()
        videos = [self._video_dict(row) for row in rows]
        return self._collapse_video_groups(videos, expanded_groups or set())

    def metadata_filter_options(
        self,
        library_ids: list[int] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        clauses: list[str] = []
        params: list[Any] = []
        if library_ids is not None:
            if not library_ids:
                return {}
            placeholders = ",".join("?" for _ in library_ids)
            clauses.append(f"library_id IN ({placeholders})")
            params.extend(library_ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            "SELECT width, height, size_bytes, duration_ms, capture_at, premiered, "
            "mtime_ns, camera_make, camera_model, lens, location, file_name, source_kind, "
            "rating "
            "FROM videos " + where,
            params,
        ).fetchall()
        counts: dict[str, dict[str, int]] = {
            key: {}
            for key in ("capture_year", "capture_month", "resolution", "size", "duration", "camera", "lens", "location", "rating", "tag")
        }

        def add(key: str, value: str) -> None:
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1

        for row in rows:
            name_capture = _capture_at_from_name(str(row["file_name"] or ""))
            date = str(row["capture_at"] or "")
            if not date:
                if str(row["source_kind"] or "") == "shooting" and name_capture:
                    date = name_capture
                elif row["premiered"]:
                    date = str(row["premiered"])
                else:
                    date = name_capture
            if not date and int(row["mtime_ns"] or 0):
                date = datetime.fromtimestamp(
                    int(row["mtime_ns"]) / 1_000_000_000
                ).isoformat(timespec="seconds")
            if date:
                add("capture_year", date[:4])
                add("capture_month", date[:7])
            width, height = int(row["width"] or 0), int(row["height"] or 0)
            short_side = min(width, height)
            if short_side >= 2160:
                resolution = "4K"
            elif short_side >= 1440:
                resolution = "1440p"
            elif short_side >= 1080:
                resolution = "1080p"
            elif short_side >= 720:
                resolution = "720p"
            elif short_side:
                resolution = "较低分辨率"
            else:
                resolution = ""
            add("resolution", resolution)
            size = int(row["size_bytes"] or 0)
            add(
                "size",
                "5 GB 以上" if size >= 5368709120 else
                "1 - 5 GB" if size >= 1073741824 else
                "100 MB - 1 GB" if size >= 104857600 else
                "小于 100 MB",
            )
            duration = int(row["duration_ms"] or 0)
            if duration:
                add(
                    "duration",
                    "30 分钟以上" if duration >= 1800000 else
                    "5 - 30 分钟" if duration >= 300000 else
                    "30 秒 - 5 分钟" if duration >= 30000 else
                    "30 秒以内",
                )
            add("camera", str(row["camera_model"] or row["camera_make"] or ""))
            add("lens", str(row["lens"] or ""))
            add("location", location_label(row["location"]))
            rating = int(round(float(row["rating"] or 0)))
            add("rating", f"{min(5, rating)} 星" if rating > 0 else "未评级")
        tag_where = where.replace("library_id", "v.library_id")
        tag_hidden = (
            "NOT EXISTS (SELECT 1 FROM hidden_tags ht "
            "WHERE ht.name = t.name COLLATE NOCASE)"
        )
        tag_where = (
            f"{tag_where} AND {tag_hidden}"
            if tag_where
            else f"WHERE {tag_hidden}"
        )
        for row in self.conn.execute(
            f"""
            SELECT t.name AS name, COUNT(DISTINCT vt.video_id) AS total
            FROM tags t
            JOIN video_tags vt ON vt.tag_id = t.id
            JOIN videos v ON v.id = vt.video_id
            {tag_where}
            GROUP BY t.name COLLATE NOCASE
            """,
            params,
        ):
            counts["tag"][row["name"]] = int(row["total"])
        return {
            key: [
                {"value": value, "count": count}
                for value, count in sorted(
                    values.items(),
                    key=lambda item: (-item[1], item[0].casefold()),
                )
            ]
            for key, values in counts.items()
        }

    def create_video_group(self, name: str, video_ids: list[int]) -> int:
        ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if len(ids) < 2:
            raise ValueError("视频组至少需要两个视频")
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, library_id FROM videos WHERE id IN ({placeholders})", ids
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("部分视频不存在")
        library_ids = {int(row["library_id"]) for row in rows}
        if len(library_ids) != 1:
            raise ValueError("只能把同一资源库的视频打组")
        cursor = self.conn.execute(
            "INSERT INTO video_groups(library_id, name, created_at) VALUES (?, ?, ?)",
            (next(iter(library_ids)), name.strip() or "视频组", utc_now()),
        )
        group_id = int(cursor.lastrowid)
        self.conn.execute(
            f"DELETE FROM video_group_members WHERE video_id IN ({placeholders})", ids
        )
        self.conn.executemany(
            "INSERT INTO video_group_members(group_id, video_id, position) VALUES (?, ?, ?)",
            [(group_id, video_id, index) for index, video_id in enumerate(ids)],
        )
        self.conn.execute(
            "DELETE FROM video_groups WHERE id NOT IN ("
            "SELECT group_id FROM video_group_members "
            "GROUP BY group_id HAVING COUNT(*) >= 2)"
        )
        self.conn.commit()
        return group_id

    def remove_video_groups(self, video_ids: list[int]) -> int:
        ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        group_ids = [
            int(row["group_id"])
            for row in self.conn.execute(
                f"SELECT DISTINCT group_id FROM video_group_members WHERE video_id IN ({placeholders})",
                ids,
            )
        ]
        self.conn.execute(
            f"DELETE FROM video_group_members WHERE video_id IN ({placeholders})", ids
        )
        if group_ids:
            group_placeholders = ",".join("?" for _ in group_ids)
            self.conn.execute(
                f"DELETE FROM video_groups WHERE id IN ({group_placeholders}) "
                "AND COALESCE((SELECT COUNT(*) FROM video_group_members m "
                "WHERE m.group_id = video_groups.id), 0) < 2",
                group_ids,
            )
        self.conn.commit()
        return len(group_ids)

    def video_groups(
        self,
        library_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if library_id is not None:
            where = "WHERE g.library_id = ?"
            params.append(int(library_id))
        rows = self.conn.execute(
            f"""
            SELECT g.id, g.name, g.library_id, COUNT(m.video_id) AS member_count
            FROM video_groups g
            LEFT JOIN video_group_members m ON m.group_id = g.id
            {where}
            GROUP BY g.id
            HAVING COUNT(m.video_id) >= 2
            ORDER BY g.name COLLATE NOCASE, g.id
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def videos_for_group(
        self,
        group_id: int,
        video_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [int(group_id)]
        video_filter = ""
        ids = list(dict.fromkeys(int(value) for value in video_ids or []))
        if video_ids is not None:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            video_filter = f"AND m.video_id IN ({placeholders})"
            params.extend(ids)
        rows = self.conn.execute(
            f"""
            SELECT v.*
            FROM video_group_members m
            JOIN videos v ON v.id = m.video_id
            WHERE m.group_id = ? {video_filter}
            ORDER BY m.position, v.id
            """,
            params,
        ).fetchall()
        return [self._video_dict(row) for row in rows]

    def add_videos_to_group(self, group_id: int, video_ids: list[int]) -> int:
        ids = list(dict.fromkeys(int(value) for value in video_ids))
        if not ids:
            return 0
        group = self.conn.execute(
            "SELECT id, library_id FROM video_groups WHERE id = ?",
            (int(group_id),),
        ).fetchone()
        if not group:
            raise ValueError("视频组不存在")
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, library_id FROM videos WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("部分视频不存在")
        if any(int(row["library_id"]) != int(group["library_id"]) for row in rows):
            raise ValueError("只能把同一资源库的视频加入这个视频组")
        old_group_ids = [
            int(row["group_id"])
            for row in self.conn.execute(
                f"""
                SELECT DISTINCT group_id FROM video_group_members
                WHERE video_id IN ({placeholders})
                """,
                ids,
            )
        ]
        self.conn.execute(
            f"DELETE FROM video_group_members WHERE video_id IN ({placeholders})",
            ids,
        )
        if old_group_ids:
            group_placeholders = ",".join("?" for _ in old_group_ids)
            self.conn.execute(
                f"""
                DELETE FROM video_groups
                WHERE id IN ({group_placeholders})
                  AND id <> ?
                  AND COALESCE((
                      SELECT COUNT(*) FROM video_group_members m
                      WHERE m.group_id = video_groups.id
                  ), 0) < 2
                """,
                [*old_group_ids, int(group_id)],
            )
        position_row = self.conn.execute(
            """
            SELECT COALESCE(MAX(position), -1) + 1 AS next_position
            FROM video_group_members WHERE group_id = ?
            """,
            (int(group_id),),
        ).fetchone()
        start = int(position_row["next_position"] or 0)
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO video_group_members(group_id, video_id, position)
            VALUES (?, ?, ?)
            """,
            [
                (int(group_id), video_id, start + index)
                for index, video_id in enumerate(ids)
            ],
        )
        self.conn.commit()
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM video_group_members WHERE group_id = ?",
                (int(group_id),),
            ).fetchone()[0]
        )

    def _collapse_video_groups(
        self,
        videos: list[dict[str, Any]],
        expanded_groups: set[int],
    ) -> list[dict[str, Any]]:
        if not videos:
            return videos
        ids = [int(video["id"]) for video in videos]
        memberships: dict[int, int] = {}
        for start in range(0, len(ids), 800):
            chunk = ids[start : start + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT video_id, group_id FROM video_group_members WHERE video_id IN ({placeholders})",
                chunk,
            ).fetchall()
            memberships.update(
                {int(row["video_id"]): int(row["group_id"]) for row in rows}
            )
        if not memberships:
            return videos
        group_ids = sorted(set(memberships.values()))
        placeholders = ",".join("?" for _ in group_ids)
        group_rows = self.conn.execute(
            f"SELECT id, name FROM video_groups WHERE id IN ({placeholders})", group_ids
        ).fetchall()
        group_names = {int(row["id"]): str(row["name"]) for row in group_rows}
        grouped: dict[int, list[dict[str, Any]]] = {}
        for video in videos:
            group_id = memberships.get(int(video["id"]))
            if group_id is not None:
                grouped.setdefault(group_id, []).append(video)
        collapsed: list[dict[str, Any]] = []
        emitted: set[int] = set()
        for video in videos:
            group_id = memberships.get(int(video["id"]))
            if group_id is None:
                collapsed.append(video)
                continue
            members = grouped[group_id]
            if len(members) < 2:
                item = dict(video)
                item["group_id"] = group_id
                item["group_name"] = group_names.get(group_id, "视频组")
                item["group_count"] = len(members)
                item["group_collapsed"] = False
                collapsed.append(item)
                continue
            if group_id in emitted:
                continue
            emitted.add(group_id)
            if group_id in expanded_groups:
                for index, member in enumerate(members):
                    item = dict(member)
                    item["group_id"] = group_id
                    item["group_name"] = group_names.get(group_id, "视频组")
                    item["group_count"] = len(members)
                    item["group_collapsed"] = False
                    if index == 0:
                        item["group_leader"] = True
                    collapsed.append(item)
            else:
                item = dict(video)
                item["group_id"] = group_id
                item["group_name"] = group_names.get(group_id, "视频组")
                item["group_count"] = len(members)
                item["group_member_ids"] = [int(member["id"]) for member in members]
                item["group_size_bytes"] = sum(int(member.get("size_bytes") or 0) for member in members)
                item["group_duration_ms"] = sum(int(member.get("duration_ms") or 0) for member in members)
                item["group_collapsed"] = True
                item["title"] = item["group_name"]
                item["file_name"] = f"{len(members)} 个视频"
                item["size_bytes"] = item["group_size_bytes"]
                item["duration_ms"] = item["group_duration_ms"]
                collapsed.append(item)
        return collapsed

    def create_category(
        self,
        name: str,
        parent_id: int | None = None,
        color: str = "#E5B567",
        library_id: int | None = None,
    ) -> int:
        if library_id is None:
            libraries = self.libraries()
            if len(libraries) != 1:
                raise ValueError("请指定分类所属资源库")
            library_id = int(libraries[0]["id"])
        if self.library(int(library_id)) is None:
            raise ValueError("分类所属资源库不存在")
        if parent_id is not None:
            parent = self.category(int(parent_id))
            if parent is None or int(parent["library_id"] or 0) != int(library_id):
                raise ValueError("父分类必须属于同一资源库")
        cursor = self.conn.execute(
            """
            INSERT INTO categories(
                library_id, name, parent_id, color, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(library_id), name.strip(), parent_id, color, utc_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def category(self, category_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM categories WHERE id = ?",
            (int(category_id),),
        ).fetchone()
        return dict(row) if row else None

    def categories(
        self,
        library_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if library_ids is None:
            rows = self.conn.execute(
                """
                SELECT c.*, COUNT(v.id) AS video_count
                FROM categories c
                LEFT JOIN videos v ON v.category_id = c.id
                WHERE c.library_id IS NOT NULL
                GROUP BY c.id
                ORDER BY c.library_id, c.parent_id, c.name COLLATE NOCASE
                """
            ).fetchall()
        elif not library_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in library_ids)
            rows = self.conn.execute(
                f"""
                SELECT c.*, COUNT(v.id) AS video_count
                FROM categories c
                LEFT JOIN videos v ON v.category_id = c.id
                WHERE c.library_id IN ({placeholders})
                GROUP BY c.id
                ORDER BY c.library_id, c.parent_id, c.name COLLATE NOCASE
                """,
                library_ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def sidebar_counts(
        self,
        library_ids: list[int] | None = None,
    ) -> dict[str, int]:
        where = ""
        params: list[Any] = []
        if library_ids is not None:
            if not library_ids:
                return {
                    "all": 0,
                    "continue": 0,
                    "favorite": 0,
                    "uncategorized": 0,
                    **{f"rating:{stars}": 0 for stars in range(6)},
                }
            placeholders = ",".join("?" for _ in library_ids)
            where = f"WHERE library_id IN ({placeholders})"
            params.extend(library_ids)
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS all_count,
                COALESCE(SUM(CASE
                    WHEN last_position_ms > 5000 AND watched = 0 THEN 1 ELSE 0
                END), 0) AS continue_count,
                COALESCE(SUM(CASE
                    WHEN datetime(added_at) >= datetime('now', '-7 days')
                    THEN 1 ELSE 0
                END), 0) AS recent_count,
                COALESCE(SUM(CASE WHEN favorite = 1 THEN 1 ELSE 0 END), 0)
                    AS favorite_count,
                COALESCE(SUM(CASE WHEN category_id IS NULL THEN 1 ELSE 0 END), 0)
                    AS uncategorized_count,
                COALESCE(SUM(CASE
                    WHEN CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) = 1
                    THEN 1 ELSE 0 END), 0) AS rating_1,
                COALESCE(SUM(CASE
                    WHEN CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) = 2
                    THEN 1 ELSE 0 END), 0) AS rating_2,
                COALESCE(SUM(CASE
                    WHEN CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) = 3
                    THEN 1 ELSE 0 END), 0) AS rating_3,
                COALESCE(SUM(CASE
                    WHEN CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) = 4
                    THEN 1 ELSE 0 END), 0) AS rating_4,
                COALESCE(SUM(CASE
                    WHEN CAST(ROUND(COALESCE(rating, 0)) AS INTEGER) >= 5
                    THEN 1 ELSE 0 END), 0) AS rating_5,
                COALESCE(SUM(CASE
                    WHEN COALESCE(rating, 0) < 0.5 THEN 1 ELSE 0 END), 0)
                    AS rating_0
            FROM videos
            {where}
            """,
            params,
        ).fetchone()
        return {
            "all": int(row["all_count"]),
            "continue": int(row["continue_count"]),
            "recent": int(row["recent_count"]),
            "favorite": int(row["favorite_count"]),
            "uncategorized": int(row["uncategorized_count"]),
            **{
                f"rating:{stars}": int(row[f"rating_{stars}"])
                for stars in range(6)
            },
        }

    def delete_category(self, category_id: int) -> None:
        self.conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        self.conn.commit()

    def rename_category(self, category_id: int, name: str) -> None:
        self.conn.execute(
            "UPDATE categories SET name = ? WHERE id = ?",
            (name.strip(), category_id),
        )
        self.conn.commit()

    def nfo_filter_options(
        self,
        library_ids: list[int] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        where = ""
        params: list[Any] = []
        if library_ids is not None:
            if not library_ids:
                return {}
            placeholders = ",".join("?" for _ in library_ids)
            where = f"WHERE library_id IN ({placeholders})"
            params = list(library_ids)
        rows = self.conn.execute(
            f"""
            SELECT id, year, premiered, country, studio, genres, nfo_tags_json,
                   content_set, mpaa, actors_json, directors_json
            FROM videos {where}
            """,
            params,
        ).fetchall()
        values: dict[str, dict[str, set[int]]] = {
            key: {}
            for key in (
                "year",
                "premiered",
                "country",
                "studio",
                "genre",
                "nfo_tag",
                "content_set",
                "mpaa",
                "actor",
                "director",
            )
        }
        hidden_tags = self.hidden_tag_names()
        tag_aliases = self.tag_alias_map()
        for row in rows:
            video_id = int(row["id"])
            for key in ("year", "premiered", "country", "studio", "content_set", "mpaa"):
                value = str(row[key] or "").strip()
                if value:
                    values[key].setdefault(value, set()).add(video_id)
            for source_key, target_key in (
                ("genres", "genre"),
                ("nfo_tags_json", "nfo_tag"),
                ("directors_json", "director"),
            ):
                try:
                    items = json.loads(row[source_key] or "[]")
                except (TypeError, json.JSONDecodeError):
                    items = []
                for item in items:
                    name = str(item).strip()
                    if target_key == "nfo_tag":
                        name = tag_aliases.get(name.casefold(), name)
                    if name and not (
                        target_key == "nfo_tag"
                        and name.casefold() in hidden_tags
                    ):
                        values[target_key].setdefault(name, set()).add(video_id)
            try:
                actors = json.loads(row["actors_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                actors = []
            for actor in actors:
                name = str(actor.get("name") or "").strip()
                if name:
                    values["actor"].setdefault(name, set()).add(video_id)
        result: dict[str, list[dict[str, Any]]] = {}
        for key, items in values.items():
            ordered = sorted(
                items.items(),
                key=lambda item: item[0].casefold(),
                reverse=key in {"year", "premiered"},
            )
            ordered.sort(key=lambda item: len(item[1]), reverse=True)
            result[key] = [
                {"value": value, "count": len(video_ids)}
                for value, video_ids in ordered
            ]
        return result

    def add_tag(self, video_id: int, tag_name: str) -> None:
        tag = tag_name.strip()
        if not tag:
            return
        self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
        tag_id = self.conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)
        ).fetchone()["id"]
        self.conn.execute(
            "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
            (video_id, tag_id),
        )
        self.conn.commit()

    def remove_tag(self, video_id: int, tag_name: str) -> None:
        self.conn.execute(
            """
            DELETE FROM video_tags
            WHERE video_id = ? AND tag_id = (
                SELECT id FROM tags WHERE name = ? COLLATE NOCASE
            )
            """,
            (video_id, tag_name),
        )
        self.conn.commit()

    def set_video_tags(self, video_id: int, tags: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
        for tag in tags:
            name = tag.strip()
            if not name:
                continue
            self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            row = self.conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            self.conn.execute(
                "INSERT OR IGNORE INTO video_tags(video_id, tag_id) VALUES (?, ?)",
                (video_id, row["id"]),
            )
        self.conn.commit()

    def tags_for_video(self, video_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN video_tags vt ON vt.tag_id = t.id
            WHERE vt.video_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM hidden_tags ht
                  WHERE ht.name = t.name COLLATE NOCASE
              )
            ORDER BY t.name COLLATE NOCASE
            """,
            (video_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def hidden_tag_names(self) -> set[str]:
        rows = self.conn.execute("SELECT name FROM hidden_tags").fetchall()
        return {str(row["name"]).casefold() for row in rows}

    def tag_alias_map(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT source_name, target_name FROM tag_aliases"
        ).fetchall()
        return {
            str(row["source_name"]).casefold(): str(row["target_name"])
            for row in rows
        }

    def rename_tag(self, old_name: str, new_name: str) -> int:
        old = old_name.strip()
        new = new_name.strip()
        if not old or not new or old.casefold() == new.casefold():
            return 0
        now = utc_now()
        old_row = self.conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (old,)
        ).fetchone()
        if old_row:
            old_id = int(old_row["id"])
            new_row = self.conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (new,)
            ).fetchone()
            if new_row and int(new_row["id"]) != old_id:
                new_id = int(new_row["id"])
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO video_tags(video_id, tag_id)
                    SELECT video_id, ? FROM video_tags WHERE tag_id = ?
                    """,
                    (new_id, old_id),
                )
                self.conn.execute("DELETE FROM tags WHERE id = ?", (old_id,))
            else:
                self.conn.execute(
                    "UPDATE tags SET name = ? WHERE id = ?",
                    (new, old_id),
                )
        self.conn.execute(
            """
            UPDATE tag_aliases
            SET target_name = ?, updated_at = ?
            WHERE target_name = ? COLLATE NOCASE
            """,
            (new, now, old),
        )
        self.conn.execute(
            """
            INSERT INTO tag_aliases(source_name, target_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                target_name = excluded.target_name,
                updated_at = excluded.updated_at
            """,
            (old, new, now),
        )
        self.conn.commit()
        return 1

    def hide_tag(self, tag_name: str) -> int:
        name = tag_name.strip()
        if not name:
            return 0
        cursor = self.conn.execute(
            """
            DELETE FROM video_tags
            WHERE tag_id = (
                SELECT id FROM tags WHERE name = ? COLLATE NOCASE
            )
            """,
            (name,),
        )
        self.conn.execute(
            """
            INSERT INTO hidden_tags(name, hidden_at) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET hidden_at = excluded.hidden_at
            """,
            (name, utc_now()),
        )
        self.conn.commit()
        return max(0, int(cursor.rowcount))

    def all_tags(self, library_ids: list[int] | None = None) -> list[dict[str, Any]]:
        video_filter = ""
        params: list[Any] = []
        if library_ids is not None:
            if not library_ids:
                return []
            placeholders = ",".join("?" for _ in library_ids)
            video_filter = f"WHERE v.library_id IN ({placeholders})"
            params.extend(library_ids)
        user_rows = self.conn.execute(
            f"""
            SELECT t.name, v.id AS video_id
            FROM tags t
            JOIN video_tags vt ON vt.tag_id = t.id
            JOIN videos v ON v.id = vt.video_id
            {video_filter}
            """,
            params,
        ).fetchall()
        nfo_rows = self.conn.execute(
            f"SELECT v.id AS video_id, v.nfo_tags_json FROM videos v {video_filter}",
            params,
        ).fetchall()
        hidden = self.hidden_tag_names()
        aliases = self.tag_alias_map()
        combined: dict[str, dict[str, Any]] = {}
        for row in user_rows:
            key = row["name"].casefold()
            display_name = aliases.get(key, row["name"])
            if key in hidden or display_name.casefold() in hidden:
                continue
            display_key = display_name.casefold()
            item = combined.setdefault(
                display_key,
                {
                    "name": display_name,
                    "manual_ids": set(),
                    "nfo_ids": set(),
                },
            )
            item["manual_ids"].add(row["video_id"])
        for row in nfo_rows:
            try:
                nfo_tags = json.loads(row["nfo_tags_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for name in nfo_tags:
                key = str(name).casefold()
                if not key:
                    continue
                display_name = aliases.get(key, str(name))
                if display_name.casefold() in hidden:
                    continue
                display_key = display_name.casefold()
                item = combined.setdefault(
                    display_key,
                    {
                        "name": display_name,
                        "manual_ids": set(),
                        "nfo_ids": set(),
                    },
                )
                item["nfo_ids"].add(row["video_id"])
        return sorted(
            (
                {
                    "id": f"tag:{key}",
                    "name": item["name"],
                    "video_count": len(item["manual_ids"] | item["nfo_ids"]),
                    "source": (
                        "both"
                        if item["manual_ids"] and item["nfo_ids"]
                        else "nfo"
                        if item["nfo_ids"]
                        else "manual"
                    ),
                }
                for key, item in combined.items()
                if item["manual_ids"] or item["nfo_ids"]
            ),
            key=lambda item: str(item["name"]).casefold(),
        )

    def record_operation(
        self,
        kind: str,
        source_path: str | None,
        target_path: str | None,
        status: str,
        detail: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO operations(kind, source_path, target_path, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kind, source_path, target_path, status, detail, utc_now()),
        )
        self.conn.commit()

    def operations(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
