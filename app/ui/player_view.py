from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QRectF, QSize, QSizeF, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..constants import APP_TITLE, DEFAULT_MASK_MAP
from ..media_tools import existing_video_path, format_duration
from ..operations import (
    clear_temporary_exposure,
    record_temporary_exposure,
)
from ..theme import COLORS
from .icons import svg_icon
from .widgets import ClickableSlider
from ..subtitles import cue_at, find_default_subtitle, parse_subtitle


class VideoSurface(QGraphicsView):
    double_clicked = Signal()
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("background: #050607; border: 0;")
        self.setScene(QGraphicsScene(self))
        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.KeepAspectRatio)
        self.video_item.setZValue(0)
        self.scene().addItem(self.video_item)
        self.scene().setBackgroundBrush(Qt.black)
        self._rotation = 0

    def set_video_rotation(self, degrees: int) -> None:
        """记录库里的 rotation 值。

        Qt 6.12 的 Windows Media Foundation 后端自己就应用了 display matrix
        （实测 videoSink.videoSize() 对 rotation=270 的视频返回 2160×3840），
        所以画面不能再手动旋转，否则会转两次导致方向反掉。
        """
        self._rotation = int(degrees or 0) % 360
        self._layout_video()

    def _layout_video(self) -> None:
        rect = QRectF(self.viewport().rect())
        # 等比缩放交给 video_item 的 KeepAspectRatio，这里只把画布铺满。
        self.video_item.setRotation(0.0)
        self.video_item.setSize(rect.size())
        self.video_item.setPos(rect.topLeft())

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.scene().setSceneRect(QRectF(self.viewport().rect()))
        self._layout_video()
        self.resized.emit()


