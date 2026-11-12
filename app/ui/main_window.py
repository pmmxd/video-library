from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QFileSystemWatcher, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..applog import log
from ..artwork import ArtworkRuntime, remove_cached_thumbnails
from ..constants import (
    ALL_LIBRARIES,
    APP_TITLE,
    APP_VERSION,
    DATA_DIR,
    DB_PATH,
    LOSSLESSCUT,
    SUBTITLE_EXTENSIONS,
    THUMB_DIR_NAME,
    restore_thumbnail_file_names,
)
from ..database import Database, utc_now
from ..media_tools import (
    existing_video_path,
    format_duration,
    write_video_rating,
    write_video_tags,
)
from ..operations import (
    ExportWorker,
    MaskWorker,
    TrimWorker,
    delete_video,
    is_within,
    move_video,
    recover_temporary_exposure,
    rename_video,
)
from ..scanner import SKIPPED_DIRECTORY_NAMES, ScanWorker
from ..security import (
    WindowsSessionLockFilter,
    register_session_lock_notification,
    unregister_session_lock_notification,
)
from ..subtitles import parse_subtitle
from ..transcription import (
    SubtitleWorker,
    subtitle_target,
    validate_whisper_installation,
)
from ..theme import COLORS
from .detail_panel import DetailPanel
from .dialogs import (
    AddLibraryDialog,
    CategoryDialog,
    DeleteDialog,
    PasswordPromptDialog,
    RelocateLibraryDialog,
    RenameDialog,
    SecurityDialog,
    SettingsDialog,
    SubtitleOptionsDialog,
    TagsDialog,
    TrimDialog,
)
from .library_view import LibraryPage
from .player_view import PlayerView, PlayerWindow
from .sidebar import Sidebar
from .top_bar import TopBar
from .task_center import TaskCenterDialog
from migrate_library import MigrationError, migrate_library


@dataclass
class TaskQueueItem:
    task_id: str
    kind: str
    payload: dict[str, Any]


