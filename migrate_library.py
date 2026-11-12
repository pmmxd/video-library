from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class MigrationError(RuntimeError):
    pass


def _identity_key(path: Path) -> str:
    return f"{str(path.parent.resolve()).casefold()}|{path.stem.casefold()}"


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _remap_path(value: str | None, old_root: Path, new_root: Path) -> str | None:
    if not value:
        return value
    path = Path(value)
    if not path.is_absolute() or not _within(path, old_root):
        return value
    relative = path.resolve().relative_to(old_root.resolve())
    return str(new_root / relative)


def _load_library(
    conn: sqlite3.Connection,
    library_id: int | None,
    old_root: Path | None,
) -> sqlite3.Row:
    if library_id is not None:
        row = conn.execute(
            "SELECT * FROM libraries WHERE id = ?",
            (library_id,),
        ).fetchone()
        if not row:
            raise MigrationError(f"资源库 ID 不存在：{library_id}")
        if old_root and (
            str(Path(row["root_path"]).resolve()).casefold()
            != str(old_root.resolve()).casefold()
        ):
            raise MigrationError("指定的旧目录与数据库中资源库根目录不一致")
        return row
    if old_root is None:
        raise MigrationError("必须指定资源库 ID 或旧目录")
    rows = conn.execute("SELECT * FROM libraries").fetchall()
    matches = [
        row
        for row in rows
        if str(Path(row["root_path"]).resolve()).casefold()
        == str(old_root.resolve()).casefold()
    ]
    if len(matches) != 1:
        raise MigrationError("旧目录没有唯一对应的资源库，请改用 --library-id")
    return matches[0]


def _build_plan(
    conn: sqlite3.Connection,
    library: sqlite3.Row,
    new_root: Path,
    verify_targets: bool = True,
) -> list[dict[str, Any]]:
    old_root = Path(library["root_path"]).resolve()
    rows = conn.execute(
        "SELECT * FROM videos WHERE library_id = ? ORDER BY id",
        (int(library["id"]),),
    ).fetchall()
    if not rows:
        raise MigrationError("这个资源库没有已登记视频")

    target_paths: dict[str, int] = {}
    target_keys: dict[str, int] = {}
    updates: list[dict[str, Any]] = []
    for row in rows:
        old_path = Path(row["path"])
        if not _within(old_path, old_root):
            raise MigrationError(
                f"视频记录不在资源库根目录内：{row['file_name']}"
            )
        relative = old_path.resolve().relative_to(old_root)
        target = new_root / relative
        if relative.is_absolute() or not _within(target, new_root):
            raise MigrationError(f"视频相对路径无效：{relative}")
        target_key = str(target.resolve()).casefold()
        if target_key in target_paths:
            raise MigrationError(f"迁移后路径重复：{target}")
        target_paths[target_key] = int(row["id"])
        identity = _identity_key(target)
        if identity in target_keys:
            raise MigrationError(f"迁移后文件标识重复：{target.name}")
        target_keys[identity] = int(row["id"])

        exists = target.is_file()
        if verify_targets and not exists and not int(row["missing"]):
            raise MigrationError(f"新目录缺少视频文件：{relative}")
        target_stat = target.stat() if verify_targets and exists else None
        old_size = int(row["size_bytes"] or 0)
        old_mtime = int(row["mtime_ns"] or 0)
        new_size = int(target_stat.st_size) if target_stat else old_size
        new_mtime = int(target_stat.st_mtime_ns) if target_stat else old_mtime
        if target_stat and new_size <= 0 and old_size > 0:
            raise MigrationError(f"视频文件为空或复制未完成：{relative}")
        metadata_changed = bool(
            target_stat
            and (new_size != old_size or new_mtime != old_mtime)
        )

        try:
            sidecars = json.loads(row["sidecars_json"] or "[]")
        except json.JSONDecodeError:
            sidecars = []
        updates.append(
            {
                "id": int(row["id"]),
                "path": str(target),
                "identity_key": identity,
                "relative_path": str(relative),
                "sidecars_json": json.dumps(
                    [
                        _remap_path(str(item), old_root, new_root)
                        for item in sidecars
                    ],
                    ensure_ascii=False,
                ),
                "nfo_path": _remap_path(row["nfo_path"], old_root, new_root),
                "subtitle_path": _remap_path(
                    row["subtitle_path"],
                    old_root,
                    new_root,
                ),
                "poster_path": _remap_path(
                    row["poster_path"],
                    old_root,
                    new_root,
                ),
                "thumb_path": _remap_path(
                    row["thumb_path"],
                    old_root,
                    new_root,
                ),
                "content_hash": (
                    None if metadata_changed else row["content_hash"]
                ),
                "size_bytes": new_size,
                "mtime_ns": new_mtime,
                "missing": 0 if exists else int(row["missing"]),
                "_metadata_changed": metadata_changed,
            }
        )

    other_paths = {
        str(row["path"]).casefold()
        for row in conn.execute(
            "SELECT path FROM videos WHERE library_id <> ?",
            (int(library["id"]),),
        ).fetchall()
    }
    conflicts = other_paths.intersection(target_paths)
    if conflicts:
        raise MigrationError(f"目标路径已被其他资源库使用：{next(iter(conflicts))}")
    return updates