class StarRating(QWidget):
    """五星评分控件：点第 n 颗打 n 分，再点当前星级清除。"""

    value_changed = Signal(int)
    _STEP = 19

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._value = 0
        self._labels: list[QLabel] = []
        for _ in range(5):
            label = QLabel("☆", self)
            label.setAlignment(Qt.AlignCenter)
            label.setFixedSize(17, 20)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(label)
            self._labels.append(label)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击星星打分，再点当前星级清除")
        self._refresh()

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        value = max(0, min(5, int(value)))
        if value == self._value:
            return
        self._value = value
        self._refresh()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        stars = max(1, min(5, int(event.position().x() // self._STEP) + 1))
        value = 0 if stars == self._value else stars
        self.set_value(value)
        self.value_changed.emit(value)
        event.accept()

    def _refresh(self) -> None:
        for index, label in enumerate(self._labels):
            filled = index < self._value
            label.setText("★" if filled else "☆")
            label.setStyleSheet(
                "font-size: 15px; background: transparent;"
                f" color: {COLORS['accent'] if filled else COLORS['muted']};"
            )


class PlayerView(QWidget):
    back_requested = Signal()
    position_changed = Signal(int, int, bool)
    subtitle_file_requested = Signal(int)
    rating_changed = Signal(int, int)
    tags_requested = Signal(int)
    video_loaded = Signal(int)
    video_missing = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue: list[dict[str, Any]] = []
        self.index = -1
        self.current_video: dict[str, Any] | None = None
        self._resume_ms = 0
        self._was_resumed = False
        self._dragging = False
        self._exposed_pair: tuple[Path, Path] | None = None
        self._fallback_attempted = False
        self._normal_geometry = None
        self._subtitle_cues = []
        self._subtitle_text = ""
        self._pending_seek_ms: int | None = None
        self._seek_target_ms: int | None = None
        self._seeking_until = 0.0
        self._subtitle_font_size = 22
        self._end_behavior = "stop"
        self._mask_map = dict(DEFAULT_MASK_MAP)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setPlaybackRate(1.0)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        back = QPushButton("返回媒体库")
        back.setIcon(svg_icon("chevron-left.svg"))
        back.setProperty("quiet", True)
        back.clicked.connect(self.close_player)
        header.addWidget(back)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        self.title = QLabel("播放")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.file_label = QLabel("")
        self.file_label.setProperty("faint", True)
        title_box.addWidget(self.title)
        title_box.addWidget(self.file_label)
        header.addLayout(title_box)
        header.addStretch(1)
        self.state_label = QLabel("直接读取")
        self.state_label.setProperty("state", True)
        header.addWidget(self.state_label)
        root.addLayout(header)

        video_frame = QFrame()
        video_frame.setStyleSheet(f"background: {COLORS['player']}; border-radius: 7px;")
        video_layout = QVBoxLayout(video_frame)
        video_layout.setContentsMargins(0, 0, 0, 0)
        self.surface = VideoSurface()
        self.surface.double_clicked.connect(self.toggle_fullscreen)
        self.surface.resized.connect(self._position_subtitle)
        self.player.setVideoOutput(self.surface.video_item.videoSink())
        self.subtitle_label = QLabel(self.surface.viewport())
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setTextFormat(Qt.PlainText)
        self.subtitle_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.set_subtitle_font_size(self._subtitle_font_size)
        self.subtitle_label.hide()
        video_layout.addWidget(self.surface)
        root.addWidget(video_frame, 1)

        self.error_label = QLabel("")
        self.error_label.setAlignment(Qt.AlignCenter)
        self.error_label.setStyleSheet(
            f"color: {COLORS['danger']}; background: {COLORS['surface_alt']}; "
            "border-radius: 6px; padding: 8px;"
        )
        self.error_label.hide()
        root.addWidget(self.error_label)

        timeline = QHBoxLayout()
        timeline.setSpacing(10)
        self.position_label = QLabel("0:00")
        self.position_label.setProperty("muted", True)
        self.position_label.setFixedWidth(50)
        self.timeline = ClickableSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.duration_label = QLabel("0:00")
        self.duration_label.setProperty("muted", True)
        self.duration_label.setFixedWidth(50)
        timeline.addWidget(self.position_label)
        timeline.addWidget(self.timeline, 1)
        timeline.addWidget(self.duration_label)
        self.rating = StarRating()
        self.rating.value_changed.connect(self.set_rating)
        timeline.addSpacing(10)
        timeline.addWidget(self.rating)
        self.tags_button = QPushButton("标签")
        self.tags_button.setToolTip("编辑这个视频的标签（写入文件元数据 Genre）")
        self.tags_button.clicked.connect(self._request_tags)
        timeline.addSpacing(6)
        timeline.addWidget(self.tags_button)
        root.addLayout(timeline)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.prev_button = self._tool_button("previous.svg", "上一个")
        self.back_button = self._tool_button("rewind.svg", "后退 10 秒")
        self.play_button = self._tool_button("play.svg", "播放 / 暂停")
        self.forward_button = self._tool_button("forward.svg", "前进 30 秒")
        self.next_button = self._tool_button("next.svg", "下一个")
        for button in (
            self.prev_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.next_button,
        ):
            controls.addWidget(button)
        controls.addSpacing(16)
        controls.addWidget(_small_label("速度"))
        self.speed_combo = QComboBox()
        for speed in (0.5, 1.0, 1.5, 2.0, 4.0):
            self.speed_combo.addItem(f"{speed:g}x", speed)
        self.speed_combo.setCurrentIndex(1)
        self.speed_combo.setFixedWidth(76)
        controls.addWidget(self.speed_combo)
        self.subtitle_button = QPushButton("字幕")
        self.subtitle_button.setIcon(svg_icon("subtitle.svg"))
        self.subtitle_button.setToolTip("选择外部字幕文件")
        self.subtitle_button.clicked.connect(self._request_subtitle)
        controls.addWidget(self.subtitle_button)
        controls.addSpacing(14)
        controls.addWidget(_small_label("音量"))
        self.volume = ClickableSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(105)
        controls.addWidget(self.volume)
        controls.addStretch(1)
        self.fullscreen_button = QPushButton("全屏")
        self.fullscreen_button.setIcon(svg_icon("fullscreen.svg"))
        controls.addWidget(self.fullscreen_button)
        root.addLayout(controls)

        self.prev_button.clicked.connect(self.previous)
        self.next_button.clicked.connect(self.next)
        self.back_button.clicked.connect(lambda: self.seek_relative(-10_000))
        self.forward_button.clicked.connect(lambda: self.seek_relative(30_000))
        self.play_button.clicked.connect(self.toggle_play)
        self.speed_combo.currentIndexChanged.connect(self._speed_changed)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.timeline.sliderPressed.connect(self._slider_pressed)
        self.timeline.sliderReleased.connect(self._slider_released)
        self.timeline.sliderMoved.connect(
            lambda value: self.position_label.setText(format_duration(value))
        )
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(self._error)

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self._save_position)
        self._save_timer.start()
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(55)
        self._seek_timer.timeout.connect(self._apply_pending_seek)

        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(1600)
        self._state_timer.timeout.connect(self._restore_state_label)
        self._state_before_flash = ""

        for key, action in (
            ("Space", self.toggle_play),
            ("Left", lambda: self.seek_relative(-5_000)),
            ("Right", lambda: self.seek_relative(5_000)),
            ("Ctrl+Left", self.previous),
            ("Ctrl+Right", self.next),
            ("Up", lambda: self.adjust_volume(5)),
            ("Down", lambda: self.adjust_volume(-5)),
            ("Shift+Up", lambda: self.seek_relative(30_000)),
            ("Shift+Down", lambda: self.seek_relative(-30_000)),
            ("PgUp", self.previous),
            ("PgDown", self.next),
            ("0", lambda: self.set_rating(0)),
            ("1", lambda: self.set_rating(1)),
            ("2", lambda: self.set_rating(2)),
            ("3", lambda: self.set_rating(3)),
            ("4", lambda: self.set_rating(4)),
            ("5", lambda: self.set_rating(5)),
            ("F", self.toggle_fullscreen),
            ("Esc", self.close_player),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
        for widget in (self, self.surface, self.surface.viewport()):
            widget.setFocusPolicy(Qt.StrongFocus)

    def _tool_button(self, icon: str, tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setIcon(svg_icon(icon))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(36, 34)
        button.setToolTip(tooltip)
        return button

    def play_video(self, video: dict[str, Any], queue: list[dict[str, Any]]) -> None:
        self.queue = queue or [video]
        self.index = next(
            (row for row, item in enumerate(self.queue) if item["id"] == video["id"]),
            -1,
        )
        if self.index < 0:
            self.queue.insert(0, video)
            self.index = 0
        self._load_current()

    def _load_current(self) -> None:
        if not 0 <= self.index < len(self.queue):
            return
        self._save_position()
        self._restore_exposed()
        self._seek_timer.stop()
        self._pending_seek_ms = None
        self._seek_target_ms = None
        self.current_video = self.queue[self.index]
        self._fallback_attempted = False
        self._was_resumed = False
        self._resume_ms = int(self.current_video.get("last_position_ms") or 0)
        path = Path(self.current_video["path"])
        self.title.setText(str(self.current_video.get("title") or path.stem))
        self.file_label.setText(path.name)
        self.error_label.hide()
        self.surface.set_video_rotation(int(self.current_video.get("rotation") or 0))
        source = existing_video_path(self.current_video, self._mask_map)
        if source is None:
            self.player.stop()
            self.player.setSource(QUrl())
            self.timeline.setRange(0, 0)
            self.timeline.setValue(0)
            self.position_label.setText("0:00")
            self.duration_label.setText("0:00")
            self._clear_subtitle()
            self.state_label.setText("文件缺失")
            self._show_error(f"文件缺失：{path.name}")
            self.prev_button.setEnabled(self.index > 0)
            self.next_button.setEnabled(self.index < len(self.queue) - 1)
            self.video_missing.emit(int(self.current_video["id"]))
            return
        path = source
        self.file_label.setText(path.name)
        self.state_label.setText(
            "直接读取伪装文件"
            if self.current_video.get("logical_ext") != self.current_video.get("physical_ext")
            else "本地文件"
        )
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPlaybackRate(float(self.speed_combo.currentData()))
        self.player.play()
        subtitle_path = (
            Path(self.current_video["subtitle_path"])
            if self.current_video.get("subtitle_path")
            else find_default_subtitle(path)
        )
        if subtitle_path and subtitle_path.exists():
            self.load_subtitle_file(subtitle_path)
        else:
            self._clear_subtitle()
        self.prev_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.queue) - 1)
        self.rating.set_value(int(self.current_video.get("rating") or 0))
        self.set_video_tags(self.current_video.get("tags") or [])
        self.video_loaded.emit(int(self.current_video["id"]))

    def _request_tags(self) -> None:
        video = self.current_video
        if not video:
            return
        self.tags_requested.emit(int(video["id"]))

    def set_video_tags(self, tags: list[str]) -> None:
        """标签按钮上显示数量，悬停能看到具体内容。"""
        cleaned = [str(tag) for tag in tags if str(tag).strip()]
        if cleaned:
            self.tags_button.setText(f"标签 {len(cleaned)}")
            self.tags_button.setToolTip(
                "标签：" + "；".join(cleaned) + "\n点击编辑（写入文件元数据 Genre）"
            )
        else:
            self.tags_button.setText("标签")
            self.tags_button.setToolTip("编辑这个视频的标签（写入文件元数据 Genre）")

    def _maybe_resume(self) -> None:
        if self._was_resumed or self._resume_ms <= 5000:
            return
        duration = self.player.duration()
        if duration and self._resume_ms < duration - 5000:
            self.player.setPosition(self._resume_ms)
        self._was_resumed = True

    def _position_changed(self, position: int) -> None:
        if self._pending_seek_ms is not None:
            display_position = self._pending_seek_ms
        elif self._seek_target_ms is not None:
            target = self._seek_target_ms
            tolerance = max(
                750,
                min(2500, int(self.player.duration() * 0.0015)),
            )
            if (
                abs(position - target) <= tolerance
                or time.monotonic() >= self._seeking_until
            ):
                self._seek_target_ms = None
                display_position = position
            else:
                display_position = target
        else:
            display_position = position
        if not self._dragging:
            self.timeline.blockSignals(True)
            self.timeline.setValue(display_position)
            self.timeline.blockSignals(False)
        self.position_label.setText(format_duration(display_position))
        self._update_subtitle(display_position)

    def _duration_changed(self, duration: int) -> None:
        self.timeline.setRange(0, duration)
        self.duration_label.setText(format_duration(duration))
        self._maybe_resume()

    def _state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.play_button.setIcon(svg_icon("pause.svg" if playing else "play.svg"))
        if not playing and self.current_video:
            self._save_position()

    def _media_status(self, status) -> None:
        if status == QMediaPlayer.LoadedMedia:
            self._maybe_resume()
        elif status == QMediaPlayer.InvalidMedia and not self._fallback_attempted:
            self._try_expose_extension()
        elif status == QMediaPlayer.EndOfMedia:
            self._save_position()
            if self._end_behavior == "loop":
                self.state_label.setText("循环播放")
                self.player.setPosition(0)
                self.player.play()
                return
            if self._end_behavior == "next" and self.index < len(self.queue) - 1:
                self.next()
                return
            self._restore_exposed()
            self.state_label.setText("播放结束")

    def _try_expose_extension(self) -> None:
        if not self.current_video:
            return
        self._fallback_attempted = True
        source = Path(self.current_video["path"])
        logical = str(self.current_video.get("logical_ext") or source.suffix)
        if source.suffix.lower() == logical.lower() or logical not in self._mask_map:
            self._show_error("无法读取这个视频文件。")
            return
        target = source.with_suffix(logical)
        if target.exists():
            self._show_error(f"无法临时恢复后缀，目标已存在：{target.name}")
            return
        try:
            source.rename(target)
        except OSError as exc:
            self._show_error(f"临时恢复后缀失败：{exc}")
            return
        self._exposed_pair = (target, source)
        record_temporary_exposure(source, target)
        self.state_label.setText("临时恢复后缀播放")
        self.player.setSource(QUrl.fromLocalFile(str(target)))
        self.player.play()

    def _restore_exposed(self) -> None:
        if not self._exposed_pair:
            return
        exposed, masked = self._exposed_pair
        try:
            if exposed.exists() and not masked.exists():
                exposed.rename(masked)
                clear_temporary_exposure()
            elif masked.exists():
                clear_temporary_exposure()
        except OSError as exc:
            self._show_error(f"恢复伪装后缀失败：{exc}")
        finally:
            self._exposed_pair = None

    def _error(self, error, message: str) -> None:
        if error != QMediaPlayer.NoError:
            self._show_error(message or "播放失败")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek_relative(self, delta_ms: int) -> None:
        if not self.current_video:
            return
        target = max(0, min(self.player.duration(), self.player.position() + delta_ms))
        if self._pending_seek_ms is not None:
            target = max(0, min(self.player.duration(), self._pending_seek_ms + delta_ms))
        self._request_seek(target)

    def previous(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._load_current()

    def next(self) -> None:
        if self.index < len(self.queue) - 1:
            self.index += 1
            self._load_current()

    def adjust_volume(self, delta: int) -> None:
        self.volume.setValue(max(0, min(100, self.volume.value() + delta)))
        self._flash_state(f"音量 {self.volume.value()}%")

    def set_rating(self, value: int) -> None:
        video = self.current_video
        if not video:
            return
        value = max(0, min(5, int(value)))
        video["rating"] = float(value) if value else None
        self.rating.set_value(value)
        self.rating_changed.emit(int(video["id"]), value)
        self._flash_state("★" * value + "☆" * (5 - value) if value else "已清除评级")

    def _flash_state(self, text: str) -> None:
        if not self._state_timer.isActive():
            self._state_before_flash = self.state_label.text()
        self.state_label.setText(text)
        self._state_timer.start()

    def _restore_state_label(self) -> None:
        self.state_label.setText(self._state_before_flash or "本地文件")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Space:
            self.toggle_play()
            return
        super().keyPressEvent(event)

    def _slider_pressed(self) -> None:
        self._dragging = True

    def _slider_released(self) -> None:
        self._dragging = False
        self._request_seek(self.timeline.value())

    def _request_seek(self, target_ms: int) -> None:
        self._pending_seek_ms = int(target_ms)
        self._seeking_until = time.monotonic() + 0.5
        self.timeline.blockSignals(True)
        self.timeline.setValue(self._pending_seek_ms)
        self.timeline.blockSignals(False)
        self.position_label.setText(format_duration(self._pending_seek_ms))
        self._update_subtitle(self._pending_seek_ms)
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _apply_pending_seek(self) -> None:
        if self._pending_seek_ms is None:
            return
        target = self._pending_seek_ms
        self._pending_seek_ms = None
        self._seek_target_ms = target
        self._seeking_until = time.monotonic() + 2.5
        self.player.setPosition(target)

    def load_subtitle_file(self, path: Path) -> None:
        cues = parse_subtitle(path)
        if not cues:
            self._clear_subtitle()
            self.error_label.setText("字幕文件为空或格式无法识别")
            self.error_label.show()
            return
        self._subtitle_cues = cues
        self._subtitle_text = ""
        self.subtitle_button.setText("字幕 · 已挂载")
        self.subtitle_button.setToolTip(str(path))
        self._update_subtitle(self.player.position())

    def _clear_subtitle(self) -> None:
        self._subtitle_cues = []
        self._subtitle_text = ""
        self.subtitle_label.hide()
        self.subtitle_button.setText("字幕")
        self.subtitle_button.setToolTip("选择外部字幕文件")

    def _request_subtitle(self) -> None:
        if self.current_video:
            self.subtitle_file_requested.emit(int(self.current_video["id"]))

    def set_subtitle_font_size(self, size: int) -> None:
        self._subtitle_font_size = max(14, min(48, int(size)))
        self.subtitle_label.setStyleSheet(
            "background: rgba(0, 0, 0, 125); color: white; "
            f"border-radius: 5px; padding: 4px 8px; font-size: {self._subtitle_font_size}px;"
        )
        if self._subtitle_text:
            self.subtitle_label.adjustSize()
            self._position_subtitle()

    def set_end_behavior(self, behavior: str) -> None:
        self._end_behavior = (
            behavior if behavior in {"stop", "loop", "next"} else "stop"
        )

    def set_mask_map(self, mask_map: dict[str, str] | None) -> None:
        self._mask_map = dict(mask_map or DEFAULT_MASK_MAP)

    def _update_subtitle(self, position_ms: int) -> None:
        cue = cue_at(self._subtitle_cues, position_ms)
        text = cue.text if cue else ""
        if text == self._subtitle_text:
            return
        self._subtitle_text = text
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))
        if text:
            self.subtitle_label.raise_()
            self._position_subtitle()

    def _position_subtitle(self) -> None:
        available = self.surface.viewport().rect()
        max_width = max(160, int(available.width() * 0.82))
        metrics = QFontMetrics(self.subtitle_label.font())
        natural_width = max(
            (metrics.horizontalAdvance(line) for line in self._subtitle_text.splitlines()),
            default=0,
        )
        width = min(max_width, max(80, natural_width + 26))
        self.subtitle_label.setFixedWidth(width)
        self.subtitle_label.adjustSize()
        max_height = max(48, int(available.height() * 0.26))
        height = min(max_height, max(38, self.subtitle_label.sizeHint().height()))
        x = max(10, (available.width() - width) // 2)
        y = max(10, available.height() - height - 24)
        self.subtitle_label.setGeometry(x, y, width, height)

    def _speed_changed(self) -> None:
        self.player.setPlaybackRate(float(self.speed_combo.currentData()))

    def _save_position(self) -> None:
        if not self.current_video:
            return
        position = int(self.player.position())
        duration = int(self.player.duration())
        watched = bool(duration and position >= duration * 0.95)
        self.position_changed.emit(int(self.current_video["id"]), position, watched)

    def close_player(self) -> None:
        self._save_position()
        self.player.stop()
        self._restore_exposed()
        self.exit_fullscreen()
        self.back_requested.emit()

    def shutdown(self) -> None:
        self._save_position()
        self.player.stop()
        self.player.setSource(QUrl())
        self._restore_exposed()

    def toggle_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            self.exit_fullscreen()
        else:
            self._normal_geometry = window.geometry()
            window.showFullScreen()
            self.fullscreen_button.setText("退出全屏")

    def exit_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            window.showNormal()
            if self._normal_geometry:
                window.setGeometry(self._normal_geometry)
            self.fullscreen_button.setText("全屏")


def _small_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", True)
    return label


class PlayerWindow(QDialog):
    closed = Signal()

    def __init__(self, player: PlayerView | None = None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowModality(Qt.NonModal)
        self.setWindowTitle(f"{APP_TITLE} · 播放器")
        self.resize(1180, 760)
        self.setMinimumSize(800, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.player = player or PlayerView()
        layout.addWidget(self.player)
        self.player.back_requested.connect(self.close)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Space:
            self.player.keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.player.exit_fullscreen()
        self.player.shutdown()
        self.closed.emit()
        super().closeEvent(event)