class MainWindow(QMainWindow):
    _ACTIVITY_EVENTS = {
        QEvent.KeyPress,
        QEvent.MouseButtonPress,
        QEvent.MouseButtonDblClick,
        QEvent.MouseMove,
        QEvent.Wheel,
        QEvent.TouchBegin,
        QEvent.TouchUpdate,
        QEvent.TouchEnd,
    }

    def __init__(self, db_path: Path = DB_PATH):
        super().__init__()
        self.db = Database(db_path)
        self._thumbnail_dir = Path(db_path).parent / THUMB_DIR_NAME
        self.artwork = ArtworkRuntime(self.db)
        self.artwork.cleanup_legacy_cache()
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._restore_runtime_assets)
        recover_temporary_exposure(self.db)
        self.current_library_id: int | None = None
        self.current_mode = "all"
        self.current_category_id: int | None = None
        self.current_tag: str | None = None
        self.current_folder: str | None = None
        self.current_nfo_filters: dict[str, str] = {}
        self.current_metadata_filters: dict[str, str] = {}
        self.expanded_groups: set[int] = set()
        self._metadata_filter_options: dict[str, list[dict[str, Any]]] = {}
        self._nfo_filter_options: dict[str, list[dict[str, Any]]] = {}
        self._metadata_filter_options_cache: dict[
            tuple[int, ...], dict[str, list[dict[str, Any]]]
        ] = {}
        self._nfo_filter_options_cache: dict[
            tuple[int, ...], dict[str, list[dict[str, Any]]]
        ] = {}
        self.selected_video_id: int | None = None
        self.scan_worker: ScanWorker | None = None
        self.subtitle_worker: SubtitleWorker | None = None
        self.subtitle_options_dialog: SubtitleOptionsDialog | None = None
        self.mask_worker: MaskWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.trim_worker: TrimWorker | None = None
        self._task_queue: list[TaskQueueItem] = []
        self._active_task: TaskQueueItem | None = None
        self._pending_lock_reason: str | None = None
        self.trim_progress: QProgressDialog | None = None
        self._pending_mask_library_id: int | None = None
        self._scan_task_id: str | None = None
        self._mask_task_id: str | None = None
        self._export_task_id: str | None = None
        self._subtitle_task_id: str | None = None
        self._pending_tag_writes: dict[int, list[str]] = {}
        self._pending_rating_writes: dict[int, float | None] = {}
        self._dirty_watch_directories: dict[int, set[str]] = {}
        self._pending_incremental_scans: dict[int, set[str]] = {}
        self._extra_watch_directories: set[str] = set()
        self._scan_automatic = False
        self._scan_incremental = False
        self._scan_refresh = False
        self._closing = False
        self._player_active = False
        self.detail_visible = True
        self.isolated_visibility = False
        self.unlocked_libraries: set[int] = set()
        try:
            self._auto_lock_minutes = int(
                self.db.setting("isolated_auto_lock_minutes") or 15
            )
        except ValueError:
            self._auto_lock_minutes = 15
        self._auto_lock_minutes = max(0, min(240, self._auto_lock_minutes))
        self._last_activity = time.monotonic()
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._auto_lock_if_idle)
        self._app_event_filter_installed = False
        self._session_lock_filter: WindowsSessionLockFilter | None = None
        self._session_notification_registered = False

        self.setWindowTitle(f"{APP_TITLE}  ·  Video Library")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 680)

        root = QWidget()
        root.setObjectName("Root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.top_bar = TopBar()
        outer.addWidget(self.top_bar)
        self.splitter = QSplitter(Qt.Horizontal)
        self.sidebar = Sidebar()
        self.content_stack = QStackedWidget()
        self.library_page = LibraryPage()
        self._player_view: PlayerView | None = None
        self._player_window: PlayerWindow | None = None
        self._subtitle_font_size = 22
        self._playback_end_behavior = (
            self.db.setting("playback_end_behavior") or "stop"
        )
        if self._playback_end_behavior not in {"stop", "loop", "next"}:
            self._playback_end_behavior = "stop"
        self._apply_saved_subtitle_size()
        self.detail = DetailPanel()
        self.task_center = TaskCenterDialog(self)
        self._subtitle_pending: tuple[int, str] | None = None
        self._subtitle_throttle = QTimer(self)
        self._subtitle_throttle.setSingleShot(True)
        self._subtitle_throttle.timeout.connect(self._flush_subtitle_progress)
        self.file_watcher = QFileSystemWatcher(self)
        self.file_watcher.directoryChanged.connect(self._file_change_detected)
        self._watch_debounce = QTimer(self)
        self._watch_debounce.setSingleShot(True)
        self._watch_debounce.setInterval(1200)
        self._watch_debounce.timeout.connect(self._auto_refresh_watched_libraries)
        self.content_stack.addWidget(self.library_page)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.content_stack)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([260, 846, 330])
        outer.addWidget(self.splitter, 1)
        self.setCentralWidget(root)

        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setProperty("faint", True)
        self.statusBar().addPermanentWidget(self.version_label)
        self.set_detail_visible(
            (self.db.setting("show_detail_panel") or "1") == "1",
            persist=False,
        )

        self._connect_signals()
        self._install_shortcuts()
        self._cleanup_legacy_missing_records()
        self.refresh_all()
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)
            self._app_event_filter_installed = True
        self._setup_session_lock_monitor()
        self._arm_auto_lock()

    def _connect_signals(self) -> None:
        self.sidebar.library_selected.connect(self.select_library)
        self.top_bar.add_library_requested.connect(self.add_library)
        self.top_bar.scan_requested.connect(self.scan_current)
        self.top_bar.relocate_library_requested.connect(
            self.relocate_current_library
        )
        self.top_bar.remove_library_requested.connect(self.remove_current_library)
        self.top_bar.settings_requested.connect(self.open_settings)
        self.top_bar.security_requested.connect(self.open_security_settings)
        self.top_bar.isolated_visibility_requested.connect(
            self.request_isolated_visibility
        )
        self.top_bar.logs_requested.connect(self.show_operation_log)
        self.top_bar.tasks_requested.connect(self.open_task_center)
        self.top_bar.open_data_requested.connect(self.open_data_directory)
        self.top_bar.detail_toggle_requested.connect(self.toggle_detail_panel)
        self.sidebar.view_selected.connect(self.select_view)
        self.sidebar.category_selected.connect(self.select_category)
        self.sidebar.add_category_requested.connect(self.add_category)
        self.sidebar.add_subcategory_requested.connect(self.add_subcategory)
        self.sidebar.rename_category_requested.connect(self.rename_category)
        self.sidebar.delete_category_requested.connect(self.delete_category)
        self.sidebar.tag_selected.connect(self.select_tag)
        self.sidebar.tag_rename_requested.connect(self.rename_tag)
        self.sidebar.tag_delete_requested.connect(self.delete_tag)
        self.sidebar.rating_selected.connect(self.select_rating)
        self.sidebar.actor_selected.connect(self.select_actor)
        self.sidebar.folder_selected.connect(self.select_folder)

        self.library_page.search_changed.connect(self.load_videos)
        self.library_page.sort_changed.connect(self.load_videos)
        self.library_page.orientation_changed.connect(self.load_videos)
        self.library_page.filters_changed.connect(self.set_nfo_filters)
        self.library_page.metadata_filters_changed.connect(
            self.set_metadata_filters
        )
        self.library_page.group_toggle_requested.connect(self.toggle_video_group)
        self.library_page.group_create_requested.connect(self.create_video_group)
        self.library_page.group_add_requested.connect(self.add_videos_to_group)
        self.library_page.group_remove_requested.connect(self.remove_video_group)
        self.library_page.cancel_scan_requested.connect(self.cancel_scan)
        self.library_page.incremental_refresh_requested.connect(
            self.refresh_incremental_current
        )
        self.library_page.video_activated.connect(self.play_video)
        self.library_page.selection_changed.connect(self.selection_changed)
        self.library_page.batch_category_requested.connect(self.batch_set_category)
        self.library_page.tag_requested.connect(self.edit_tags)
        self.library_page.batch_move_requested.connect(self.batch_move)
        self.library_page.batch_delete_requested.connect(self.batch_delete)
        self.library_page.batch_favorite_requested.connect(self.batch_favorite)
        self.library_page.rename_requested.connect(self.rename_selected)
        self.library_page.default_open_requested.connect(self.open_with_default)
        self.library_page.reveal_requested.connect(self.reveal_in_explorer)
        self.library_page.export_requested.connect(self.export_videos)
        self.library_page.losslesscut_requested.connect(self.open_with_losslesscut)
        self.library_page.trim_requested.connect(self.trim_video)
        self.library_page.subtitle_requested.connect(
            lambda video_id: self.choose_subtitle(video_id, autoplay=True)
        )
        self.library_page.subtitle_generation_requested.connect(
            self.start_subtitle_generation
        )
        self.library_page.clear_filter_requested.connect(self.clear_filters)
        self.detail.tag_filter_requested.connect(self.select_tag)
        self.detail.actor_filter_requested.connect(self.select_actor)
        self.detail.metadata_filter_requested.connect(
            self.library_page.open_metadata_filters
        )
        self.detail.nfo_filter_requested.connect(
            self.library_page.open_nfo_filters
        )
        self.detail.metadata_filters_changed.connect(self.set_metadata_filters)
        self.detail.nfo_filters_changed.connect(self.set_nfo_filters)
        self.detail.collapse_requested.connect(
            lambda: self.set_detail_visible(False)
        )

        self.task_center.cancel_requested.connect(self.cancel_task)

    @property
    def player_view(self) -> PlayerView:
        return self._ensure_player()

    @property
    def player_window(self) -> PlayerWindow:
        self._ensure_player()
        assert self._player_window is not None
        return self._player_window

    def _ensure_player(self) -> PlayerView:
        """播放器首次用到时才创建：QMediaPlayer 会加载 FFmpeg 后端，提前建会白占上百兆内存。"""
        if self._player_view is None:
            self._player_view = PlayerView()
            self._player_window = PlayerWindow(self._player_view, self)
            self._player_view.back_requested.connect(self.close_player)
            self._player_window.closed.connect(self._player_window_closed)
            self._player_view.position_changed.connect(self.save_position)
            self._player_view.subtitle_file_requested.connect(
                lambda video_id: self.choose_subtitle(video_id, autoplay=False)
            )
            self._player_view.rating_changed.connect(self.set_video_rating)
            self._player_view.tags_requested.connect(self._edit_tags_from_player)
            self._player_view.video_loaded.connect(self._refresh_player_tags)
            self._player_view.video_missing.connect(self._remove_missing_video)
            self._player_view.set_subtitle_font_size(self._subtitle_font_size)
            self._player_view.set_end_behavior(self._playback_end_behavior)
            self._player_view.set_mask_map(self.db.mask_map())
        return self._player_view

    def _install_shortcuts(self) -> None:
        search = QShortcut(QKeySequence("Ctrl+F"), self)
        search.activated.connect(self._focus_search)
        scan = QShortcut(QKeySequence("F5"), self)
        scan.activated.connect(self.refresh_incremental_current)
        delete = QShortcut(QKeySequence("Delete"), self.library_page)
        delete.setContext(Qt.WidgetWithChildrenShortcut)
        delete.activated.connect(self.delete_selected)
        play = QShortcut(QKeySequence("Return"), self.library_page.list_view)
        play.setContext(Qt.WidgetWithChildrenShortcut)
        play.activated.connect(self._play_selected)
        space = QShortcut(QKeySequence(Qt.Key_Space), self.library_page.list_view)
        space.setContext(Qt.WidgetWithChildrenShortcut)
        space.activated.connect(self._play_selected)

    def _focus_search(self) -> None:
        self.library_page.search_edit.setFocus()
        self.library_page.search_edit.selectAll()

    def refresh_all(self) -> None:
        libraries = self.db.libraries()
        valid_ids = {library["id"] for library in libraries}
        if self.current_library_id != ALL_LIBRARIES and self.current_library_id not in valid_ids:
            self.current_library_id = ALL_LIBRARIES if libraries else None
        selected = next(
            (
                library
                for library in libraries
                if library["id"] == self.current_library_id
            ),
            None,
        )
        if selected and selected.get("isolated") and not self.isolated_visibility:
            self.current_library_id = ALL_LIBRARIES
            self.unlocked_libraries.clear()
        self.sidebar.set_libraries(
            libraries,
            self.current_library_id,
            show_isolated=self.isolated_visibility,
        )
        self.top_bar.set_libraries(libraries, self.current_library_id)
        self.top_bar.set_isolated_state(
            self.isolated_visibility,
            any(library.get("isolated") for library in libraries),
        )
        self.refresh_categories()
        self.refresh_folders()
        self.refresh_tags()
        self.refresh_video_groups()
        self.refresh_filter_options()
        self.refresh_metadata_filter_options()
        self.load_videos()
        self._configure_file_watcher()
        self._arm_auto_lock()

    def _cleanup_legacy_missing_records(self) -> None:
        records = self.db.missing_videos()
        if not records:
            return
        libraries = {
            int(library["id"]): library
            for library in self.db.libraries()
        }
        removed_ids: list[int] = []
        recovered_ids: list[int] = []
        for video in records:
            library = libraries.get(int(video["library_id"]))
            if not library or not Path(library["root_path"]).is_dir():
                continue
            if existing_video_path(video):
                recovered_ids.append(int(video["id"]))
            else:
                removed_ids.append(int(video["id"]))
        for video_id in recovered_ids:
            self.db.update_video(video_id, missing=0)
        self.db.delete_video_records(removed_ids)

    def _configure_file_watcher(self) -> None:
        paths: set[str] = set()
        library_roots: list[Path] = []
        for library in self.db.libraries():
            root = Path(library["root_path"])
            if not root.is_dir():
                continue
            library_roots.append(root.resolve())
            paths.add(str(root.resolve()))
            for raw_path in self.db.video_paths_for_library(int(library["id"])):
                parent = Path(raw_path).parent
                if parent.is_dir() and is_within(parent, root):
                    paths.add(str(parent.resolve()))
        self._extra_watch_directories = {
            path
            for path in self._extra_watch_directories
            if Path(path).is_dir()
            and any(is_within(Path(path), root) for root in library_roots)
        }
        paths.update(self._extra_watch_directories)
        current = set(self.file_watcher.directories())
        for path in sorted(current - paths):
            self.file_watcher.removePath(path)
        self.file_watcher.addPaths(sorted(paths - current))

    def _file_change_detected(self, directory: str) -> None:
        if self._closing:
            return
        if self.mask_worker and self.mask_worker.isRunning():
            return
        if self.trim_worker and self.trim_worker.isRunning():
            return
        changed = Path(directory)
        matches = [
            library
            for library in self.db.libraries()
            if is_within(changed, Path(library["root_path"]))
        ]
        if not matches:
            return
        library = max(matches, key=lambda item: len(str(item["root_path"])))
        scan_directories = self._incremental_scan_directories(changed)
        self._dirty_watch_directories.setdefault(
            int(library["id"]),
            set(),
        ).update(scan_directories)
        if not self._player_active:
            self._watch_debounce.start()

    def _incremental_scan_directories(self, changed: Path) -> list[str]:
        watched = {
            str(Path(path).resolve()).casefold()
            for path in self.file_watcher.directories()
        }
        pending = [changed.resolve()]
        found: dict[str, str] = {}
        while pending:
            current = pending.pop()
            key = str(current).casefold()
            if key in found:
                continue
            found[key] = str(current)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if (
                            entry.name.startswith(".")
                            or entry.name in SKIPPED_DIRECTORY_NAMES
                        ):
                            continue
                        child = Path(entry.path).resolve()
                        child_key = str(child).casefold()
                        if child_key in watched:
                            continue
                        self._extra_watch_directories.add(str(child))
                        self.file_watcher.addPath(str(child))
                        pending.append(child)
            except OSError:
                continue
        return sorted(found.values())

    def _auto_refresh_watched_libraries(self) -> None:
        if not self._dirty_watch_directories:
            return
        for library_id, directories in self._dirty_watch_directories.items():
            self._pending_incremental_scans.setdefault(
                library_id,
                set(),
            ).update(directories)
        self._dirty_watch_directories.clear()
        self._start_next_pending_watch_scan()

    def refresh_incremental_current(self) -> None:
        if self.current_library_id is None:
            self.add_library()
            return
        allowed = set(self.scope_library_ids())
        queued = False
        self._watch_debounce.stop()
        for library_id, directories in self._dirty_watch_directories.items():
            if library_id not in allowed:
                continue
            self._pending_incremental_scans.setdefault(
                library_id,
                set(),
            ).update(directories)
            queued = True
        self._dirty_watch_directories = {
            library_id: directories
            for library_id, directories in self._dirty_watch_directories.items()
            if library_id not in allowed
        }
        already_pending = any(
            library_id in allowed and directories
            for library_id, directories in self._pending_incremental_scans.items()
        )
        if not queued and not already_pending:
            if self._player_active:
                self.status_label.setText("播放中，关闭播放器后即可检查文件变更")
                return
            if current_library_id := self.current_library_id:
                targets = [int(current_library_id)]
            else:
                targets = sorted(int(library_id) for library_id in allowed)
            if not targets:
                self.status_label.setText("没有可刷新的资源库")
                return
            self.status_label.setText(f"正在检查 {len(targets)} 个资源库的文件变更…")
            for library_id in targets:
                self.scan_library(library_id, refresh=True)
            return
        self.status_label.setText("已加入增量刷新队列")
        self._start_next_pending_watch_scan()

    def _start_next_pending_watch_scan(self) -> None:
        if self._player_active:
            return
        while self._pending_incremental_scans:
            library_id = next(iter(self._pending_incremental_scans))
            directories = self._pending_incremental_scans.pop(library_id)
            if not directories:
                continue
            library = self.db.library(library_id)
            if not library:
                continue
            self.scan_library(
                library_id,
                automatic=True,
                directories=sorted(directories),
            )
            return

    def current_library(self) -> dict[str, Any] | None:
        if self.current_library_id is None or self.current_library_id == ALL_LIBRARIES:
            return None
        return self.db.library(self.current_library_id)

    def scope_library_ids(self) -> list[int]:
        if self.current_library_id == ALL_LIBRARIES:
            return [
                int(library["id"])
                for library in self.db.libraries()
                if not library.get("isolated")
            ]
        if self.current_library_id is None:
            return []
        return [int(self.current_library_id)]

    def refresh_categories(self) -> None:
        categories = (
            []
            if self.current_library_id in {None, ALL_LIBRARIES}
            else self.db.categories([int(self.current_library_id)])
        )
        self.sidebar.set_categories(categories)
        self.detail.set_categories(categories)
        self.library_page.set_categories(categories)

    def refresh_folders(self) -> None:
        library = self.current_library()
        folders = (
            self.db.top_level_folders(int(library["id"]))
            if library
            else []
        )
        names = {str(folder["name"]).casefold() for folder in folders}
        if (
            self.current_folder
            and self.current_folder.casefold() not in names
        ):
            self.current_folder = None
            self.sidebar.select_folder_name(None)
        self.sidebar.set_folders(folders)

    def refresh_tags(self) -> list[dict[str, Any]]:
        tags = self.db.all_tags(self.scope_library_ids())
        self.sidebar.set_tags(tags)
        return tags

    def _sync_tag_filter_options(self, tags: list[dict[str, Any]]) -> None:
        options = dict(self._metadata_filter_options)
        options["tag"] = [
            {"value": str(tag["name"]), "count": int(tag.get("video_count") or 0)}
            for tag in sorted(
                tags,
                key=lambda item: (
                    -int(item.get("video_count") or 0),
                    str(item.get("name") or "").casefold(),
                ),
            )
        ]
        self._metadata_filter_options = options
        self._metadata_filter_options_cache[
            tuple(self.scope_library_ids())
        ] = options
        self.library_page.set_metadata_filter_options(options)
        self.detail.set_filter_options(options, self._nfo_filter_options)

    def refresh_sidebar_counts(self) -> None:
        self.sidebar.set_counts(
            self.db.sidebar_counts(self.scope_library_ids())
        )

    def refresh_video_groups(self) -> None:
        library_id = (
            int(self.current_library_id)
            if self.current_library_id not in {None, ALL_LIBRARIES}
            else None
        )
        self.library_page.set_video_groups(self.db.video_groups(library_id))

    def refresh_filter_options(self, use_cache: bool = False) -> None:
        scope = tuple(self.scope_library_ids())
        if use_cache and scope in self._nfo_filter_options_cache:
            self._nfo_filter_options = self._nfo_filter_options_cache[scope]
        else:
            self._nfo_filter_options = self.db.nfo_filter_options(list(scope))
            self._nfo_filter_options_cache[scope] = self._nfo_filter_options
        self.sidebar.set_actors(self._nfo_filter_options.get("actor", []))
        self.library_page.set_filter_options(self._nfo_filter_options)
        self.detail.set_filter_options(
            self._metadata_filter_options,
            self._nfo_filter_options,
        )

    def refresh_metadata_filter_options(self, use_cache: bool = False) -> None:
        scope = tuple(self.scope_library_ids())
        if use_cache and scope in self._metadata_filter_options_cache:
            self._metadata_filter_options = (
                self._metadata_filter_options_cache[scope]
            )
        else:
            self._metadata_filter_options = self.db.metadata_filter_options(
                list(scope)
            )
            self._metadata_filter_options_cache[scope] = (
                self._metadata_filter_options
            )
        self.library_page.set_metadata_filter_options(
            self._metadata_filter_options
        )
        self.detail.set_filter_options(
            self._metadata_filter_options,
            self._nfo_filter_options,
        )

    def select_library(self, library_id: int) -> None:
        if library_id == self.current_library_id:
            return
        libraries = self.db.libraries()
        target_library = (
            next(
                (
                    library
                    for library in libraries
                    if int(library["id"]) == int(library_id)
                ),
                None,
            )
            if library_id not in {None, ALL_LIBRARIES}
            else None
        )
        if target_library and target_library.get("isolated"):
            if not self.isolated_visibility:
                self._reset_library_selection()
                return
            if not self.db.has_library_access_password(library_id):
                QMessageBox.warning(
                    self,
                    "尚未设置密码",
                    "这个隔离资源库还没有第二层进入密码。\n"
                    "请先在“工具 → 密码与隔离”中设置。",
                )
                self._reset_library_selection()
                self.open_security_settings()
                return
            if library_id not in self.unlocked_libraries:
                dialog = PasswordPromptDialog(
                    "进入隔离资源库",
                    f"请输入“{target_library['name']}”的第二层进入密码。",
                    self,
                )
                if (
                    dialog.exec() != QDialog.Accepted
                    or not self.db.verify_library_access_password(
                        library_id,
                        dialog.password(),
                    )
                ):
                    if dialog.result() == QDialog.Accepted:
                        QMessageBox.warning(self, "密码错误", "第二层密码不正确。")
                    self._reset_library_selection()
                    return
                self.unlocked_libraries.add(library_id)
                self._note_activity()
        self.current_library_id = library_id
        self.current_category_id = None
        self.current_tag = None
        self.current_folder = None
        self.current_nfo_filters = {}
        self.current_metadata_filters = {}
        self.expanded_groups.clear()
        self.current_mode = "all"
        self.library_page.set_active_filters({})
        self.library_page.set_active_metadata_filters({})
        self.sidebar.set_view_mode("all")
        self.sidebar.select_library_id(library_id)
        self.top_bar.set_libraries(libraries, library_id)
        self.refresh_categories()
        self.refresh_folders()
        self.refresh_tags()
        self.refresh_video_groups()
        self.refresh_filter_options(use_cache=True)
        self.refresh_metadata_filter_options(use_cache=True)
        self.load_videos()
        self._configure_file_watcher()

    def _reset_library_selection(self) -> None:
        selected = (
            self.current_library_id
            if self.current_library_id is not None
            else ALL_LIBRARIES
        )
        self.sidebar.select_library_id(selected)
        self.top_bar.set_libraries(self.db.libraries(), selected)

    def select_view(self, mode: str) -> None:
        self.current_mode = mode
        self.current_category_id = None
        self._clear_scope_filters()
        self.sidebar.clear_category_selection()
        self.load_videos()

    def select_category(self, category_id: object) -> None:
        if category_id is None:
            self.current_category_id = None
            self.current_mode = "all"
            self._clear_scope_filters()
            self.sidebar.set_view_mode("all")
            self.sidebar.clear_category_selection()
            self.load_videos()
            return
        self.current_category_id = int(category_id)
        self.current_mode = "all"
        self._clear_scope_filters()
        self.load_videos()

    def select_tag(self, tag: str) -> None:
        self.current_category_id = None
        self.current_mode = "all"
        self._clear_scope_filters()
        self.current_tag = tag
        self.sidebar.select_tag_name(tag)
        self.load_videos()

    def select_folder(self, folder: str) -> None:
        self.current_category_id = None
        self.current_mode = "all"
        self._clear_scope_filters()
        self.current_folder = folder
        self.sidebar.select_folder_name(folder)
        self.load_videos()

    def select_rating(self, stars: int) -> None:
        self.current_mode = "all"
        self.current_category_id = None
        self._clear_scope_filters()
        if stars >= 0:
            self.current_metadata_filters = {
                "rating": f"{stars} 星" if stars else "未评级"
            }
        self.library_page.set_active_metadata_filters(
            self.current_metadata_filters
        )
        self.load_videos()

    def select_actor(self, actor: str) -> None:
        self.current_mode = "all"
        self.current_category_id = None
        self._clear_scope_filters()
        self.current_nfo_filters = {"actor": actor}
        self.library_page.set_active_filters(self.current_nfo_filters)
        self.sidebar.select_actor_name(actor)
        self.load_videos()

    def load_videos(self, _value=None) -> None:
        self.refresh_sidebar_counts()
        search = self.library_page.search_edit.text()
        sort = str(self.library_page.sort_combo.currentData())
        descending = self.library_page.sort_descending()
        orientation = self.library_page.orientation()
        videos = self.db.videos(
            library_id=(
                self.current_library_id
                if self.current_library_id not in {None, ALL_LIBRARIES}
                else None
            ),
            library_ids=(
                self.scope_library_ids()
                if self.current_library_id == ALL_LIBRARIES
                else None
            ),
            category_id=self.current_category_id,
            mode=self.current_mode,
            search=search,
            sort=sort,
            descending=descending,
            tag=self.current_tag,
            nfo_filters=self.current_nfo_filters,
            orientation=orientation,
            metadata_filters=self.current_metadata_filters,
            folder=self.current_folder,
            expanded_groups=self.expanded_groups,
        )
        self.library_page.set_videos(videos)
        title = {
            "all": "全部视频",
            "continue": "继续观看",
            "recent": "最近加入",
            "favorite": "收藏",
            "uncategorized": "未分类",
        }.get(self.current_mode, "全部视频")
        if (
            self.current_library_id == ALL_LIBRARIES
            and self.current_mode == "all"
        ):
            title = "全部资源库"
        if self.current_tag:
            title = f"标签  #{self.current_tag}"
        elif self.current_category_id is not None:
            category = next(
                (
                    item
                    for item in self.db.categories()
                    if item["id"] == self.current_category_id
                ),
                None,
            )
            if category:
                title = category["name"]
        if self.current_folder:
            title = f"文件夹 · {self.current_folder}"
        actor = self.current_nfo_filters.get("actor")
        if actor:
            title = f"演员 · {actor}"
        self.library_page.set_title(title)
        if not self.db.libraries():
            self.library_page.set_empty_message(
                "还没有资源库",
                "添加一个目录，影库只会管理其中的视频和关联资源。",
            )
        elif (
            self.current_library_id == ALL_LIBRARIES
            and not self.scope_library_ids()
            and self.current_mode == "all"
            and not search.strip()
            and self.current_category_id is None
            and not self.current_tag
        ):
            self.library_page.set_empty_message(
                "没有可汇总的资源库",
                "所有资源库都已隔离。请从左侧选择一个资源库，或在设置中关闭隔离。",
            )
        elif not videos:
            self.library_page.set_empty_message(
                "这里还没有视频",
                "当前分类、标签或搜索条件没有结果。",
                show_clear=bool(
                    self.current_category_id is not None
                    or self.current_tag
                    or self.current_folder
                    or self.current_nfo_filters
                    or self.current_metadata_filters
                    or search.strip()
                    or self.current_mode != "all"
                    or orientation != "all"
                ),
            )
        self.detail.set_video(None)
        self.detail.set_active_filters(
            self.current_metadata_filters,
            self.current_nfo_filters,
        )
        self.selected_video_id = None
        visible_count = sum(
            int(video.get("group_count") or 1)
            if video.get("group_collapsed")
            else 1
            for video in videos
        )
        self._update_status(visible_count)
        self._reload_player_queue()

    def clear_filters(self) -> None:
        self.current_mode = "all"
        self.current_category_id = None
        self._clear_scope_filters()
        self.sidebar.set_view_mode("all")
        self.sidebar.clear_category_selection()
        self.load_videos()

    def _clear_scope_filters(self) -> None:
        self.current_tag = None
        self.current_folder = None
        self.current_nfo_filters = {}
        self.current_metadata_filters = {}
        self.expanded_groups.clear()
        self.library_page.set_orientation("all")
        self.library_page.search_edit.clear()
        self.library_page.set_active_filters({})
        self.library_page.set_active_metadata_filters({})

    def set_nfo_filters(self, filters: object) -> None:
        self.current_nfo_filters = {
            str(key): str(value) for key, value in dict(filters).items()
        }
        self.sidebar.select_actor_name(
            self.current_nfo_filters.get("actor")
        )
        self.library_page.set_active_filters(self.current_nfo_filters)
        self.load_videos()

    def set_metadata_filters(self, filters: object) -> None:
        self.current_metadata_filters = {
            str(key): str(value) for key, value in dict(filters).items()
        }
        self.library_page.set_active_metadata_filters(
            self.current_metadata_filters
        )
        self.load_videos()

    def toggle_video_group(self, group_id: int, expanded: bool) -> None:
        group_id = int(group_id)
        current = self.library_page.current_videos()
        group_index = next(
            (
                index
                for index, video in enumerate(current)
                if int(video.get("group_id") or 0) == group_id
            ),
            None,
        )
        if group_index is None:
            return
        anchor_id = int(current[group_index]["id"])
        group_name = str(current[group_index].get("group_name") or "视频组")
        if expanded:
            member_ids = list(current[group_index].get("group_member_ids") or [])
            members = self.db.videos_for_group(group_id, member_ids)
            if len(members) < 2:
                self.load_videos()
                return
            replacement = []
            for index, member in enumerate(members):
                item = dict(member)
                item["group_id"] = group_id
                item["group_name"] = group_name
                item["group_count"] = len(members)
                item["group_collapsed"] = False
                if index == 0:
                    item["group_leader"] = True
                replacement.append(item)
            self.expanded_groups.add(group_id)
            anchor_id = int(members[0]["id"])
        else:
            members = [
                video
                for video in current
                if int(video.get("group_id") or 0) == group_id
            ]
            if len(members) < 2:
                return
            replacement = [self._collapsed_group_item(members, group_name)]
            self.expanded_groups.discard(group_id)
            anchor_id = int(members[0]["id"])
        remove_count = 1 if expanded else len(members)
        self.library_page.replace_video_range(
            group_index,
            remove_count,
            replacement,
        )
        visible = self.library_page.current_videos()
        self._update_status(
            sum(
                int(video.get("group_count") or 1)
                if video.get("group_collapsed")
                else 1
                for video in visible
            )
        )
        self._reload_player_queue()
        self.library_page.scroll_to_video(anchor_id)
        self.status_label.setText(
            f"已{'展开' if expanded else '收起'}视频组"
        )

    @staticmethod
    def _collapsed_group_item(
        members: list[dict[str, Any]],
        group_name: str,
    ) -> dict[str, Any]:
        item = dict(members[0])
        item.pop("group_leader", None)
        item["group_id"] = int(item["group_id"])
        item["group_name"] = group_name
        item["group_count"] = len(members)
        item["group_member_ids"] = [int(member["id"]) for member in members]
        item["group_size_bytes"] = sum(
            int(member.get("size_bytes") or 0) for member in members
        )
        item["group_duration_ms"] = sum(
            int(member.get("duration_ms") or 0) for member in members
        )
        item["group_collapsed"] = True
        item["title"] = group_name
        item["file_name"] = f"{len(members)} 个视频"
        item["size_bytes"] = item["group_size_bytes"]
        item["duration_ms"] = item["group_duration_ms"]
        return item

    def add_videos_to_group(self, video_ids: object, group_id: int) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        if not ids:
            return
        try:
            count = self.db.add_videos_to_group(group_id, ids)
        except Exception as exc:
            QMessageBox.warning(self, "加入视频组失败", str(exc))
            return
        self.expanded_groups.discard(int(group_id))
        self.refresh_video_groups()
        self.load_videos()
        self.library_page.select_video(ids[0])
        self.status_label.setText(
            f"已加入 {len(ids)} 个视频 · 视频组现有 {count} 个"
        )

    def create_video_group(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        if len(ids) < 2:
            return
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        default_name = (
            str(videos[0].get("capture_date") or videos[0].get("title") or "视频组")
            if videos
            else "视频组"
        )
        name, ok = QInputDialog.getText(
            self,
            "创建视频组",
            "视频组名称",
            text=default_name,
        )
        if not ok:
            return
        try:
            group_id = self.db.create_video_group(name, ids)
        except Exception as exc:
            QMessageBox.warning(self, "创建视频组失败", str(exc))
            return
        self.expanded_groups.discard(group_id)
        self.refresh_video_groups()
        self.load_videos()
        self.status_label.setText(f"已创建视频组 · {len(ids)} 个视频")

    def remove_video_group(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        removed = self.db.remove_video_groups(ids)
        self.expanded_groups.clear()
        self.refresh_video_groups()
        self.load_videos()
        if removed:
            self.status_label.setText("已取消视频组")

    def _update_status(self, count: int | None = None) -> None:
        library = self.current_library()
        if self.current_library_id == ALL_LIBRARIES:
            library_name = "全部资源库"
        else:
            library_name = library["name"] if library else "未选择资源库"
        text = f"{library_name}"
        if count is not None:
            text += f"  ·  当前显示 {count} 个视频"
        self.status_label.setText(text)

    def selection_changed(self, videos: list[dict[str, Any]]) -> None:
        if not videos:
            self.selected_video_id = None
            self.detail.set_video(None)
            return
        video = videos[0]
        self.selected_video_id = int(video["id"])
        self.detail.set_video(video)
        self.detail.set_tags(self.db.tags_for_video(int(video["id"])))
        if len(videos) > 1:
            self.status_label.setText(f"已选择 {len(videos)} 个视频")

    def add_library(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            QMessageBox.information(self, "正在扫描", "请等待当前扫描完成。")
            return
        dialog = AddLibraryDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        name, root, kind, mask_enabled, isolated = dialog.values()
        new_root = Path(root)
        for library in self.db.libraries():
            old_root = Path(library["root_path"])
            if is_within(new_root, old_root) or is_within(old_root, new_root):
                QMessageBox.warning(
                    self,
                    "资源库范围重叠",
                    "新目录与已有资源库相同、互相包含或互为子目录。"
                    "为避免重复管理和误操作，请选择独立目录。",
                )
                return
        try:
            library_id = self.db.add_library(name, new_root, kind, False)
            if isolated:
                self.db.update_library_isolated(library_id, True)
        except Exception as exc:
            QMessageBox.critical(self, "添加失败", str(exc))
            return
        self.current_library_id = library_id
        self.current_mode = "all"
        self.isolated_visibility = isolated
        if isolated:
            self.unlocked_libraries.add(library_id)
        self._pending_mask_library_id = library_id if mask_enabled else None
        self.refresh_all()
        self.scan_current()
        if isolated:
            self._note_activity()
            if not (
                self.db.has_isolated_reveal_password()
                and self.db.has_library_access_password(library_id)
            ):
                self.open_security_settings()
            if not (
                self.db.has_isolated_reveal_password()
                and self.db.has_library_access_password(library_id)
            ):
                self.isolated_visibility = False
                self.unlocked_libraries.clear()
                self.current_library_id = ALL_LIBRARIES
                self.refresh_all()
                QMessageBox.information(
                    self,
                    "隔离资源库已隐藏",
                    "新资源库已隔离并扫描，但两层密码尚未设置完整，"
                    "因此不会继续显示名称。",
                )

    def relocate_current_library(self) -> None:
        if self.current_library_id in {None, ALL_LIBRARIES}:
            QMessageBox.information(
                self,
                "未选择资源库",
                "请先选择一个资源库，再迁移它的目录。",
            )
            return
        library = self.db.library(int(self.current_library_id))
        if not library:
            return
        if self._player_active:
            QMessageBox.information(
                self,
                "正在播放",
                "请先关闭播放器，再迁移资源库目录。",
            )
            return
        workers = (
            self.scan_worker,
            self.mask_worker,
            self.subtitle_worker,
            self.trim_worker,
        )
        if any(worker and worker.isRunning() for worker in workers):
            QMessageBox.information(
                self,
                "任务正在运行",
                "请等待扫描、字幕、伪装或裁剪任务完成后再迁移。",
            )
            return

        dialog = RelocateLibraryDialog(
            str(library["name"]),
            str(library["root_path"]),
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        new_root = Path(dialog.new_root())
        old_root = Path(library["root_path"])
        result = QMessageBox.question(
            self,
            "确认迁移资源库",
            f"确认视频文件已经迁移到：\n{new_root}\n\n"
            "软件将只更新当前数据库中的链接，不会复制或删除文件。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return

        library_id = int(library["id"])
        self._watch_debounce.stop()
        self._dirty_watch_directories.pop(library_id, None)
        self._pending_incremental_scans.pop(library_id, None)
        self.file_watcher.blockSignals(True)
        try:
            self._restore_runtime_assets()
            migration = migrate_library(
                self.db.path,
                old_root,
                new_root,
                library_id=library_id,
                copy_files=False,
                apply=True,
            )
        except (MigrationError, OSError, sqlite3.Error) as exc:
            QMessageBox.critical(self, "迁移失败", str(exc))
            return
        finally:
            self.file_watcher.blockSignals(False)

        self.db.record_operation(
            "relocate_library",
            str(old_root),
            str(new_root),
            "success",
            f"迁移 {migration['video_count']} 个视频记录",
        )
        self.refresh_all()
        self.status_label.setText(
            f"资源库“{library['name']}”已迁移到新目录"
        )
        QMessageBox.information(
            self,
            "资源库迁移完成",
            f"已更新 {migration['video_count']} 个视频记录。\n\n"
            + (
                f"已刷新 {migration['metadata_refresh_count']} 个文件的"
                "大小或修改时间。\n\n"
                if migration["metadata_refresh_count"]
                else ""
            )
            + f"数据库备份：\n{migration['backup']}",
        )

    def remove_library(self, library_id: int) -> None:
        library = self.db.library(library_id)
        if not library:
            return
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放。")
            return
        if (
            self.scan_worker
            and self.scan_worker.isRunning()
            and self._scan_library_id == library_id
        ) or (self.mask_worker and self.mask_worker.isRunning()):
            QMessageBox.information(
                self,
                "任务正在运行",
                "该资源库正在扫描或处理文件，请完成后再删除。",
            )
            return
        result = QMessageBox.question(
            self,
            "删除资源库",
            f"确定删除资源库“{library['name']}”？\n\n"
            "将删除该资源库的索引、分类关联、播放记录和生成缩略图。\n"
            "不会删除、移动或修改资源库中的视频、字幕、NFO 和原始图片。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        thumbnail_paths = self.db.remove_library(library_id)
        removed_thumbnails = remove_cached_thumbnails(
            self._thumbnail_dir.parent,
            thumbnail_paths,
        )
        if self.current_library_id == library_id:
            self.current_library_id = None
        self.unlocked_libraries.discard(library_id)
        self.refresh_all()
        self.status_label.setText(
            f"已删除资源库“{library['name']}” · 清理 {removed_thumbnails} 个缩略图"
        )

    def remove_current_library(self) -> None:
        if self.current_library_id in {None, ALL_LIBRARIES}:
            QMessageBox.information(
                self,
                "未选择资源库",
                "请先从顶部“资源库”菜单或左侧列表选择一个资源库。",
            )
            return
        self.remove_library(int(self.current_library_id))

    def open_data_directory(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

    def _task_worker_running(self) -> bool:
        workers = (
            self.scan_worker,
            self.subtitle_worker,
            self.mask_worker,
            self.export_worker,
            self.trim_worker,
        )
        return any(worker is not None and worker.isRunning() for worker in workers)

    def _task_queue_busy(self) -> bool:
        return bool(
            self._active_task
            or self._task_queue
            or self._task_worker_running()
        )

    def _enqueue_task(self, task: TaskQueueItem) -> None:
        self._task_queue.append(task)
        self._idle_timer.stop()
        QTimer.singleShot(0, self._start_next_task)

    def _start_next_task(self) -> None:
        if self._active_task or self._task_worker_running():
            return
        if not self._task_queue:
            self._finish_task_activity()
            return
        task = self._task_queue.pop(0)
        self._active_task = task
        self.task_center.run_task(task.task_id)
        if task.kind == "scan":
            self._run_scan_task(task)
        elif task.kind == "subtitle":
            self._run_subtitle_task(task)
        elif task.kind == "mask":
            self._run_mask_task(task)
        elif task.kind == "export":
            self._run_export_task(task)
        else:
            self.task_center.finish_task(
                task.task_id,
                "failed",
                f"未知任务类型：{task.kind}",
            )
            self._active_task = None
            QTimer.singleShot(0, self._start_next_task)

    def _complete_active_task(self) -> None:
        self._active_task = None
        QTimer.singleShot(150, self._start_next_task)

    def _finish_task_activity(self) -> None:
        if self._task_queue_busy():
            return
        reason = self._pending_lock_reason
        self._pending_lock_reason = None
        if reason:
            self._lock_isolated_libraries(reason)
        else:
            self._note_activity()

    def scan_current(self) -> None:
        if self.current_library_id == ALL_LIBRARIES:
            QMessageBox.information(
                self,
                "请选择资源库",
                "“全部资源库”是汇总视图。请先在左侧选择一个资源库再扫描。",
            )
            return
        if self.current_library_id is None:
            self.add_library()
            return
        self.scan_library(self.current_library_id)

    def scan_library(
        self,
        library_id: int,
        automatic: bool = False,
        directories: list[str] | None = None,
        refresh: bool = False,
    ) -> None:
        directories = sorted(set(directories or []))
        incremental = bool(directories)
        library = self.db.library(library_id)
        if not library:
            return
        if not Path(library["root_path"]).is_dir():
            if automatic:
                return
            QMessageBox.warning(self, "资源库离线", "当前资源库目录不可访问。")
            return
        if not automatic and not refresh:
            self.current_library_id = library_id
        if incremental:
            for queued in self._task_queue:
                if (
                    queued.kind == "scan"
                    and int(queued.payload["library_id"]) == library_id
                    and queued.payload["directories"]
                ):
                    queued.payload["directories"] = sorted(
                        set(queued.payload["directories"]).union(directories)
                    )
                    self.task_center.update_task(
                        queued.task_id,
                        detail="文件变更已合并，等待刷新",
                    )
                    return
        elif refresh:
            duplicate = (
                self._active_task
                and self._active_task.kind == "scan"
                and int(self._active_task.payload["library_id"]) == library_id
                and not self._active_task.payload["directories"]
            ) or any(
                task.kind == "scan"
                and int(task.payload["library_id"]) == library_id
                and not task.payload["directories"]
                for task in self._task_queue
            )
            if duplicate:
                self.status_label.setText("当前资源库的文件变更刷新已在任务队列中")
                return
        task_id = f"scan-{uuid.uuid4().hex}"
        task_name = (
            "资源库自动更新"
            if automatic
            else "刷新文件变更"
            if refresh or incremental
            else "资源扫描"
        )
        self.task_center.start_task(
            task_id,
            task_name,
            str(library["name"]),
            "排队等待刷新" if incremental or refresh else "排队等待扫描",
            state="queued",
        )
        self._enqueue_task(
            TaskQueueItem(
                task_id,
                "scan",
                {
                    "library_id": library_id,
                    "automatic": bool(automatic),
                    "incremental": incremental,
                    "refresh": bool(refresh),
                    "directories": directories,
                },
            )
        )

    def _run_scan_task(self, task: TaskQueueItem) -> None:
        library_id = int(task.payload["library_id"])
        directories = list(task.payload["directories"])
        incremental = bool(task.payload["incremental"])
        refresh = bool(task.payload.get("refresh"))
        self._scan_library_id = library_id
        self._scan_automatic = bool(task.payload["automatic"])
        self._scan_incremental = incremental
        self._scan_refresh = refresh
        self._scan_task_id = task.task_id
        self.task_center.run_task(
            task.task_id,
            "开始增量刷新"
            if incremental
            else "开始检查文件变更"
            if refresh
            else "开始扫描",
        )
        self.scan_worker = ScanWorker(
            str(self.db.path),
            library_id,
            self,
            directories=directories or None,
        )
        self.scan_worker.progress.connect(self._scan_progress)
        self.scan_worker.message.connect(self._scan_message)
        self.scan_worker.finished_scan.connect(self._scan_finished)
        self.scan_worker.finished.connect(self._scan_thread_finished)
        self.library_page.set_scanning(
            True,
            "准备增量刷新"
            if incremental
            else "准备检查文件变更"
            if refresh
            else "准备扫描",
            cancellable=True,
        )
        self.scan_worker.start()

    def _scan_progress(self, current: int, total: int, name: str) -> None:
        label = (
            "刷新变更"
            if self._scan_incremental or self._scan_refresh
            else "更新资源库"
        )
        self.library_page.set_scanning(
            True,
            f"{label} {current}/{total}  {name}",
            current,
            total,
            cancellable=True,
        )
        if self._scan_task_id:
            percent = int(current * 100 / total) if total else 0
            self.task_center.update_task(
                self._scan_task_id,
                progress=percent,
                detail=f"{current}/{total}  {name}",
            )
        self.status_label.setText(f"正在{label} · {current}/{total}  {name}")

    def _scan_message(self, message: str) -> None:
        self.status_label.setText(message)
        if self._scan_task_id:
            self.task_center.update_task(self._scan_task_id, detail=message)

    def _scan_finished(self, imported: int, errors: int, complete: bool) -> None:
        automatic = self._scan_automatic
        incremental = self._scan_incremental
        refresh = self._scan_refresh
        self._scan_automatic = False
        self._scan_incremental = False
        self._scan_refresh = False
        self.library_page.set_scanning(False)
        if self._scan_task_id:
            self.task_center.finish_task(
                self._scan_task_id,
                "completed" if complete else "cancelled",
                (
                    f"处理 {imported} 个变更文件，失败 {errors} 个"
                    if incremental or refresh
                    else f"导入 {imported} 个视频，失败 {errors} 个"
                )
                if complete
                else f"扫描已取消，已处理 {imported} 个视频",
            )
            self._scan_task_id = None
        self.refresh_all()
        if complete:
            self.status_label.setText(
                (
                    f"文件变更已刷新 · 已处理 {imported} 个视频"
                    if incremental or refresh
                    else f"扫描完成 · {imported} 个视频"
                )
                + (f" · {errors} 个失败" if errors else "")
            )
            pending_mask = self._pending_mask_library_id
            self._pending_mask_library_id = None
            if pending_mask is not None and self.db.library(pending_mask):
                self._start_mask(pending_mask, True)

    def _scan_thread_finished(self) -> None:
        worker = self.scan_worker
        if worker is not None:
            self.scan_worker = None
            worker.deleteLater()
        self._scan_refresh = False
        if self._scan_task_id:
            self.task_center.finish_task(
                self._scan_task_id,
                "failed",
                "扫描线程异常结束",
            )
            self._scan_task_id = None
        self._complete_active_task()
        QTimer.singleShot(150, self._start_next_pending_watch_scan)

    def cancel_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.requestInterruption()
            self.library_page.progress_label.setText("正在取消扫描")
            self.library_page.cancel_button.setEnabled(False)
            if self._scan_task_id:
                self.task_center.update_task(
                    self._scan_task_id,
                    detail="正在取消扫描...",
                )

    def open_task_center(self) -> None:
        self.task_center.show()

    def cancel_task(self, task_id: str) -> None:
        for index, queued in enumerate(self._task_queue):
            if queued.task_id == task_id:
                self._task_queue.pop(index)
                self.task_center.finish_task(
                    task_id,
                    "cancelled",
                    "已从队列取消",
                )
                self._finish_task_activity()
                return
        if task_id == self._scan_task_id:
            self.cancel_scan()
        elif task_id == self._subtitle_task_id and self.subtitle_worker:
            self.subtitle_worker.cancel()
        elif task_id == self._mask_task_id and self.mask_worker:
            self.mask_worker.requestInterruption()
            self.task_center.update_task(task_id, detail="正在取消...")
        elif task_id == self._export_task_id and self.export_worker:
            self.export_worker.requestInterruption()
            self.task_center.update_task(task_id, detail="正在停止复制...")

    def start_subtitle_generation(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        QTimer.singleShot(0, lambda: self._start_subtitle_generation(ids))

    def _start_subtitle_generation(self, ids: list[int]) -> None:
        if self.subtitle_options_dialog:
            self.subtitle_options_dialog.show()
            self.subtitle_options_dialog.raise_()
            self.subtitle_options_dialog.activateWindow()
            return
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if not videos:
            return
        dialog = SubtitleOptionsDialog(len(videos), self)
        dialog.setModal(False)
        self.subtitle_options_dialog = dialog
        dialog.accepted.connect(
            lambda: self._begin_subtitle_generation(videos, dialog)
        )
        dialog.finished.connect(
            lambda _result: self._subtitle_options_closed(dialog)
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _subtitle_options_closed(self, dialog: SubtitleOptionsDialog) -> None:
        if self.subtitle_options_dialog is dialog:
            self.subtitle_options_dialog = None
        dialog.deleteLater()

    def _begin_subtitle_generation(
        self,
        videos: list[dict[str, Any]],
        dialog: SubtitleOptionsDialog,
    ) -> None:
        language, model, overwrite, translate = dialog.values()
        generate_videos: list[dict[str, Any]] = []
        translate_videos: list[dict[str, Any]] = []
        skipped = 0
        for video in videos:
            original = subtitle_target(video, translated=False)
            translated = subtitle_target(video, translated=True)
            if overwrite:
                generate_videos.append(video)
            elif translated.exists() and translate:
                skipped += 1
            elif original.exists():
                if translate:
                    translate_videos.append(video)
                else:
                    skipped += 1
            else:
                generate_videos.append(video)

        missing = validate_whisper_installation(model) if generate_videos else []
        if generate_videos and missing:
            QMessageBox.critical(
                self,
                "离线字幕组件不完整",
                "找不到以下本地文件：\n\n"
                + "\n".join(missing[:6])
                + "\n\n软件不会联网下载模型，请补齐后重试。",
            )
            return
        if generate_videos:
            self._enqueue_subtitle_tasks(
                generate_videos,
                language,
                model,
                overwrite,
                translate,
            )
        if translate_videos:
            self._enqueue_subtitle_tasks(
                translate_videos,
                language,
                "",
                overwrite,
                True,
                translation_only=True,
            )
        if not generate_videos and not translate_videos:
            self.status_label.setText(
                f"已跳过 {skipped} 个已有字幕的视频，没有创建任务"
            )
        elif skipped:
            self.status_label.setText(
                f"已加入 {len(generate_videos) + len(translate_videos)} 个任务，"
                f"跳过 {skipped} 个已有字幕的视频"
            )
        else:
            parts = []
            if generate_videos:
                parts.append(f"生成 {len(generate_videos)} 个")
            if translate_videos:
                parts.append(f"翻译 {len(translate_videos)} 个")
            self.status_label.setText(
                "已加入 " + "，".join(parts) + "任务 · 全部任务将按顺序执行"
            )

    def _enqueue_subtitle_tasks(
        self,
        videos: list[dict[str, Any]],
        language: str,
        model: str,
        overwrite: bool,
        translate: bool,
        translation_only: bool = False,
    ) -> None:
        language_name = {"": "自动检测", "ja": "日语", "en": "英语"}.get(
            language,
            language,
        )
        tasks: list[dict[str, Any]] = []
        for video in videos:
            task_id = f"{'translate' if translation_only else 'subtitle'}-{uuid.uuid4().hex}"
            title = str(
                video.get("display_name")
                or video.get("file_name")
                or f"视频 {video.get('id')}"
            )
            tasks.append(
                {
                    "id": task_id,
                    "kind": "字幕翻译" if translation_only else "字幕生成",
                    "title": title,
                    "detail": (
                        f"{language_name} · 排队中"
                        if translation_only
                        else f"{language_name} · {model} · 排队中"
                    ),
                    "outputs": [
                        str(
                            subtitle_target(
                                video,
                                translated=translate or translation_only,
                            )
                        )
                    ],
                }
            )
            self._enqueue_task(
                TaskQueueItem(
                    task_id,
                    "subtitle",
                    {
                        "video": video,
                        "language": language,
                        "model": model,
                        "overwrite": overwrite,
                        "translate": translate,
                        "translation_only": translation_only,
                    },
                )
            )
        self.task_center.start_tasks(tasks, state="queued")
        action = "字幕翻译" if translation_only else "字幕"
        self.status_label.setText(
            f"已加入 {len(tasks)} 个{action}任务 · 全部任务将按顺序执行"
        )

    def _launch_subtitle_worker(
        self,
        videos: list[dict[str, Any]],
        language: str,
        model: str,
        overwrite: bool,
        translate: bool,
    ) -> None:
        self._enqueue_subtitle_tasks(
            videos,
            language,
            model,
            overwrite,
            translate,
        )

    def _run_subtitle_task(self, task: TaskQueueItem) -> None:
        self._subtitle_task_id = task.task_id
        language = str(task.payload["language"])
        model = str(task.payload["model"])
        translate = bool(task.payload["translate"])
        translation_only = bool(task.payload.get("translation_only"))
        language_name = {"": "自动检测", "ja": "日语", "en": "英语"}.get(
            language,
            language,
        )
        self.task_center.run_task(
            task.task_id,
            (
                f"{language_name} · 正在翻译已有字幕"
                if translation_only
                else f"{language_name} · {model} · 准备加载本地模型"
            ),
        )
        videos = [task.payload["video"]]
        self.subtitle_worker = SubtitleWorker(
            str(self.db.path),
            videos,
            language,
            model,
            bool(task.payload["overwrite"]),
            translate,
            translation_only,
            parent=self,
        )
        self.subtitle_worker.progress.connect(self._subtitle_progress)
        self.subtitle_worker.completed.connect(self._subtitle_finished)
        self.subtitle_worker.finished.connect(self._subtitle_thread_finished)
        self.subtitle_worker.start()
        self.status_label.setText("字幕任务已开始 · 可从顶部“任务中心”查看进度")

    def _subtitle_progress(self, percent: int, detail: str) -> None:
        self._subtitle_pending = (percent, detail)
        if self._subtitle_throttle.isActive():
            return
        self._flush_subtitle_progress()

    def _flush_subtitle_progress(self) -> None:
        pending = self._subtitle_pending
        self._subtitle_pending = None
        if not pending:
            return
        percent, detail = pending
        self.status_label.setText(f"字幕生成 · {detail}")
        if self._subtitle_task_id:
            # 任务中心表格较重，限速刷新；否则翻译进度会拖住界面导致“无响应”
            self.task_center.update_task(
                self._subtitle_task_id,
                progress=percent,
                detail=detail,
            )
            self._subtitle_throttle.start(250)

    def _subtitle_finished(
        self,
        generated: int,
        skipped: int,
        failed: int,
        cancelled: bool,
        detail: str,
    ) -> None:
        self._subtitle_throttle.stop()
        self._subtitle_pending = None
        task_id = self._subtitle_task_id
        self._subtitle_task_id = None
        state = (
            "cancelled"
            if cancelled
            else "failed"
            if failed and not generated and not skipped
            else "completed"
        )
        if task_id:
            self.task_center.finish_task(task_id, state, detail)
        log(f"字幕任务结束：state={state} detail={detail}")
        # 刷新整库较慢，延后一拍执行，避免任务结束瞬间界面卡住
        QTimer.singleShot(0, self.refresh_all)
        self.status_label.setText(f"字幕任务 · {detail}")

    def _subtitle_thread_finished(self) -> None:
        worker = self.subtitle_worker
        if worker is not None:
            self.subtitle_worker = None
            worker.deleteLater()
        if self._subtitle_task_id:
            self.task_center.finish_task(
                self._subtitle_task_id,
                "failed",
                "字幕线程异常结束",
            )
            self._subtitle_task_id = None
        self._complete_active_task()
        QTimer.singleShot(150, self._start_next_pending_watch_scan)

    def open_settings(self) -> None:
        library = self.current_library()
        dialog = SettingsDialog(
            library,
            bool(library and library.get("mask_enabled")),
            int(self.db.setting("subtitle_font_size") or 22),
            (self.db.setting("show_detail_panel") or "1") == "1",
            self._playback_end_behavior,
            self._auto_lock_minutes,
            mask_map=self.db.mask_map(),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        library_name = dialog.library_name_value() if library else ""
        if library and not library_name:
            QMessageBox.warning(
                self,
                "名称无效",
                "资源库名称不能为空。",
            )
            return
        try:
            mask_map = dialog.mask_extension_map()
        except ValueError as exc:
            QMessageBox.warning(self, "伪装后缀无效", str(exc))
            return
        mask_map_changed = mask_map != self.db.mask_map()
        mask_task_active = bool(
            self._active_task and self._active_task.kind == "mask"
        )
        if mask_map_changed and (self._player_active or mask_task_active):
            QMessageBox.information(
                self,
                "暂时无法修改映射",
                "请先停止播放并等待当前伪装任务结束，再修改后缀映射。",
            )
            return
        subtitle_size = dialog.subtitle_font_size()
        self.db.set_setting("subtitle_font_size", str(subtitle_size))
        self._subtitle_font_size = subtitle_size
        if self._player_view is not None:
            self._player_view.set_subtitle_font_size(subtitle_size)
        self._playback_end_behavior = dialog.playback_end_behavior()
        self.db.set_setting(
            "playback_end_behavior",
            self._playback_end_behavior,
        )
        if self._player_view is not None:
            self._player_view.set_end_behavior(self._playback_end_behavior)
        self._auto_lock_minutes = max(
            0,
            min(240, int(dialog.auto_lock_minutes())),
        )
        self.db.set_setting(
            "isolated_auto_lock_minutes",
            str(self._auto_lock_minutes),
        )
        self._note_activity()
        self.set_detail_visible(dialog.show_detail_panel(), persist=True)
        if library:
            if library_name != str(library.get("name") or ""):
                self.db.rename_library(int(library["id"]), library_name)
                library["name"] = library_name
                self.status_label.setText("资源库名称已更新")
                self.refresh_all()
            requested_kind = dialog.project_kind()
            if requested_kind != str(library.get("kind") or "auto"):
                self.db.update_library_kind(int(library["id"]), requested_kind)
                self.status_label.setText("项目类型已更新，下一次扫描会刷新元数据")
            old_isolated = bool(library.get("isolated"))
            isolated = dialog.isolated()
            if isolated != old_isolated:
                self.db.update_library_isolated(int(library["id"]), isolated)
                library_id = int(library["id"])
                if isolated:
                    ready = self.db.has_isolated_reveal_password() and (
                        self.db.has_library_access_password(library_id)
                    )
                    if not ready:
                        self.isolated_visibility = False
                        self.unlocked_libraries.clear()
                        self.current_library_id = ALL_LIBRARIES
                        self.refresh_all()
                        QMessageBox.information(
                            self,
                            "需要设置两层密码",
                            "资源库已隔离。请先设置第一层显示密码和"
                            "这个资源库的第二层进入密码。",
                        )
                        self.open_security_settings()
                        return
                    self.isolated_visibility = True
                    self.unlocked_libraries.add(library_id)
                    self._note_activity()
                else:
                    self.unlocked_libraries.discard(library_id)
                self.refresh_all()
                library = self.db.library(library_id) or library
        requested = dialog.mask_enabled()
        if mask_map_changed:
            try:
                self.db.set_mask_map(mask_map)
            except ValueError as exc:
                QMessageBox.warning(self, "伪装后缀无效", str(exc))
                return
            if self._player_view is not None:
                self._player_view.set_mask_map(mask_map)
        if library and requested != bool(library.get("mask_enabled")):
            self.toggle_mask(requested)
        if mask_map_changed:
            for row in self.db.libraries():
                if row.get("mask_enabled"):
                    self._start_mask(int(row["id"]), True)

    def _apply_saved_subtitle_size(self) -> None:
        try:
            size = int(self.db.setting("subtitle_font_size") or 22)
        except ValueError:
            size = 22
        self._subtitle_font_size = size
        if self._player_view is not None:
            self._player_view.set_subtitle_font_size(size)

    def set_detail_visible(self, visible: bool, persist: bool = True) -> None:
        self.detail_visible = bool(visible)
        self.detail.setVisible(bool(visible))
        self.top_bar.set_detail_visible(bool(visible))
        if persist:
            self.db.set_setting("show_detail_panel", "1" if visible else "0")

    def toggle_detail_panel(self) -> None:
        self.set_detail_visible(not self.detail_visible)

    def open_security_settings(self) -> None:
        dialog = SecurityDialog(
            self.db,
            show_library_names=self.isolated_visibility,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            self.refresh_all()
            self.status_label.setText("密码设置已保存")

    def request_isolated_visibility(self) -> None:
        isolated = [
            library for library in self.db.libraries() if library.get("isolated")
        ]
        if not isolated:
            QMessageBox.information(
                self,
                "没有隔离资源库",
                "请先在资源库设置中勾选“从全部资源库中隔离”。",
            )
            return
        if self.isolated_visibility:
            if self.current_library_id in {
                int(library["id"]) for library in isolated
            }:
                self.select_library(ALL_LIBRARIES)
            self.isolated_visibility = False
            self.unlocked_libraries.clear()
            self.refresh_all()
            self.status_label.setText("已隐藏隔离资源库名称")
            return
        if not self.db.has_isolated_reveal_password():
            QMessageBox.information(
                self,
                "尚未设置第一层密码",
                "请先在“工具 → 密码与隔离”中设置显示隔离资源库的密码。",
            )
            self.open_security_settings()
            return
        dialog = PasswordPromptDialog(
            "显示隔离资源库",
            "请输入第一层密码。验证后只会显示隔离资源库名称，进入具体资源库还需要第二层密码。",
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        if not self.db.verify_isolated_reveal_password(dialog.password()):
            QMessageBox.warning(self, "密码错误", "第一层密码不正确。")
            return
        self.isolated_visibility = True
        self._note_activity()
        self.refresh_all()
        self.status_label.setText("已显示隔离资源库名称，进入时仍需第二层密码")

    def _note_activity(self) -> None:
        self._last_activity = time.monotonic()
        self._arm_auto_lock()

    def _arm_auto_lock(self) -> None:
        if (
            self._closing
            or self._task_queue_busy()
            or self._auto_lock_minutes <= 0
            or not self.isActiveWindow()
            or self.isMinimized()
            or not (self.isolated_visibility or self.unlocked_libraries)
        ):
            self._idle_timer.stop()
            return
        remaining = (
            self._auto_lock_minutes * 60 - (time.monotonic() - self._last_activity)
        )
        if remaining <= 0:
            self._lock_isolated_libraries("闲置超时")
            return
        self._idle_timer.start(max(1, int(remaining * 1000)))

    def _auto_lock_if_idle(self) -> None:
        if (
            self._task_queue_busy()
            or not self.isActiveWindow()
            or self.isMinimized()
            or not (self.isolated_visibility or self.unlocked_libraries)
        ):
            self._idle_timer.stop()
            return
        remaining = (
            self._auto_lock_minutes * 60 - (time.monotonic() - self._last_activity)
        )
        if remaining <= 0:
            self._lock_isolated_libraries("闲置超时")
            return
        self._idle_timer.start(max(1, int(remaining * 1000)))

    def _lock_isolated_libraries(self, reason: str) -> None:
        if self._closing:
            self._idle_timer.stop()
            return
        if self._task_queue_busy():
            self._pending_lock_reason = self._pending_lock_reason or reason
            self._idle_timer.stop()
            self.status_label.setText("任务进行中，资源库保持活跃；完成后执行锁定")
            return
        if not (self.isolated_visibility or self.unlocked_libraries):
            self._idle_timer.stop()
            return
        library = self.current_library()
        current_isolated = bool(library and library.get("isolated"))
        self.isolated_visibility = False
        self.unlocked_libraries.clear()
        if current_isolated:
            self.select_library(ALL_LIBRARIES)
        self.refresh_all()
        self.status_label.setText(f"隔离资源库已锁定 · {reason}")

    def _setup_session_lock_monitor(self) -> None:
        if os.name != "nt":
            return
        app = QApplication.instance()
        if app is None:
            return
        if not register_session_lock_notification(int(self.winId())):
            return
        self._session_notification_registered = True
        self._session_lock_filter = WindowsSessionLockFilter(
            lambda: self._lock_isolated_libraries("Windows 已锁屏或注销")
        )
        app.installNativeEventFilter(self._session_lock_filter)

    def _teardown_session_lock_monitor(self) -> None:
        self._idle_timer.stop()
        app = QApplication.instance()
        if app is None:
            return
        if self._app_event_filter_installed:
            app.removeEventFilter(self)
            self._app_event_filter_installed = False
        if self._session_lock_filter is not None:
            app.removeNativeEventFilter(self._session_lock_filter)
            self._session_lock_filter = None
        if self._session_notification_registered:
            unregister_session_lock_notification(int(self.winId()))
            self._session_notification_registered = False

    def eventFilter(self, watched, event) -> bool:
        if (
            self._idle_timer.isActive()
            and event.type() in self._ACTIVITY_EVENTS
        ):
            self._last_activity = time.monotonic()
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.ActivationChange:
            if self.isActiveWindow() and not self.isMinimized():
                self._note_activity()
            else:
                self._idle_timer.stop()
        super().changeEvent(event)

    def toggle_mask(self, enable: bool) -> None:
        library = self.current_library()
        if not library:
            return
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放，再切换伪装模式。")
            return
        self._start_mask(int(library["id"]), enable)

    def _start_mask(self, library_id: int, enable: bool) -> None:
        if (
            self._active_task
            and self._active_task.kind == "mask"
            and int(self._active_task.payload["library_id"]) == library_id
        ) or any(
            task.kind == "mask"
            and int(task.payload["library_id"]) == library_id
            for task in self._task_queue
        ):
            self.status_label.setText("这个资源库的伪装切换已在任务队列中")
            return
        library = self.db.library(library_id)
        if not library:
            return
        task_id = f"mask-{uuid.uuid4().hex}"
        action = "启用" if enable else "关闭"
        self.task_center.start_task(
            task_id,
            "文件处理",
            f"{library['name']} · {action}伪装",
            "排队等待处理",
            state="queued",
        )
        self._enqueue_task(
            TaskQueueItem(
                task_id,
                "mask",
                {"library_id": library_id, "enable": bool(enable)},
            )
        )

    def _run_mask_task(self, task: TaskQueueItem) -> None:
        self._mask_task_id = task.task_id
        library_id = int(task.payload["library_id"])
        enable = bool(task.payload["enable"])
        action = "启用" if enable else "关闭"
        self.task_center.run_task(task.task_id, f"正在{action}伪装")
        self._restore_runtime_assets()
        self.mask_worker = MaskWorker(
            str(self.db.path),
            library_id,
            enable,
            self,
        )
        self.mask_worker.progress.connect(
            lambda total, done, name: self._mask_progress(total, done, name)
        )
        self.mask_worker.finished_mask.connect(self._mask_finished)
        self.mask_worker.finished.connect(self._mask_thread_finished)
        self.library_page.set_scanning(True, "准备切换伪装")
        self.top_bar.tools_button.setEnabled(False)
        self.mask_worker.start()

    def _mask_progress(self, total: int, done: int, name: str) -> None:
        self.library_page.set_scanning(
            True,
            f"切换伪装 {done}/{total}  {name}",
            done,
            total,
        )
        percent = int(done * 100 / total) if total else 0
        if self._mask_task_id:
            self.task_center.update_task(
                self._mask_task_id,
                progress=percent,
                detail=f"{done}/{total}  {name}",
            )

    def _mask_finished(self, success: bool) -> None:
        failures = list(self.mask_worker.failures) if self.mask_worker else []
        cancelled = bool(
            self.mask_worker and self.mask_worker.isInterruptionRequested()
        )
        task_id = self._mask_task_id
        self._mask_task_id = None
        self.library_page.set_scanning(False)
        self.top_bar.tools_button.setEnabled(True)
        self.artwork.reset_after_mask()
        self.refresh_all()
        if task_id:
            self.task_center.finish_task(
                task_id,
                "cancelled" if cancelled else "completed" if success else "failed",
                "文件处理已取消"
                if cancelled
                else "伪装后缀切换完成"
                if success
                else f"部分文件未切换，共 {len(failures)} 个",
            )
        if cancelled:
            self.status_label.setText("伪装后缀切换已取消")
        elif failures:
            QMessageBox.warning(
                self,
                "部分文件未切换",
                "以下文件因冲突或占用被跳过：\n\n" + "\n".join(failures[:12]),
            )
        else:
            self.status_label.setText("伪装后缀切换完成")

    def _mask_thread_finished(self) -> None:
        worker = self.mask_worker
        if worker is not None:
            self.mask_worker = None
            worker.deleteLater()
        if self._mask_task_id:
            self.task_center.finish_task(
                self._mask_task_id,
                "failed",
                "文件处理线程异常结束",
            )
            self._mask_task_id = None
        self._complete_active_task()
        QTimer.singleShot(150, self._start_next_pending_watch_scan)

    def play_video(self, video_id: int) -> None:
        video = self.db.video(video_id)
        if not video:
            return
        source = existing_video_path(video)
        if source is None:
            self._remove_missing_video(video_id)
            QMessageBox.warning(
                self,
                "文件不存在",
                "视频文件不存在，已从数据库中移除这条记录。",
            )
            return
        if video.get("missing"):
            self.db.update_video(video_id, missing=0)
            video["missing"] = 0
            for item in self.library_page.current_videos():
                if int(item["id"]) == int(video_id):
                    item["missing"] = 0
                    break
            self.library_page.refresh_video(video_id)
            self.refresh_sidebar_counts()
        queue = self.library_page.current_videos()
        if not any(item["id"] == video_id for item in queue):
            queue = [video]
        self._player_active = True
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()
        self.player_view.play_video(video, queue)
        self.player_view.setFocus()
        self.status_label.setText("正在播放")

    def _remove_missing_video(self, video_id: int) -> None:
        video_id = int(video_id)
        if (
            self._player_view is not None
            and self._player_view.current_video
            and int(self._player_view.current_video["id"]) == video_id
        ):
            self.close_player()
        self.db.delete_video_record(video_id)
        self.load_videos()
        self.status_label.setText("视频文件不存在，已移除数据库记录")

    def _refresh_player_tags(self, video_id: int) -> None:
        if self._player_view is None:
            return
        current = self._player_view.current_video
        if not current or int(current["id"]) != int(video_id):
            return
        self._player_view.set_video_tags(self.db.tags_for_video(video_id))

    def _reload_player_queue(self) -> None:
        """播放窗口打开时左/右方向键切换视频，队列跟随当前列表保持最新。"""
        if not self._player_active or self._player_view is None:
            return
        current = self._player_view.current_video
        if not current:
            return
        queue = self.library_page.current_videos()
        if not any(item["id"] == current["id"] for item in queue):
            queue = [current] + [item for item in queue if item["id"] != current["id"]]
        self._player_view.queue = queue
        self._player_view.index = next(
            (row for row, item in enumerate(queue) if item["id"] == current["id"]),
            0,
        )

    def open_with_default(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        if len(ids) != 1:
            QMessageBox.information(
                self,
                "请选择单个视频",
                "默认软件只能用于打开单个视频。",
            )
            return
        video = self.db.video(ids[0])
        if not video:
            return
        if video.get("logical_ext") != video.get("physical_ext"):
            QMessageBox.information(
                self,
                "伪装文件",
                "伪装后缀文件请使用 LosslessCut 或应用内播放器打开。",
            )
            return
        path = Path(video["path"])
        if not path.exists():
            QMessageBox.warning(self, "文件不存在", str(path))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "打开失败", "系统没有可用的默认软件。")

    def reveal_in_explorer(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        if len(ids) != 1:
            QMessageBox.information(
                self,
                "请选择单个视频",
                "文件夹定位一次只能用于一个视频。",
            )
            return
        video = self.db.video(ids[0])
        if not video:
            return
        path = existing_video_path(video)
        if not path:
            QMessageBox.warning(self, "文件不存在", str(video.get("path") or ""))
            return
        if os.name == "nt":
            try:
                subprocess.Popen(
                    ["explorer.exe", "/select,", str(path)],
                    creationflags=0x08000000,
                )
            except OSError as exc:
                QMessageBox.critical(self, "打开文件夹失败", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def open_with_losslesscut(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if not videos:
            return
        if not LOSSLESSCUT:
            QMessageBox.warning(
                self,
                "LosslessCut 未配置",
                "请将 LosslessCut 加入 PATH，或设置 LOSSLESSCUT_PATH。",
            )
            return
        executable = Path(LOSSLESSCUT)
        if not executable.exists():
            QMessageBox.warning(
                self,
                "LosslessCut 不存在",
                str(executable),
            )
            return
        paths = [str(Path(video["path"])) for video in videos]
        missing = [path for path in paths if not Path(path).exists()]
        if missing:
            QMessageBox.warning(
                self,
                "文件不存在",
                "\n".join(missing[:8]),
            )
            return
        try:
            subprocess.Popen(
                [str(executable), *paths],
                cwd=str(executable.parent),
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except OSError as exc:
            QMessageBox.critical(self, "LosslessCut 启动失败", str(exc))
            return
        if len(paths) > 1:
            threading.Timer(
                2.5,
                lambda: self._trigger_losslesscut_concat(executable),
            ).start()
            self.status_label.setText(
                f"已在 LosslessCut 中载入 {len(paths)} 个文件，请确认合并顺序"
            )
        else:
            self.status_label.setText("已用 LosslessCut 打开视频")

    @staticmethod
    def _trigger_losslesscut_concat(executable: Path) -> None:
        try:
            subprocess.run(
                [
                    str(executable),
                    "--keyboardAction",
                    "concatBatch",
                ],
                cwd=str(executable.parent),
                timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def export_videos(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if not videos:
            return
        destination = QFileDialog.getExistingDirectory(
            self,
            "选择导出文件夹",
            str(Path.home()),
        )
        if not destination:
            return
        target_dir = Path(destination).resolve()
        library_roots = [
            Path(str(library["root_path"])).resolve()
            for library in self.db.libraries()
        ]
        if any(is_within(target_dir, root) for root in library_roots):
            QMessageBox.warning(
                self,
                "导出位置无效",
                "导出目录不能位于任何资源库目录内。",
            )
            return

        items: list[dict[str, Any]] = []
        missing: list[str] = []
        for video in videos:
            source = existing_video_path(video)
            if source is None:
                missing.append(str(video.get("file_name") or video.get("id")))
                continue
            subtitles: dict[str, Path] = {}
            for raw_path in [
                video.get("subtitle_path"),
                *(video.get("sidecars") or []),
            ]:
                if not raw_path:
                    continue
                subtitle = Path(str(raw_path))
                if (
                    subtitle.suffix.lower() in SUBTITLE_EXTENSIONS
                    and subtitle.is_file()
                ):
                    subtitles[str(subtitle.resolve()).casefold()] = subtitle
            items.append(
                {
                    "source": str(source),
                    "stem": source.stem,
                    "logical_ext": str(video.get("logical_ext") or source.suffix),
                    "subtitles": [str(path) for path in subtitles.values()],
                }
            )
        if not items:
            QMessageBox.warning(
                self,
                "没有可导出的文件",
                "所选视频文件都不存在。",
            )
            return

        task_id = f"export-{uuid.uuid4().hex}"
        self.task_center.start_task(
            task_id,
            "文件导出",
            f"{len(items)} 个视频",
            f"等待复制到 {target_dir}",
            [str(target_dir)],
            state="queued",
        )
        self._enqueue_task(
            TaskQueueItem(
                task_id,
                "export",
                {"items": items, "destination": str(target_dir)},
            )
        )
        if missing:
            self.status_label.setText(
                f"已加入导出任务，另有 {len(missing)} 个文件不存在"
            )

    def _run_export_task(self, task: TaskQueueItem) -> None:
        self._export_task_id = task.task_id
        destination = Path(str(task.payload["destination"]))
        self.task_center.run_task(
            task.task_id,
            f"正在复制到 {destination}",
        )
        self.export_worker = ExportWorker(
            list(task.payload["items"]),
            destination,
            self,
        )
        self.export_worker.progress.connect(self._export_progress)
        self.export_worker.finished_export.connect(self._export_finished)
        self.export_worker.finished.connect(self._export_thread_finished)
        self.export_worker.start()
        self.status_label.setText("正在导出视频和字幕")

    def _export_progress(self, percent: int, detail: str) -> None:
        self.status_label.setText(f"正在导出 · {detail}")
        if self._export_task_id:
            self.task_center.update_task(
                self._export_task_id,
                progress=percent,
                detail=detail,
            )

    def _export_finished(
        self,
        copied_videos: int,
        copied_subtitles: int,
        failures: object,
    ) -> None:
        task_id = self._export_task_id
        self._export_task_id = None
        failure_list = [str(item) for item in list(failures or [])]
        cancelled = bool(
            self.export_worker and self.export_worker.isInterruptionRequested()
        )
        state = (
            "cancelled"
            if cancelled
            else "failed"
            if failure_list
            else "completed"
        )
        detail = (
            f"导出 {copied_videos} 个视频、{copied_subtitles} 个字幕"
            + (f"，失败 {len(failure_list)} 项" if failure_list else "")
        )
        if task_id:
            self.task_center.finish_task(task_id, state, detail)
        self.status_label.setText(detail)

    def _export_thread_finished(self) -> None:
        worker = self.export_worker
        if worker is not None:
            self.export_worker = None
            worker.deleteLater()
        if self._export_task_id:
            self.task_center.finish_task(
                self._export_task_id,
                "failed",
                "导出线程异常结束",
            )
            self._export_task_id = None
        self._complete_active_task()

    def trim_video(self, video_id: int) -> None:
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放再裁剪。")
            return
        if self._task_queue_busy():
            QMessageBox.information(
                self,
                "任务正在运行",
                "请等待当前任务队列完成后再裁剪。",
            )
            return
        video = self.db.video(video_id)
        if not video:
            return
        library = self.db.library(int(video["library_id"]))
        if not library:
            return
        if video.get("missing"):
            QMessageBox.warning(self, "文件不可用", "这个视频已被标记为缺失。")
            return
        dialog = TrimDialog(video, Path(library["root_path"]), self)
        if dialog.exec() != QDialog.Accepted:
            return
        start_ms, end_ms, output, mode = dialog.values()
        if not is_within(output, Path(library["root_path"])):
            QMessageBox.warning(
                self,
                "输出位置无效",
                "裁剪结果只能保存在当前资源库目录内。",
            )
            return
        if output.exists():
            QMessageBox.warning(self, "文件已存在", str(output))
            return
        if self._task_queue_busy():
            QMessageBox.information(
                self,
                "任务正在运行",
                "任务队列刚刚开始运行，请完成后再裁剪。",
            )
            return
        self._trim_library_id = int(library["id"])
        self._idle_timer.stop()
        self.trim_progress = QProgressDialog(
            "正在使用 FFmpeg 裁剪...",
            "取消",
            0,
            0,
            self,
        )
        self.trim_progress.setWindowTitle("FFmpeg 裁剪")
        self.trim_progress.setWindowModality(Qt.WindowModal)
        self.trim_progress.setMinimumDuration(0)
        self.trim_progress.setAutoClose(False)
        self.trim_progress.setAutoReset(False)
        self.trim_worker = TrimWorker(
            str(self.db.path),
            Path(video["path"]),
            output,
            start_ms,
            end_ms,
            mode,
            self,
        )
        self.trim_progress.canceled.connect(self._cancel_trim)
        self.trim_worker.finished_trim.connect(self._trim_finished)
        self.trim_worker.finished.connect(self._trim_thread_finished)
        self.trim_worker.start()
        self.trim_progress.show()

    def choose_subtitle(self, video_id: int, autoplay: bool) -> None:
        video = self.db.video(video_id)
        if not video:
            return
        current = Path(str(video.get("path") or ""))
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择字幕文件",
            str(current.parent),
            "字幕文件 (*.srt *.ass *.ssa *.vtt);;所有文件 (*.*)",
        )
        if not path:
            return
        if not parse_subtitle(Path(path)):
            QMessageBox.warning(
                self,
                "字幕无法识别",
                "这个文件没有可读取的字幕内容，请选择有效的 SRT、ASS、SSA 或 VTT 字幕。",
            )
            return
        self.db.update_video(video_id, subtitle_path=path)
        if autoplay or not self._player_active:
            self.play_video(video_id)
        elif self._player_view is not None and self._player_view.current_video and (
            int(self._player_view.current_video["id"]) == video_id
        ):
            self._player_view.load_subtitle_file(Path(path))
        self.status_label.setText(f"已挂载字幕：{Path(path).name}")

    def _cancel_trim(self) -> None:
        if self.trim_worker and self.trim_worker.isRunning():
            self.trim_worker.requestInterruption()
            if self.trim_progress:
                self.trim_progress.setLabelText("正在取消裁剪...")

    def _trim_finished(self, success: bool, output: str, message: str) -> None:
        if self.trim_progress:
            self.trim_progress.close()
            self.trim_progress = None
        if success:
            library_id = getattr(self, "_trim_library_id", self.current_library_id)
            output_path = Path(output)
            library = self.db.library(int(library_id)) if library_id is not None else None
            mask_map = self.db.mask_map()
            masked_ext = mask_map.get(output_path.suffix.lower())
            if (
                library
                and library.get("mask_enabled")
                and masked_ext
            ):
                masked = output_path.with_suffix(masked_ext)
                if not masked.exists():
                    try:
                        output_path.rename(masked)
                        self.db.record_operation(
                            "mask",
                            str(output_path),
                            str(masked),
                            "success",
                        )
                        output_path = masked
                    except OSError:
                        pass
            QMessageBox.information(
                self,
                "裁剪完成",
                f"输出文件：\n{output_path}",
            )
            if library_id is not None:
                self.scan_library(int(library_id))
        else:
            QMessageBox.warning(self, "裁剪失败", message or "FFmpeg 执行失败")

    def _trim_thread_finished(self) -> None:
        worker = self.trim_worker
        if worker is not None:
            self.trim_worker = None
            worker.deleteLater()
        QTimer.singleShot(150, self._start_next_task)
        QTimer.singleShot(150, self._start_next_pending_watch_scan)

    def close_player(self) -> None:
        if self._player_window is not None:
            self._player_window.close()
        self._player_active = False
        self._update_status()

    def _player_window_closed(self) -> None:
        self._player_active = False
        self.refresh_sidebar_counts()
        self._flush_pending_tag_writes()
        self._flush_pending_rating_writes()
        if self._dirty_watch_directories:
            self._watch_debounce.start(300)
        else:
            QTimer.singleShot(150, self._start_next_pending_watch_scan)
        self._update_status()

    @staticmethod
    def _uses_masked_suffix(video: dict[str, Any]) -> bool:
        logical_ext = str(video.get("logical_ext") or "").lower()
        physical_ext = str(video.get("physical_ext") or "").lower()
        return bool(logical_ext and physical_ext and logical_ext != physical_ext)

    def _flush_pending_tag_writes(self) -> None:
        if not self._pending_tag_writes:
            return
        pending = self._pending_tag_writes
        self._pending_tag_writes = {}
        failed: list[str] = []
        written = 0
        masked = 0
        for video_id, tags in pending.items():
            video = self.db.video(video_id)
            if not video or not video.get("path"):
                continue
            if self._uses_masked_suffix(video):
                masked += 1
                continue
            ok, detail = write_video_tags(Path(str(video["path"])), tags)
            if ok:
                written += 1
            else:
                failed.append(f"{video.get('file_name')}：{detail}")
        if failed:
            QMessageBox.warning(
                self,
                "部分文件标签未写入",
                "\n".join(failed[:8])
                + ("\n…" if len(failed) > 8 else ""),
            )
        elif written or masked:
            detail = f"写入 {written} 个视频的标签元数据" if written else ""
            masked_detail = (
                f"{masked} 个伪装视频仅保存在影库"
                if masked
                else ""
            )
            self.status_label.setText(
                "播放器已关闭 · "
                + " · ".join(item for item in (detail, masked_detail) if item)
            )

    def _flush_pending_rating_writes(self) -> None:
        if not self._pending_rating_writes:
            return
        pending = self._pending_rating_writes
        self._pending_rating_writes = {}
        failed: list[str] = []
        written = 0
        masked = 0
        for video_id, rating in pending.items():
            video = self.db.video(video_id)
            if not video or not video.get("path"):
                continue
            if self._uses_masked_suffix(video):
                masked += 1
                continue
            ok, detail = write_video_rating(
                Path(str(video["path"])),
                rating,
            )
            if ok:
                written += 1
            else:
                failed.append(f"{video.get('file_name')}：{detail}")
        if failed:
            QMessageBox.warning(
                self,
                "部分评级未写入",
                "\n".join(failed[:8])
                + ("\n…" if len(failed) > 8 else ""),
            )
        elif written or masked:
            detail = f"写入 {written} 个视频的评级" if written else ""
            masked_detail = (
                f"{masked} 个伪装视频仅保存在影库"
                if masked
                else ""
            )
            self.status_label.setText(
                "播放器已关闭 · "
                + " · ".join(item for item in (detail, masked_detail) if item)
            )

    def save_position(self, video_id: int, position: int, watched: bool) -> None:
        if position <= 0 and not watched:
            return
        self.db.update_video(
            video_id,
            last_position_ms=position,
            last_played_at=utc_now(),
            watched=int(watched),
        )

    def set_video_rating(self, video_id: int, rating: int) -> None:
        value = float(rating) if rating else None
        video = self.db.video(video_id)
        if not video:
            return
        old_rating = video.get("rating")
        self.db.update_video(video_id, rating=value)
        video["rating"] = value
        updated_video = video
        self.sidebar.adjust_rating_count(old_rating, value)
        updated_visible = False
        for item in self.library_page.current_videos():
            if int(item["id"]) == int(video_id):
                item["rating"] = value
                updated_visible = True
                break
        if updated_visible:
            self.library_page.refresh_video(video_id)
        if self.selected_video_id == int(video_id):
            self.detail.set_video(updated_video)
        label = f"已评级 {'★' * rating}{'☆' * (5 - rating)}" if rating else "已清除评级"
        if self.current_metadata_filters.get("rating"):
            self.load_videos()
            self.library_page.select_video(video_id)
        if self._uses_masked_suffix(updated_video):
            self.status_label.setText(
                f"{label} · 已保存到影库（伪装视频不写外部元数据）"
            )
            return
        if (
            self._player_active
            and self._player_view is not None
            and self._player_view.current_video
            and int(self._player_view.current_video["id"]) == int(video_id)
        ):
            self._pending_rating_writes[video_id] = value
            self.status_label.setText(
                f"{label} · 侧边栏已更新，关闭播放器后写入文件"
            )
            return
        self.status_label.setText(f"{label} · 正在更新文件元数据…")
        QApplication.processEvents()
        written, detail = True, ""
        if updated_video.get("path"):
            written, detail = write_video_rating(
                Path(str(updated_video["path"])),
                value,
            )
        if written:
            self.status_label.setText(f"{label} · 已写入文件元数据")
        else:
            self.status_label.setText(f"{label} · 写入文件元数据失败：{detail}")

    def selected_video(self) -> dict[str, Any] | None:
        if self.selected_video_id is None:
            return None
        return self.db.video(self.selected_video_id)

    def _play_selected(self) -> None:
        if self.selected_video_id is not None:
            self.play_video(self.selected_video_id)

    def rename_selected(self, video_id: int) -> None:
        if self._player_active:
            return
        video = self.db.video(video_id)
        library = self.db.library(int(video["library_id"])) if video else None
        if not video or not library:
            return
        dialog = RenameDialog(video["stem"], self)
        if dialog.exec() != QDialog.Accepted:
            return
        new_stem = dialog.value()
        if new_stem == video["stem"]:
            return
        self._restore_runtime_assets()
        try:
            rename_video(self.db, video, library, new_stem)
        except Exception as exc:
            QMessageBox.critical(self, "重命名失败", str(exc))
            return
        self.refresh_all()
        self.library_page.select_video(video_id)
        self.status_label.setText("重命名完成")

    def move_selected(self, video_id: int) -> None:
        self.batch_move([video_id])

    def delete_selected(self, _video_id: int | None = None) -> None:
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放。")
            return
        selected = self.library_page.selected_videos()
        if not selected and self.selected_video_id is not None:
            video = self.db.video(self.selected_video_id)
            selected = [video] if video else []
        if selected and any(video.get("group_collapsed") for video in selected):
            selected = [
                video
                for video_id in self.library_page.selected_action_ids()
                if (video := self.db.video(video_id))
            ]
        self._delete_videos(selected)

    def _delete_videos(self, selected: list[dict[str, Any]]) -> None:
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放。")
            return
        if not selected:
            return
        missing_count = sum(
            1 for video in selected if existing_video_path(video) is None
        )
        if len(selected) == 1:
            video = selected[0]
            sidecars = len(video.get("sidecars") or [])
            dialog = DeleteDialog(
                video.get("title") or video["file_name"],
                sidecars,
                self,
                missing=missing_count == 1,
            )
            if dialog.exec() != QDialog.Accepted:
                return
            include_sidecars = dialog.include_sidecars()
        else:
            prompt = (
                f"确定移除选中的 {len(selected)} 条缺失记录？\n\n"
                "文件已经不存在；仍存在的关联文件也会移入回收站。"
                if missing_count == len(selected)
                else f"确定把选中的 {len(selected)} 个视频移入回收站？\n\n"
                "缺失视频只移除数据库记录；关联文件也会一并处理。"
            )
            result = QMessageBox.question(
                self,
                "批量移至回收站",
                prompt,
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if result != QMessageBox.Yes:
                return
            include_sidecars = True

        total_deleted = 0
        selected_ids = [int(video["id"]) for video in selected]
        failures: list[str] = []
        self._restore_runtime_assets()
        for video in selected:
            library = self.db.library(int(video["library_id"]))
            if not library:
                failures.append(f"{video.get('file_name')}: 资源库不存在")
                continue
            try:
                deleted, failed = delete_video(
                    self.db,
                    video,
                    library,
                    include_sidecars,
                )
                total_deleted += deleted
                failures.extend(failed)
            except Exception as exc:
                failures.append(f"{video.get('file_name')}: {exc}")
        removed_records = sum(
            1 for video_id in selected_ids if self.db.video(video_id) is None
        )
        self.refresh_all()
        if failures:
            QMessageBox.warning(
                self,
                "部分文件未删除",
                "\n".join(failures[:12]),
            )
        else:
            self.status_label.setText(
                f"已移入回收站 {total_deleted} 个文件 · "
                f"移除 {removed_records} 条记录"
            )

    def set_favorite(self, video_id: int, favorite: bool) -> None:
        self.db.update_video(video_id, favorite=int(favorite))
        self.load_videos()
        self.library_page.select_video(video_id)

    def set_category(self, video_id: int, category_id: object) -> None:
        video = self.db.video(video_id)
        if video is None:
            return
        if category_id is not None:
            category = self.db.category(int(category_id))
            if (
                category is None
                or int(category["library_id"] or 0)
                != int(video["library_id"])
            ):
                QMessageBox.warning(
                    self,
                    "分类不可用",
                    "只能为视频选择所属资源库内的分类。",
                )
                return
        self.db.update_video(
            video_id,
            category_id=int(category_id) if category_id is not None else None,
        )
        self.refresh_categories()
        self.load_videos()
        self.library_page.select_video(video_id)

    def set_tags(self, video_id: int, tags: object) -> None:
        values = [str(tag) for tag in list(tags)]
        self.db.set_video_tags(video_id, values)
        video = self.db.video(video_id)
        if (
            video
            and video.get("path")
            and not self._uses_masked_suffix(video)
        ):
            write_video_tags(Path(str(video["path"])), values)
        self.refresh_tags()
        self.refresh_metadata_filter_options()
        self.detail.set_tags(self.db.tags_for_video(video_id))
        self.load_videos()
        self.library_page.select_video(video_id)

    def batch_set_category(self, video_ids: object, category_id: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        category = int(category_id) if category_id is not None else None
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if category is not None:
            item = self.db.category(category)
            if item is None:
                return
            library_ids = {int(video["library_id"]) for video in videos}
            if library_ids != {int(item["library_id"] or 0)}:
                QMessageBox.warning(
                    self,
                    "不能跨库分类",
                    "所选视频必须来自同一资源库，并使用该资源库的分类。",
                )
                return
        for video_id in ids:
            self.db.update_video(video_id, category_id=category)
        self.refresh_categories()
        self.load_videos()
        self.status_label.setText(f"已更新 {len(ids)} 个视频的分类")

    def edit_tags(self, video_ids: object, parent=None) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if not videos:
            return
        batch = len(ids) > 1
        existing = [] if batch else self.db.tags_for_video(ids[0])
        available = [
            str(tag["name"])
            for tag in self.db.all_tags(self.scope_library_ids())
        ]
        dialog = TagsDialog(
            existing,
            parent or self,
            batch_count=len(ids) if batch else None,
            available=available,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        tags = dialog.values()
        written = 0
        deferred = 0
        masked = 0
        failed: list[str] = []
        write_jobs: list[tuple[dict[str, Any], list[str]]] = []
        playing_id = (
            int(self._player_view.current_video["id"])
            if self._player_active
            and self._player_view is not None
            and self._player_view.current_video
            else None
        )
        for video in videos:
            video_id = int(video["id"])
            if batch:
                merged = self.db.tags_for_video(video_id)
                target = list(dict.fromkeys([*merged, *tags]))
            else:
                target = tags
            self.db.set_video_tags(video_id, target)
            write_jobs.append((video, target))

        tag_snapshot = self.refresh_tags()
        self._sync_tag_filter_options(tag_snapshot)
        target_map = {
            int(video["id"]): target for video, target in write_jobs
        }
        for item in self.library_page.current_videos():
            video_id = int(item["id"])
            if video_id in target_map:
                item["tags"] = target_map[video_id]
                self.library_page.refresh_video(video_id)
        tag_filter_active = bool(
            self.current_tag or self.current_metadata_filters.get("tag")
        )
        if tag_filter_active:
            self.load_videos()
        self.library_page.select_video(ids[0])
        if self.selected_video_id in ids:
            self.detail.set_tags(self.db.tags_for_video(self.selected_video_id))
        if self._player_view is not None and self._player_view.current_video:
            current_playing_id = int(self._player_view.current_video["id"])
            if current_playing_id in ids:
                self._player_view.set_video_tags(
                    self.db.tags_for_video(current_playing_id)
                )
        self.status_label.setText(
            f"已更新 {len(ids)} 个视频的标签 · 正在同步文件元数据…"
        )
        QApplication.processEvents()

        for video, target in write_jobs:
            video_id = int(video["id"])
            if not video.get("path"):
                continue
            if self._uses_masked_suffix(video):
                masked += 1
                continue
            if playing_id == video_id:
                self._pending_tag_writes[video_id] = target
                deferred += 1
                continue
            ok, detail = write_video_tags(
                Path(str(video["path"])),
                target,
            )
            if ok:
                written += 1
            else:
                failed.append(f"{video.get('file_name')}：{detail}")
        message = f"已更新 {len(ids)} 个视频的标签"
        if written:
            message += f" · 已写入文件元数据 {written} 个"
        if deferred:
            message += f" · 播放中的 {deferred} 个将在关闭播放器后写入"
        if masked:
            message += f" · {masked} 个伪装视频仅保存在影库"
        if failed:
            message += f" · {len(failed)} 个写入失败"
        self.status_label.setText(message)
        if failed:
            QMessageBox.warning(
                self,
                "部分文件标签未写入",
                "\n".join(failed[:8])
                + ("\n…" if len(failed) > 8 else ""),
            )

    def _edit_tags_from_player(self, video_id: int) -> None:
        """播放器里点“标签”：对话框挂到播放窗口上，避免被全屏遮住。"""
        self.edit_tags([video_id], parent=self.player_window)

    def batch_favorite(self, video_ids: object, favorite: bool) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        for video_id in ids:
            self.db.update_video(video_id, favorite=int(favorite))
        self.load_videos()
        self.status_label.setText(
            f"已{'收藏' if favorite else '取消收藏'} {len(ids)} 个视频"
        )

    def batch_move(self, video_ids: object) -> None:
        if self._player_active:
            QMessageBox.information(self, "正在播放", "请先停止播放再移动文件。")
            return
        ids = [int(video_id) for video_id in list(video_ids)]
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        if not videos:
            return
        library_ids = {int(video["library_id"]) for video in videos}
        if len(library_ids) != 1:
            QMessageBox.information(
                self,
                "不能跨资源库移动",
                "请选择同一个资源库内的视频后再移动。",
            )
            return
        library = self.db.library(next(iter(library_ids)))
        if not library:
            return
        target = QFileDialog.getExistingDirectory(
            self,
            "选择目标目录（必须位于当前资源库内）",
            str(Path(videos[0]["path"]).parent),
        )
        if not target:
            return
        if not is_within(Path(target), Path(library["root_path"])):
            QMessageBox.warning(
                self,
                "目标超出资源库",
                "只能移动到当前资源库的根目录或其子目录。",
            )
            return
        moved = 0
        failures: list[str] = []
        self._restore_runtime_assets()
        for video in videos:
            try:
                move_video(self.db, video, library, Path(target))
                moved += 1
            except Exception as exc:
                failures.append(f"{video.get('file_name')}: {exc}")
        self.refresh_all()
        if failures:
            QMessageBox.warning(
                self,
                "部分文件未移动",
                "\n".join(failures[:12]),
            )
        else:
            self.status_label.setText(f"已移动 {moved} 个视频")

    def batch_delete(self, video_ids: object) -> None:
        ids = [int(video_id) for video_id in list(video_ids)]
        videos = [video for video_id in ids if (video := self.db.video(video_id))]
        self._delete_videos(videos)

    def add_category(self) -> None:
        if self.current_library_id in {None, ALL_LIBRARIES}:
            QMessageBox.information(
                self,
                "请选择资源库",
                "分类跟随资源库管理，请先选择一个具体资源库。",
            )
            return
        self._show_create_category(None, "新建分类")

    def add_subcategory(self, parent_id: int) -> None:
        self._show_create_category(parent_id, "新建子分类")

    def _show_create_category(
        self,
        selected_parent_id: int | None,
        title: str,
    ) -> None:
        parent_category = (
            self.db.category(selected_parent_id)
            if selected_parent_id is not None
            else None
        )
        if selected_parent_id is not None and parent_category is None:
            return
        if parent_category is not None:
            library_id = int(parent_category["library_id"] or 0)
        else:
            if self.current_library_id in {None, ALL_LIBRARIES}:
                QMessageBox.information(
                    self,
                    "请选择资源库",
                    "分类跟随资源库管理，请先选择一个具体资源库。",
                )
                return
            library_id = int(self.current_library_id)
        categories = self.db.categories([library_id])
        labels = [(item["id"], item["name"]) for item in categories]
        dialog = CategoryDialog(
            labels,
            selected_parent_id=selected_parent_id,
            title_text=title,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        name, parent_id = dialog.values()
        if not name:
            return
        try:
            self.db.create_category(
                name,
                parent_id,
                library_id=library_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "分类创建失败", str(exc))
            return
        self.refresh_categories()

    def rename_category(self, category_id: int) -> None:
        category = next(
            (item for item in self.db.categories() if item["id"] == category_id),
            None,
        )
        if not category:
            return
        name, ok = QInputDialog.getText(
            self,
            "重命名分类",
            "分类名称",
            text=category["name"],
        )
        if not ok or not name.strip() or name.strip() == category["name"]:
            return
        try:
            self.db.rename_category(category_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "重命名失败", str(exc))
            return
        self.refresh_categories()

    def delete_category(self, category_id: int) -> None:
        categories = self.db.categories()
        category = next((item for item in categories if item["id"] == category_id), None)
        if not category:
            return
        child_count = sum(
            1 for item in categories if item.get("parent_id") == category_id
        )
        message = (
            f"确定删除分类“{category['name']}”？\n\n"
            f"该分类下的视频会变为未分类，资源文件不会受影响。"
        )
        if child_count:
            message += f"\n同时会删除 {child_count} 个直接子分类。"
        result = QMessageBox.question(
            self,
            "删除分类",
            message,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        self.db.delete_category(category_id)
        if self.current_category_id == category_id:
            self.current_category_id = None
        self.refresh_categories()
        self.load_videos()

    def delete_tag(self, tag: str) -> None:
        tag = tag.strip()
        if not tag:
            return
        item = next(
            (
                candidate
                for candidate in self.db.all_tags()
                if str(candidate["name"]).casefold() == tag.casefold()
            ),
            None,
        )
        count = int(item.get("video_count") or 0) if item else 0
        result = QMessageBox.question(
            self,
            "删除标签",
            f"确定删除标签“{tag}”？\n\n"
            f"该标签会从影库中 {count} 个相关视频里移除，并永久隐藏。\n"
            "视频文件、图片和 movie.nfo 不会被修改。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        self.db.hide_tag(tag)
        if self.current_tag and self.current_tag.casefold() == tag.casefold():
            self.current_tag = None
            self.sidebar.set_view_mode("all")
        if (
            self.current_metadata_filters.get("tag", "").casefold()
            == tag.casefold()
        ):
            self.current_metadata_filters.pop("tag", None)
            self.library_page.set_active_metadata_filters(
                self.current_metadata_filters
            )
        if (
            self.current_nfo_filters.get("nfo_tag", "").casefold()
            == tag.casefold()
        ):
            self.current_nfo_filters.pop("nfo_tag", None)
            self.library_page.set_active_filters(self.current_nfo_filters)
        self.refresh_tags()
        self.refresh_filter_options()
        self.refresh_metadata_filter_options()
        self.load_videos()
        self.status_label.setText(f"已从影库删除标签 · {tag}")

    def rename_tag(self, tag: str) -> None:
        tag = tag.strip()
        if not tag:
            return
        name, ok = QInputDialog.getText(
            self,
            "重命名标签",
            "标签名称",
            text=tag,
        )
        name = name.strip()
        if not ok or not name or name.casefold() == tag.casefold():
            return
        self.db.rename_tag(tag, name)
        if self.current_tag and self.current_tag.casefold() == tag.casefold():
            self.current_tag = name
        if (
            self.current_metadata_filters.get("tag", "").casefold()
            == tag.casefold()
        ):
            self.current_metadata_filters["tag"] = name
            self.library_page.set_active_metadata_filters(
                self.current_metadata_filters
            )
        if (
            self.current_nfo_filters.get("nfo_tag", "").casefold()
            == tag.casefold()
        ):
            self.current_nfo_filters["nfo_tag"] = name
            self.library_page.set_active_filters(self.current_nfo_filters)
        self.refresh_tags()
        if self.current_tag:
            self.sidebar.select_tag_name(self.current_tag)
        self.refresh_filter_options()
        self.refresh_metadata_filter_options()
        self.load_videos()
        self.status_label.setText(f"标签已重命名 · {tag} → {name}")

    def show_operation_log(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("操作日志")
        dialog.resize(920, 540)
        layout = QVBoxLayout(dialog)
        table = QTableWidget()
        rows = self.db.operations()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["时间", "类型", "状态", "源路径", "目标 / 详情"])
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            target = row.get("target_path") or row.get("detail") or ""
            values = (
                row.get("created_at", ""),
                row.get("kind", ""),
                row.get("status", ""),
                row.get("source_path", ""),
                target,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                table.setItem(row_index, column, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._teardown_session_lock_monitor()
        self._watch_debounce.stop()
        self.file_watcher.blockSignals(True)
        watched = self.file_watcher.directories()
        if watched:
            self.file_watcher.removePaths(watched)
        if self._player_window is not None:
            self._player_window.close()
        if self._player_view is not None:
            self._player_view.shutdown()
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.requestInterruption()
            self.scan_worker.wait(2500)
        if self.subtitle_worker and self.subtitle_worker.isRunning():
            self.subtitle_worker.cancel()
            self.subtitle_worker.wait(6000)
        if self.mask_worker and self.mask_worker.isRunning():
            self.mask_worker.requestInterruption()
            self.mask_worker.wait(2500)
        if self.export_worker and self.export_worker.isRunning():
            self.export_worker.requestInterruption()
            self.export_worker.wait(6000)
        if self.trim_worker and self.trim_worker.isRunning():
            self.trim_worker.requestInterruption()
            self.trim_worker.wait(5000)
        self._restore_runtime_assets()
        self.db.close()
        event.accept()

    def _restore_runtime_assets(self) -> None:
        restore_thumbnail_file_names(self._thumbnail_dir)
        self.artwork.restore()