def _backup_database(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = db_path.with_name(f"{db_path.name}.migration-{stamp}.bak")
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup


def _apply_plan(
    db_path: Path,
    library_id: int,
    new_root: Path,
    updates: list[dict[str, Any]],
) -> Path:
    backup = _backup_database(db_path)
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE libraries SET root_path = ? WHERE id = ?",
            (str(new_root), library_id),
        )
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.executemany(
            """
            UPDATE videos
            SET path = ?, identity_key = ?, relative_path = ?,
                sidecars_json = ?, nfo_path = ?, subtitle_path = ?,
                poster_path = ?, thumb_path = ?, content_hash = ?,
                size_bytes = ?, mtime_ns = ?, missing = ?, updated_at = ?
            WHERE id = ? AND library_id = ?
            """,
            [
                (
                    item["path"],
                    item["identity_key"],
                    item["relative_path"],
                    item["sidecars_json"],
                    item["nfo_path"],
                    item["subtitle_path"],
                    item["poster_path"],
                    item["thumb_path"],
                    item["content_hash"],
                    item["size_bytes"],
                    item["mtime_ns"],
                    item["missing"],
                    now,
                    item["id"],
                    library_id,
                )
                for item in updates
            ],
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise MigrationError(f"数据库路径更新失败：{exc}") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return backup


def migrate_library(
    db_path: Path,
    old_root: Path,
    new_root: Path,
    library_id: int | None = None,
    copy_files: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    old_root = Path(old_root)
    new_root = Path(new_root)
    if not db_path.is_file():
        raise MigrationError(f"数据库不存在：{db_path}")
    if copy_files and not old_root.is_dir():
        raise MigrationError(f"旧资源库目录不存在：{old_root}")
    old_root = old_root.resolve()
    new_root = new_root.expanduser().resolve()
    if old_root == new_root or _within(new_root, old_root) or _within(old_root, new_root):
        raise MigrationError("新旧资源库目录不能相同或互相包含")

    if copy_files:
        if new_root.exists():
            raise MigrationError("--copy 要求新目录尚不存在，避免覆盖已有文件")
        shutil.copytree(old_root, new_root, copy_function=shutil.copy2)
    verify_targets = new_root.is_dir()
    if not verify_targets and (copy_files or apply):
        raise MigrationError(f"新资源库目录不存在：{new_root}")

    mode = "ro" if not apply else "rw"
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode={mode}",
        uri=True,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    try:
        library = _load_library(conn, library_id, old_root)
        for other in conn.execute(
            "SELECT id, root_path FROM libraries WHERE id <> ?",
            (int(library["id"]),),
        ).fetchall():
            other_root = Path(other["root_path"]).resolve()
            if (
                new_root == other_root
                or _within(new_root, other_root)
                or _within(other_root, new_root)
            ):
                raise MigrationError(
                    f"新目录与资源库 {other['id']} 的目录重叠"
                )
        updates = _build_plan(
            conn,
            library,
            new_root,
            verify_targets=verify_targets,
        )
    finally:
        conn.close()

    backup = None
    if apply:
        backup = _apply_plan(
            db_path,
            int(library["id"]),
            new_root,
            updates,
        )
    return {
        "library_id": int(library["id"]),
        "library_name": str(library["name"]),
        "video_count": len(updates),
        "metadata_refresh_count": sum(
            1 for item in updates if item["_metadata_changed"]
        ),
        "old_root": str(old_root),
        "new_root": str(new_root),
        "copied": copy_files,
        "applied": apply,
        "backup": str(backup) if backup else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="迁移 VideoLibrary 资源库目录并保留现有数据库记录"
    )
    parser.add_argument("--db", required=True, type=Path, help="library.db 路径")
    parser.add_argument("--library-id", type=int, help="资源库 ID")
    parser.add_argument("--old-root", type=Path, help="旧资源库根目录")
    parser.add_argument("--new-root", required=True, type=Path, help="新资源库根目录")
    parser.add_argument("--copy", action="store_true", help="复制整个目录到新位置")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际更新数据库；不加时只做校验和预览",
    )
    return parser


def _ensure_app_closed() -> None:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq VideoLibrary.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if "VideoLibrary.exe" in result.stdout:
        raise MigrationError("请先完全关闭 VideoLibrary，再更新数据库")


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _parser().parse_args(argv)
    if args.library_id is None and args.old_root is None:
        print("必须指定 --library-id 或 --old-root", file=sys.stderr)
        return 2
    old_root = args.old_root
    if old_root is None:
        conn = sqlite3.connect(
            f"file:{Path(args.db).resolve().as_posix()}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT root_path FROM libraries WHERE id = ?",
                (args.library_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            print(f"资源库 ID 不存在：{args.library_id}", file=sys.stderr)
            return 2
        old_root = Path(row["root_path"])
    try:
        if args.apply:
            _ensure_app_closed()
        result = migrate_library(
            args.db,
            old_root,
            args.new_root,
            library_id=args.library_id,
            copy_files=args.copy,
            apply=args.apply,
        )
    except MigrationError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    print(f"资源库：{result['library_name']} (ID {result['library_id']})")
    print(f"视频记录：{result['video_count']}")
    if result["metadata_refresh_count"]:
        print(
            f"刷新过期文件元数据：{result['metadata_refresh_count']} 个"
        )
    print(f"旧目录：{result['old_root']}")
    print(f"新目录：{result['new_root']}")
    if result["copied"]:
        print("文件复制：完成")
    if result["applied"]:
        print(f"数据库更新：完成（不新增资源库 ID）")
        print(f"数据库备份：{result['backup']}")
    else:
        print("数据库更新：未执行（预览模式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
