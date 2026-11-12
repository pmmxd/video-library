from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..media_tools import (
    format_bitrate,
    format_duration,
    format_size,
    resolution_label,
)
from ..constants import runtime_artwork_path
from ..theme import COLORS
from .icons import svg_icon
from .widgets import configure_filter_combo


class DetailPanel(QWidget):
    tag_filter_requested = Signal(str)
    actor_filter_requested = Signal(str)
    metadata_filter_requested = Signal()
    nfo_filter_requested = Signal()
    collapse_requested = Signal()
    metadata_filters_changed = Signal(object)
    nfo_filters_changed = Signal(object)

    METADATA_FIELDS = (
        ("capture_year", "拍摄年份"),
        ("capture_month", "拍摄月份"),
        ("resolution", "分辨率"),
        ("duration", "视频时长"),
        ("size", "文件大小"),
        ("camera", "相机 / 设备"),
        ("lens", "镜头"),
        ("location", "拍摄地点"),
    )
    NFO_FIELDS = (
        ("year", "首映年份"),
        ("premiered", "首映日期"),
        ("country", "国家 / 地区"),
        ("studio", "片商"),
        ("genre", "类型"),
        ("nfo_tag", "NFO 标签"),
        ("content_set", "系列 / Set"),
        ("mpaa", "分级"),
        ("actor", "演员"),
        ("director", "导演"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(300)
        self.setMaximumWidth(360)
        self.current_video: dict[str, Any] | None = None
        self._categories: list[dict[str, Any]] = []
        self._tag_links: dict[str, str] = {}
        self._actor_links: dict[str, str] = {}
        self._metadata_options: dict[str, list[dict[str, Any]]] = {}
        self._nfo_options: dict[str, list[dict[str, Any]]] = {}
        self._active_metadata_filters: dict[str, str] = {}
        self._active_nfo_filters: dict[str, str] = {}
        self.metadata_combos: dict[str, QComboBox] = {}
        self.nfo_combos: dict[str, QComboBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(12, 10, 8, 6)
        tab_row.setSpacing(4)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_buttons: list[QPushButton] = []
        for index, text in enumerate(("详情", "元数据", "NFO 筛选")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("tab", True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda checked=False, value=index: self.tabs.setCurrentIndex(value)
            )
            self.tab_group.addButton(button, index)
            tab_row.addWidget(button)
            self.tab_buttons.append(button)
        self.tab_buttons[0].setChecked(True)
        tab_row.addStretch(1)
        collapse = QToolButton()
        collapse.setIcon(svg_icon("chevron-right.svg"))
        collapse.setIconSize(QSize(16, 16))
        collapse.setToolTip("收起右侧侧边栏")
        collapse.setFixedSize(26, 26)
        collapse.clicked.connect(self.collapse_requested)
        tab_row.addWidget(collapse)
        outer.addLayout(tab_row)
        self.tabs = QStackedWidget()
        outer.addWidget(self.tabs, 1)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        self.tabs.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 22, 20, 20)
        layout.setSpacing(12)

        self.cover = QLabel()
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setMinimumSize(260, 146)
        self.cover.setMaximumSize(310, 320)
        self.cover.setStyleSheet(
            f"background: {COLORS['surface_alt']}; border-radius: 7px; "
            f"color: {COLORS['faint']};"
        )
        layout.addWidget(self.cover)

        self.screenshot_title = QLabel("预览截图")
        self.screenshot_title.setProperty("section", True)
        layout.addWidget(self.screenshot_title)
        self.screenshot_row = QWidget()
        screenshot_layout = QHBoxLayout(self.screenshot_row)
        screenshot_layout.setContentsMargins(0, 0, 0, 0)
        screenshot_layout.setSpacing(6)
        self.screenshot_labels: list[QLabel] = []
        for _ in range(3):
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            label.setFixedSize(82, 46)
            label.setStyleSheet(
                f"background: {COLORS['surface_alt']}; border-radius: 5px;"
            )
            screenshot_layout.addWidget(label)
            self.screenshot_labels.append(label)
        screenshot_layout.addStretch(1)
        layout.addWidget(self.screenshot_row)
        self.screenshot_title.hide()
        self.screenshot_row.hide()

        self.title = QLabel("视频详情")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 18px; font-weight: 650;")
        layout.addWidget(self.title)

        self.meta = QLabel("")
        self.meta.setProperty("muted", True)
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.badge_resolution = QLabel("")
        self.badge_resolution.setProperty("badge", True)
        self.badge_codec = QLabel("")
        self.badge_codec.setProperty("badge", True)
        self.badge_mask = QLabel("")
        self.badge_mask.setProperty("accentBadge", True)
        self.badge_favorite = QLabel("已收藏")
        self.badge_favorite.setProperty("accentBadge", True)
        badges.addWidget(self.badge_resolution)
        badges.addWidget(self.badge_codec)
        badges.addWidget(self.badge_mask)
        badges.addWidget(self.badge_favorite)
        badges.addStretch(1)
        self.badge_row = QWidget()
        self.badge_row.setLayout(badges)
        layout.addWidget(self.badge_row)

        self.nfo_info = QLabel("")
        self.nfo_info.setWordWrap(True)
        self.nfo_info.setProperty("muted", True)
        layout.addWidget(self.nfo_info)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet(f"color: {COLORS['line']};")
        layout.addWidget(divider)

        category_label = QLabel("分类")
        category_label.setProperty("section", True)
        layout.addWidget(category_label)
        self.category_label = QLabel("未分类")
        layout.addWidget(self.category_label)

        tag_label = QLabel("软件标签")
        tag_label.setProperty("section", True)
        layout.addWidget(tag_label)
        self.tags_label = QLabel("")
        self.tags_label.setWordWrap(True)
        self.tags_label.setTextFormat(Qt.RichText)
        self.tags_label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.TextSelectableByMouse
        )
        self.tags_label.linkActivated.connect(self._tag_link_clicked)
        layout.addWidget(self.tags_label)

        nfo_tag_label = QLabel("NFO 标签")
        nfo_tag_label.setProperty("section", True)
        layout.addWidget(nfo_tag_label)
        self.nfo_tags = QLabel("")
        self.nfo_tags.setWordWrap(True)
        self.nfo_tags.setTextFormat(Qt.RichText)
        self.nfo_tags.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.TextSelectableByMouse
        )
        self.nfo_tags.linkActivated.connect(self._tag_link_clicked)
        layout.addWidget(self.nfo_tags)

        actor_label = QLabel("演员")
        actor_label.setProperty("section", True)
        layout.addWidget(actor_label)
        self.actors_label = QLabel("")
        self.actors_label.setWordWrap(True)
        self.actors_label.setTextFormat(Qt.RichText)
        self.actors_label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.TextSelectableByMouse
        )
        self.actors_label.linkActivated.connect(self._actor_link_clicked)
        layout.addWidget(self.actors_label)

        plot_label = QLabel("简介")
        plot_label.setProperty("section", True)
        layout.addWidget(plot_label)
        self.plot = QLabel("")
        self.plot.setWordWrap(True)
        self.plot.setAlignment(Qt.AlignTop)
        self.plot.setProperty("muted", True)
        self.plot.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.plot)

        path_label = QLabel("文件")
        path_label.setProperty("section", True)
        layout.addWidget(path_label)
        self.path = QLabel("")
        self.path.setWordWrap(True)
        self.path.setProperty("faint", True)
        self.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.path)
        layout.addStretch(1)

        self.tabs.addWidget(self._build_metadata_page())
        self.tabs.addWidget(self._build_nfo_page())

        self.set_video(None)

    def _page_shell(self, title: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 16, 20, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 16px; font-weight: 650;")
        layout.addWidget(heading)
        return scroll, layout

    def _build_metadata_page(self) -> QScrollArea:
        scroll, layout = self._page_shell("视频元数据")
        self.technical_meta = QLabel("未选择视频")
        self.technical_meta.setWordWrap(True)
        self.technical_meta.setTextFormat(Qt.RichText)
        self.technical_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.technical_meta)
        self.metadata_filter_state = QLabel("未启用元数据筛选")
        self.metadata_filter_state.setProperty("muted", True)
        self.metadata_filter_state.setWordWrap(True)
        layout.addWidget(self.metadata_filter_state)
        filter_title = QLabel("筛选条件")
        filter_title.setProperty("section", True)
        layout.addWidget(filter_title)
        for key, label in self.METADATA_FIELDS:
            field_label = QLabel(label)
            field_label.setProperty("muted", True)
            layout.addWidget(field_label)
            combo = QComboBox()
            configure_filter_combo(combo)
            combo.currentIndexChanged.connect(
                lambda checked=False, field=key: self._inline_filter_changed(
                    "metadata", field
                )
            )
            self.metadata_combos[key] = combo
            layout.addWidget(combo)
        button = QPushButton("打开完整元数据筛选")
        button.clicked.connect(self.metadata_filter_requested)
        layout.addWidget(button)
        layout.addStretch(1)
        return scroll

    def _build_nfo_page(self) -> QScrollArea:
        scroll, layout = self._page_shell("NFO 筛选与内容")
        self.nfo_page_info = QLabel("未选择视频")
        self.nfo_page_info.setWordWrap(True)
        self.nfo_page_info.setTextFormat(Qt.RichText)
        self.nfo_page_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.nfo_page_info)
        self.nfo_filter_state = QLabel("未启用 NFO 筛选")
        self.nfo_filter_state.setProperty("muted", True)
        self.nfo_filter_state.setWordWrap(True)
        layout.addWidget(self.nfo_filter_state)
        filter_title = QLabel("筛选条件")
        filter_title.setProperty("section", True)
        layout.addWidget(filter_title)
        for key, label in self.NFO_FIELDS:
            field_label = QLabel(label)
            field_label.setProperty("muted", True)
            layout.addWidget(field_label)
            combo = QComboBox()
            configure_filter_combo(combo)
            combo.currentIndexChanged.connect(
                lambda checked=False, field=key: self._inline_filter_changed(
                    "nfo", field
                )
            )
            self.nfo_combos[key] = combo
            layout.addWidget(combo)
        button = QPushButton("打开完整 NFO 筛选")
        button.clicked.connect(self.nfo_filter_requested)
        layout.addWidget(button)
        layout.addStretch(1)
        return scroll

    def set_active_filters(
        self,
        metadata_filters: dict[str, str],
        nfo_filters: dict[str, str],
    ) -> None:
        self._active_metadata_filters = dict(metadata_filters)
        self._active_nfo_filters = dict(nfo_filters)
        self.metadata_filter_state.setText(
            "当前元数据筛选：" + "，".join(metadata_filters.values())
            if metadata_filters
            else "未启用元数据筛选"
        )
        self.nfo_filter_state.setText(
            "当前 NFO 筛选：" + "，".join(nfo_filters.values())
            if nfo_filters
            else "未启用 NFO 筛选"
        )
        self._sync_filter_combos()

    def set_filter_options(
        self,
        metadata_options: dict[str, list[dict[str, Any]]],
        nfo_options: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._metadata_options = metadata_options
        self._nfo_options = nfo_options
        self._populate_filter_combos(
            self.metadata_combos,
            metadata_options,
            self._active_metadata_filters,
        )
        self._populate_filter_combos(
            self.nfo_combos,
            nfo_options,
            self._active_nfo_filters,
        )

    def _populate_filter_combos(
        self,
        combos: dict[str, QComboBox],
        options: dict[str, list[dict[str, Any]]],
        active: dict[str, str],
    ) -> None:
        for key, combo in combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部", None)
            for option in options.get(key, []):
                value = str(option.get("value") or "")
                if not value:
                    continue
                combo.addItem(
                    f"{value}  ·  {int(option.get('count') or 0)}",
                    value,
                )
            index = combo.findData(active.get(key))
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

    def _sync_filter_combos(self) -> None:
        for combos, active in (
            (self.metadata_combos, self._active_metadata_filters),
            (self.nfo_combos, self._active_nfo_filters),
        ):
            for key, combo in combos.items():
                combo.blockSignals(True)
                index = combo.findData(active.get(key))
                combo.setCurrentIndex(max(0, index))
                combo.blockSignals(False)

    def _inline_filter_changed(self, kind: str, field: str) -> None:
        combos = self.metadata_combos if kind == "metadata" else self.nfo_combos
        values = {
            key: str(combo.currentData())
            for key, combo in combos.items()
            if combo.currentData()
        }
        if kind == "metadata":
            self.metadata_filters_changed.emit(values)
        else:
            self.nfo_filters_changed.emit(values)

    def set_categories(self, categories: list[dict[str, Any]]) -> None:
        self._categories = categories
        if self.current_video:
            self._set_category(self.current_video.get("category_id"))

    def _set_category(self, category_id: int | None) -> None:
        category = next(
            (item for item in self._categories if item["id"] == category_id),
            None,
        )
        self.category_label.setText(category["name"] if category else "未分类")

    def set_video(self, video: dict[str, Any] | None) -> None:
        self.current_video = video
        if not video:
            self.cover.clear()
            self.cover.setText("选择一个视频")
            self.title.setText("视频详情")
            self.meta.clear()
            self.plot.clear()
            self.path.clear()
            self.tags_label.clear()
            self.nfo_tags.clear()
            self.actors_label.clear()
            self._tag_links.clear()
            self._actor_links.clear()
            self.category_label.setText("未分类")
            self.badge_resolution.clear()
            self.badge_codec.clear()
            self.badge_mask.clear()
            self.badge_row.hide()
            self.nfo_info.clear()
            self.technical_meta.setText("未选择视频")
            self.nfo_page_info.setText("未选择视频")
            self.screenshot_title.hide()
            self.screenshot_row.hide()
            return

        self.badge_row.show()
        poster = runtime_artwork_path(video.get("poster_path"))
        preview = runtime_artwork_path(video.get("thumb_path"))
        cover_path = poster or preview
        pixmap = QPixmap(str(cover_path)) if cover_path else QPixmap()
        if not pixmap.isNull():
            portrait = pixmap.height() > pixmap.width() * 1.1
            target_width = 230 if portrait else 260
            target_height = 300 if portrait else 146
            self.cover.setPixmap(
                pixmap.scaled(
                    target_width,
                    target_height,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        else:
            self.cover.clear()
            self.cover.setText("暂无封面")

        screenshots = []
        for value in video.get("sidecars") or []:
            path = Path(str(value))
            if path.parent.name.casefold() != ".screenshot":
                continue
            runtime_path = runtime_artwork_path(path)
            if runtime_path:
                screenshots.append(runtime_path)
            if len(screenshots) == len(self.screenshot_labels):
                break
        for index, label in enumerate(self.screenshot_labels):
            if index < len(screenshots):
                shot = QPixmap(str(screenshots[index]))
                if not shot.isNull():
                    label.setPixmap(
                        shot.scaled(
                            label.size(),
                            Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation,
                        )
                    )
                    label.show()
                    continue
            label.clear()
            label.hide()
        self.screenshot_title.setVisible(bool(screenshots))
        self.screenshot_row.setVisible(bool(screenshots))

        title = video.get("title") or video.get("file_name") or "未命名视频"
        self.title.setText(str(title))
        duration = format_duration(int(video.get("duration_ms") or 0))
        year = video.get("year") or ""
        capture_at = str(video.get("capture_at") or "").replace("T", " ")
        size = format_size(int(video.get("size_bytes") or 0))
        bitrate = format_bitrate(int(video.get("bitrate_bps") or 0))
        rating = int(round(float(video.get("rating") or 0)))
        stars = "★" * min(5, rating) + "☆" * max(0, 5 - min(5, rating))
        self.meta.setText(
            "  ·  ".join(
                part
                for part in (
                    stars if rating else "",
                    capture_at or str(year),
                    duration,
                    size,
                    bitrate,
                )
                if part
            )
        )
        self.badge_resolution.setText(
            resolution_label(int(video.get("width") or 0), int(video.get("height") or 0))
        )
        self.badge_codec.setText(str(video.get("codec") or "未知编码").upper())
        masked = video.get("logical_ext") != video.get("physical_ext")
        self.badge_mask.setText(
            f"{video.get('logical_ext')} → {video.get('physical_ext')}"
            if masked
            else str(video.get("logical_ext") or "").upper()
        )
        self.badge_favorite.setVisible(bool(video.get("favorite")))
        self._set_category(video.get("category_id"))

        tagline = str(video.get("tagline") or "").strip()
        plot = str(video.get("plot") or "").strip()
        self.plot.setText(
            f"{tagline}\n\n{plot}" if tagline and plot else tagline or plot or "没有简介。"
        )

        id_values = [
            str(item.get("value"))
            for item in (video.get("nfo_ids") or [])
            if item.get("value")
        ]
        info_parts = [
            f"评级 {stars}" if rating else "",
            f"ID {' / '.join(id_values)}" if id_values else "",
            capture_at,
            str(video.get("camera_model") or video.get("camera_make") or ""),
            str(video.get("lens") or ""),
            str(video.get("location") or ""),
            str(video.get("premiered") or ""),
            str(video.get("country") or ""),
            str(video.get("studio") or ""),
            str(video.get("content_set") or ""),
            str(video.get("mpaa") or ""),
            (
                "导演 " + " / ".join(video.get("directors") or [])
                if video.get("directors")
                else ""
            ),
        ]
        actors = [
            str(item.get("name"))
            for item in (video.get("actors") or [])
            if item.get("name")
        ]
        genres = video.get("genres") or []
        if genres:
            info_parts.append(" / ".join(str(item) for item in genres))
        self.nfo_info.setText("  ·  ".join(part for part in info_parts if part))

        nfo_tags = video.get("nfo_tags") or []
        self.nfo_tags.setText(self._tags_html(nfo_tags, "nfo", COLORS["accent"]))
        self.actors_label.setText(self._actors_html(actors))
        self.path.setText(str(video.get("path") or ""))
        self.tags_label.setText("")
        self._set_technical_page(video)
        self._set_nfo_page(video)

    def _set_technical_page(self, video: dict[str, Any]) -> None:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        mtime_ns = int(video.get("mtime_ns") or 0)
        modified = ""
        if mtime_ns:
            modified = datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        rating = int(round(float(video.get("rating") or 0)))
        stars = "★" * min(5, rating) + "☆" * max(0, 5 - min(5, rating))
        values = (
            ("文件", video.get("file_name") or ""),
            ("评级", stars if rating else ""),
            ("拍摄时间", str(video.get("capture_at") or "").replace("T", " ")),
            ("分辨率", f"{width} × {height}" if width or height else "未知"),
            ("旋转", f"{int(video.get('rotation') or 0)}°"),
            ("时长", format_duration(int(video.get("duration_ms") or 0))),
            ("码率", format_bitrate(int(video.get("bitrate_bps") or 0))),
            ("帧率", f"{float(video.get('fps') or 0):g} fps" if video.get("fps") else "未知"),
            ("编码", str(video.get("codec") or "未知").upper()),
            ("大小", format_size(int(video.get("size_bytes") or 0))),
            ("修改时间", modified),
            ("相机", video.get("camera_model") or video.get("camera_make") or ""),
            ("镜头", video.get("lens") or ""),
            ("地点", video.get("location") or ""),
            ("路径", video.get("path") or ""),
        )
        self.technical_meta.setText(
            "<br>".join(
                f"<span style='color:{COLORS['muted']}'>{html.escape(label)}</span>&nbsp;&nbsp;"
                f"{html.escape(str(value))}"
                for label, value in values
                if value not in {None, ""}
            )
        )

    def _set_nfo_page(self, video: dict[str, Any]) -> None:
        ids = " / ".join(
            str(item.get("value"))
            for item in (video.get("nfo_ids") or [])
            if item.get("value")
        )
        actors = " / ".join(
            str(item.get("name"))
            for item in (video.get("actors") or [])
            if item.get("name")
        )
        genres = " / ".join(str(item) for item in (video.get("genres") or []))
        nfo_tags = " / ".join(str(item) for item in (video.get("nfo_tags") or []))
        values = (
            ("标题", video.get("title") or ""),
            ("编号", ids),
            ("首映", video.get("premiered") or ""),
            ("国家", video.get("country") or ""),
            ("片商", video.get("studio") or ""),
            ("类型", genres),
            ("NFO 标签", nfo_tags),
            ("演员", actors),
            ("简介", video.get("plot") or ""),
        )
        body = "<br>".join(
            f"<span style='color:{COLORS['muted']}'>{html.escape(label)}</span>&nbsp;&nbsp;"
            f"{html.escape(str(value))}"
            for label, value in values
            if value
        )
        self.nfo_page_info.setText(body or "此视频没有可用的 movie.nfo 字段。")

    def set_tags(self, tags: list[str]) -> None:
        self.tags_label.setText(self._tags_html(tags, "manual", COLORS["teal"]))

    def _tags_html(self, tags: list[Any], prefix: str, color: str) -> str:
        for key in [key for key in self._tag_links if key.startswith(prefix + ":")]:
            self._tag_links.pop(key, None)
        if not tags:
            return "无"
        links = []
        for index, tag in enumerate(tags):
            name = str(tag)
            key = f"{prefix}:{index}"
            self._tag_links[key] = name
            links.append(
                f'<a href="{key}" style="color:{color};text-decoration:none">'
                f"#{html.escape(name)}</a>"
            )
        return "&nbsp;&nbsp;".join(links)

    def _tag_link_clicked(self, link: str) -> None:
        tag = self._tag_links.get(link)
        if tag:
            self.tag_filter_requested.emit(tag)

    def _actors_html(self, actors: list[str]) -> str:
        self._actor_links.clear()
        if not actors:
            return "无"
        links = []
        for index, actor in enumerate(actors):
            key = f"actor:{index}"
            self._actor_links[key] = actor
            links.append(
                f'<a href="{key}" style="color:{COLORS["teal"]};text-decoration:none">'
                f"{html.escape(actor)}</a>"
            )
        return "&nbsp;&nbsp;".join(links)

    def _actor_link_clicked(self, link: str) -> None:
        actor = self._actor_links.get(link)
        if actor:
            self.actor_filter_requested.emit(actor)
