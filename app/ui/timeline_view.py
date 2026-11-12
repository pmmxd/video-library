from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..media_tools import format_duration, format_size
from .models import VideoListModel, VideoRoles
from .widgets import VideoCardDelegate


def _video_date(video: dict[str, Any]) -> str:
    value = str(video.get("capture_at") or video.get("premiered") or "")
    if value:
        return value[:10]
    mtime_ns = int(video.get("mtime_ns") or 0)
    if mtime_ns:
        return datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime("%Y-%m-%d")
    return ""


class TimelineMonthSection(QWidget):
    video_activated = Signal(int)
    video_selected = Signal(object)
    context_menu_requested = Signal(object, QPoint)
    collapsed_changed = Signal(str, bool)
    collapse_started = Signal()
    group_toggle_requested = Signal(int, bool)

    def __init__(
        self,
        year: str,
        month: str,
        videos: list[dict[str, Any]],
        card_scale: float = 0.997,
        collapsed: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.year = year
        self.month = month
        self.videos = videos
        self.card_scale = card_scale
        self.collapsed = bool(collapsed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 0, 0, 14)
        layout.setSpacing(6)

        count = sum(
            int(video.get("group_count") or 1)
            if video.get("group_collapsed")
            else 1
            for video in videos
        )
        size = sum(int(video.get("size_bytes") or 0) for video in videos)
        duration = sum(int(video.get("duration_ms") or 0) for video in videos)
        self._header_summary = (
            f"{int(month[5:7]):02d} 月    {count} 个视频    "
            f"{format_size(size)}    {format_duration(duration)}"
        )
        self.header = QPushButton()
        self.header.setProperty("quiet", True)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setToolTip("收起或展开这个月份")
        self.header.clicked.connect(self.toggle_collapsed)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 8, 0)
        header_row.addWidget(self.header)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        self.model = VideoListModel(self)
        self.model.set_videos(videos)
        self.view = QListView()
        self.view.setModel(self.model)
        self.delegate = VideoCardDelegate(self.view)
        self.delegate.group_toggle_requested.connect(
            self.group_toggle_requested.emit
        )
        self.view.setItemDelegate(self.delegate)
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setSpacing(4)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setUniformItemSizes(False)
        self.view.setSelectionMode(QListView.SingleSelection)
        self.view.setMouseTracking(True)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.clicked.connect(self._selected)
        self.view.doubleClicked.connect(self._activate)
        self.view.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.view)
        self.view.viewport().installEventFilter(self)
        self.set_collapsed(self.collapsed, emit_signal=False)
        QTimer.singleShot(0, self.relayout)

    def _update_header(self) -> None:
        self.header.setText(f"{'▸' if self.collapsed else '▾'}  {self._header_summary}")

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed: bool, emit_signal: bool = True) -> None:
        collapsed = bool(collapsed)
        if emit_signal:
            self.collapse_started.emit()
        self.setUpdatesEnabled(False)
        self.collapsed = collapsed
        self._update_header()
        self.view.setVisible(not self.collapsed)
        self.layout().activate()
        self.setUpdatesEnabled(True)
        self.update()
        if emit_signal:
            self.collapsed_changed.emit(self.month, self.collapsed)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#303844"), 2))
        painter.drawLine(14, 0, 14, self.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E4B363"))
        painter.drawEllipse(QRectF(7, 18, 14, 14))

    def _activate(self, index) -> None:
        video_id = index.data(VideoRoles.VideoId)
        if video_id is not None:
            self.video_activated.emit(int(video_id))

    def _selected(self, index) -> None:
        video = index.data(VideoRoles.Video)
        if video:
            self.video_selected.emit(video)

    def _context_menu(self, position: QPoint) -> None:
        index = self.view.indexAt(position)
        if not index.isValid():
            return
        self.view.setCurrentIndex(index)
        video = index.data(VideoRoles.Video)
        if video:
            self.video_selected.emit(video)
            self.context_menu_requested.emit(
                video,
                self.view.viewport().mapToGlobal(position),
            )

    def eventFilter(self, watched, event) -> bool:
        if watched is self.view.viewport() and event.type() == QEvent.Resize:
            QTimer.singleShot(0, self.relayout)
        return super().eventFilter(watched, event)

    def set_card_scale(self, scale: float) -> None:
        if abs(scale - self.card_scale) < 1e-6:
            return
        self.card_scale = scale
        self.relayout()

    def relayout(self) -> None:
        # QTimer.singleShot(0, ...) 可能在分区被销毁后才触发，此时视图像素对象已释放
        if not isValid(self.view):
            return
        available = max(180, self.view.viewport().width() - 12)
        desired = max(150, round(190 * self.card_scale))
        capacity = max(1, available // desired)
        columns = min(capacity, max(1, len(self.videos)))
        cell_width = (
            available // columns
            if len(self.videos) >= capacity
            else desired + 10
        )
        card_width = max(150, cell_width - 10)
        card_height = max(180, round(card_width * 0.529) + 91)
        cell_height = card_height + 10
        self.delegate.set_card_size(QSize(card_width, card_height))
        self.view.setGridSize(QSize(cell_width, cell_height))
        rows = max(1, math.ceil(len(self.videos) / columns))
        self.view.setFixedHeight(rows * cell_height + 8)
        self.view.doItemsLayout()


class TimelineView(QWidget):
    video_activated = Signal(int)
    video_selected = Signal(object)
    context_menu_requested = Signal(object, QPoint)
    group_toggle_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sections: list[TimelineMonthSection] = []
        self._year_sections: dict[str, list[TimelineMonthSection]] = {}
        self._year_buttons: dict[str, QPushButton] = {}
        self._year_labels: dict[str, str] = {}
        self._collapsed_months: set[str] = set()
        self._collapsed_years: set[str] = set()
        self._card_scale = 0.997
        self._scale_pending = False
        self._collapse_scroll_value = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        self.body = QWidget()
        self.scroll.setWidget(self.body)
        self.content_layout = QVBoxLayout(self.body)
        self.content_layout.setContentsMargins(8, 4, 8, 12)
        self.content_layout.setSpacing(4)
        self.content_layout.addStretch(1)

    def set_videos(self, videos: list[dict[str, Any]]) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._sections.clear()
        self._year_sections.clear()
        self._year_buttons.clear()
        self._year_labels.clear()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for video in videos:
            date = _video_date(video)
            if len(date) >= 7:
                grouped[date[:4]][date[:7]].append(video)
        for year in sorted(grouped, reverse=True):
            months = grouped[year]
            year_count = sum(
                int(video.get("group_count") or 1)
                if video.get("group_collapsed")
                else 1
                for items in months.values()
                for video in items
            )
            year_size = sum(
                int(video.get("size_bytes") or 0)
                for items in months.values()
                for video in items
            )
            year_duration = sum(
                int(video.get("duration_ms") or 0)
                for items in months.values()
                for video in items
            )
            year_header = QWidget()
            year_row = QHBoxLayout(year_header)
            year_row.setContentsMargins(8, 12, 8, 4)
            year_row.setSpacing(8)
            year_button = QPushButton()
            year_button.setProperty("quiet", True)
            year_button.setCursor(Qt.PointingHandCursor)
            year_button.setStyleSheet("font-size: 18px; font-weight: 700; text-align: left;")
            year_summary = (
                f"{year} 年    "
                f"{year_count} 个视频    {format_size(year_size)}    "
                f"{format_duration(year_duration)}"
            )
            self._year_labels[year] = year_summary
            year_button.setText(
                f"{'▸' if year in self._collapsed_years else '▾'}  {year_summary}"
            )
            year_button.setToolTip("收起或展开这一年的月份")
            year_button.clicked.connect(
                lambda checked=False, value=year: self._toggle_year(value)
            )
            year_row.addWidget(year_button, 1)
            self.content_layout.addWidget(year_header)
            self._year_buttons[year] = year_button
            year_sections: list[TimelineMonthSection] = []
            for month in sorted(months, reverse=True):
                section = TimelineMonthSection(
                    year,
                    month,
                    months[month],
                    self._card_scale,
                    month in self._collapsed_months,
                    self.body,
                )
                section.video_activated.connect(self.video_activated.emit)
                section.video_selected.connect(self.video_selected.emit)
                section.context_menu_requested.connect(
                    self.context_menu_requested.emit
                )
                section.collapsed_changed.connect(self._month_collapsed)
                section.collapse_started.connect(self._freeze_scroll_for_collapse)
                section.group_toggle_requested.connect(
                    self.group_toggle_requested.emit
                )
                self.content_layout.addWidget(section)
                self._sections.append(section)
                year_sections.append(section)
            self._year_sections[year] = year_sections
            self._apply_year_state(year)
        if not grouped:
            empty = QLabel("没有可用的拍摄时间")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color: #8F9AA8; padding: 40px;")
            self.content_layout.addWidget(empty)
        self.content_layout.addStretch(1)
        QTimer.singleShot(0, self._relayout_sections)

    def _month_collapsed(self, month: str, collapsed: bool) -> None:
        if collapsed:
            self._collapsed_months.add(month)
        else:
            self._collapsed_months.discard(month)
        QTimer.singleShot(0, self._finish_scroll_collapse)

    def _toggle_year(self, year: str) -> None:
        self._freeze_scroll_for_collapse()
        if year in self._collapsed_years:
            self._collapsed_years.discard(year)
        else:
            self._collapsed_years.add(year)
        self._apply_year_state(year)
        QTimer.singleShot(0, self._finish_scroll_collapse)

    def _freeze_scroll_for_collapse(self) -> None:
        self._collapse_scroll_value = self.scroll.verticalScrollBar().value()
        self.scroll.viewport().setUpdatesEnabled(False)

    def _finish_scroll_collapse(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(min(self._collapse_scroll_value, bar.maximum()))
        self.scroll.viewport().setUpdatesEnabled(True)
        self.scroll.viewport().update()

    def _apply_year_state(self, year: str) -> None:
        collapsed = year in self._collapsed_years
        sections = self._year_sections.get(year, [])
        for section in sections:
            section.setVisible(not collapsed)
            if not collapsed:
                section.set_collapsed(
                    section.month in self._collapsed_months,
                    emit_signal=False,
                )
        button = self._year_buttons.get(year)
        if button:
            button.setText(
                f"{'▸' if collapsed else '▾'}  {self._year_labels.get(year, '')}"
            )

    def set_card_scale(self, value: int) -> None:
        scale = 0.78 + (value / 100) * 0.62
        if abs(scale - self._card_scale) < 1e-6:
            return
        self._card_scale = scale
        # 时间轴不可见时重排没有意义，等真正显示时再补一次
        if not self.isVisible():
            self._scale_pending = True
            return
        for section in self._sections:
            section.set_card_scale(scale)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._scale_pending:
            self._scale_pending = False
            for section in self._sections:
                section.set_card_scale(self._card_scale)

    def _relayout_sections(self) -> None:
        for section in self._sections:
            section.relayout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._relayout_sections)
