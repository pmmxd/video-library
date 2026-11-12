from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QProgressBar,
    QPushButton,
    QSlider,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from ..media_tools import format_duration, format_size
from .icons import svg_icon
from .models import VideoListModel, VideoRoles
from .widgets import EmptyState, VideoCardDelegate
from .dialogs import MetadataFilterDialog, NfoFilterDialog
from .timeline_view import TimelineView


class LibraryPage(QWidget):
    search_changed = Signal(str)
    sort_changed = Signal(str)
    orientation_changed = Signal(str)
    filters_changed = Signal(object)
    metadata_filters_changed = Signal(object)
    cancel_scan_requested = Signal()
    incremental_refresh_requested = Signal()
    video_activated = Signal(int)
    selection_changed = Signal(object)
    batch_category_requested = Signal(object, object)
    tag_requested = Signal(object)
    batch_move_requested = Signal(object)
    batch_delete_requested = Signal(object)
    batch_favorite_requested = Signal(object, bool)
    rename_requested = Signal(int)
    default_open_requested = Signal(object)
    reveal_requested = Signal(object)
    export_requested = Signal(object)
    losslesscut_requested = Signal(object)
    trim_requested = Signal(int)
    subtitle_requested = Signal(int)
    subtitle_generation_requested = Signal(object)
    clear_filter_requested = Signal()
    group_toggle_requested = Signal(int, bool)
    group_create_requested = Signal(object)
    group_add_requested = Signal(object, int)
    group_remove_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = VideoListModel(self)
        self._categories: list[dict[str, Any]] = []
        self._filter_options: dict[str, list[dict[str, Any]]] = {}
        self._active_filters: dict[str, str] = {}
        self._metadata_options: dict[str, list[dict[str, Any]]] = {}
        self._active_metadata_filters: dict[str, str] = {}
        self._video_groups: list[dict[str, Any]] = []
        self._orientation = "all"
        self._sort_descending = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = QLabel("全部视频")
        self.title_label.setProperty("heading", True)
        self.count_label = QLabel("0 个视频")
        self.count_label.setProperty("muted", True)
        titles.addWidget(self.title_label)
        titles.addWidget(self.count_label)
        header.addLayout(titles)
        header.addStretch(1)

        self.search_edit = QLineEdit()
        self.search_edit.setProperty("role", "search")
        self.search_edit.setPlaceholderText("搜索标题、路径、类型")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(250)
        header.addWidget(self.search_edit)
        self.clear_filters_button = QPushButton("清除筛选")
        self.clear_filters_button.setProperty("quiet", True)
        self.clear_filters_button.setToolTip("清除搜索、分类、标签、元数据和 NFO 筛选")
        self.clear_filters_button.clicked.connect(self.clear_filter_requested)
        header.addWidget(self.clear_filters_button)
        self.incremental_refresh_button = QPushButton("刷新变更")
        self.incremental_refresh_button.setIcon(svg_icon("refresh.svg"))
        self.incremental_refresh_button.setIconSize(QSize(14, 14))
        self.incremental_refresh_button.setProperty("quiet", True)
        self.incremental_refresh_button.setToolTip(
            "只刷新文件管理器中新增、删除或改名的内容"
        )
        self.incremental_refresh_button.clicked.connect(
            self.incremental_refresh_requested
        )
        header.addWidget(self.incremental_refresh_button)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem("添加时间", "added")
        self.sort_combo.addItem("拍摄时间", "capture_time")
        self.sort_combo.addItem("文件修改时间", "file_time")
        self.sort_combo.addItem("首映时间", "premiered")
        self.sort_combo.addItem("名称", "title")
        self.sort_combo.addItem("文件名", "name")
        self.sort_combo.addItem("文件大小", "size")
        self.sort_combo.addItem("码率", "bitrate")
        self.sort_combo.addItem("时长", "duration")
        self.sort_combo.addItem("最近播放", "recent")
        self.sort_combo.setFixedWidth(122)
        header.addWidget(self.sort_combo)
        self.sort_descending_button = QPushButton("↓")
        self.sort_descending_button.setCheckable(True)
        self.sort_descending_button.setChecked(True)
        self.sort_descending_button.setFixedWidth(36)
        self.sort_descending_button.setAccessibleName("排序方向")
        self.sort_descending_button.setToolTip("当前降序，点击切换为升序")
        header.addWidget(self.sort_descending_button)
        self.filter_button = QPushButton("NFO 筛选")
        self.filter_button.clicked.connect(self._open_filters)
        self.metadata_filter_button = QPushButton("元数据")
        self.metadata_filter_button.clicked.connect(self._open_metadata_filters)
        header.addWidget(self.metadata_filter_button)
        header.addWidget(self.filter_button)
        layout.addLayout(header)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.stat_count_value = QLabel("0")
        self.stat_size_value = QLabel("0 B")
        self.stat_duration_value = QLabel("0:00")
        stat_specs = (
            ("视频数量", self.stat_count_value),
            ("总大小", self.stat_size_value),
            ("总时长", self.stat_duration_value),
        )
        for title, value in stat_specs:
            frame = QFrame()
            frame.setProperty("statCard", True)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(12, 9, 14, 9)
            frame_layout.setSpacing(2)
            label = QLabel(title)
            label.setProperty("section", True)
            value.setStyleSheet("font-size: 16px; font-weight: 650;")
            frame_layout.addWidget(label)
            frame_layout.addWidget(value)
            frame.setMinimumWidth(130)
            stats_row.addWidget(frame)
        stats_row.addStretch(1)
        layout.addLayout(stats_row)

        orientation_tabs = QHBoxLayout()
        orientation_tabs.setSpacing(4)
        self.orientation_group = QButtonGroup(self)
        self.orientation_group.setExclusive(True)
        self.orientation_buttons: dict[str, QPushButton] = {}
        for text, value in (
            ("全部", "all"),
            ("横屏", "landscape"),
            ("竖屏", "portrait"),
            ("时间轴", "timeline"),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("tab", True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda checked=False, key=value: self._orientation_clicked(key)
            )
            self.orientation_group.addButton(button)
            self.orientation_buttons[value] = button
            orientation_tabs.addWidget(button)
        orientation_tabs.addStretch(1)
        orientation_tabs.addWidget(QLabel("卡片"))
        self.card_size_slider = QSlider(Qt.Horizontal)
        self.card_size_slider.setRange(0, 100)
        self.card_size_slider.setValue(35)
        self.card_size_slider.setFixedWidth(116)
        self.card_size_slider.setToolTip("调整卡片大小")
        # 拖动滑块会连续触发 valueChanged，直接重排会卡；合并到一次 30ms 的延迟执行
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(30)
        self._relayout_timer.timeout.connect(self._relayout_cards)
        self.card_size_slider.valueChanged.connect(self._schedule_relayout)
        orientation_tabs.addWidget(self.card_size_slider)
        self.orientation_buttons["all"].setChecked(True)
        layout.addLayout(orientation_tabs)

        self.progress_frame = QWidget()
        progress_layout = QHBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(10)
        self.progress_label = QLabel("准备扫描")
        self.progress_label.setProperty("muted", True)
        self.progress_label.setMinimumWidth(190)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("quiet", True)
        self.cancel_button.hide()
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        progress_layout.addWidget(self.cancel_button)
        self.progress_frame.hide()
        layout.addWidget(self.progress_frame)

        self.stack_host = QWidget()
        self.stack = QStackedLayout(self.stack_host)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.card_delegate = VideoCardDelegate(self.list_view)
        self.card_delegate.group_toggle_requested.connect(
            self.group_toggle_requested.emit
        )
        self.list_view.setItemDelegate(self.card_delegate)
        self.list_view.setViewMode(QListView.IconMode)
        self.list_view.setResizeMode(QListView.Adjust)
        self.list_view.setMovement(QListView.Static)
        self.list_view.setSpacing(4)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_view.setGridSize(QSize(248, 230))
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(QListView.ExtendedSelection)
        self.list_view.setMouseTracking(True)
        self.list_view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.empty_state = EmptyState()
        self.empty_state.action_clicked.connect(self.clear_filter_requested)
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.empty_state)
        self.timeline_view = TimelineView()
        self.timeline_view.video_activated.connect(self.video_activated.emit)
        self.timeline_view.video_selected.connect(
            lambda video: self.selection_changed.emit([video])
        )
        self.timeline_view.context_menu_requested.connect(
            self._timeline_context_menu
        )
        self.timeline_view.group_toggle_requested.connect(
            self.group_toggle_requested.emit
        )
        self.stack.addWidget(self.timeline_view)
        layout.addWidget(self.stack_host, 1)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(260)
        self.search_edit.textChanged.connect(
            lambda: self._search_timer.start()
        )
        self._search_timer.timeout.connect(
            lambda: self.search_changed.emit(self.search_edit.text())
        )
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        self.sort_descending_button.toggled.connect(
            self._sort_direction_changed
        )
        self.cancel_button.clicked.connect(self.cancel_scan_requested)
        self.list_view.doubleClicked.connect(self._activate)
        self.list_view.clicked.connect(self._clicked)
        self.list_view.selectionModel().selectionChanged.connect(
            self._selection_changed
        )
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        self.list_view.viewport().installEventFilter(self)

    def _sort_changed(self) -> None:
        sort = str(self.sort_combo.currentData())
        self._set_sort_descending(sort not in {"title", "name"})
        self.sort_changed.emit(sort)

    def _sort_direction_changed(self, descending: bool) -> None:
        self._set_sort_descending(descending)
        self.sort_changed.emit(str(self.sort_combo.currentData()))

    def _set_sort_descending(self, descending: bool) -> None:
        self._sort_descending = bool(descending)
        self.sort_descending_button.blockSignals(True)
        self.sort_descending_button.setChecked(self._sort_descending)
        self.sort_descending_button.setText("↓" if self._sort_descending else "↑")
        self.sort_descending_button.setToolTip(
            "当前降序，点击切换为升序"
            if self._sort_descending
            else "当前升序，点击切换为降序"
        )
        self.sort_descending_button.blockSignals(False)

    def sort_descending(self) -> bool:
        return self._sort_descending

    def _activate(self, index) -> None:
        video = index.data(VideoRoles.Video) or {}
        if video.get("group_collapsed") and video.get("group_id") is not None:
            self.group_toggle_requested.emit(int(video["group_id"]), True)
            return
        video_id = index.data(VideoRoles.VideoId)
        if video_id is not None:
            self.video_activated.emit(int(video_id))

    def _clicked(self, index) -> None:
        video = index.data(VideoRoles.Video) or {}
        if video.get("group_collapsed") and video.get("group_id") is not None:
            group_id = int(video["group_id"])
            QTimer.singleShot(
                0,
                lambda: self.group_toggle_requested.emit(group_id, True),
            )

    def _selection_changed(self, selected, deselected) -> None:
        self.selection_changed.emit(self.selected_videos())

    def set_videos(
        self,
        videos: list[dict[str, Any]],
        schedule_relayout: bool = True,
    ) -> None:
        self.model.set_videos(videos)
        self._update_video_view(schedule_relayout)

    def replace_video_range(
        self,
        start: int,
        remove_count: int,
        videos: list[dict[str, Any]],
    ) -> None:
        self.model.replace_range(start, remove_count, videos)
        self._update_video_view(False)

    def _update_video_view(self, schedule_relayout: bool) -> None:
        videos = self.model.videos()
        count = sum(
            int(video.get("group_count") or 1)
            if video.get("group_collapsed")
            else 1
            for video in videos
        )
        size = sum(int(video.get("size_bytes") or 0) for video in videos)
        duration = sum(int(video.get("duration_ms") or 0) for video in videos)
        self.count_label.setText(f"{count} 个视频")
        self.stat_count_value.setText(str(count))
        self.stat_size_value.setText(format_size(size))
        self.stat_duration_value.setText(format_duration(duration))
        if not videos:
            self.stack.setCurrentIndex(1)
        elif self._orientation == "timeline":
            self.timeline_view.set_videos(videos)
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(0)
        if schedule_relayout:
            QTimer.singleShot(0, self._relayout_cards)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    def orientation(self) -> str:
        return self._orientation

    def _orientation_clicked(self, value: str) -> None:
        self._orientation = value
        self._apply_orientation_layout()
        self.orientation_changed.emit(value)

    def set_orientation(self, value: str) -> None:
        self._orientation = value if value in self.orientation_buttons else "all"
        self.orientation_buttons[self._orientation].setChecked(True)
        self._apply_orientation_layout()

    def _apply_orientation_layout(self) -> None:
        portrait = self._orientation == "portrait"
        self.card_delegate.set_portrait_mode(portrait)
        self._relayout_cards()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.list_view.viewport() and event.type() == QEvent.Resize:
            QTimer.singleShot(0, self._relayout_cards)
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._relayout_cards)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._relayout_cards)

    def _schedule_relayout(self, _value=None) -> None:
        if not self._relayout_timer.isActive():
            self._relayout_timer.start()

    def _relayout_cards(self, _value=None) -> None:
        portrait = self._orientation == "portrait"
        base_width = 210 if portrait else 238
        scale = 0.78 + (self.card_size_slider.value() / 100) * 0.62
        desired_width = max(150, round(base_width * scale))
        available = max(1, self.list_view.viewport().width() - 10)
        scrollbar = self.list_view.verticalScrollBar()
        if not scrollbar.isVisible():
            available = max(1, available - scrollbar.sizeHint().width())
        columns = max(1, available // desired_width)
        cell_width = max(1, available // columns)
        card_width = max(150, cell_width - 10)
        poster_ratio = 1.6 if portrait else 0.529
        card_height = max(180, round(card_width * poster_ratio) + 91)
        cell_height = card_height + 10
        grid = QSize(cell_width, cell_height)
        card = QSize(card_width, card_height)
        if grid == self.list_view.gridSize() and card == self.card_delegate.card_size:
            return
        self.card_delegate.set_card_size(card)
        self.list_view.setGridSize(grid)
        self.list_view.doItemsLayout()
        self.list_view.viewport().update()
        self.timeline_view.set_card_scale(self.card_size_slider.value())

    def set_scanning(
        self,
        active: bool,
        label: str = "",
        value: int = 0,
        maximum: int = 0,
        cancellable: bool = False,
    ) -> None:
        self.progress_frame.setVisible(active)
        self.incremental_refresh_button.setEnabled(not active)
        self.cancel_button.setVisible(active and cancellable)
        self.cancel_button.setEnabled(True)
        if active:
            self.progress_label.setText(label or "正在扫描")
            self.progress_bar.setMaximum(maximum or 0)
            self.progress_bar.setValue(value)

    def selected_videos(self) -> list[dict[str, Any]]:
        return [
            index.data(VideoRoles.Video)
            for index in self.list_view.selectionModel().selectedRows()
            if index.isValid()
        ]

    def selected_action_ids(self) -> list[int]:
        ids: list[int] = []
        for video in self.selected_videos():
            member_ids = video.get("group_member_ids") if video.get("group_collapsed") else None
            ids.extend(int(value) for value in (member_ids or [video["id"]]))
        return list(dict.fromkeys(ids))

    def select_video(self, video_id: int) -> None:
        row = self.model.row_for_id(video_id)
        if row < 0:
            return
        index = self.model.index(row, 0)
        self.list_view.setCurrentIndex(index)
        self.list_view.scrollTo(index)

    def refresh_video(self, video_id: int) -> None:
        """单个视频的数据在内存 dict 里改过后，通知视图重绘这一行（评级等）。"""
        row = self.model.row_for_id(video_id)
        if row < 0:
            return
        index = self.model.index(row, 0)
        self.model.dataChanged.emit(index, index)

    def scroll_to_video(self, video_id: int) -> None:
        """只滚动到指定视频，不改变选中状态（用于折叠/展开后保持位置）。"""
        row = self.model.row_for_id(video_id)
        if row < 0:
            return
        self.list_view.scrollTo(
            self.model.index(row, 0), QAbstractItemView.PositionAtCenter
        )

    def set_empty_message(
        self,
        title: str,
        subtitle: str,
        show_clear: bool = False,
    ) -> None:
        self.empty_state.set_text(title, subtitle, show_clear)

    def current_videos(self) -> list[dict[str, Any]]:
        return self.model.videos()

    def set_categories(self, categories: list[dict[str, Any]]) -> None:
        self._categories = categories

    def set_video_groups(self, groups: list[dict[str, Any]]) -> None:
        self._video_groups = list(groups)

    def set_filter_options(self, options: dict[str, list[dict[str, Any]]]) -> None:
        self._filter_options = options

    def set_metadata_filter_options(
        self,
        options: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._metadata_options = options

    def set_active_filters(self, filters: dict[str, str]) -> None:
        self._active_filters = dict(filters)
        count = len(self._active_filters)
        self.filter_button.setText(f"NFO 筛选 · {count}" if count else "NFO 筛选")
        self.filter_button.setProperty("accent", bool(count))
        self.filter_button.style().unpolish(self.filter_button)
        self.filter_button.style().polish(self.filter_button)

    def set_active_metadata_filters(self, filters: dict[str, str]) -> None:
        self._active_metadata_filters = dict(filters)
        count = len(self._active_metadata_filters)
        self.metadata_filter_button.setText(f"元数据 · {count}" if count else "元数据")
        self.metadata_filter_button.setProperty("accent", bool(count))
        self.metadata_filter_button.style().unpolish(self.metadata_filter_button)
        self.metadata_filter_button.style().polish(self.metadata_filter_button)

    def _open_filters(self) -> None:
        dialog = NfoFilterDialog(
            self._filter_options,
            self._active_filters,
            self,
        )
        if dialog.exec() == QDialog.Accepted:
            self.filters_changed.emit(dialog.values())

    def open_nfo_filters(self) -> None:
        self._open_filters()

    def _open_metadata_filters(self) -> None:
        dialog = MetadataFilterDialog(
            self._metadata_options,
            self._active_metadata_filters,
            self,
        )
        if dialog.exec() == QDialog.Accepted:
            self.metadata_filters_changed.emit(dialog.values())

    def open_metadata_filters(self) -> None:
        self._open_metadata_filters()

    def _show_context_menu(self, position: QPoint) -> None:
        index = self.list_view.indexAt(position)
        if not index.isValid():
            return
        if not self.list_view.selectionModel().isSelected(index):
            self.list_view.setCurrentIndex(index)
        selected = self.selected_videos()
        if not selected:
            return
        self._show_video_menu(
            selected,
            self.list_view.viewport().mapToGlobal(position),
        )

    def _timeline_context_menu(self, video: dict[str, Any], position: QPoint) -> None:
        self._show_video_menu([video], position)

    def _show_video_menu(
        self,
        selected: list[dict[str, Any]],
        global_position: QPoint,
    ) -> None:
        ids: list[int] = []
        for video in selected:
            member_ids = video.get("group_member_ids") if video.get("group_collapsed") else None
            ids.extend(int(value) for value in (member_ids or [video["id"]]))
        ids = list(dict.fromkeys(ids))
        representative_id = int(selected[0]["id"])
        menu = QMenu(self)
        play = menu.addAction("播放")
        create_group = None
        add_group_actions: dict[object, int] = {}
        toggle_group = None
        remove_group = None
        group_ids = {
            int(video["group_id"])
            for video in selected
            if video.get("group_id") is not None
        }
        if len(selected) >= 2:
            create_group = menu.addAction("创建视频组...")
        selected_libraries = {
            int(video["library_id"])
            for video in selected
            if video.get("library_id") is not None
        }
        available_groups = [
            group
            for group in self._video_groups
            if int(group["library_id"]) in selected_libraries
        ]
        if ids and available_groups:
            add_group_menu = menu.addMenu("加入现有视频组")
            for group in available_groups:
                action = add_group_menu.addAction(
                    f"{group['name']}  ·  {int(group['member_count'])} 个"
                )
                add_group_actions[action] = int(group["id"])
        if len(selected) == 1 and selected[0].get("group_collapsed"):
            toggle_group = menu.addAction("展开视频组")
        elif len(group_ids) == 1 and all(
            video.get("group_id") is not None for video in selected
        ):
            toggle_group = menu.addAction("收起视频组")
        if group_ids and all(video.get("group_id") is not None for video in selected):
            remove_group = menu.addAction("取消视频组")
        menu.addSeparator()
        default_open = menu.addAction("用默认软件打开")
        default_open.setEnabled(len(selected) == 1 and len(ids) == 1)
        reveal = menu.addAction("在文件夹管理器中打开")
        reveal.setEnabled(len(selected) == 1 and len(ids) == 1)
        export = menu.addAction(f"导出到文件夹（{len(ids)}）...")
        losslesscut = menu.addAction("用 LosslessCut 编辑/合并")
        trim = menu.addAction("FFmpeg 裁剪...")
        trim.setEnabled(len(selected) == 1 and len(ids) == 1)
        subtitle = menu.addAction("选择字幕并播放...")
        subtitle.setEnabled(len(selected) == 1 and len(ids) == 1)
        generate_subtitle = menu.addAction(
            f"生成字幕（Whisper，{len(ids)}）..."
        )
        generate_subtitle.setEnabled(bool(ids))
        menu.addSeparator()
        all_favorite = all(bool(video.get("favorite")) for video in selected)
        favorite = menu.addAction("取消收藏" if all_favorite else "收藏")
        rename = menu.addAction("重命名")
        rename.setEnabled(len(selected) == 1 and len(ids) == 1)
        menu.addSeparator()
        category_menu = menu.addMenu("加入分类")
        single_library_id = (
            next(iter(selected_libraries))
            if len(selected_libraries) == 1
            else None
        )
        uncategorized = category_menu.addAction(
            "未分类" if single_library_id is not None else "清除分类"
        )
        category_actions: dict[object, int] = {}
        if single_library_id is not None:
            available_categories = [
                category
                for category in self._categories
                if int(category.get("library_id") or 0) == single_library_id
            ]
            for category in available_categories:
                action = category_menu.addAction(
                    str(category.get("display_name") or category["name"])
                )
                category_actions[action] = int(category["id"])
            if not available_categories:
                empty = category_menu.addAction("该资源库暂无分类")
                empty.setEnabled(False)
        else:
            cross_library = category_menu.addAction("跨库选择只能清除分类")
            cross_library.setEnabled(False)
        tag = menu.addAction("编辑标签...")
        move = menu.addAction("移动到...")
        menu.addSeparator()
        delete = menu.addAction(f"移至回收站（{len(ids)}）")
        chosen = menu.exec(global_position)
        if chosen == play:
            self.video_activated.emit(representative_id)
        elif create_group is not None and chosen == create_group:
            self.group_create_requested.emit(ids)
        elif chosen in add_group_actions:
            self.group_add_requested.emit(ids, add_group_actions[chosen])
        elif toggle_group is not None and chosen == toggle_group:
            group_id = next(iter(group_ids))
            self.group_toggle_requested.emit(
                group_id,
                bool(selected[0].get("group_collapsed")),
            )
        elif remove_group is not None and chosen == remove_group:
            self.group_remove_requested.emit(ids)
        elif chosen == default_open:
            self.default_open_requested.emit(ids)
        elif chosen == reveal:
            self.reveal_requested.emit(ids)
        elif chosen == export:
            self.export_requested.emit(ids)
        elif chosen == losslesscut:
            self.losslesscut_requested.emit(ids)
        elif chosen == trim:
            self.trim_requested.emit(representative_id)
        elif chosen == subtitle:
            self.subtitle_requested.emit(representative_id)
        elif chosen == generate_subtitle:
            self.subtitle_generation_requested.emit(ids)
        elif chosen == favorite:
            self.batch_favorite_requested.emit(ids, not all_favorite)
        elif chosen == rename:
            self.rename_requested.emit(representative_id)
        elif chosen == uncategorized:
            self.batch_category_requested.emit(ids, None)
        elif chosen in category_actions:
            self.batch_category_requested.emit(ids, category_actions[chosen])
        elif chosen == tag:
            self.tag_requested.emit(ids)
        elif chosen == move:
            self.batch_move_requested.emit(ids)
        elif chosen == delete:
            self.batch_delete_requested.emit(ids)
