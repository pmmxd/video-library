from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from PySide6.QtCore import QEvent, QPoint, QThread, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMessageBox,
    QProgressBar,
    QToolButton,
)

from app import scanner as scanner_module
from app import media_tools as media_tools_module
from app import translation as translation_module
from app.artwork import ArtworkRuntime, remove_cached_thumbnails
from app.constants import (
    ALL_LIBRARIES,
    DEFAULT_MASK_MAP,
    THUMB_EXTENSION,
    THUMB_DIR_NAME,
    THUMB_RUNTIME_EXTENSION,
    enable_thumbnail_runtime_names,
    restore_thumbnail_file_names,
    runtime_artwork_path,
)
from app.database import Database
from app.media_tools import (
    collect_sidecars,
    capture_at_from_filename,
    existing_video_path,
    find_poster,
    find_preview,
    identity_key,
    is_jpeg,
    logical_extension,
    parse_nfo,
    parse_timecode,
    sniff_media,
    thumbnail_for,
)
from app.operations import (
    ExportWorker,
    MaskWorker,
    delete_video,
    is_within,
    move_video,
    rename_video,
)
from app.security import (
    WM_WTSSESSION_CHANGE,
    WTS_SESSION_LOCK,
    WindowsSessionLockFilter,
    create_password_record,
    verify_password,
)
from app.scanner import ScanWorker, content_hash
from app.subtitles import SubtitleCue, cue_at, find_default_subtitle, format_srt, parse_subtitle
from app.transcribe_child import _event_line, _flush_stdio, transcribe_job
from app.transcription import (
    SubtitleWorker,
    subtitle_command,
    subtitle_target,
    transcription_worker_command,
    validate_whisper_installation,
)
from app.translation import translate_cues
from app.ui.dialogs import (
    DeleteDialog,
    MetadataFilterDialog,
    NfoFilterDialog,
    SettingsDialog,
    TagsDialog,
)
from app.ui.library_view import LibraryPage
from app.ui.icons import icon_path
from app.ui.models import VideoListModel, VideoRoles
from app.ui.player_view import PlayerView, PlayerWindow
from app.ui.detail_panel import DetailPanel
from app.ui.main_window import MainWindow, TaskQueueItem
from app.ui.sidebar import COUNT_ROLE, Sidebar
from app.ui.task_center import TaskCenterDialog
from app.ui.timeline_view import TimelineView
from app.ui.widgets import ClickableSlider, video_has_subtitle
from migrate_library import migrate_library


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.db = Database(Path(self.temp.name) / "test.db")
        self.library_id = self.db.add_library("Test", self.root)
        self.library = self.db.library(self.library_id)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def _add_video(
        self,
        path: Path,
        logical: str = ".mp4",
        width: int = 1920,
        height: int = 1080,
        **fields: object,
    ) -> int:
        data = {
            "library_id": self.library_id,
            "path": str(path),
            "identity_key": identity_key(path),
            "relative_path": path.name,
            "file_name": path.name,
            "stem": path.stem,
            "logical_ext": logical,
            "physical_ext": path.suffix.lower(),
            "title": "Demo",
            "duration_ms": 120_000,
            "width": width,
            "height": height,
            "content_hash": content_hash(path),
            "size_bytes": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
            "sidecars": [],
        }
        data.update(fields)
        return self.db.upsert_video(data, "test")

    def test_orientation_filter_and_portrait_poster_preference(self) -> None:
        landscape = self.root / "landscape.mp4"
        portrait = self.root / "portrait.mp4"
        landscape.write_bytes(b"\x00\x00\x00\x18ftypisom-landscape")
        portrait.write_bytes(b"\x00\x00\x00\x18ftypisom-portrait")
        landscape_id = self._add_video(landscape, width=1920, height=1080)
        portrait_id = self._add_video(portrait, width=1080, height=1920)

        self.assertEqual(
            [row["id"] for row in self.db.videos(orientation="landscape")],
            [landscape_id],
        )
        self.assertEqual(
            [row["id"] for row in self.db.videos(orientation="portrait")],
            [portrait_id],
        )

        fanart = self.root / "fanart.jpg"
        poster = self.root / "poster.jpg"
        self._write_jpeg(fanart, (640, 360), "#3355cc")
        self._write_jpeg(poster, (360, 640), "#cc5533")
        video = self.db.video(portrait_id)
        video["thumb_path"] = str(fanart)
        video["poster_path"] = str(poster)
        model = VideoListModel()
        model.set_videos([video])
        self.assertEqual(model.index(0, 0).data(VideoRoles.ThumbPath), str(poster))

        app = QApplication.instance() or QApplication([])
        page = LibraryPage()
        page._orientation_clicked("portrait")
        self.assertEqual(page.orientation(), "portrait")
        page.set_orientation("all")
        self.assertEqual(page.orientation(), "all")

    def test_all_library_heading_remains_visible(self) -> None:
        app = QApplication.instance() or QApplication([])
        page = LibraryPage()
        page.set_title("全部资源库")
        self.assertFalse(page.title_label.isHidden())
        self.assertFalse(page.count_label.isHidden())
        self.assertEqual(page.title_label.text(), "全部资源库")
        page.deleteLater()
        app.processEvents()
        page.deleteLater()
        app.processEvents()

    def test_orientation_uses_rotation_not_display_aspect_ratio(self) -> None:
        self.assertEqual(
            media_tools_module._display_dimensions(
                {
                    "width": 2336,
                    "height": 1080,
                    "side_data_list": [{"rotation": -90}],
                }
            ),
            (1080, 2336),
        )
        self.assertEqual(
            media_tools_module._display_dimensions(
                {
                    "width": 1920,
                    "height": 1080,
                    "display_aspect_ratio": "9:16",
                }
            ),
            (1920, 1080),
        )
        self.assertEqual(
            media_tools_module._display_dimensions(
                {
                    "width": 1920,
                    "height": 1080,
                    "tags": {"rotate": "90"},
                    "display_aspect_ratio": "16:9",
                }
            ),
            (1080, 1920),
        )

    def test_card_size_slider_fills_available_width(self) -> None:
        app = QApplication.instance() or QApplication([])
        page = LibraryPage()
        page.resize(1000, 720)
        page.show()
        app.processEvents()

        page.card_size_slider.setValue(0)
        page._relayout_cards()
        small = page.list_view.gridSize()
        small_card = page.card_delegate.sizeHint(None, None)
        page.card_size_slider.setValue(100)
        page._relayout_cards()
        large = page.list_view.gridSize()
        large_card = page.card_delegate.sizeHint(None, None)

        available = page.list_view.viewport().width()
        columns = max(1, available // large.width())
        self.assertGreater(large.width(), small.width())
        self.assertGreater(large_card.width(), small_card.width())
        self.assertGreater(large_card.height(), small_card.height())
        self.assertGreaterEqual(columns * large.width(), available * 0.95)
        self.assertGreater((columns + 1) * large.width(), available)
        page.close()
        app.processEvents()

    def test_video_subtitle_badge_flag_uses_db_and_sidecars(self) -> None:
        self.assertFalse(video_has_subtitle({"sidecars": ["movie.nfo"]}))
        self.assertTrue(
            video_has_subtitle({"subtitle_path": "media/movie.srt"})
        )
        self.assertTrue(
            video_has_subtitle(
                {
                    "sidecars": [
                        "media/movie.nfo",
                        "media/movie.zh-CN.srt",
                    ]
                }
            )
        )

    def test_thumbnail_and_disguised_artwork_load_directly(self) -> None:
        app = QApplication.instance() or QApplication([])
        thumbnail = self.root / f"sample{THUMB_EXTENSION}"
        artwork = self.root / "poster.txt"
        self._write_jpeg(thumbnail, (320, 180), "#3355cc")
        self._write_jpeg(artwork, (180, 320), "#cc5533")
        self.assertFalse(QPixmap(thumbnail).isNull())
        self.assertFalse(QPixmap(artwork).isNull())
        app.processEvents()

    def test_all_internal_icons_are_valid_svg_assets(self) -> None:
        app = QApplication.instance() or QApplication([])
        icons = sorted(icon_path("all.svg").parent.glob("*.svg"))
        self.assertGreaterEqual(len(icons), 10)
        for path in icons:
            pixmap = QIcon(str(path)).pixmap(32, 32)
            self.assertFalse(pixmap.isNull(), path.name)
        app.processEvents()

    def test_subtitle_overlay_updates_in_video_surface(self) -> None:
        app = QApplication.instance() or QApplication([])
        subtitle = self.root / "subtitle.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:04,000\n测试字幕\n",
            encoding="utf-8",
        )
        player = PlayerView()
        player.resize(900, 620)
        player.show()
        player.set_subtitle_font_size(30)
        self.assertIn("font-size: 30px", player.subtitle_label.styleSheet())
        player.load_subtitle_file(subtitle)
        player._update_subtitle(1000)
        app.processEvents()
        self.assertEqual(player.subtitle_label.text(), "测试字幕")
        self.assertTrue(player.subtitle_label.isVisible())
        self.assertLess(
            player.subtitle_label.width(),
            player.surface.viewport().width() * 0.5,
        )
        self.assertLessEqual(
            player.subtitle_label.geometry().right(),
            player.surface.viewport().rect().right(),
        )
        player.shutdown()
        player.close()
        app.processEvents()

    def test_clickable_slider_uses_one_stable_click_position(self) -> None:
        app = QApplication.instance() or QApplication([])
        slider = ClickableSlider(Qt.Horizontal)
        slider.setRange(0, 1000)
        slider.resize(300, 30)
        slider.show()
        app.processEvents()
        moved = []
        slider.sliderMoved.connect(moved.append)
        QTest.mouseClick(
            slider,
            Qt.LeftButton,
            pos=QPoint(225, slider.height() // 2),
        )
        app.processEvents()
        self.assertGreater(slider.value(), 600)
        self.assertLess(slider.value(), 900)
        self.assertTrue(moved)
        self.assertEqual(min(moved), max(moved))
        slider.deleteLater()
        app.processEvents()

    def test_seek_display_holds_target_until_decoder_catches_up(self) -> None:
        app = QApplication.instance() or QApplication([])
        player = PlayerView()
        player.timeline.setRange(0, 120_000)
        player._pending_seek_ms = 80_000
        player._position_changed(20_000)
        self.assertEqual(player.timeline.value(), 80_000)
        player._pending_seek_ms = None
        player._seek_target_ms = 80_000
        player._seeking_until = 1e12
        player._position_changed(20_000)
        self.assertEqual(player.timeline.value(), 80_000)
        player._position_changed(80_500)
        self.assertEqual(player.timeline.value(), 80_500)
        self.assertIsNone(player._seek_target_ms)
        player.shutdown()
        player.close()
        app.processEvents()

    def test_player_end_behavior_stop_loop_and_next(self) -> None:
        app = QApplication.instance() or QApplication([])
        view = PlayerView()
        view.queue = [
            {"id": 1, "path": str(self.root / "one.mp4")},
            {"id": 2, "path": str(self.root / "two.mp4")},
        ]
        view.index = 0
        view.current_video = view.queue[0]
        media_player = MagicMock()
        view.player = media_player

        with (
            patch.object(view, "_save_position"),
            patch.object(view, "_restore_exposed") as restore,
            patch.object(view, "next") as next_video,
        ):
            view.set_end_behavior("loop")
            view._media_status(QMediaPlayer.EndOfMedia)
            media_player.setPosition.assert_called_once_with(0)
            media_player.play.assert_called_once()
            next_video.assert_not_called()
            restore.assert_not_called()

            media_player.reset_mock()
            view.set_end_behavior("next")
            view._media_status(QMediaPlayer.EndOfMedia)
            next_video.assert_called_once()
            media_player.play.assert_not_called()
            restore.assert_not_called()

            view.set_end_behavior("stop")
            view._media_status(QMediaPlayer.EndOfMedia)
            restore.assert_called_once()

        view.shutdown()
        view.close()
        app.processEvents()

    def test_player_reports_missing_video(self) -> None:
        app = QApplication.instance() or QApplication([])
        view = PlayerView()
        missing_id = 321
        video = {
            "id": missing_id,
            "path": str(self.root / "missing.mp4"),
            "logical_ext": ".mp4",
            "physical_ext": ".mp4",
        }
        emitted: list[int] = []
        view.video_missing.connect(emitted.append)

        view.play_video(video, [video])

        self.assertEqual(emitted, [missing_id])
        self.assertEqual(view.state_label.text(), "文件缺失")
        self.assertIn("文件缺失", view.error_label.text())
        self.assertFalse(view.error_label.isHidden())
        view.shutdown()
        view.close()
        app.processEvents()

    def test_main_window_removes_record_when_play_file_does_not_exist(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "deleted-before-play.mp4"
        video_path.write_bytes(b"video")
        video_id = self._add_video(video_path)
        video_path.unlink()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            with patch("app.ui.main_window.QMessageBox.warning") as warning:
                window.play_video(video_id)
            warning.assert_called_once()
            self.assertIsNone(self.db.video(video_id))
            self.assertNotIn("missing", window.sidebar._special_items)
            self.assertEqual(window.library_page.current_videos(), [])
        finally:
            window.close()
            app.processEvents()

    def test_startup_cleans_legacy_missing_flags(self) -> None:
        app = QApplication.instance() or QApplication([])
        missing_path = self.root / "clean-missing.mp4"
        recovered_path = self.root / "clean-recovered.mp4"
        missing_path.write_bytes(b"missing")
        recovered_path.write_bytes(b"recovered")
        missing_id = self._add_video(missing_path)
        recovered_id = self._add_video(recovered_path)
        self.db.update_video(missing_id, missing=1)
        self.db.update_video(recovered_id, missing=1)
        missing_path.unlink()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            self.assertIsNone(self.db.video(missing_id))
            recovered = self.db.video(recovered_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["missing"], 0)
            self.assertTrue(recovered_path.exists())
            self.assertEqual(window.db.missing_videos([self.library_id]), [])
        finally:
            window.close()
            app.processEvents()

    def test_settings_dialog_exposes_playback_end_behavior(self) -> None:
        app = QApplication.instance() or QApplication([])
        dialog = SettingsDialog(
            None,
            False,
            playback_end_behavior="next",
            auto_lock_minutes=25,
        )
        self.assertEqual(dialog.playback_end_behavior(), "next")
        self.assertEqual(dialog.auto_lock_minutes(), 25)
        self.assertIn("关闭软件", dialog.lock_rules_label.text())
        self.assertIn("Windows 锁屏或注销", dialog.lock_rules_label.text())
        self.assertIn("切换资源库：保留", dialog.lock_rules_label.text())
        dialog.playback_end_combo.setCurrentIndex(
            dialog.playback_end_combo.findData("loop")
        )
        self.assertEqual(dialog.playback_end_behavior(), "loop")
        dialog.auto_lock_spin.setValue(0)
        self.assertEqual(dialog.auto_lock_minutes(), 0)
        dialog.deleteLater()
        app.processEvents()

    def test_settings_dialog_edits_global_mask_map(self) -> None:
        app = QApplication.instance() or QApplication([])
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mp4"] = ".asset"
        dialog = SettingsDialog(None, False, mask_map=custom)
        self.assertEqual(dialog.mask_extension_edits[".mp4"].text(), ".asset")
        self.assertEqual(
            set(dialog.mask_extension_map()),
            set(DEFAULT_MASK_MAP),
        )
        dialog.mask_extension_edits[".mp4"].setText("media")
        self.assertEqual(dialog.mask_extension_map()[".mp4"], ".media")
        dialog.deleteLater()
        app.processEvents()

    def test_main_window_loads_saved_playback_end_behavior(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.set_setting("playback_end_behavior", "loop")
        window = MainWindow(self.db.path)
        try:
            self.assertEqual(window._playback_end_behavior, "loop")
        finally:
            window.close()
            app.processEvents()

    def test_settings_renames_current_library(self) -> None:
        app = QApplication.instance() or QApplication([])
        window = MainWindow(self.db.path)

        class FakeSettingsDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.Accepted

            def library_name_value(self):
                return "新的资源库名称"

            def subtitle_font_size(self):
                return 22

            def playback_end_behavior(self):
                return "stop"

            def show_detail_panel(self):
                return True

            def project_kind(self):
                return "auto"

            def isolated(self):
                return False

            def mask_enabled(self):
                return False

            def mask_extension_map(self):
                return dict(DEFAULT_MASK_MAP)

            def auto_lock_minutes(self):
                return 25

        try:
            window.select_library(self.library_id)
            with patch(
                "app.ui.main_window.SettingsDialog",
                FakeSettingsDialog,
            ):
                window.open_settings()
            self.assertEqual(
                self.db.library(self.library_id)["name"],
                "新的资源库名称",
            )
            self.assertIn(
                "新的资源库名称",
                window.sidebar.library_combo.currentText(),
            )
            self.assertEqual(
                window.top_bar.scope_label.text(),
                "新的资源库名称",
            )
            self.assertEqual(window._auto_lock_minutes, 25)
            self.assertEqual(
                self.db.setting("isolated_auto_lock_minutes"),
                "25",
            )
        finally:
            window.close()
            app.processEvents()

    def test_settings_mask_map_queues_enabled_libraries(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_mask(self.library_id, True)
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mp4"] = ".asset"

        class FakeSettingsDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.Accepted

            def library_name_value(self):
                return "Test"

            def mask_extension_map(self):
                return dict(custom)

            def subtitle_font_size(self):
                return 22

            def playback_end_behavior(self):
                return "stop"

            def show_detail_panel(self):
                return True

            def project_kind(self):
                return "auto"

            def isolated(self):
                return False

            def mask_enabled(self):
                return True

            def auto_lock_minutes(self):
                return 15

        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            with (
                patch(
                    "app.ui.main_window.SettingsDialog",
                    FakeSettingsDialog,
                ),
                patch.object(window, "_start_mask") as start_mask,
            ):
                window.open_settings()
            self.assertEqual(self.db.mask_map()[".mp4"], ".asset")
            start_mask.assert_called_once_with(self.library_id, True)
        finally:
            window.close()
            app.processEvents()

    def test_isolated_idle_lock_clears_both_levels(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            window._auto_lock_minutes = 1
            window._last_activity = 1.0
            with (
                patch.object(window, "isActiveWindow", return_value=True),
                patch("app.ui.main_window.time.monotonic", return_value=1000.0),
            ):
                window._arm_auto_lock()
            self.assertFalse(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, set())
            self.assertEqual(window.current_library_id, ALL_LIBRARIES)
            self.assertIn("闲置超时", window.status_label.text())
        finally:
            window.close()
            app.processEvents()

    def test_minimized_window_does_not_lock_isolated_library(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            with patch.object(window, "isMinimized", return_value=True):
                window.changeEvent(QEvent(QEvent.WindowStateChange))
            self.assertTrue(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, {self.library_id})
        finally:
            window.close()
            app.processEvents()

    def test_zero_auto_lock_minutes_disables_idle_rule(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            window._auto_lock_minutes = 0
            window._last_activity = 1.0
            with (
                patch.object(window, "isActiveWindow", return_value=True),
                patch("app.ui.main_window.time.monotonic", return_value=1000.0),
            ):
                window._arm_auto_lock()
            self.assertTrue(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, {self.library_id})
            self.assertFalse(window._idle_timer.isActive())
        finally:
            window.close()
            app.processEvents()

    def test_idle_lock_pauses_when_app_is_not_active(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window._auto_lock_minutes = 1
            window._last_activity = 1.0
            with (
                patch.object(window, "isActiveWindow", return_value=False),
                patch("app.ui.main_window.time.monotonic", return_value=1000.0),
            ):
                window._arm_auto_lock()
            self.assertTrue(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, {self.library_id})
            self.assertFalse(window._idle_timer.isActive())
        finally:
            window.close()
            app.processEvents()

    def test_active_task_defers_isolated_lock_until_queue_finishes(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            window._active_task = TaskQueueItem(
                "subtitle-test",
                "subtitle",
                {},
            )
            window._lock_isolated_libraries("Windows 已锁屏或注销")
            self.assertTrue(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, {self.library_id})
            self.assertEqual(
                window._pending_lock_reason,
                "Windows 已锁屏或注销",
            )
            window._active_task = None
            window._finish_task_activity()
            self.assertFalse(window.isolated_visibility)
            self.assertEqual(window.unlocked_libraries, set())
            self.assertEqual(window.current_library_id, ALL_LIBRARIES)
        finally:
            window.close()
            app.processEvents()

    def test_scan_and_subtitle_jobs_dispatch_serially(self) -> None:
        app = QApplication.instance() or QApplication([])
        window = MainWindow(self.db.path)
        scan = TaskQueueItem("scan-test", "scan", {})
        subtitle = TaskQueueItem("subtitle-test", "subtitle", {})
        window._task_queue = [scan, subtitle]
        try:
            with (
                patch.object(window.task_center, "run_task"),
                patch.object(window, "_run_scan_task") as run_scan,
                patch.object(window, "_run_subtitle_task") as run_subtitle,
            ):
                window._start_next_task()
                self.assertEqual(window._active_task, scan)
                run_scan.assert_called_once_with(scan)
                run_subtitle.assert_not_called()
                window._active_task = None
                window._start_next_task()
                self.assertEqual(window._active_task, subtitle)
                run_subtitle.assert_called_once_with(subtitle)
        finally:
            window._active_task = None
            window._task_queue.clear()
            window.close()
            app.processEvents()

    def test_switching_library_preserves_second_level_unlock(self) -> None:
        app = QApplication.instance() or QApplication([])
        other_root = self.root / "other"
        other_root.mkdir()
        other_id = self.db.add_library("Other", other_root)
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            window.select_library(other_id)
            self.assertEqual(
                window.unlocked_libraries,
                {self.library_id},
            )
            window.select_library(ALL_LIBRARIES)
            self.assertEqual(
                window.unlocked_libraries,
                {self.library_id},
            )
            self.assertTrue(window.isolated_visibility)
        finally:
            window.close()
            app.processEvents()

    def test_library_switch_reuses_filter_option_cache(self) -> None:
        app = QApplication.instance() or QApplication([])
        other_root = self.root / "cache-other"
        other_root.mkdir()
        other_id = self.db.add_library("Cache Other", other_root)
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window.select_library(other_id)
            with (
                patch.object(
                    window.db,
                    "nfo_filter_options",
                    side_effect=AssertionError("NFO cache should be reused"),
                ),
                patch.object(
                    window.db,
                    "metadata_filter_options",
                    side_effect=AssertionError("metadata cache should be reused"),
                ),
            ):
                window.select_library(self.library_id)
        finally:
            window.close()
            app.processEvents()

    def test_manual_refresh_scans_when_watcher_has_no_dirty_paths(self) -> None:
        app = QApplication.instance() or QApplication([])
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window._dirty_watch_directories.clear()
            window._pending_incremental_scans.clear()
            with patch.object(window, "scan_library") as scan:
                window.refresh_incremental_current()
            scan.assert_called_once_with(self.library_id, refresh=True)
            self.assertIn("正在检查", window.status_label.text())
        finally:
            window.close()
            app.processEvents()

    def test_idle_lock_rebuilds_library_switcher(self) -> None:
        app = QApplication.instance() or QApplication([])
        other_root = self.root / "other"
        other_root.mkdir()
        other_id = self.db.add_library("Other", other_root)
        self.db.update_library_isolated(self.library_id, True)
        window = MainWindow(self.db.path)
        try:
            window.isolated_visibility = True
            window.unlocked_libraries.add(self.library_id)
            window.current_library_id = self.library_id
            window.refresh_all()
            self.assertIn(
                self.library_id,
                [
                    window.sidebar.library_combo.itemData(index)
                    for index in range(window.sidebar.library_combo.count())
                ],
            )
            window._lock_isolated_libraries("闲置超时")
            library_ids = [
                window.sidebar.library_combo.itemData(index)
                for index in range(window.sidebar.library_combo.count())
            ]
            self.assertNotIn(self.library_id, library_ids)
            self.assertIn(other_id, library_ids)
            other_index = window.sidebar.library_combo.findData(other_id)
            window.sidebar.library_combo.setCurrentIndex(other_index)
            app.processEvents()
            self.assertEqual(window.current_library_id, other_id)
        finally:
            window.close()
            app.processEvents()

    def test_left_sidebar_filters_replace_previous_filter(self) -> None:
        app = QApplication.instance() or QApplication([])
        for folder in ("A", "B"):
            (self.root / folder).mkdir()
        first_path = self.root / "A" / "first.mp4"
        second_path = self.root / "B" / "second.mp4"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")
        first_id = self._add_video(first_path)
        second_id = self._add_video(second_path)
        first_category = self.db.create_category("分类 A")
        second_category = self.db.create_category("分类 B")
        self.db.update_video(
            first_id,
            relative_path=str(Path("A") / "first.mp4"),
            category_id=first_category,
            rating=5,
        )
        self.db.update_video(
            second_id,
            relative_path=str(Path("B") / "second.mp4"),
            category_id=second_category,
            rating=3,
        )
        self.db.set_video_tags(second_id, ["标签 B"])
        self.db.conn.execute(
            "UPDATE videos SET actors_json = ? WHERE id = ?",
            (
                json.dumps([{"name": "演员 A", "role": ""}], ensure_ascii=False),
                first_id,
            ),
        )
        self.db.conn.execute(
            "UPDATE videos SET actors_json = ? WHERE id = ?",
            (
                json.dumps([{"name": "演员 B", "role": ""}], ensure_ascii=False),
                second_id,
            ),
        )
        self.db.conn.commit()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window.select_category(first_category)
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [first_id],
            )
            window.select_category(second_category)
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [second_id],
            )
            window.select_rating(5)
            self.assertIsNone(window.current_category_id)
            self.assertEqual(window.current_metadata_filters, {"rating": "5 星"})
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [first_id],
            )
            window.select_actor("演员 B")
            self.assertEqual(window.current_metadata_filters, {})
            self.assertEqual(window.current_nfo_filters, {"actor": "演员 B"})
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [second_id],
            )
            window.select_folder("A")
            self.assertEqual(window.current_nfo_filters, {})
            self.assertEqual(window.current_folder, "A")
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [first_id],
            )
            window.select_tag("标签 B")
            self.assertIsNone(window.current_folder)
            self.assertEqual(window.current_tag, "标签 B")
            self.assertEqual(
                [video["id"] for video in window.library_page.current_videos()],
                [second_id],
            )
        finally:
            window.close()
            app.processEvents()

    def test_player_window_is_independent_and_nonmodal(self) -> None:
        app = QApplication.instance() or QApplication([])
        window = PlayerWindow()
        window.show()
        app.processEvents()
        self.assertTrue(window.isWindow())
        self.assertFalse(window.isModal())
        self.assertIs(window.player.parentWidget(), window)
        window.close()
        app.processEvents()

    def test_detail_panel_has_three_switching_tabs(self) -> None:
        app = QApplication.instance() or QApplication([])
        panel = DetailPanel()
        self.assertEqual(panel.tabs.count(), 3)
        self.assertEqual([button.text() for button in panel.tab_buttons], ["详情", "元数据", "NFO 筛选"])
        panel.tab_buttons[1].click()
        self.assertEqual(panel.tabs.currentIndex(), 1)
        panel.tab_buttons[2].click()
        self.assertEqual(panel.tabs.currentIndex(), 2)
        panel.deleteLater()
        app.processEvents()

    def test_detail_inline_filters_follow_active_state(self) -> None:
        app = QApplication.instance() or QApplication([])
        panel = DetailPanel()
        panel.set_filter_options(
            {
                "capture_year": [
                    {"value": "2026", "count": 5},
                    {"value": "2025", "count": 3},
                ]
            },
            {"country": [{"value": "Japan", "count": 4}]},
        )
        panel.set_active_filters(
            {"capture_year": "2026"},
            {"country": "Japan"},
        )
        self.assertEqual(panel.metadata_combos["capture_year"].currentData(), "2026")
        self.assertEqual(panel.nfo_combos["country"].currentData(), "Japan")
        emitted: list[dict[str, str]] = []
        panel.metadata_filters_changed.connect(emitted.append)
        combo = panel.metadata_combos["capture_year"]
        combo.setCurrentIndex(combo.findData("2025"))
        self.assertEqual(emitted[-1], {"capture_year": "2025"})
        panel.deleteLater()
        app.processEvents()

    def test_sidebar_tags_live_below_categories_with_counts(self) -> None:
        app = QApplication.instance() or QApplication([])
        sidebar = Sidebar()
        sidebar.set_categories(
            [
                {
                    "id": 1,
                    "name": "分类",
                    "parent_id": None,
                    "color": "#E5B567",
                }
            ]
        )
        sidebar.set_tags(
            [
                {"name": "演示", "video_count": 7, "source": "both"},
                {"name": "高清", "video_count": 3, "source": "manual"},
            ]
        )
        sidebar.set_counts(
            {
                "all": 20,
                "continue": 4,
                "favorite": 6,
                "uncategorized": 1,
                "rating:5": 3,
                "rating:0": 2,
            }
        )
        self.assertEqual(
            sidebar._special_items["all"].data(0, COUNT_ROLE),
            20,
        )
        self.assertEqual(
            sidebar._special_items["continue"].data(0, COUNT_ROLE),
            4,
        )
        self.assertEqual(sidebar._rating_items[5].data(0, COUNT_ROLE), 3)
        self.assertEqual(sidebar._rating_items[0].data(0, COUNT_ROLE), 2)
        sidebar.adjust_rating_count(5, 3)
        self.assertEqual(sidebar._rating_items[5].data(0, COUNT_ROLE), 2)
        self.assertEqual(sidebar._rating_items[3].data(0, COUNT_ROLE), 1)
        sidebar.adjust_rating_count(3, 0)
        self.assertEqual(sidebar._rating_items[3].data(0, COUNT_ROLE), 0)
        self.assertEqual(sidebar._rating_items[0].data(0, COUNT_ROLE), 3)
        root = sidebar._tag_root
        self.assertIsNotNone(root)
        self.assertEqual(
            sidebar.organize_tree.indexOfTopLevelItem(root),
            sidebar.organize_tree.topLevelItemCount() - 1,
        )
        self.assertEqual(root.data(0, COUNT_ROLE), 2)
        item = sidebar._tag_items["演示"]
        self.assertEqual(item.data(0, COUNT_ROLE), 7)
        selected: list[str] = []
        sidebar.tag_selected.connect(selected.append)
        sidebar._organize_clicked(item, 0)
        self.assertEqual(selected, ["演示"])
        deleted: list[str] = []
        sidebar.tag_delete_requested.connect(deleted.append)
        with patch("app.ui.sidebar.QMenu") as menu_class:
            menu = menu_class.return_value
            rename_action = object()
            delete_action = object()
            menu.addAction.side_effect = [rename_action, delete_action]
            menu.exec.return_value = delete_action
            sidebar._tag_menu(item, QPoint(10, 10))
            menu.exec.assert_called_once_with(
                sidebar.organize_tree.viewport().mapToGlobal(QPoint(10, 10))
            )
        self.assertEqual(deleted, ["演示"])
        renamed: list[str] = []
        sidebar.tag_rename_requested.connect(renamed.append)
        with patch("app.ui.sidebar.QMenu") as menu_class:
            menu = menu_class.return_value
            rename_action = object()
            menu.addAction.side_effect = [rename_action, object()]
            menu.exec.return_value = rename_action
            sidebar._tag_menu(item, QPoint(10, 10))
        self.assertEqual(renamed, ["演示"])
        sidebar._category_expanded(root)
        root.setExpanded(True)
        self.assertTrue(root.isExpanded())
        sidebar._category_collapsed(root)
        root.setExpanded(False)
        self.assertFalse(root.isExpanded())
        sidebar.deleteLater()
        app.processEvents()

    def test_recent_view_and_count_use_last_seven_days(self) -> None:
        app = QApplication.instance() or QApplication([])
        recent_path = self.root / "recent.mp4"
        old_path = self.root / "old.mp4"
        recent_path.write_bytes(b"recent")
        old_path.write_bytes(b"old")
        recent_id = self._add_video(recent_path)
        old_id = self._add_video(old_path)
        old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(
            timespec="seconds"
        )
        self.db.conn.execute(
            "UPDATE videos SET added_at = ? WHERE id = ?",
            (old_time, old_id),
        )
        self.db.conn.commit()

        recent = self.db.videos(library_id=self.library_id, mode="recent")
        self.assertEqual([int(video["id"]) for video in recent], [recent_id])
        self.assertEqual(
            self.db.sidebar_counts([self.library_id])["recent"],
            1,
        )
        sidebar = Sidebar()
        sidebar.set_counts({"all": 2, "recent": 1})
        try:
            self.assertEqual(
                sidebar._special_items["recent"].data(0, COUNT_ROLE),
                1,
            )
        finally:
            sidebar.deleteLater()
            app.processEvents()

    def test_categories_are_scoped_to_each_library(self) -> None:
        other_root = self.root / "other-library"
        other_root.mkdir()
        other_id = self.db.add_library("Other", other_root)
        first_category = self.db.create_category(
            "动作",
            library_id=self.library_id,
        )
        second_category = self.db.create_category(
            "动作",
            library_id=other_id,
        )
        first_video = self.root / "first.mp4"
        second_video = other_root / "second.mp4"
        first_video.write_bytes(b"first")
        second_video.write_bytes(b"second")
        first_id = self._add_video(first_video)
        second_id = self._add_video(second_video, library_id=other_id)
        self.db.update_video(first_id, category_id=first_category)
        self.db.update_video(second_id, category_id=second_category)

        self.assertEqual(
            [item["id"] for item in self.db.categories([self.library_id])],
            [first_category],
        )
        self.assertEqual(
            [item["id"] for item in self.db.categories([other_id])],
            [second_category],
        )
        self.assertEqual(
            {
                item["id"]: item["library_id"]
                for item in self.db.categories([self.library_id, other_id])
            },
            {
                first_category: self.library_id,
                second_category: other_id,
            },
        )

        self.db.delete_category(first_category)
        self.assertIsNone(self.db.video(first_id)["category_id"])
        self.assertEqual(self.db.video(second_id)["category_id"], second_category)

    def test_new_category_binds_to_selected_library(self) -> None:
        app = QApplication.instance() or QApplication([])
        other_root = self.root / "other-library"
        other_root.mkdir()
        other_id = self.db.add_library("Other", other_root)
        window = MainWindow(self.db.path)

        class FakeCategoryDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.Accepted

            def values(self):
                return "当前库分类", None

        try:
            window.select_library(self.library_id)
            with patch(
                "app.ui.main_window.CategoryDialog",
                FakeCategoryDialog,
            ):
                window.add_category()
            categories = self.db.categories([self.library_id])
            self.assertEqual([item["name"] for item in categories], ["当前库分类"])
            self.assertEqual(self.db.categories([other_id]), [])
        finally:
            window.close()
            app.processEvents()

    def test_all_libraries_hide_categories_and_block_creation(self) -> None:
        app = QApplication.instance() or QApplication([])
        category_id = self.db.create_category(
            "仅测试库",
            library_id=self.library_id,
        )
        window = MainWindow(self.db.path)
        try:
            window.select_library(ALL_LIBRARIES)
            self.assertEqual(window.sidebar._category_items, {})
            with patch("app.ui.main_window.QMessageBox.information") as info:
                window.add_category()
            info.assert_called_once()
            self.assertIsNotNone(self.db.category(category_id))

            window.select_library(self.library_id)
            self.assertIn(category_id, window.sidebar._category_items)
        finally:
            window.close()
            app.processEvents()

    def test_batch_category_rejects_mixed_library_selection(self) -> None:
        app = QApplication.instance() or QApplication([])
        other_root = self.root / "other-library"
        other_root.mkdir()
        other_id = self.db.add_library("Other", other_root)
        category_id = self.db.create_category(
            "仅当前库",
            library_id=self.library_id,
        )
        first_video = self.root / "first.mp4"
        second_video = other_root / "second.mp4"
        first_video.write_bytes(b"first")
        second_video.write_bytes(b"second")
        first_id = self._add_video(first_video)
        second_id = self._add_video(second_video, library_id=other_id)
        window = MainWindow(self.db.path)
        try:
            with patch(
                "app.ui.main_window.QMessageBox.warning"
            ) as warning:
                window.batch_set_category([first_id, second_id], category_id)
            warning.assert_called_once()
            self.assertIsNone(self.db.video(first_id)["category_id"])
            self.assertIsNone(self.db.video(second_id)["category_id"])
        finally:
            window.close()
            app.processEvents()

    def test_old_global_categories_are_discarded(self) -> None:
        migration_path = Path(self.temp.name) / "migration.db"
        db = Database(migration_path)
        first_root = Path(self.temp.name) / "migration-first"
        second_root = Path(self.temp.name) / "migration-second"
        first_root.mkdir()
        second_root.mkdir()
        first_library = db.add_library("First", first_root)
        second_library = db.add_library("Second", second_root)

        def add_video(library_id: int, root: Path, name: str) -> int:
            path = root / name
            path.write_bytes(name.encode("ascii"))
            return db.upsert_video(
                {
                    "library_id": library_id,
                    "path": str(path),
                    "identity_key": identity_key(path),
                    "relative_path": name,
                    "file_name": name,
                    "stem": path.stem,
                    "logical_ext": ".mp4",
                    "physical_ext": ".mp4",
                    "title": name,
                    "content_hash": content_hash(path),
                    "size_bytes": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                },
                "migration",
            )

        first_video = add_video(first_library, first_root, "first.mp4")
        second_video = add_video(second_library, second_root, "second.mp4")
        shared = db.create_category("共用", library_id=first_library)
        empty = db.create_category("空分类", library_id=first_library)
        db.update_video(first_video, category_id=shared)
        db.update_video(second_video, category_id=shared)
        db.close()

        conn = sqlite3.connect(migration_path)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            CREATE TABLE categories_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER,
                color TEXT NOT NULL DEFAULT '#E5B567',
                created_at TEXT NOT NULL,
                UNIQUE(parent_id, name COLLATE NOCASE)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO categories_old(id, name, parent_id, color, created_at)
            SELECT id, name, parent_id, color, created_at FROM categories
            """
        )
        conn.execute("DROP TABLE categories")
        conn.execute("ALTER TABLE categories_old RENAME TO categories")
        conn.execute("INSERT INTO tags(name) VALUES ('orphan-tag')")
        conn.execute(
            "INSERT INTO video_tags(video_id, tag_id) VALUES (999999, 1)"
        )
        conn.commit()
        conn.close()

        migrated = Database(migration_path)
        try:
            self.assertEqual(
                migrated.categories([first_library, second_library]),
                [],
            )
            for video_id in (first_video, second_video):
                self.assertIsNone(migrated.video(video_id)["category_id"])
        finally:
            migrated.close()

    def test_sidebar_shows_only_top_30_collapsed_actors(self) -> None:
        app = QApplication.instance() or QApplication([])
        sidebar = Sidebar()
        sidebar.set_categories(
            [
                {
                    "id": 1,
                    "name": "分类",
                    "parent_id": None,
                    "color": "#E5B567",
                }
            ]
        )
        sidebar.set_actors(
            [
                {"value": f"演员 {index:02d}", "count": 100 - index}
                for index in range(35)
            ]
        )
        root = sidebar._actor_root
        self.assertIsNotNone(root)
        self.assertFalse(root.isExpanded())
        self.assertEqual(root.data(0, COUNT_ROLE), 35)
        self.assertEqual(root.childCount(), 30)
        self.assertEqual(root.child(0).text(0), "演员 00")
        self.assertEqual(root.child(0).data(0, COUNT_ROLE), 100)
        self.assertLess(
            sidebar.organize_tree.indexOfTopLevelItem(root),
            sidebar.organize_tree.indexOfTopLevelItem(
                sidebar._category_items[1]
            ),
        )
        emitted: list[str] = []
        sidebar.actor_selected.connect(emitted.append)
        sidebar._organize_clicked(root.child(4), 0)
        self.assertEqual(emitted, ["演员 04"])
        sidebar.select_actor_name("演员 06")
        self.assertEqual(
            sidebar.organize_tree.currentItem().text(0),
            "演员 06",
        )
        sidebar.deleteLater()
        app.processEvents()

    def test_sidebar_actor_filter_uses_existing_nfo_filter(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "actor-filter.mp4"
        video_path.write_bytes(b"video")
        self._add_video(video_path)
        self.db.conn.execute(
            "UPDATE videos SET actors_json = ?",
            (json.dumps([{"name": "Actor A", "role": "Role"}]),),
        )
        self.db.conn.commit()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            root = window.sidebar._actor_root
            self.assertIsNotNone(root)
            window.sidebar._organize_clicked(root.child(0), 0)
            self.assertEqual(window.current_nfo_filters, {"actor": "Actor A"})
            self.assertEqual(window.library_page.title_label.text(), "演员 · Actor A")
            self.assertEqual(len(window.library_page.current_videos()), 1)
            window.clear_filters()
            self.assertEqual(window.current_nfo_filters, {})
        finally:
            window.close()
            app.processEvents()

    def test_sidebar_folder_group_filters_first_level_folder(self) -> None:
        app = QApplication.instance() or QApplication([])
        sidebar = Sidebar()
        sidebar.set_folders(
            [
                {"name": "2025", "video_count": 12},
                {"name": "2026", "video_count": 8},
            ]
        )
        root = sidebar._folder_root
        self.assertIsNotNone(root)
        self.assertFalse(root.isExpanded())
        self.assertEqual(root.childCount(), 2)
        self.assertEqual(root.data(0, COUNT_ROLE), 2)
        self.assertEqual(root.child(0).text(0), "2025")
        self.assertEqual(root.child(0).data(0, COUNT_ROLE), 12)
        self.assertEqual(root.child(1).data(0, COUNT_ROLE), 8)
        emitted: list[str] = []
        sidebar.folder_selected.connect(emitted.append)
        sidebar._organize_clicked(root.child(1), 0)
        self.assertEqual(emitted, ["2026"])
        sidebar.select_folder_name("2025")
        self.assertEqual(
            sidebar.organize_tree.currentItem().text(0),
            "2025",
        )
        sidebar.deleteLater()
        app.processEvents()

    def test_filter_dropdowns_scroll_for_long_options(self) -> None:
        app = QApplication.instance() or QApplication([])
        long_value = "非常长的筛选条件名称-" + "A" * 80
        metadata = {"camera": [{"value": long_value, "count": 12}]}
        nfo = {"actor": [{"value": long_value, "count": 8}]}

        panel = DetailPanel()
        panel.set_filter_options(metadata, nfo)
        for combo in (
            panel.metadata_combos["camera"],
            panel.nfo_combos["actor"],
        ):
            self.assertEqual(combo.maxVisibleItems(), 10)
            self.assertEqual(
                combo.view().verticalScrollBarPolicy(),
                Qt.ScrollBarAsNeeded,
            )
            self.assertEqual(combo.view().textElideMode(), Qt.ElideRight)

        metadata_dialog = MetadataFilterDialog(metadata)
        combo = metadata_dialog._combos["camera"]
        self.assertEqual(combo.maxVisibleItems(), 10)
        self.assertEqual(
            combo.view().horizontalScrollBarPolicy(),
            Qt.ScrollBarAsNeeded,
        )

        nfo_dialog = NfoFilterDialog(nfo)
        self.assertEqual(
            nfo_dialog.option_list.verticalScrollBarPolicy(),
            Qt.ScrollBarAsNeeded,
        )
        self.assertEqual(nfo_dialog.option_list.textElideMode(), Qt.ElideRight)

        for widget in (panel, metadata_dialog, nfo_dialog):
            widget.deleteLater()
        app.processEvents()

    def test_capture_time_from_shooting_filename(self) -> None:
        self.assertEqual(
            capture_at_from_filename(Path("2026-05-04-084615- p3.MP4")),
            "2026-05-04T08:46:15",
        )
        self.assertEqual(
            capture_at_from_filename(Path("20240504_084615.mp4")),
            "2024-05-04T08:46:15",
        )
        self.assertEqual(capture_at_from_filename(Path("movie.mp4")), "")

    def test_metadata_filters_and_group_collapse(self) -> None:
        first = self.root / "2026-05-04-084615-a.mp4"
        second = self.root / "2026-05-04-085000-b.mp4"
        other = self.root / "2025-01-01-010000-c.mp4"
        for path in (first, second, other):
            path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        first_id = self._add_video(
            first,
            capture_at="2026-05-04T08:46:15",
            camera_model="NIKON Z fc",
            source_kind="shooting",
            size_bytes=200 * 1024 * 1024,
        )
        second_id = self._add_video(
            second,
            capture_at="2026-05-04T08:50:00",
            camera_model="NIKON Z fc",
            source_kind="shooting",
            size_bytes=300 * 1024 * 1024,
        )
        other_id = self._add_video(
            other,
            capture_at="2025-01-01T01:00:00",
            camera_model="DJI Osmo Pocket 3",
            source_kind="shooting",
        )
        filtered = self.db.videos(
            metadata_filters={
                "capture_year": "2026",
                "capture_month": "2026-05",
                "camera": "NIKON Z fc",
            }
        )
        self.assertEqual({row["id"] for row in filtered}, {first_id, second_id})

        group_id = self.db.create_video_group("上午拍摄", [first_id, second_id])
        collapsed = self.db.videos()
        group = next(row for row in collapsed if row.get("group_collapsed"))
        self.assertEqual(group["group_count"], 2)
        self.assertEqual(group["size_bytes"], 500 * 1024 * 1024)
        self.assertIn(other_id, {row["id"] for row in collapsed})
        self.assertEqual(sum(1 for row in collapsed if row.get("group_id") == group_id), 1)

        expanded = self.db.videos(expanded_groups={group_id})
        self.assertEqual({row["id"] for row in expanded}, {first_id, second_id, other_id})
        self.assertFalse(
            next(row for row in expanded if row["id"] == first_id)["group_collapsed"]
        )

    def test_add_selected_videos_to_existing_group(self) -> None:
        paths = [self.root / f"group-add-{index}.mp4" for index in range(4)]
        for path in paths:
            path.write_bytes(b"video")
        ids = [self._add_video(path) for path in paths]
        first_group = self.db.create_video_group("第一组", ids[:2])
        second_group = self.db.create_video_group("第二组", ids[2:])

        count = self.db.add_videos_to_group(first_group, [ids[1], ids[2]])

        self.assertEqual(count, 3)
        self.assertEqual(
            {row["id"] for row in self.db.videos_for_group(first_group)},
            {ids[0], ids[1], ids[2]},
        )
        self.assertNotIn(
            second_group,
            {group["id"] for group in self.db.video_groups()},
        )

    def test_group_toggle_updates_only_affected_rows(self) -> None:
        app = QApplication.instance() or QApplication([])
        paths = [self.root / f"group-toggle-{index}.mp4" for index in range(3)]
        for path in paths:
            path.write_bytes(b"video")
        ids = [self._add_video(path) for path in paths]
        group_id = self.db.create_video_group("切换组", ids[:2])

        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            self.assertEqual(len(window.library_page.current_videos()), 2)
            with patch.object(
                window,
                "load_videos",
                side_effect=AssertionError("group toggle must not reload the library"),
            ):
                window.toggle_video_group(group_id, True)
                self.assertEqual(len(window.library_page.current_videos()), 3)
                window.toggle_video_group(group_id, False)
                self.assertEqual(len(window.library_page.current_videos()), 2)
        finally:
            window.close()

    def test_multi_selection_can_choose_existing_group(self) -> None:
        app = QApplication.instance() or QApplication([])
        page = LibraryPage()
        page.set_video_groups(
            [
                {
                    "id": 10,
                    "name": "目标组",
                    "library_id": 1,
                    "member_count": 3,
                }
            ]
        )
        selected = [
            {"id": 1, "library_id": 1, "file_name": "one.mp4"},
            {"id": 2, "library_id": 1, "file_name": "two.mp4"},
        ]
        emitted: list[tuple[list[int], int]] = []
        page.group_add_requested.connect(
            lambda ids, group_id: emitted.append((list(ids), int(group_id)))
        )
        with patch("app.ui.library_view.QMenu") as menu_class:
            menu = menu_class.return_value
            group_menu = MagicMock()
            category_menu = MagicMock()
            menu.addMenu.side_effect = [group_menu, category_menu]
            group_action = object()
            group_menu.addAction.return_value = group_action
            menu.exec.return_value = group_action
            page._show_video_menu(selected, QPoint(10, 10))
        self.assertEqual(emitted, [([1, 2], 10)])
        page.deleteLater()
        app.processEvents()

    def test_timeline_aggregates_months_and_sizes(self) -> None:
        app = QApplication.instance() or QApplication([])
        timeline = TimelineView()
        timeline.set_videos(
            [
                {"id": 1, "capture_at": "2026-05-04T08:46:15", "size_bytes": 100, "duration_ms": 1000},
                {"id": 2, "capture_at": "2026-05-04T08:50:00", "size_bytes": 200, "duration_ms": 2000},
                {"id": 3, "capture_at": "2025-12-01T01:00:00", "size_bytes": 300, "duration_ms": 3000},
            ]
        )
        timeline.resize(900, 700)
        timeline.show()
        app.processEvents()
        self.assertEqual(len(timeline._sections), 2)
        newest = timeline._sections[0]
        self.assertEqual(newest.month, "2026-05")
        self.assertEqual(newest.model.rowCount(), 2)
        timeline.set_card_scale(0)
        small = newest.delegate.sizeHint(None, None)
        timeline.set_card_scale(100)
        large = newest.delegate.sizeHint(None, None)
        self.assertGreater(large.width(), small.width())
        newest.toggle_collapsed()
        self.assertTrue(newest.view.isHidden())
        timeline.set_videos(
            [
                {"id": 1, "capture_at": "2026-05-04T08:46:15", "size_bytes": 100, "duration_ms": 1000},
                {"id": 2, "capture_at": "2026-05-04T08:50:00", "size_bytes": 200, "duration_ms": 2000},
            ]
        )
        app.processEvents()
        self.assertTrue(timeline._sections[0].collapsed)
        timeline._toggle_year("2026")
        self.assertTrue(timeline._sections[0].isHidden())
        timeline.deleteLater()
        app.processEvents()
    def test_remove_library_cleans_index_and_cached_thumbnails_only(self) -> None:
        video_path = self.root / "keep.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom-keep")
        poster = self.root / "poster.jpg"
        self._write_jpeg(poster, (360, 640), "#cc5533")
        video_id = self._add_video(video_path)
        cache_dir = Path(self.temp.name) / THUMB_DIR_NAME
        cache_dir.mkdir()
        thumb_name = "generated.vthumb"
        (cache_dir / thumb_name).write_bytes(b"\xff\xd8\xffcached")
        (cache_dir / thumb_name).with_suffix(THUMB_RUNTIME_EXTENSION).write_bytes(
            b"\xff\xd8\xffruntime"
        )
        self.db.update_video(
            video_id,
            thumb_path=f"{THUMB_DIR_NAME}\\{thumb_name}",
            poster_path=str(poster),
        )
        self.db.set_library_access_password(self.library_id, "secret")

        paths = self.db.remove_library(self.library_id)

        removed = remove_cached_thumbnails(Path(self.temp.name), paths)
        self.assertEqual(removed, 2)
        self.assertIsNone(self.db.library(self.library_id))
        self.assertEqual(self.db.videos(library_id=self.library_id), [])
        self.assertFalse(self.db.has_library_access_password(self.library_id))
        self.assertTrue(video_path.exists())
        self.assertTrue(poster.exists())
        self.assertFalse((cache_dir / thumb_name).exists())
        self.assertFalse((cache_dir / thumb_name).with_suffix(THUMB_RUNTIME_EXTENSION).exists())

    def test_database_search_category_and_tag(self) -> None:
        video_path = self.root / "demo.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(video_path)
        category_id = self.db.create_category("动作")
        self.db.update_video(video_id, category_id=category_id, favorite=1)
        self.db.set_video_tags(video_id, ["演示", "高清"])
        rows = self.db.videos(library_id=self.library_id, search="Demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category_id"], category_id)
        tagged = self.db.videos(library_id=self.library_id, tag="演示")
        self.assertEqual(len(tagged), 1)

    def test_file_watcher_queues_automatic_scan_for_changed_directory(self) -> None:
        app = QApplication.instance() or QApplication([])
        nested = self.root / "nested"
        nested.mkdir()
        video_path = nested / "watched.mp4"
        video_path.write_bytes(b"video")
        self._add_video(video_path)
        self.assertEqual(
            self.db.video_paths_for_library(self.library_id),
            [str(video_path)],
        )
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            watched = {
                str(Path(path).resolve())
                for path in window.file_watcher.directories()
            }
            self.assertIn(str(self.root.resolve()), watched)
            self.assertIn(str(nested.resolve()), watched)
            with patch.object(window, "scan_library") as scan:
                window._file_change_detected(str(nested))
                self.assertTrue(window._watch_debounce.isActive())
                window.library_page.incremental_refresh_button.click()
                scan.assert_called_once_with(
                    self.library_id,
                    automatic=True,
                    directories=[str(nested.resolve())],
                )
        finally:
            window.close()
            app.processEvents()

    def test_sidebar_counts_match_special_views(self) -> None:
        first = self.root / "first.mp4"
        second = self.root / "second.mp4"
        third = self.root / "third.mp4"
        for path in (first, second, third):
            path.write_bytes(b"video")
        first_id = self._add_video(first)
        second_id = self._add_video(second)
        third_id = self._add_video(third)
        category_id = self.db.create_category("已分类")
        self.db.update_video(
            first_id,
            favorite=1,
            last_position_ms=6000,
            rating=5,
        )
        self.db.update_video(
            second_id,
            category_id=category_id,
            last_position_ms=9000,
            watched=1,
            rating=2,
        )
        self.db.update_video(third_id, rating=5)

        counts = self.db.sidebar_counts([self.library_id])

        self.assertEqual(counts["all"], 3)
        self.assertEqual(counts["continue"], 1)
        self.assertEqual(counts["favorite"], 1)
        self.assertEqual(counts["uncategorized"], 2)
        self.assertEqual(counts["rating:0"], 0)
        self.assertEqual(counts["rating:2"], 1)
        self.assertEqual(counts["rating:5"], 2)
        self.assertEqual(
            self.db.sidebar_counts([])["all"],
            0,
        )

    def test_database_lists_and_filters_top_level_folders(self) -> None:
        for name in ("2025", "2026", "空目录"):
            (self.root / name).mkdir()
        first = self.root / "2025" / "first.mp4"
        second = self.root / "2026" / "second.mp4"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self._scan()

        folders = {
            item["name"]: item["video_count"]
            for item in self.db.top_level_folders(self.library_id)
        }
        self.assertEqual(folders, {"2025": 1, "2026": 1, "空目录": 0})
        self.assertEqual(
            [video["file_name"] for video in self.db.videos(folder="2025")],
            ["first.mp4"],
        )
        self.assertEqual(
            [video["file_name"] for video in self.db.videos(folder="2026")],
            ["second.mp4"],
        )

    def test_main_window_folder_filter_uses_first_level_folder(self) -> None:
        app = QApplication.instance() or QApplication([])
        (self.root / "A").mkdir()
        (self.root / "B").mkdir()
        first = self.root / "A" / "a.mp4"
        second = self.root / "B" / "b.mp4"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        self._scan()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            root = window.sidebar._folder_root
            self.assertIsNotNone(root)
            window.sidebar._organize_clicked(root.child(0), 0)
            self.assertEqual(window.current_folder, "A")
            self.assertEqual(window.library_page.title_label.text(), "文件夹 · A")
            self.assertEqual(
                [video["file_name"] for video in window.library_page.current_videos()],
                ["a.mp4"],
            )
        finally:
            window.close()
            app.processEvents()

    def test_folder_list_updates_after_first_level_folder_changes(self) -> None:
        app = QApplication.instance() or QApplication([])
        (self.root / "旧文件夹").mkdir()
        video = self.root / "旧文件夹" / "video.mp4"
        video.write_bytes(b"video")
        self._scan()
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            self.assertIn("旧文件夹", window.sidebar._folder_items)

            new_folder = self.root / "新文件夹"
            new_folder.mkdir()
            (new_folder / "new.mp4").write_bytes(b"new")
            os.replace(self.root / "旧文件夹", self.root / "改名文件夹")
            self._scan()
            window.refresh_all()

            self.assertNotIn("旧文件夹", window.sidebar._folder_items)
            self.assertIn("改名文件夹", window.sidebar._folder_items)
            self.assertIn("新文件夹", window.sidebar._folder_items)
        finally:
            window.close()
            app.processEvents()

    def test_hidden_tag_is_removed_globally_and_stays_hidden(self) -> None:
        first = self.root / "first.mp4"
        second = self.root / "second.mp4"
        first.write_bytes(b"video")
        second.write_bytes(b"video")
        first_id = self._add_video(first)
        second_id = self._add_video(second, nfo_tags=["NFO 删除"])
        self.db.set_video_tags(first_id, ["保留", "删除"])
        self.db.set_video_tags(second_id, ["删除"])

        self.db.hide_tag("删除")
        self.db.hide_tag("NFO 删除")
        self.db.set_video_tags(first_id, ["保留", "删除"])

        self.assertEqual(self.db.tags_for_video(first_id), ["保留"])
        self.assertEqual(self.db.tags_for_video(second_id), [])
        self.assertNotIn(
            "删除",
            {item["name"] for item in self.db.all_tags()},
        )
        self.assertNotIn(
            "NFO 删除",
            {item["name"] for item in self.db.all_tags()},
        )
        self.assertEqual(self.db.videos(tag="删除"), [])
        self.assertNotIn(
            "删除",
            {
                item["value"]
                for item in self.db.metadata_filter_options().get("tag", [])
            },
        )
        self.assertNotIn(
            "NFO 删除",
            {
                item["value"]
                for item in self.db.nfo_filter_options().get("nfo_tag", [])
            },
        )

    def test_rename_tag_updates_manual_and_nfo_tags_globally(self) -> None:
        manual = self.root / "manual.mp4"
        nfo = self.root / "nfo.mp4"
        manual.write_bytes(b"video")
        nfo.write_bytes(b"video")
        manual_id = self._add_video(manual)
        nfo_id = self._add_video(nfo, nfo_tags=["旧标签"])
        self.db.set_video_tags(manual_id, ["旧标签"])

        self.db.rename_tag("旧标签", "新标签")
        self.assertEqual(self.db.tags_for_video(manual_id), ["新标签"])
        combined = next(
            item for item in self.db.all_tags() if item["name"] == "新标签"
        )
        self.assertEqual(combined["video_count"], 2)
        self.assertEqual(
            {item["id"] for item in self.db.videos(tag="新标签")},
            {manual_id, nfo_id},
        )
        self.assertIn(
            "新标签",
            {
                item["value"]
                for item in self.db.metadata_filter_options().get("tag", [])
            },
        )
        self.assertIn(
            "新标签",
            {
                item["value"]
                for item in self.db.nfo_filter_options().get("nfo_tag", [])
            },
        )

        self.db.rename_tag("新标签", "最终标签")
        self.assertEqual(
            {item["id"] for item in self.db.videos(tag="最终标签")},
            {manual_id, nfo_id},
        )
        self.assertFalse(
            any(item["name"] == "旧标签" for item in self.db.all_tags())
        )

    def test_library_scan_completion_state_survives_until_finish(self) -> None:
        self.assertEqual(self.db.library(self.library_id)["scan_complete"], 0)
        self.db.begin_scan(self.library_id, "first-token")
        self.assertEqual(self.db.library(self.library_id)["scan_complete"], 0)
        self.db.finish_scan(self.library_id, "first-token")
        self.assertEqual(self.db.library(self.library_id)["scan_complete"], 1)

    def test_two_level_password_hashes(self) -> None:
        record = create_password_record("secret-one")
        self.assertTrue(verify_password("secret-one", record))
        self.assertFalse(verify_password("wrong", record))
        self.assertNotIn("secret-one", record)
        self.db.set_isolated_reveal_password("global-pass")
        self.db.set_library_access_password(self.library_id, "library-pass")
        self.assertTrue(self.db.verify_isolated_reveal_password("global-pass"))
        self.assertTrue(
            self.db.verify_library_access_password(
                self.library_id,
                "library-pass",
            )
        )
        self.assertNotIn(
            "global-pass",
            self.db.setting("isolated_reveal_password") or "",
        )

    def test_windows_session_lock_filter_invokes_callback(self) -> None:
        callback = MagicMock()
        message = MagicMock()
        message.message = WM_WTSSESSION_CHANGE
        message.wParam = WTS_SESSION_LOCK
        session_filter = WindowsSessionLockFilter(callback)
        with patch(
            "app.security.wintypes.MSG.from_address",
            return_value=message,
        ):
            result = session_filter.nativeEventFilter(
                b"windows_generic_MSG",
                123,
            )
        self.assertEqual(result, (False, 0))
        callback.assert_called_once_with()

    def test_alias_content_detection(self) -> None:
        mp4_alias = self.root / "movie.dll"
        mp4_alias.write_bytes(b"\x00\x00\x00\x18ftypisom")
        self.assertEqual(sniff_media(mp4_alias), ".mp4")
        self.assertEqual(logical_extension(mp4_alias), ".mp4")

        ts_alias = self.root / "movie.dat"
        ts_alias.write_bytes(b"\x47" + b"\x00" * 187 + b"\x47")
        self.assertEqual(sniff_media(ts_alias), ".ts")

        normal_dll = self.root / "runtime.dll"
        normal_dll.write_bytes(b"MZ" + b"\x00" * 128)
        self.assertIsNone(sniff_media(normal_dll))
        self.assertEqual(logical_extension(normal_dll), "")

        jpg_alias = self.root / "poster.txt"
        jpg_alias.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        self.assertTrue(is_jpeg(jpg_alias))

    def test_custom_mask_map_round_trip_and_validation(self) -> None:
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mp4"] = ".asset"
        self.db.set_mask_map(custom)
        self.assertEqual(self.db.mask_map()[".mp4"], ".asset")
        with self.assertRaises(ValueError):
            self.db.set_mask_map({**custom, ".mkv": ".asset"})
        with self.assertRaises(ValueError):
            self.db.set_mask_map({**custom, ".mp4": "asset"})

    def test_custom_mask_uses_inverse_extension_not_content_guess(self) -> None:
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mov"] = ".media"
        movie = self.root / "movie.media"
        movie.write_bytes(b"\x00\x00\x00\x18ftypqt  ")
        self.assertEqual(sniff_media(movie), ".mp4")
        self.assertEqual(logical_extension(movie, custom), ".mov")

    def test_timecode_parsing(self) -> None:
        self.assertEqual(parse_timecode("01:02:03"), 3_723_000)
        self.assertEqual(parse_timecode("02:03.5"), 123_500)
        self.assertEqual(parse_timecode("90"), 90_000)
        self.assertIsNone(parse_timecode("bad-time"))

    def test_subtitle_detection_and_parsing(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"")
        subtitle = self.root / "movie.srt"
        subtitle.write_text(
            """1
00:00:01,000 --> 00:00:03,500
<i>Hello</i>

2
00:00:04,000 --> 00:00:06,000
World
""",
            encoding="utf-8",
        )
        self.assertEqual(find_default_subtitle(video), subtitle)
        cues = parse_subtitle(subtitle)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello")
        self.assertEqual(cue_at(cues, 2_000).text, "Hello")
        self.assertIsNone(cue_at(cues, 7_000))

        ass = self.root / "movie.ass"
        ass.write_text(
            """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}ASS text
""",
            encoding="utf-8",
        )
        ass_cues = parse_subtitle(ass)
        self.assertEqual(ass_cues[0].text, "ASS text")

    def test_movie_nfo_fields_are_parsed_and_searchable(self) -> None:
        nfo_path = self.root / "movie.nfo"
        nfo_path.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>ABC-123 Example</title>
  <plot>Plot text</plot>
  <runtime>120</runtime>
  <mpaa>NC-17</mpaa>
  <uniqueid type="num" default="true">ABC-123</uniqueid>
  <uniqueid type="cid">abc00123</uniqueid>
  <genre>Drama</genre>
  <tag>Tag One</tag>
  <country>Japan</country>
  <set><name>Series One</name></set>
  <premiered>2025-02-03</premiered>
  <studio>Studio A</studio>
  <director>Director A</director>
  <actor><name>Actor A</name><role>Role A</role></actor>
</movie>
""",
            encoding="utf-8",
        )
        parsed = parse_nfo(nfo_path)
        self.assertEqual(parsed["nfo_id"], "ABC-123")
        self.assertEqual(parsed["premiered"], "2025-02-03")
        self.assertEqual(parsed["content_set"], "Series One")
        self.assertEqual(parsed["nfo_tags"], ["Tag One"])
        self.assertEqual(parsed["duration_ms"], 7_200_000)

        video_path = self.root / "example.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        data = {
            "library_id": self.library_id,
            "path": str(video_path),
            "identity_key": identity_key(video_path),
            "relative_path": video_path.name,
            "file_name": video_path.name,
            "stem": video_path.stem,
            "logical_ext": ".mp4",
            "physical_ext": ".mp4",
            "title": parsed["title"],
            "duration_ms": parsed["duration_ms"],
            "bitrate_bps": 5_000_000,
            "size_bytes": video_path.stat().st_size,
            "mtime_ns": video_path.stat().st_mtime_ns,
            **parsed,
        }
        video_id = self.db.upsert_video(data, "test")
        video = self.db.video(video_id)
        self.assertEqual(video["nfo_ids"][1]["value"], "abc00123")
        self.assertEqual(video["actors"][0]["name"], "Actor A")
        self.assertEqual(video["bitrate_bps"], 5_000_000)
        self.assertEqual(len(self.db.videos(search="ABC-123")), 1)
        self.assertEqual(
            len(
                self.db.videos(
                    nfo_filters={
                        "country": "Japan",
                        "genre": "Drama",
                        "actor": "Actor A",
                    }
                )
            ),
            1,
        )
        for file_name, country in (("example-2.mp4", "Japan"), ("example-3.mp4", "Korea")):
            variant_path = self.root / file_name
            variant_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
            self.db.upsert_video(
                {
                    **data,
                    "path": str(variant_path),
                    "identity_key": identity_key(variant_path),
                    "relative_path": variant_path.name,
                    "file_name": variant_path.name,
                    "stem": variant_path.stem,
                    "country": country,
                },
                "test",
            )
        options = self.db.nfo_filter_options()
        countries = {
            item["value"]: item["count"] for item in options["country"]
        }
        self.assertEqual(countries, {"Japan": 2, "Korea": 1})
        self.assertEqual(list(countries), ["Japan", "Korea"])
        self.assertEqual(options["actor"][0], {"value": "Actor A", "count": 3})
        nfo_tag = next(
            item for item in self.db.all_tags() if item["name"] == "Tag One"
        )
        self.assertEqual(nfo_tag["source"], "nfo")

    def test_rename_updates_video_and_same_stem_sidecar(self) -> None:
        video_path = self.root / "old.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        subtitle = self.root / "old.srt"
        subtitle.write_text("1", encoding="utf-8")
        video_id = self._add_video(video_path)
        video = self.db.video(video_id)
        self.db.update_video(
            video_id,
            sidecars_json=json.dumps([str(subtitle)]),
        )
        video = self.db.video(video_id)
        new_path, moves = rename_video(self.db, video, self.library, "new")
        self.assertTrue(new_path.exists())
        self.assertTrue((self.root / "new.srt").exists())
        self.assertEqual(len(moves), 2)
        self.assertFalse(video_path.exists())
        updated = self.db.video(video_id)
        self.assertEqual(updated["file_name"], "new.mp4")
        self.assertEqual(updated["stem"], "new")
        self.assertEqual(updated["title"], "new")

    def test_move_stays_inside_library(self) -> None:
        video_path = self.root / "old.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(video_path)
        target = self.root / "archive"
        target.mkdir()
        new_path, _ = move_video(
            self.db,
            self.db.video(video_id),
            self.library,
            target,
        )
        self.assertTrue(new_path.exists())
        self.assertTrue(is_within(new_path, self.root))
        self.assertFalse(is_within(Path(tempfile.gettempdir()), self.root))

    def test_move_uses_masked_physical_path(self) -> None:
        video_path = self.root / "masked.dat"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom-masked")
        video_id = self._add_video(video_path, logical=".ts")
        self.db.update_video(video_id, physical_ext=".dat")
        target = self.root / "archive"
        target.mkdir()
        new_path, _ = move_video(
            self.db,
            self.db.video(video_id),
            self.library,
            target,
        )
        self.assertEqual(new_path, target / "masked.dat")
        self.assertTrue(new_path.exists())
        self.assertFalse(video_path.exists())
        self.assertEqual(self.db.video(video_id)["logical_ext"], ".ts")

    def test_category_parent_expansion_state_survives_refresh(self) -> None:
        app = QApplication.instance() or QApplication([])
        sidebar = Sidebar()
        parent_id = self.db.create_category("父级")
        self.db.create_category("子级", parent_id)
        categories = self.db.categories()
        sidebar.set_categories(categories)
        parent = sidebar._category_items[parent_id]
        self.assertTrue(parent.isExpanded())
        parent.setExpanded(False)
        sidebar.set_categories(categories)
        parent = sidebar._category_items[parent_id]
        self.assertFalse(parent.isExpanded())
        sidebar.deleteLater()
        app.processEvents()

    def test_mask_worker_unmasks_registered_file(self) -> None:
        masked_path = self.root / "demo.dll"
        masked_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(masked_path, ".mp4")
        worker = MaskWorker(str(self.db.path), self.library_id, False)
        worker.run()
        video = self.db.video(video_id)
        self.assertEqual(video["physical_ext"], ".mp4")
        self.assertTrue((self.root / "demo.mp4").exists())
        self.assertFalse(masked_path.exists())

    def test_mask_worker_uses_custom_map_both_directions(self) -> None:
        source = self.root / "custom.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(source, ".mp4")
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mp4"] = ".asset"
        self.db.set_mask_map(custom)

        MaskWorker(str(self.db.path), self.library_id, True).run()
        masked = self.root / "custom.asset"
        self.assertTrue(masked.exists())
        self.assertEqual(self.db.video(video_id)["physical_ext"], ".asset")

        MaskWorker(str(self.db.path), self.library_id, False).run()
        self.assertTrue(source.exists())
        self.assertFalse(masked.exists())
        self.assertEqual(self.db.video(video_id)["physical_ext"], ".mp4")

    def test_scan_recognizes_custom_masked_extension(self) -> None:
        custom = dict(DEFAULT_MASK_MAP)
        custom[".mp4"] = ".asset"
        self.db.set_mask_map(custom)
        source = self.root / "scan-custom.asset"
        source.write_bytes(b"\x00\x00\x00\x18ftypisom")

        ScanWorker(str(self.db.path), self.library_id).run()

        videos = self.db.videos(library_id=self.library_id)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["logical_ext"], ".mp4")
        self.assertEqual(videos[0]["physical_ext"], ".asset")

    def test_export_worker_copies_video_and_subtitles_without_moving_sources(self) -> None:
        source = self.root / "export.dll"
        source.write_bytes(b"masked-video-content")
        subtitle = self.root / "export.zh-CN.srt"
        subtitle.write_text("subtitle", encoding="utf-8")
        destination = Path(self.temp.name) / "exported"
        results: list[tuple] = []
        worker = ExportWorker(
            [
                {
                    "source": str(source),
                    "stem": source.stem,
                    "logical_ext": ".mp4",
                    "subtitles": [str(subtitle)],
                }
            ],
            destination,
        )
        worker.finished_export.connect(lambda *args: results.append(args))
        worker.run()

        self.assertEqual(results[0], (1, 1, []))
        self.assertEqual((destination / "export.mp4").read_bytes(), source.read_bytes())
        self.assertEqual(
            (destination / "export.zh-CN.srt").read_text(encoding="utf-8"),
            "subtitle",
        )
        self.assertTrue(source.exists())
        self.assertTrue(subtitle.exists())

    def test_export_action_queues_single_and_batch_items(self) -> None:
        app = QApplication.instance() or QApplication([])
        videos = []
        for name in ("export-a.mp4", "export-b.mp4"):
            path = self.root / name
            path.write_bytes(name.encode("ascii"))
            subtitle = path.with_suffix(".srt")
            subtitle.write_text(name, encoding="utf-8")
            videos.append(
                (
                    self._add_video(path, sidecars=[str(subtitle)]),
                    subtitle,
                )
            )
        destination = Path(self.temp.name) / "export-output"
        window = MainWindow(self.db.path)
        try:
            with (
                patch(
                    "app.ui.main_window.QFileDialog.getExistingDirectory",
                    return_value=str(destination),
                ),
                patch.object(window, "_enqueue_task") as enqueue,
                patch.object(window.task_center, "start_task"),
            ):
                window.export_videos([video_id for video_id, _path in videos])
            task = enqueue.call_args.args[0]
            self.assertEqual(task.kind, "export")
            self.assertEqual(len(task.payload["items"]), 2)
            self.assertTrue(
                all(
                    len(item["subtitles"]) == 1
                    for item in task.payload["items"]
                )
            )
        finally:
            window.close()
            app.processEvents()

    def test_existing_video_path_handles_mask_and_temporary_exposure(self) -> None:
        masked = self.root / "masked.dat"
        masked.write_bytes(b"video")
        video = {
            "path": str(self.root / "masked.mp4"),
            "logical_ext": ".mp4",
            "physical_ext": ".dat",
        }
        self.assertEqual(existing_video_path(video), masked)

        exposed = self.root / "exposed.ts"
        exposed.write_bytes(b"video")
        video = {
            "path": str(self.root / "exposed.dat"),
            "logical_ext": ".ts",
            "physical_ext": ".dat",
        }
        self.assertEqual(existing_video_path(video), exposed)

    def test_name_size_and_file_time_sorting(self) -> None:
        specs = (
            ("b.mp4", 200, 1_000_000_000),
            ("a.mp4", 100, 3_000_000_000),
            ("c.mp4", 300, 2_000_000_000),
        )
        for name, size, timestamp in specs:
            path = self.root / name
            path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * size)
            os.utime(path, ns=(timestamp, timestamp))
            self._add_video(path)
        self.assertEqual(
            [row["file_name"] for row in self.db.videos(sort="name")],
            ["a.mp4", "b.mp4", "c.mp4"],
        )
        self.assertEqual(
            [row["file_name"] for row in self.db.videos(sort="size")],
            ["c.mp4", "b.mp4", "a.mp4"],
        )
        self.assertEqual(
            [row["file_name"] for row in self.db.videos(sort="file_time")],
            ["a.mp4", "c.mp4", "b.mp4"],
        )

    def test_capture_time_sorting_supports_both_directions(self) -> None:
        specs = (
            ("old.mp4", "2024-01-02T03:04:05"),
            ("new.mp4", "2026-05-06T07:08:09"),
            ("middle.mp4", "2025-03-04T05:06:07"),
        )
        for name, capture_at in specs:
            path = self.root / name
            path.write_bytes(b"video")
            self._add_video(path, capture_at=capture_at)
        self.assertEqual(
            [
                row["file_name"]
                for row in self.db.videos(sort="capture_time", descending=True)
            ],
            ["new.mp4", "middle.mp4", "old.mp4"],
        )
        self.assertEqual(
            [
                row["file_name"]
                for row in self.db.videos(sort="capture_time", descending=False)
            ],
            ["old.mp4", "middle.mp4", "new.mp4"],
        )

    def test_sort_direction_control_uses_natural_default_and_toggles(self) -> None:
        app = QApplication.instance() or QApplication([])
        page = LibraryPage()
        page.sort_combo.setCurrentIndex(
            page.sort_combo.findData("capture_time")
        )
        self.assertTrue(page.sort_descending())
        page.sort_descending_button.click()
        self.assertFalse(page.sort_descending())
        page.sort_combo.setCurrentIndex(page.sort_combo.findData("name"))
        self.assertFalse(page.sort_descending())
        page.sort_descending_button.click()
        self.assertTrue(page.sort_descending())
        page.deleteLater()
        app.processEvents()

    def test_thumbnail_paths_are_relocatable(self) -> None:
        video_path = self.root / "thumb-test.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(video_path)
        thumb_dir = Path(self.temp.name) / THUMB_DIR_NAME
        thumb_dir.mkdir()
        thumb = thumb_dir / f"cache{THUMB_EXTENSION}"
        jpeg = b"\xff\xd8\xff\xe0thumbnail"
        thumb.write_bytes(jpeg)
        self.db.update_video(video_id, thumb_path=r"C:\Old\Data\thumbnails\cache.jpg")
        repaired = self.db.repair_thumbnail_paths(thumb_dir)
        self.assertEqual(repaired, 1)
        thumb_path = self.db.video(video_id)["thumb_path"]
        self.assertEqual(
            thumb_path,
            str(Path(THUMB_DIR_NAME) / f"cache{THUMB_EXTENSION}"),
        )
        enable_thumbnail_runtime_names(thumb_dir)
        runtime = thumb_dir / f"cache{THUMB_RUNTIME_EXTENSION}"
        self.assertFalse(thumb.exists())
        self.assertEqual(runtime.read_bytes(), jpeg)
        restore_thumbnail_file_names(thumb_dir)
        self.assertTrue(thumb.exists())
        self.assertFalse(runtime.exists())

    def test_delete_uses_recycle_bin(self) -> None:
        video_path = self.root / "delete-me.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(video_path)
        deleted, failures = delete_video(
            self.db,
            self.db.video(video_id),
            self.library,
            False,
        )
        self.assertEqual(deleted, 1)
        self.assertEqual(failures, [])
        self.assertFalse(video_path.exists())
        self.assertIsNone(self.db.video(video_id))

    def test_delete_missing_video_removes_database_record(self) -> None:
        video_path = self.root / "already-missing.mp4"
        video_path.write_bytes(b"video")
        nfo_path = self.root / "movie.nfo"
        nfo_path.write_text(
            "<movie><title>待清理</title><tag>旧标签</tag></movie>",
            encoding="utf-8",
        )
        video_id = self._add_video(
            video_path,
            nfo_path=str(nfo_path),
            nfo_tags=["旧标签"],
            sidecars=[str(nfo_path)],
        )
        self.db.set_video_tags(video_id, ["手动标签"])
        video_path.unlink()
        self.db.update_video(video_id, missing=1)

        deleted, failures = delete_video(
            self.db,
            self.db.video(video_id),
            self.library,
            True,
        )

        self.assertEqual(deleted, 1)
        self.assertEqual(failures, [])
        self.assertIsNone(self.db.video(video_id))
        self.assertFalse(nfo_path.exists())
        self.assertEqual(self.db.tags_for_video(video_id), [])
        self.assertEqual(self.db.all_tags(), [])

    def test_missing_cleanup_dialog_selects_sidecars_by_default(self) -> None:
        app = QApplication.instance() or QApplication([])
        dialog = DeleteDialog("missing", 3, missing=True)
        self.assertTrue(dialog.include_sidecars())
        dialog.deleteLater()
        app.processEvents()

    def _scan(self) -> None:
        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(
                scanner_module,
                "probe_video",
                return_value={"duration_ms": 120_000, "bitrate_bps": 1_000_000},
            ),
            patch.object(scanner_module, "thumbnail_for", return_value=False),
        ):
            ScanWorker(str(self.db.path), self.library_id).run()

    def test_scan_migrates_hashed_move_and_preserves_user_data(self) -> None:
        old_path = self.root / "old.mp4"
        old_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"content")
        video_id = self._add_video(old_path)
        category_id = self.db.create_category("保留分类")
        self.db.update_video(
            video_id,
            category_id=category_id,
            favorite=1,
            last_position_ms=42_000,
        )
        self.db.set_video_tags(video_id, ["保留标签"])

        archive = self.root / "archive"
        archive.mkdir()
        new_path = archive / "renamed.dat"
        os.replace(old_path, new_path)
        changed_time = new_path.stat().st_mtime_ns + 10_000_000
        os.utime(new_path, ns=(changed_time, changed_time))

        self._scan()
        video = self.db.video(video_id)
        self.assertEqual(video["path"], str(new_path.resolve()))
        self.assertFalse(video["missing"])
        self.assertEqual(video["category_id"], category_id)
        self.assertEqual(video["favorite"], 1)
        self.assertEqual(video["last_position_ms"], 42_000)
        self.assertEqual(self.db.tags_for_video(video_id), ["保留标签"])
        self.assertEqual(len(self.db.videos(library_id=self.library_id)), 1)

    def test_external_rename_updates_file_fields_and_display_name(self) -> None:
        old_path = self.root / "old-name.mp4"
        old_nfo = self.root / "old-name.nfo"
        old_path.write_bytes(b"\x00\x00\x00\x18ftypisom-rename")
        old_nfo.write_text(
            "<movie><title>NFO 旧标题</title><plot>详情保留</plot></movie>",
            encoding="utf-8",
        )
        self._scan()
        original = self.db.video_by_path(old_path.resolve())
        self.assertIsNotNone(original)
        self.assertEqual(original["title"], "NFO 旧标题")
        video_id = int(original["id"])

        new_path = self.root / "new-name.mp4"
        new_nfo = self.root / "new-name.nfo"
        os.replace(old_path, new_path)
        os.replace(old_nfo, new_nfo)
        self._scan()

        renamed = self.db.video(video_id)
        self.assertEqual(renamed["path"], str(new_path.resolve()))
        self.assertEqual(renamed["file_name"], "new-name.mp4")
        self.assertEqual(renamed["stem"], "new-name")
        self.assertEqual(renamed["title"], "new-name")
        self.assertEqual(renamed["plot"], "详情保留")

        self._scan()
        self.assertEqual(self.db.video(video_id)["title"], "new-name")

    def test_library_migration_preserves_database_metadata(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        video_path = nested / "movie.mp4"
        video_path.write_bytes(b"migration-video")
        video_id = self._add_video(video_path)
        category_id = self.db.create_category("迁移保留")
        self.db.update_video(
            video_id,
            category_id=category_id,
            favorite=1,
            rating=4,
            last_position_ms=12_345,
            nfo_path=str(nested / "movie.nfo"),
            poster_path=str(nested / "poster.txt"),
            subtitle_path=str(nested / "movie.zh.srt"),
            sidecars_json=json.dumps(
                [str(nested / "movie.nfo"), str(nested / "poster.txt")],
                ensure_ascii=False,
            ),
        )
        self.db.set_video_tags(video_id, ["迁移标签"])
        new_root = Path(self.temp.name) / "new-library"

        result = migrate_library(
            self.db.path,
            self.root,
            new_root,
            library_id=self.library_id,
            copy_files=True,
            apply=True,
        )

        moved = self.db.video(video_id)
        moved_path = new_root / "nested" / "movie.mp4"
        self.assertEqual(result["video_count"], 1)
        self.assertTrue(moved_path.exists())
        self.assertEqual(self.db.library(self.library_id)["root_path"], str(new_root))
        self.assertEqual(moved["path"], str(moved_path))
        self.assertEqual(moved["relative_path"], str(Path("nested") / "movie.mp4"))
        self.assertEqual(moved["category_id"], category_id)
        self.assertEqual(moved["favorite"], 1)
        self.assertEqual(moved["rating"], 4)
        self.assertEqual(moved["last_position_ms"], 12_345)
        self.assertEqual(self.db.tags_for_video(video_id), ["迁移标签"])
        self.assertEqual(moved["nfo_path"], str(new_root / "nested" / "movie.nfo"))
        self.assertEqual(
            moved["poster_path"],
            str(new_root / "nested" / "poster.txt"),
        )
        self.assertEqual(
            moved["subtitle_path"],
            str(new_root / "nested" / "movie.zh.srt"),
        )
        self.assertIn(
            str(new_root / "nested" / "poster.txt"),
            moved["sidecars"],
        )
        self.assertTrue(Path(result["backup"]).is_file())

    def test_library_migration_preview_does_not_write_database(self) -> None:
        video_path = self.root / "preview.mp4"
        video_path.write_bytes(b"preview-video")
        self._add_video(video_path)
        new_root = Path(self.temp.name) / "preview-not-created"

        result = migrate_library(
            self.db.path,
            self.root,
            new_root,
            library_id=self.library_id,
        )

        self.assertFalse(result["applied"])
        self.assertFalse(result["copied"])
        self.assertIsNone(result["backup"])
        self.assertFalse(new_root.exists())
        self.assertEqual(
            self.db.library(self.library_id)["root_path"],
            str(self.root),
        )

    def test_library_migration_works_after_manual_move(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        video_path = nested / "moved.mp4"
        video_path.write_bytes(b"manual-move-video")
        video_id = self._add_video(video_path)
        category_id = self.db.create_category("手动迁移保留")
        self.db.update_video(video_id, category_id=category_id, favorite=1)
        new_root = Path(self.temp.name) / "manually-moved"
        shutil.move(str(self.root), str(new_root))

        result = migrate_library(
            self.db.path,
            self.root,
            new_root,
            library_id=self.library_id,
            apply=True,
        )

        moved = self.db.video(video_id)
        self.assertTrue(result["applied"])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.db.library(self.library_id)["root_path"], str(new_root))
        self.assertEqual(moved["path"], str(new_root / "nested" / "moved.mp4"))
        self.assertEqual(moved["category_id"], category_id)
        self.assertEqual(moved["favorite"], 1)

    def test_main_window_relocates_current_library_without_new_ids(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "ui-move.mp4"
        video_path.write_bytes(b"ui-migration-video")
        video_id = self._add_video(video_path)
        video_path.write_bytes(b"ui-migration-video-after-metadata-write")
        category_id = self.db.create_category("界面迁移保留")
        self.db.update_video(video_id, category_id=category_id, favorite=1)
        new_root = Path(self.temp.name) / "ui-moved"
        shutil.move(str(self.root), str(new_root))

        class FakeRelocateDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.Accepted

            def new_root(self):
                return str(new_root)

        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            with (
                patch(
                    "app.ui.main_window.RelocateLibraryDialog",
                    FakeRelocateDialog,
                ),
                patch(
                    "app.ui.main_window.QMessageBox.question",
                    return_value=QMessageBox.Yes,
                ),
                patch(
                    "app.ui.main_window.QMessageBox.information"
                ) as information,
                patch("app.ui.main_window.QMessageBox.critical") as critical,
            ):
                window.relocate_current_library()
            critical.assert_not_called()
            self.assertIn("已刷新 1 个文件", information.call_args.args[2])
            library = self.db.library(self.library_id)
            moved = self.db.video(video_id)
            self.assertEqual(library["root_path"], str(new_root))
            self.assertEqual(moved["path"], str(new_root / "ui-move.mp4"))
            self.assertEqual(
                moved["size_bytes"],
                (new_root / "ui-move.mp4").stat().st_size,
            )
            self.assertIsNone(moved["content_hash"])
            self.assertEqual(moved["category_id"], category_id)
            self.assertEqual(moved["favorite"], 1)
            self.assertEqual(window.current_library_id, self.library_id)
        finally:
            window.close()
            app.processEvents()

    def test_scan_adds_new_files_and_removes_deleted_records(self) -> None:
        deleted_path = self.root / "deleted.mp4"
        deleted_path.write_bytes(b"\x00\x00\x00\x18ftypisomdeleted")
        deleted_id = self._add_video(deleted_path)
        deleted_path.unlink()

        added_path = self.root / "added.mkv"
        added_path.write_bytes(b"\x1a\x45\xdf\xa3new-file")
        self._scan()

        self.assertIsNone(self.db.video(deleted_id))
        added = self.db.video_by_path(added_path.resolve())
        self.assertIsNotNone(added)
        self.assertFalse(added["missing"])

    def test_incremental_scan_only_prunes_changed_directory(self) -> None:
        old_dir = self.root / "old-dir"
        new_dir = self.root / "new-dir"
        untouched_dir = self.root / "untouched-dir"
        for directory in (old_dir, new_dir, untouched_dir):
            directory.mkdir()
        deleted_path = old_dir / "deleted.mp4"
        deleted_path.write_bytes(b"old-video")
        deleted_id = self._add_video(deleted_path)
        untouched_path = untouched_dir / "untouched.mp4"
        untouched_path.write_bytes(b"untouched-video")
        untouched_id = self._add_video(untouched_path)
        deleted_path.unlink()
        added_path = new_dir / "added.mp4"
        added_path.write_bytes(b"new-video")
        outside_scope = untouched_dir / "outside.mp4"
        outside_scope.write_bytes(b"outside-video")

        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(
                scanner_module,
                "probe_video",
                return_value={
                    "duration_ms": 10_000,
                    "bitrate_bps": 1_000_000,
                    "width": 1920,
                    "height": 1080,
                },
            ),
            patch.object(scanner_module, "thumbnail_for", return_value=False),
        ):
            ScanWorker(
                str(self.db.path),
                self.library_id,
                directories=[str(new_dir)],
            ).run()

        self.assertIsNotNone(self.db.video_by_path(added_path.resolve()))
        self.assertFalse(self.db.video(deleted_id)["missing"])
        self.assertIsNone(self.db.video_by_path(outside_scope.resolve()))

        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(scanner_module, "probe_video", return_value={}),
            patch.object(scanner_module, "thumbnail_for", return_value=False),
        ):
            ScanWorker(
                str(self.db.path),
                self.library_id,
                directories=[str(old_dir)],
            ).run()

        self.assertIsNone(self.db.video(deleted_id))
        self.assertFalse(self.db.video(untouched_id)["missing"])

    def test_incremental_scan_keeps_existing_unrecognized_media(self) -> None:
        scope = self.root / "masked-scope"
        scope.mkdir()
        preserved = scope / "preserved.dll"
        preserved.write_bytes(b"masked-content")
        preserved_id = self._add_video(preserved, logical=".dll")
        deleted = scope / "deleted.dll"
        deleted.write_bytes(b"deleted-content")
        deleted_id = self._add_video(deleted, logical=".dll")
        deleted.unlink()
        original_logical = scanner_module.logical_extension

        def fake_logical(path: Path) -> str:
            if path == preserved:
                return ""
            return original_logical(path)

        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(scanner_module, "probe_video", return_value={}),
            patch.object(scanner_module, "thumbnail_for", return_value=False),
            patch.object(scanner_module, "logical_extension", side_effect=fake_logical),
        ):
            ScanWorker(
                str(self.db.path),
                self.library_id,
                directories=[str(scope)],
            ).run()

        self.assertIsNotNone(self.db.video(preserved_id))
        self.assertIsNone(self.db.video(deleted_id))

    def test_scan_refreshes_orientation_metadata_once(self) -> None:
        video_path = self.root / "rotated.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom-rotated")
        self._add_video(video_path, width=1920, height=1080)
        probe = {
            "duration_ms": 120_000,
            "bitrate_bps": 1_000_000,
            "width": 1080,
            "height": 1920,
        }
        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(scanner_module, "probe_video", return_value=probe) as mocked_probe,
            patch.object(scanner_module, "thumbnail_for", return_value=False),
        ):
            ScanWorker(str(self.db.path), self.library_id).run()
            ScanWorker(str(self.db.path), self.library_id).run()
        row = self.db.video_by_path(video_path.resolve())
        self.assertEqual((row["width"], row["height"]), (1080, 1920))
        self.assertEqual(mocked_probe.call_count, 1)

    def test_shooting_scan_stores_capture_device_and_orientation(self) -> None:
        video_path = self.root / "2026-05-04-084615- p3.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom-shooting")
        self.db.update_library_kind(self.library_id, "shooting")
        thumbs = Path(self.temp.name) / "thumbs"

        def create_thumbnail(_video: Path, output: Path, _duration: int, _poster) -> bool:
            self._write_jpeg(output, (270, 480), "#3355cc")
            return True

        with (
            patch.object(scanner_module, "THUMB_DIR", thumbs),
            patch.object(
                scanner_module,
                "probe_video",
                return_value={
                    "duration_ms": 30_000,
                    "bitrate_bps": 2_000_000,
                    "width": 1080,
                    "height": 1920,
                    "rotation": 270,
                    "codec": "hevc",
                    "fps": 30.0,
                    "capture_at": "",
                    "camera_make": "DJI",
                    "camera_model": "Osmo Pocket 3",
                    "lens": "固定镜头",
                    "location": "30.2,120.2",
                    "plot": "测试拍摄",
                },
            ),
            patch.object(scanner_module, "thumbnail_for", side_effect=create_thumbnail),
        ):
            ScanWorker(str(self.db.path), self.library_id).run()

        row = next(
            item for item in self.db.videos(library_id=self.library_id)
            if item["file_name"] == video_path.name
        )
        self.assertEqual(row["capture_at"], "2026-05-04T08:46:15")
        self.assertEqual(row["rotation"], 270)
        self.assertEqual((row["width"], row["height"]), (1080, 1920))
        self.assertEqual(row["camera_model"], "Osmo Pocket 3")
        self.assertEqual(row["lens"], "固定镜头")
        self.assertEqual(row["source_kind"], "shooting")

    def test_masked_rename_keeps_physical_extension(self) -> None:
        physical = self.root / "masked.dll"
        physical.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(physical, ".mp4")
        self.db.update_video(video_id, path=str(self.root / "masked.mp4"))

        new_path, _ = rename_video(
            self.db,
            self.db.video(video_id),
            self.library,
            "renamed",
        )
        self.assertEqual(new_path.suffix, ".dll")
        self.assertTrue((self.root / "renamed.dll").exists())
        self.assertFalse((self.root / "renamed.mp4").exists())
        updated = self.db.video(video_id)
        self.assertEqual(updated["physical_ext"], ".dll")
        self.assertEqual(updated["title"], "renamed")

    def test_masked_delete_uses_actual_extension(self) -> None:
        physical = self.root / "trash-masked.dat"
        physical.write_bytes(b"\x47" + b"\x00" * 187)
        video_id = self._add_video(physical, ".ts")
        self.db.update_video(video_id, path=str(self.root / "trash-masked.ts"))
        deleted, failures = delete_video(
            self.db,
            self.db.video(video_id),
            self.library,
            False,
        )
        self.assertEqual(deleted, 1)
        self.assertEqual(failures, [])
        self.assertFalse(physical.exists())
        self.assertIsNone(self.db.video(video_id))

    def test_sidecar_collection_reuses_directory_entries(self) -> None:
        video = self.root / "movie.mkv"
        video.write_bytes(b"video")
        subtitle = self.root / "movie.srt"
        subtitle.write_text("subtitle", encoding="utf-8")
        translated = self.root / "movie.zh-CN.srt"
        translated.write_text("translated", encoding="utf-8")
        unrelated = self.root / "movie2.srt"
        unrelated.write_text("unrelated", encoding="utf-8")
        siblings = [video, subtitle, translated, unrelated]
        with patch.object(Path, "iterdir", side_effect=AssertionError("unexpected scan")):
            self.assertEqual(
                collect_sidecars(video, None, None, siblings),
                [subtitle, translated],
            )

    def test_subtitle_command_targets_video_folder_and_optional_language(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        row = self.db.video(video_id)
        self.assertEqual(subtitle_target(row), self.root / "movie.srt")
        command = subtitle_command(
            Path("whisper.exe"),
            Path("models"),
            "large-v3",
            [video],
            self.root / "out",
            "ja",
        )
        self.assertEqual(command[0], str(video))
        self.assertIn("--model_dir", command)
        self.assertEqual(command[command.index("--language") + 1], "ja")
        self.assertEqual(command[command.index("--word_timestamps") + 1], "True")
        auto_command = subtitle_command(
            Path("whisper.exe"),
            Path("models"),
            "medium",
            [video],
            self.root / "out",
            "",
        )
        self.assertNotIn("--language", auto_command)

    def test_transcription_worker_command_uses_hidden_same_entrypoint(self) -> None:
        root = self.root / "app"
        command = transcription_worker_command(self.root / "job.json", root)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(root / "main.py"))
        self.assertEqual(command[2], "--transcribe-worker")
        self.assertEqual(command[3], str(self.root / "job.json"))

    def test_transcribe_job_writes_cues_and_reports_cuda_progress(self) -> None:
        source = self.root / "movie.mp4"
        source.write_bytes(b"video")
        output_dir = self.root / "output"
        output_dir.mkdir()
        calls: list[tuple[str, str | None, bool]] = []
        progress: list[tuple[int, str]] = []

        class FakeSegment:
            start = 1.0
            end = 2.5
            text = "本地字幕"

        class FakeInfo:
            duration = 4.0

        class FakeModel:
            def transcribe(self, path, language, vad_filter):
                calls.append((path, language, vad_filter))
                return [FakeSegment()], FakeInfo()

        with patch("app.transcribe_child._load_model", return_value=FakeModel()):
            transcribe_job(
                {
                    "model": "large-v3",
                    "model_root": self.root / "models",
                    "language": "ja",
                    "inputs": [source],
                    "output_dir": output_dir,
                },
                lambda percent, detail: progress.append((percent, detail)),
            )

        cues = parse_subtitle(output_dir / "movie.srt")
        self.assertEqual(cues[0].text, "本地字幕")
        self.assertEqual(calls, [(str(source), "ja", True)])
        self.assertTrue(any("CUDA float16" in detail for _percent, detail in progress))

    def test_transcribe_child_ignores_missing_standard_streams(self) -> None:
        with (
            patch("app.transcribe_child.sys.stdout", None),
            patch("app.transcribe_child.sys.stderr", None),
        ):
            _flush_stdio()

    def test_transcribe_child_json_events_are_ascii_safe(self) -> None:
        payload = {"type": "progress", "detail": "正在加载 large-v3 模型"}
        encoded = _event_line(payload)
        self.assertTrue(encoded.isascii())
        self.assertEqual(json.loads(encoded)["detail"], payload["detail"])

    def test_subtitle_worker_commits_srt_and_updates_database(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        result: dict = {}

        def fake_process(_executable, _model_root, inputs, output_dir) -> tuple[bool, str, bool]:
            subtitle = output_dir / f"{inputs[0].stem}.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n本地字幕\n",
                encoding="utf-8",
            )
            return True, "", False

        worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "ja",
            "medium",
        )
        worker.completed.connect(lambda *args: result.setdefault("completed", args))
        with (
            patch(
                "app.transcription.whisper_installation",
                return_value=(Path("whisper.exe"), self.root),
            ),
            patch("app.transcription.validate_whisper_installation", return_value=[]),
            patch.object(SubtitleWorker, "_run_process", side_effect=fake_process),
        ):
            worker.run()

        target = self.root / "movie.srt"
        self.assertTrue(target.exists())
        self.assertEqual(parse_subtitle(target)[0].text, "本地字幕")
        updated = self.db.video(video_id)
        self.assertEqual(updated["subtitle_path"], str(target))
        self.assertIn(str(target), updated["sidecars"])
        self.assertEqual(result["completed"][:4], (1, 0, 0, False))

    def test_subtitle_worker_handles_masked_video(self) -> None:
        masked = self.root / "masked.dat"
        masked.write_bytes(b"video")
        video_id = self._add_video(masked, logical=".ts")
        self.db.update_video(video_id, path=str(self.root / "masked.ts"))
        inputs_seen: list[Path] = []

        def fake_process(_executable, _model_root, inputs, output_dir) -> tuple[bool, str, bool]:
            inputs_seen.extend(inputs)
            (output_dir / f"{inputs[0].stem}.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,500\n伪装字幕\n",
                encoding="utf-8",
            )
            return True, "", False

        worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "ja",
            "medium",
        )
        with (
            patch(
                "app.transcription.whisper_installation",
                return_value=(Path("whisper.exe"), self.root),
            ),
            patch("app.transcription.validate_whisper_installation", return_value=[]),
            patch.object(SubtitleWorker, "_run_process", side_effect=fake_process),
        ):
            worker.run()

        target = self.root / "masked.srt"
        self.assertEqual(inputs_seen, [masked])
        self.assertEqual(parse_subtitle(target)[0].text, "伪装字幕")
        self.assertEqual(self.db.video(video_id)["subtitle_path"], str(target))

    def test_subtitle_worker_translates_masked_video_to_chinese(self) -> None:
        masked = self.root / "translated.dat"
        masked.write_bytes(b"video")
        video_id = self._add_video(masked, logical=".ts")
        self.db.update_video(video_id, path=str(self.root / "translated.ts"))

        def fake_process(_executable, _model_root, inputs, output_dir) -> tuple[bool, str, bool]:
            (output_dir / f"{inputs[0].stem}.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,500\nこれはテストです。\n",
                encoding="utf-8",
            )
            return True, "", False

        def fake_translate(cues, source_language, **_kwargs):
            self.assertEqual(source_language, "ja")
            return [SubtitleCue(cue.start_ms, cue.end_ms, "这是测试。") for cue in cues]

        worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "ja",
            "medium",
            translate=True,
        )
        completed: list[tuple] = []
        worker.completed.connect(lambda *args: completed.append(args))
        with (
            patch(
                "app.transcription.whisper_installation",
                return_value=(Path("whisper.exe"), self.root),
            ),
            patch("app.transcription.validate_whisper_installation", return_value=[]),
            patch.object(SubtitleWorker, "_run_process", side_effect=fake_process),
            patch("app.transcription.translate_cues", side_effect=fake_translate),
        ):
            worker.run()

        original = self.root / "translated.srt"
        translated = self.root / "translated.zh-CN.srt"
        self.assertEqual(parse_subtitle(original)[0].text, "これはテストです。")
        self.assertEqual(parse_subtitle(translated)[0].text, "这是测试。")
        updated = self.db.video(video_id)
        self.assertEqual(updated["subtitle_path"], str(translated))
        self.assertIn(str(original), updated["sidecars"])
        self.assertIn(str(translated), updated["sidecars"])
        self.assertEqual(completed[0][:4], (1, 0, 0, False))

    def test_subtitle_worker_translation_only_skips_whisper(self) -> None:
        video = self.root / "translate-only.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        source = self.root / "translate-only.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,500\nこれはテストです。\n",
            encoding="utf-8",
        )
        completed: list[tuple] = []
        worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "ja",
            "",
            translate=True,
            translation_only=True,
        )
        worker.completed.connect(lambda *args: completed.append(args))

        def fake_translate(cues, source_language, **_kwargs):
            self.assertEqual(source_language, "ja")
            return [
                SubtitleCue(cue.start_ms, cue.end_ms, "这是翻译。")
                for cue in cues
            ]

        with (
            patch(
                "app.transcription.whisper_installation",
                side_effect=AssertionError("translation-only must not load Whisper"),
            ),
            patch("app.transcription.translate_cues", side_effect=fake_translate),
        ):
            worker.run()

        target = self.root / "translate-only.zh-CN.srt"
        self.assertEqual(parse_subtitle(target)[0].text, "这是翻译。")
        self.assertEqual(self.db.video(video_id)["subtitle_path"], str(target))
        self.assertEqual(completed[0][:4], (1, 0, 0, False))

    def test_existing_subtitle_enqueues_translation_only_without_whisper(self) -> None:
        app = QApplication.instance() or QApplication([])
        video = self.root / "existing.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        (self.root / "existing.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
            encoding="utf-8",
        )
        dialog = MagicMock()
        dialog.values.return_value = ("ja", "large-v3", False, True)
        window = MainWindow(self.db.path)
        try:
            with (
                patch(
                    "app.ui.main_window.validate_whisper_installation",
                    side_effect=AssertionError("must not validate Whisper"),
                ),
                patch.object(window, "_enqueue_subtitle_tasks") as enqueue,
            ):
                window._begin_subtitle_generation(
                    [self.db.video(video_id)],
                    dialog,
                )
            enqueue.assert_called_once()
            self.assertTrue(enqueue.call_args.kwargs["translation_only"])
        finally:
            window.close()
            app.processEvents()

    def test_existing_translated_subtitle_skips_new_task(self) -> None:
        app = QApplication.instance() or QApplication([])
        video = self.root / "translated-existing.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        (self.root / "translated-existing.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
            encoding="utf-8",
        )
        (self.root / "translated-existing.zh-CN.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n你好\n",
            encoding="utf-8",
        )
        dialog = MagicMock()
        dialog.values.return_value = ("ja", "large-v3", False, True)
        window = MainWindow(self.db.path)
        try:
            with (
                patch(
                    "app.ui.main_window.validate_whisper_installation",
                    side_effect=AssertionError("must not validate Whisper"),
                ),
                patch.object(window, "_enqueue_subtitle_tasks") as enqueue,
            ):
                window._begin_subtitle_generation(
                    [self.db.video(video_id)],
                    dialog,
                )
            enqueue.assert_not_called()
            self.assertIn("跳过 1", window.status_label.text())
        finally:
            window.close()
            app.processEvents()

    def test_overwrite_existing_subtitles_generates_and_translates_again(self) -> None:
        app = QApplication.instance() or QApplication([])
        video = self.root / "overwrite-existing.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        (self.root / "overwrite-existing.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
            encoding="utf-8",
        )
        (self.root / "overwrite-existing.zh-CN.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n你好\n",
            encoding="utf-8",
        )
        dialog = MagicMock()
        dialog.values.return_value = ("ja", "large-v3", True, True)
        window = MainWindow(self.db.path)
        try:
            with (
                patch(
                    "app.ui.main_window.validate_whisper_installation",
                    return_value=[],
                ) as validate,
                patch.object(window, "_enqueue_subtitle_tasks") as enqueue,
            ):
                window._begin_subtitle_generation(
                    [self.db.video(video_id)],
                    dialog,
                )
            validate.assert_called_once_with("large-v3")
            enqueue.assert_called_once()
            self.assertFalse(
                enqueue.call_args.kwargs.get("translation_only", False)
            )
        finally:
            window.close()
            app.processEvents()

    def test_translation_batches_preserve_cues_and_timing(self) -> None:
        cues = [
            SubtitleCue(1000, 2000, "こんにちは"),
            SubtitleCue(2500, 3500, "ありがとう"),
        ]
        generated: list[str] = []

        def fake_generate(prompt: str) -> str:
            generated.append(prompt)
            return "1. 你好\n2. 谢谢"

        with (
            patch("app.translation.ensure_translation_model"),
            patch("app.translation._generate", side_effect=fake_generate),
        ):
            result = translate_cues(cues, "ja")

        self.assertIn("Japanese", generated[0])
        self.assertEqual([cue.text for cue in result], ["你好", "谢谢"])
        self.assertEqual(
            [(cue.start_ms, cue.end_ms) for cue in result],
            [(1000, 2000), (2500, 3500)],
        )
        self.assertIn("00:00:02,500 --> 00:00:03,500", format_srt(result))

    def test_translation_timeout_is_reported_separately(self) -> None:
        with patch(
            "app.translation.urllib.request.urlopen",
            side_effect=URLError(TimeoutError("timed out")),
        ):
            with self.assertRaises(translation_module.TranslationError) as caught:
                translation_module._request_json("/api/generate", {"prompt": "x"})
        self.assertIn("超时", str(caught.exception))
        self.assertNotIn("不可用", str(caught.exception))

    def test_subtitle_worker_skips_existing_srt_and_cleans_cancelled_output(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        existing = self.root / "movie.srt"
        existing.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n保留原字幕\n",
            encoding="utf-8",
        )
        skipped_result: dict = {}
        skipped_worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "",
            "medium",
        )
        skipped_worker.completed.connect(
            lambda *args: skipped_result.setdefault("completed", args)
        )
        with (
            patch(
                "app.transcription.whisper_installation",
                return_value=(Path("whisper.exe"), self.root),
            ),
            patch("app.transcription.validate_whisper_installation", return_value=[]),
            patch.object(
                SubtitleWorker,
                "_run_process",
                side_effect=AssertionError("existing SRT must not start Whisper"),
            ),
        ):
            skipped_worker.run()
        self.assertEqual(skipped_result["completed"][:4], (0, 1, 0, False))
        self.assertEqual(parse_subtitle(existing)[0].text, "保留原字幕")
        self.assertIsNone(self.db.video(video_id)["subtitle_path"])

        existing.unlink()
        temporary_dirs: list[Path] = []
        cancelled_result: dict = {}

        def fake_cancelled(_executable, _model_root, inputs, output_dir):
            temporary_dirs.append(output_dir)
            (output_dir / f"{inputs[0].stem}.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n半成品\n",
                encoding="utf-8",
            )
            return False, "已取消", True

        cancelled_worker = SubtitleWorker(
            str(self.db.path),
            [self.db.video(video_id)],
            "en",
            "medium",
        )
        cancelled_worker.completed.connect(
            lambda *args: cancelled_result.setdefault("completed", args)
        )
        with (
            patch(
                "app.transcription.whisper_installation",
                return_value=(Path("whisper.exe"), self.root),
            ),
            patch("app.transcription.validate_whisper_installation", return_value=[]),
            patch.object(SubtitleWorker, "_run_process", side_effect=fake_cancelled),
        ):
            cancelled_worker.run()
        self.assertFalse(existing.exists())
        self.assertFalse(temporary_dirs[0].exists())
        self.assertTrue(cancelled_result["completed"][3])

    def test_task_center_tracks_scan_and_subtitle_tasks(self) -> None:
        app = QApplication.instance() or QApplication([])
        dialog = TaskCenterDialog()
        self.assertFalse(dialog.isVisible())
        dialog.start_task("scan-1", "资源扫描", "测试库", "准备扫描")
        dialog.update_task("scan-1", progress=60, detail="3/5  movie.mp4")
        bar = dialog.table.cellWidget(0, 3).findChild(QProgressBar)
        self.assertEqual(bar.value(), 60)
        dialog.finish_task("scan-1", "completed", "导入 5 个视频")
        self.assertEqual(dialog.table.item(0, 0).text(), "已完成")
        subtitle = self.root / "movie.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
            encoding="utf-8",
        )
        dialog.start_task(
            "subtitle-1",
            "字幕生成",
            "1 个视频",
            "加载模型",
            [str(subtitle)],
        )
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(dialog.table.item(0, 0).text(), "运行中")
        dialog.finish_task("subtitle-1", "completed", "生成 1，跳过 0，失败 0")
        dialog.table.selectRow(0)
        dialog._selection_changed()
        self.assertTrue(dialog.open_subtitle_button.isEnabled())
        self.assertTrue(dialog.open_folder_button.isEnabled())
        with patch("app.ui.task_center.QDesktopServices.openUrl", return_value=True):
            dialog._open_selected_subtitle()
        dialog.start_task("subtitle-2", "字幕生成", "1 个视频", "加载模型")
        dialog.table.selectRow(0)
        dialog._selection_changed()
        self.assertTrue(dialog.cancel_button.isEnabled())
        dialog.clear_finished()
        self.assertEqual(dialog.table.rowCount(), 1)
        dialog.start_task(
            "subtitle-queued",
            "字幕生成",
            "排队视频",
            "等待执行",
            state="queued",
        )
        self.assertEqual(dialog.table.item(0, 0).text(), "排队中")
        dialog.table.selectRow(0)
        dialog._selection_changed()
        self.assertTrue(dialog.cancel_button.isEnabled())
        dialog.clear_finished()
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.deleteLater()
        app.processEvents()

    def test_main_window_does_not_import_whisper_at_startup(self) -> None:
        app = QApplication.instance() or QApplication([])
        self.assertNotIn("faster_whisper", sys.modules)
        video = self.root / "lazy.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        window = MainWindow(self.db.path)
        app.processEvents()
        self.assertNotIn("faster_whisper", sys.modules)
        self.assertNotIn("ctranslate2", sys.modules)
        self.assertEqual(window.top_bar.tasks_button.text(), "任务中心")
        self.assertFalse(window.task_center.isVisible())
        window.top_bar.tasks_button.click()
        app.processEvents()
        self.assertTrue(window.task_center.isVisible())
        window.start_subtitle_generation([video_id])
        app.processEvents()
        app.processEvents()
        self.assertIsNotNone(window.subtitle_options_dialog)
        self.assertTrue(window.subtitle_options_dialog.isVisible())
        self.assertFalse(window.subtitle_options_dialog.isModal())
        self.assertEqual(window.subtitle_options_dialog.language_combo.currentData(), "")
        self.assertTrue(window.subtitle_options_dialog.translate_checkbox.isChecked())
        self.assertTrue(window.isEnabled())
        window.subtitle_options_dialog.reject()
        app.processEvents()
        window.close()
        app.processEvents()

    def test_subtitle_worker_is_released_only_after_thread_finishes(self) -> None:
        app = QApplication.instance() or QApplication([])
        video = self.root / "thread-lifecycle.mp4"
        video.write_bytes(b"video")
        video_id = self._add_video(video)
        created: list[QThread] = []

        class ProbeWorker(QThread):
            progress = Signal(int, str)
            completed = Signal(int, int, int, bool, str)

            def __init__(self, *_args, parent=None):
                super().__init__(parent)
                created.append(self)

            def run(self) -> None:
                self.completed.emit(1, 0, 0, False, "完成")
                QThread.msleep(500)

        window = MainWindow(self.db.path)
        window.refresh_all = lambda: None
        with (
            patch("app.ui.main_window.SubtitleWorker", ProbeWorker),
            patch("app.ui.main_window.log"),
            patch.object(window.task_center, "start_tasks"),
            patch.object(window.task_center, "run_task"),
            patch.object(window.task_center, "finish_task"),
        ):
            window._launch_subtitle_worker(
                [self.db.video(video_id)],
                "",
                "large-v3",
                False,
                False,
            )
            deadline = 1000
            while not created and deadline > 0:
                QTest.qWait(10)
                deadline -= 10

            self.assertTrue(created)
            self.assertIs(window.subtitle_worker, created[0])
            self.assertTrue(created[0].isRunning())

            self.assertTrue(created[0].wait(2000))
            QTest.qWait(20)
            self.assertIsNone(window.subtitle_worker)

        window.close()
        app.processEvents()

    def test_subtitle_queue_accepts_more_jobs_and_runs_serially(self) -> None:
        app = QApplication.instance() or QApplication([])
        videos = []
        for name in ("queue-a.mp4", "queue-b.mp4"):
            path = self.root / name
            path.write_bytes(b"video")
            videos.append(self.db.video(self._add_video(path)))
        started: list[int] = []

        class SerialWorker(QThread):
            progress = Signal(int, str)
            completed = Signal(int, int, int, bool, str)

            def __init__(self, _db_path, input_videos, *_args, parent=None):
                super().__init__(parent)
                self.video_id = int(input_videos[0]["id"])

            def run(self) -> None:
                started.append(self.video_id)
                QThread.msleep(120)
                self.completed.emit(1, 0, 0, False, "完成")

        window = MainWindow(self.db.path)
        window.refresh_all = lambda: None
        try:
            with (
                patch("app.ui.main_window.SubtitleWorker", SerialWorker),
                patch("app.ui.main_window.log"),
                patch.object(window.task_center, "start_tasks"),
                patch.object(window.task_center, "run_task"),
                patch.object(window.task_center, "finish_task"),
            ):
                window._launch_subtitle_worker(
                    [videos[0]],
                    "",
                    "large-v3",
                    False,
                    False,
                )
                deadline = 2000
                while not started and deadline > 0:
                    QTest.qWait(10)
                    deadline -= 10
                self.assertEqual(started, [int(videos[0]["id"])])
                window._launch_subtitle_worker(
                    [videos[1]],
                    "",
                    "large-v3",
                    False,
                    False,
                )
                self.assertEqual(len(window._task_queue), 1)
                deadline = 4000
                while (
                    (len(started) < 2 or window._active_task or window._task_queue)
                    and deadline > 0
                ):
                    QTest.qWait(10)
                    deadline -= 10
            self.assertEqual(
                started,
                [int(videos[0]["id"]), int(videos[1]["id"])],
            )
        finally:
            window.close()
            app.processEvents()

    def test_empty_category_and_all_view_reset_scope_filters(self) -> None:
        app = QApplication.instance() or QApplication([])
        video = self.root / "scope.mp4"
        video.write_bytes(b"video")
        self._add_video(video)
        empty_category_id = self.db.create_category("空分类")
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window.set_metadata_filters({"rating": "5 星"})
            window.library_page.search_edit.setText("不存在的标题")
            window.library_page.set_orientation("portrait")

            window.select_category(empty_category_id)
            self.assertEqual(window.current_metadata_filters, {})
            self.assertEqual(window.current_nfo_filters, {})
            self.assertEqual(window.library_page.search_edit.text(), "")
            self.assertEqual(window.library_page.orientation(), "all")
            self.assertEqual(window.library_page.current_videos(), [])

            window.select_view("all")
            QTest.qWait(300)
            self.assertEqual(window.library_page.title_label.text(), "全部视频")
            self.assertEqual(len(window.library_page.current_videos()), 1)
            self.assertEqual(window.current_metadata_filters, {})
        finally:
            window.close()

    def _write_jpeg(self, path: Path, size: tuple[int, int], color: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(size[0], size[1], QImage.Format_RGB32)
        image.fill(QColor(color))
        self.assertTrue(image.save(str(path), "JPEG"))

    def test_library_artwork_uses_existing_preview_images(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom")
        poster = self.root / "poster.txt"
        fanart = self.root / "fanart.txt"
        screenshots = self.root / ".screenshot"
        self._write_jpeg(poster, (120, 180), "#cc5533")
        self._write_jpeg(fanart, (320, 180), "#3355cc")
        self._write_jpeg(screenshots / "ScreenShot-01.txt", (320, 180), "#33cc55")
        self._write_jpeg(screenshots / "ScreenShot-00.txt", (320, 180), "#cccc33")

        self.assertEqual(find_poster(video), poster)
        self.assertEqual(find_preview(video), fanart)
        sidecars = collect_sidecars(video, None, poster)
        self.assertIn(fanart, sidecars)
        self.assertIn(screenshots / "ScreenShot-00.txt", sidecars)

        fanart.unlink()
        self.assertEqual(find_preview(video), screenshots / "ScreenShot-00.txt")
        (screenshots / "ScreenShot-00.txt").unlink()
        (screenshots / "ScreenShot-01.txt").unlink()
        self.assertEqual(find_preview(video), poster)

        output = Path(self.temp.name) / "generated-preview.vthumb"
        with patch(
            "app.media_tools._hidden_run",
            side_effect=AssertionError("FFmpeg extraction should be skipped"),
        ):
            self.assertTrue(thumbnail_for(video, output, 120_000, poster))
        self.assertTrue(output.exists())

    def test_artwork_runtime_exposes_and_restores_source_images(self) -> None:
        video = self.root / "movie.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom")
        poster = self.root / "poster.txt"
        fanart = self.root / "fanart.txt"
        screenshot = self.root / ".screenshot" / "ScreenShot-00.txt"
        self._write_jpeg(poster, (120, 180), "#cc5533")
        self._write_jpeg(fanart, (320, 180), "#3355cc")
        self._write_jpeg(screenshot, (320, 180), "#33cc55")
        video_id = self._add_video(video)
        self.db.update_video(
            video_id,
            poster_path=str(poster),
            sidecars_json=json.dumps(
                [str(poster), str(fanart), str(screenshot)],
                ensure_ascii=False,
            ),
        )

        journal = Path(self.temp.name) / "artwork-journal.json"
        runtime = ArtworkRuntime(self.db)
        self.assertGreaterEqual(runtime.refresh(), 3)
        self.assertEqual(runtime.refresh(), 0)
        self.assertTrue(poster.with_suffix(".jpg").exists())
        self.assertTrue(fanart.with_suffix(".jpg").exists())
        self.assertTrue(screenshot.with_suffix(".jpg").exists())
        self.assertTrue(journal.exists())
        self.assertEqual(runtime_artwork_path(poster).suffix, ".jpg")

        runtime.restore()
        self.assertTrue(poster.exists())
        self.assertTrue(fanart.exists())
        self.assertTrue(screenshot.exists())
        self.assertFalse(journal.exists())

    def test_artwork_runtime_supports_custom_image_suffix(self) -> None:
        video = self.root / "custom-art.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom")
        poster = self.root / "poster.cover"
        self._write_jpeg(poster, (120, 180), "#cc5533")
        video_id = self._add_video(video)
        self.db.update_video(
            video_id,
            poster_path=str(poster),
            sidecars_json=json.dumps([str(poster)], ensure_ascii=False),
        )
        custom = dict(DEFAULT_MASK_MAP)
        custom[".jpg"] = ".cover"
        self.db.set_mask_map(custom)

        runtime = ArtworkRuntime(self.db)
        self.assertGreaterEqual(runtime.refresh(), 1)
        exposed = poster.with_suffix(".jpg")
        self.assertTrue(exposed.exists())
        self.assertEqual(runtime_artwork_path(poster), exposed)

        runtime.restore()
        self.assertTrue(poster.exists())
        self.assertFalse(exposed.exists())

    def test_scan_uses_source_artwork_or_generates_fallback(self) -> None:
        artwork_video = self.root / "artwork.mp4"
        artwork_video.write_bytes(b"\x00\x00\x00\x18ftypisom-artwork")
        self._write_jpeg(self.root / "fanart.txt", (320, 180), "#3355cc")
        with (
            patch.object(scanner_module, "THUMB_DIR", Path(self.temp.name) / "thumbs"),
            patch.object(
                scanner_module,
                "probe_video",
                return_value={"duration_ms": 120_000, "bitrate_bps": 1_000_000},
            ),
            patch.object(scanner_module, "thumbnail_for") as thumbnail,
        ):
            ScanWorker(str(self.db.path), self.library_id).run()
            thumbnail.assert_not_called()
        artwork_row = next(
            row for row in self.db.videos(library_id=self.library_id)
            if row["file_name"] == "artwork.mp4"
        )
        self.assertEqual(artwork_row["thumb_path"], str((self.root / "fanart.txt").resolve()))

        (self.root / "fanart.txt").unlink()
        fallback_video = self.root / "fallback.mp4"
        fallback_video.write_bytes(b"\x00\x00\x00\x18ftypisom-fallback")
        thumbs = Path(self.temp.name) / "thumbs"

        def create_thumbnail(_video: Path, output: Path, _duration: int, _poster) -> bool:
            self._write_jpeg(output, (320, 180), "#33cc55")
            return True

        with (
            patch.object(scanner_module, "THUMB_DIR", thumbs),
            patch.object(
                scanner_module,
                "probe_video",
                return_value={"duration_ms": 120_000, "bitrate_bps": 1_000_000},
            ),
            patch.object(scanner_module, "thumbnail_for", side_effect=create_thumbnail) as thumbnail,
        ):
            ScanWorker(str(self.db.path), self.library_id).run()
            thumbnail.assert_called()
        fallback_row = next(
            row for row in self.db.videos(library_id=self.library_id)
            if row["file_name"] == "fallback.mp4"
        )
        fallback_path = Path(fallback_row["thumb_path"])
        self.assertEqual(fallback_path.parent.name, THUMB_DIR_NAME)
        self.assertEqual(fallback_path.suffix, THUMB_EXTENSION)
        self.assertTrue(
            (thumbs / fallback_path.with_suffix(THUMB_RUNTIME_EXTENSION).name).exists()
        )

    def test_move_keeps_screenshot_folder_with_video(self) -> None:
        video_path = self.root / "movie.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        screenshot = self.root / ".screenshot" / "ScreenShot-00.txt"
        self._write_jpeg(screenshot, (320, 180), "#3355cc")
        video_id = self._add_video(video_path)
        self.db.update_video(
            video_id,
            sidecars_json=json.dumps([str(screenshot)], ensure_ascii=False),
        )

        target = self.root / "archive"
        target.mkdir()
        move_video(self.db, self.db.video(video_id), self.library, target)

        moved_screenshot = target / ".screenshot" / screenshot.name
        self.assertTrue(moved_screenshot.exists())
        self.assertFalse(screenshot.exists())
        self.assertIn(
            str(moved_screenshot),
            json.loads(self.db.video(video_id)["sidecars_json"]),
        )

    def test_tags_dialog_echoes_adds_and_removes_tags(self) -> None:
        app = QApplication.instance() or QApplication([])
        dialog = TagsDialog(
            ["已有", "保留"],
            available=["已有", "保留", "可加入"],
        )
        self.assertEqual(
            [
                dialog.tag_combo.itemText(index)
                for index in range(dialog.tag_combo.count())
            ],
            ["已有  ·  已设置", "保留  ·  已设置", "可加入"],
        )
        self.assertEqual(dialog.tag_edit.text(), "")
        self.assertEqual(dialog.existing_button.text(), "已有标签")
        self.assertFalse(dialog.tag_combo.model().item(0).isEnabled())
        self.assertFalse(dialog.tag_combo.model().item(1).isEnabled())
        dialog._add_combo_item(2)
        dialog.tag_edit.setText("新增，另一个")
        dialog._add_from_input()
        retained_item = dialog.tags_list.item(1)
        remove_button = dialog.tags_list.itemWidget(retained_item).findChild(
            QToolButton,
            "TagRemoveButton",
        )
        self.assertFalse(remove_button.icon().isNull())
        remove_button.click()
        self.assertNotIn("保留", dialog.values())
        dialog._add_combo_item(dialog.tag_combo.findData("保留"))
        self.assertEqual(
            dialog.values(),
            ["已有", "可加入", "新增", "另一个", "保留"],
        )
        dialog.deleteLater()
        app.processEvents()

    def test_player_receives_current_manual_tags(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "tagged-player.mp4"
        video_path.write_bytes(b"video")
        video_id = self._add_video(video_path)
        self.db.set_video_tags(video_id, ["标签甲", "标签乙"])
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            video = self.db.video(video_id)
            window.player_view.play_video(video, [video])
            self.assertEqual(window.player_view.tags_button.text(), "标签 2")
            self.assertIn("标签甲", window.player_view.tags_button.toolTip())
        finally:
            window.close()
            app.processEvents()

    def test_player_tag_metadata_write_waits_for_close(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "deferred-tags.mp4"
        video_path.write_bytes(b"video")
        video_id = self._add_video(video_path)
        self.db.set_video_tags(video_id, ["已有标签"])
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window.player_view.current_video = {"id": video_id}
            window._player_active = True

            class FakeTagsDialog:
                def __init__(self, *_args, **_kwargs):
                    pass

                def exec(self):
                    return QDialog.Accepted

                def values(self):
                    return ["已有标签", "新标签"]

            with (
                patch("app.ui.main_window.TagsDialog", FakeTagsDialog),
                patch(
                    "app.ui.main_window.write_video_tags",
                    return_value=(True, ""),
                ) as writer,
            ):
                window.edit_tags([video_id])
                self.assertEqual(
                    self.db.tags_for_video(video_id),
                    ["已有标签", "新标签"],
                )
                self.assertIsNotNone(window.sidebar._tag_items.get("新标签"))
                self.assertEqual(window._pending_tag_writes[video_id], ["已有标签", "新标签"])
                writer.assert_not_called()

                window._player_active = False
                window._flush_pending_tag_writes()
                writer.assert_called_once_with(
                    video_path,
                    ["已有标签", "新标签"],
                )
                self.assertEqual(window._pending_tag_writes, {})
        finally:
            window.close()
            app.processEvents()

    def test_player_rating_updates_sidebar_before_file_write(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "deferred-rating.mp4"
        video_path.write_bytes(b"video")
        video_id = self._add_video(video_path, rating=2)
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            window.player_view.current_video = {"id": video_id}
            window._player_active = True

            with patch(
                "app.ui.main_window.write_video_rating",
                return_value=(True, ""),
            ) as writer:
                window.set_video_rating(video_id, 5)

                self.assertEqual(self.db.video(video_id)["rating"], 5)
                self.assertEqual(window._pending_rating_writes[video_id], 5)
                self.assertEqual(
                    window.sidebar._rating_items[5].data(0, COUNT_ROLE),
                    1,
                )
                self.assertEqual(
                    window.sidebar._rating_items[2].data(0, COUNT_ROLE),
                    0,
                )
                writer.assert_not_called()

                window._player_active = False
                window._flush_pending_rating_writes()
                writer.assert_called_once_with(video_path, 5)
                self.assertEqual(window._pending_rating_writes, {})
        finally:
            window.close()
            app.processEvents()

    def test_masked_video_metadata_writes_stay_in_database(self) -> None:
        app = QApplication.instance() or QApplication([])
        video_path = self.root / "masked-metadata.asset"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        video_id = self._add_video(video_path, logical=".mp4")
        window = MainWindow(self.db.path)
        try:
            window.select_library(self.library_id)
            with (
                patch("app.ui.main_window.write_video_tags") as tag_writer,
                patch("app.ui.main_window.write_video_rating") as rating_writer,
                patch("app.ui.main_window.QMessageBox.warning") as warning,
            ):
                window.set_tags(video_id, ["伪装标签"])
                window.set_video_rating(video_id, 4)
                window._pending_tag_writes[video_id] = ["伪装标签"]
                window._pending_rating_writes[video_id] = 4
                window._flush_pending_tag_writes()
                window._flush_pending_rating_writes()

            tag_writer.assert_not_called()
            rating_writer.assert_not_called()
            warning.assert_not_called()
            self.assertEqual(self.db.video(video_id)["rating"], 4)
            self.assertEqual(self.db.tags_for_video(video_id), ["伪装标签"])
            self.assertIn("仅保存在影库", window.status_label.text())
        finally:
            window.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
