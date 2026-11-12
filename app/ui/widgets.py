from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..constants import SUBTITLE_EXTENSIONS
from ..media_tools import (
    format_bitrate,
    format_duration,
    format_size,
    resolution_label,
)
from ..theme import COLORS
from .icons import svg_pixmap
from .models import VideoRoles


def video_has_subtitle(video: dict) -> bool:
    if str(video.get("subtitle_path") or "").strip():
        return True
    return any(
        Path(str(sidecar)).suffix.lower() in SUBTITLE_EXTENSIONS
        for sidecar in video.get("sidecars") or []
    )


def configure_filter_combo(combo: QComboBox, max_visible: int = 10) -> None:
    combo.setMaxVisibleItems(max_visible)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(18)
    view = combo.view()
    view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    view.setTextElideMode(Qt.ElideRight)
    view.setUniformItemSizes(True)


class EmptyState(QWidget):
    action_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(12)
        layout.addStretch(1)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setPixmap(svg_pixmap("play.svg", 52))
        layout.addWidget(self.icon_label)

        self.title = QLabel("还没有视频")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self.title)

        self.subtitle = QLabel("添加一个资源库后，视频会在这里形成索引。")
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setProperty("muted", True)
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)
        self.action_button = QPushButton("清除筛选")
        self.action_button.setProperty("accent", True)
        self.action_button.clicked.connect(lambda: self.action_clicked.emit())
        self.action_button.hide()
        layout.addWidget(self.action_button, 0, Qt.AlignHCenter)
        layout.addStretch(2)

    def set_text(self, title: str, subtitle: str, show_action: bool = False) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.action_button.setVisible(show_action)


class ClickableSlider(QSlider):
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self.maximum() <= self.minimum():
            super().mousePressEvent(event)
            return
        value = self._value_at(event.position())
        self.setValue(value)
        self.setSliderDown(True)
        self.sliderMoved.emit(value)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self.isSliderDown() or not event.buttons() & Qt.LeftButton:
            super().mouseMoveEvent(event)
            return
        value = self._value_at(event.position())
        self.setValue(value)
        self.sliderMoved.emit(value)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self.isSliderDown():
            super().mouseReleaseEvent(event)
            return
        self.setSliderDown(False)
        event.accept()

    def _value_at(self, position) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderHandle,
            self,
        )
        if self.orientation() == Qt.Horizontal:
            start = groove.x()
            span = max(1, groove.width() - handle.width())
            offset = round(position.x() - handle.width() / 2) - start
        else:
            start = groove.y()
            span = max(1, groove.height() - handle.height())
            offset = round(position.y() - handle.height() / 2) - start
        offset = max(0, min(span, offset))
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            offset,
            span,
            option.upsideDown,
        )


class VideoCardDelegate(QStyledItemDelegate):
    CACHE_BYTES = 32 * 1024 * 1024
    group_toggle_requested = Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._cache_bytes = 0
        self._portrait_mode = False
        self._card_size = QSize(238, 220)
        self._placeholder = svg_pixmap("play.svg", 54)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return self._card_size

    @property
    def card_size(self) -> QSize:
        return self._card_size

    def set_card_size(self, size: QSize) -> None:
        self._card_size = size

    def set_portrait_mode(self, enabled: bool) -> None:
        self._portrait_mode = enabled

    def _card_rect(self, item_rect: QRect, collapsed: bool) -> QRect:
        rect = item_rect.adjusted(5, 5, -5, -5)
        if collapsed:
            rect = rect.adjusted(0, 0, -8, -8)
        return rect

    def _poster_rect(self, item_rect: QRect, collapsed: bool) -> QRect:
        rect = self._card_rect(item_rect, collapsed)
        poster_height = round(rect.width() * (1.6 if self._portrait_mode else 0.529))
        return QRect(rect.left() + 9, rect.top() + 9, rect.width() - 18, poster_height)

    def group_badge_rect(self, item_rect: QRect, collapsed: bool) -> QRect:
        """视频组的段数徽章；展开后组长卡片上这块就是"收起"按钮。"""
        poster = self._poster_rect(item_rect, collapsed)
        width = 94 if collapsed else 88
        return QRect(poster.left() + 7, poster.top() + 7, width, 22)

    def _draw_chevron(self, painter: QPainter, x: int, y: int, up: bool) -> None:
        painter.setPen(
            QPen(QColor("#111820"), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        )
        half = 3
        tip = y - half // 2 if up else y + half // 2
        tail = y + half // 2 if up else y - half // 2
        painter.drawLine(x - half, tail, x, tip)
        painter.drawLine(x, tip, x + half, tail)

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() == QEvent.MouseButtonRelease:
            video = index.data(VideoRoles.Video) or {}
            if video.get("group_id") is not None:
                collapsed = bool(video.get("group_collapsed"))
                can_toggle = collapsed or bool(video.get("group_leader"))
                if can_toggle and self.group_badge_rect(
                    option.rect, collapsed
                ).contains(event.pos()):
                    self.group_toggle_requested.emit(
                        int(video["group_id"]),
                        collapsed,
                    )
                    return True
        return super().editorEvent(event, model, option, index)

    def _poster(self, path: str | None) -> QPixmap | None:
        if not path:
            return None
        if path not in self._pixmaps:
            pixmap = QPixmap(path)
            if pixmap.isNull():
                return None
            self._pixmaps[path] = pixmap
            self._cache_bytes += max(1, pixmap.width() * pixmap.height() * 4)
            while len(self._pixmaps) > 1 and self._cache_bytes > self.CACHE_BYTES:
                _, evicted = self._pixmaps.popitem(last=False)
                self._cache_bytes -= max(1, evicted.width() * evicted.height() * 4)
        self._pixmaps.move_to_end(path)
        return self._pixmaps[path]

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        video = index.data(VideoRoles.Video) or {}
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        rect = option.rect.adjusted(5, 5, -5, -5)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        grouped = bool(video.get("group_collapsed"))
        leader = bool(video.get("group_leader"))
        in_group = video.get("group_id") is not None
        if grouped:
            for offset, color in ((10, "#102527"), (5, "#173235")):
                ghost = QPainterPath()
                ghost.addRoundedRect(QRectF(rect.adjusted(offset, offset, 0, 0)), 8, 8)
                painter.fillPath(ghost, QColor(color))
            rect = rect.adjusted(0, 0, -10, -10)

        card = QPainterPath()
        card.addRoundedRect(QRectF(rect), 8, 8)
        if in_group:
            painter.fillPath(card, QColor("#214044" if hovered else "#1B2F31"))
            painter.setPen(
                QPen(
                    QColor(COLORS["teal"]),
                    2.4 if selected else 1.6,
                )
            )
            painter.drawPath(card)
            painter.fillRect(
                QRectF(rect.left() + 1.5, rect.top() + 10, 3.5, rect.height() - 20),
                QColor(COLORS["teal"]),
            )
        else:
            painter.fillPath(
                card,
                QColor(
                    "#1D3032"
                    if selected
                    else "#202731"
                    if hovered
                    else COLORS["surface"]
                ),
            )
            painter.setPen(
                QPen(
                    QColor(COLORS["teal"] if selected else COLORS["line"]),
                    2.0 if selected else 1.2,
                )
            )
            painter.drawPath(card)

        poster_height = round(
            rect.width() * (1.6 if self._portrait_mode else 0.529)
        )
        poster_rect = QRect(
            rect.left() + 9,
            rect.top() + 9,
            rect.width() - 18,
            poster_height,
        )
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(poster_rect), 6, 6)
        painter.setClipPath(clip)
        painter.fillRect(poster_rect, QColor("#101419"))
        pixmap = self._poster(index.data(VideoRoles.ThumbPath))
        if pixmap:
            if self._portrait_mode:
                # 竖屏页统一竖卡，封面填满裁剪最省空间
                scaled = pixmap.scaled(
                    poster_rect.size(),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                source_x = max(0, (scaled.width() - poster_rect.width()) // 2)
                source_y = max(0, (scaled.height() - poster_rect.height()) // 2)
                painter.drawPixmap(
                    poster_rect,
                    scaled,
                    QRect(
                        source_x,
                        source_y,
                        poster_rect.width(),
                        poster_rect.height(),
                    ),
                )
            else:
                # 全部/横屏页是统一卡片，完整显示才分得出横竖屏
                scaled = pixmap.scaled(
                    poster_rect.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                target = QRect(0, 0, scaled.width(), scaled.height())
                target.moveCenter(poster_rect.center())
                painter.drawPixmap(target.topLeft(), scaled)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#252D37"))
            painter.drawEllipse(poster_rect.center(), 22, 22)
            target = QRect(0, 0, 28, 28)
            target.moveCenter(poster_rect.center())
            painter.drawPixmap(target, self._placeholder)

        painter.setClipping(False)
        progress = float(index.data(VideoRoles.Progress) or 0)
        if progress > 0 and not grouped:
            painter.fillRect(
                QRect(poster_rect.left(), poster_rect.bottom() - 3, poster_rect.width(), 3),
                QColor(35, 40, 47),
            )
            painter.fillRect(
                QRect(
                    poster_rect.left(),
                    poster_rect.bottom() - 3,
                    int(poster_rect.width() * progress),
                    3,
                ),
                QColor(COLORS["accent"]),
            )

        rating = int(round(float(video.get("rating") or 0)))
        if rating > 0 and not grouped:
            stars = "★" * min(5, rating) + "☆" * max(0, 5 - min(5, rating))
            box = QRect(
                poster_rect.left() + 6,
                poster_rect.bottom() - 26,
                min(68, max(40, poster_rect.width() - 12)),
                20,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(18, 22, 27, 205))
            painter.drawRoundedRect(box, 4, 4)
            star_font = QFont(option.font)
            star_font.setPointSize(9)
            painter.setFont(star_font)
            painter.setPen(QColor(COLORS["accent"]))
            painter.drawText(box, Qt.AlignCenter, stars)

        masked = video.get("physical_ext") != video.get("logical_ext")
        if masked:
            badge = QRect(poster_rect.right() - 47, poster_rect.top() + 7, 40, 18)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(18, 22, 27, 220))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor(COLORS["teal"]))
            font = QFont(option.font)
            font.setPointSize(7)
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignCenter, "伪装")

        if video_has_subtitle(video):
            badge = QRect(
                poster_rect.right() - 57,
                poster_rect.top() + (29 if masked else 7),
                50,
                18,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(18, 22, 27, 220))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor(COLORS["teal"]))
            font = QFont(option.font)
            font.setPointSize(7)
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignCenter, "有字幕")

        if grouped:
            badge = self.group_badge_rect(option.rect, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["teal"]))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#111820"))
            font = QFont(option.font)
            font.setPointSize(8)
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.drawText(
                badge.adjusted(5, 0, -12, 0),
                Qt.AlignCenter,
                f"视频组 · {int(video.get('group_count') or 0)}",
            )
            self._draw_chevron(painter, badge.right() - 8, badge.center().y(), up=False)
        elif video.get("group_id") is not None:
            if video.get("group_leader"):
                badge = self.group_badge_rect(option.rect, False)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(COLORS["teal"]))
                painter.drawRoundedRect(badge, 4, 4)
                painter.setPen(QColor("#111820"))
                font = QFont(option.font)
                font.setPointSize(8)
                font.setWeight(QFont.DemiBold)
                painter.setFont(font)
                painter.drawText(
                    badge.adjusted(5, 0, -12, 0),
                    Qt.AlignCenter,
                    f"收起 · {int(video.get('group_count') or 0)}",
                )
                self._draw_chevron(
                    painter, badge.right() - 8, badge.center().y(), up=True
                )
            else:
                badge = QRect(poster_rect.left() + 7, poster_rect.top() + 7, 38, 20)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(18, 22, 27, 220))
                painter.drawRoundedRect(badge, 4, 4)
                painter.setPen(QColor(COLORS["teal"]))
                font = QFont(option.font)
                font.setPointSize(7)
                font.setWeight(QFont.DemiBold)
                painter.setFont(font)
                painter.drawText(badge, Qt.AlignCenter, "组内")

        title_rect = QRect(rect.left() + 11, poster_rect.bottom() + 12, rect.width() - 22, 23)
        font = QFont(option.font)
        font.setPointSize(10)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        title = QFontMetrics(font).elidedText(
            str(video.get("title") or video.get("file_name") or ""),
            Qt.ElideRight,
            title_rect.width(),
        )
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, title)

        subtitle_font = QFont(option.font)
        subtitle_font.setPointSize(8)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(COLORS["muted"]))
        duration = format_duration(int(video.get("duration_ms") or 0))
        resolution = resolution_label(
            int(video.get("width") or 0),
            int(video.get("height") or 0),
        )
        codec = str(video.get("codec") or "视频").upper()
        capture_at = str(video.get("capture_at") or "").replace("T", " ")[:16]
        subtitle = (
            f"{int(video.get('group_count') or 0)} 个片段  ·  {duration}  ·  {codec}"
            if grouped
            else f"{capture_at}  ·  {resolution}  ·  {codec}"
            if capture_at
            else f"{duration}  ·  {resolution}  ·  {codec}"
        )
        metrics = QFontMetrics(subtitle_font)
        subtitle = metrics.elidedText(subtitle, Qt.ElideRight, rect.width() - 22)
        painter.drawText(
            QRect(rect.left() + 11, poster_rect.bottom() + 34, rect.width() - 22, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            subtitle,
        )
        storage = (
            f"{format_size(int(video.get('size_bytes') or 0))}  ·  "
            f"{format_bitrate(int(video.get('bitrate_bps') or 0))}"
        )
        if capture_at and not grouped:
            storage = f"{duration}  ·  {storage}"
        storage = metrics.elidedText(storage, Qt.ElideRight, rect.width() - 22)
        painter.drawText(
            QRect(rect.left() + 11, poster_rect.bottom() + 53, rect.width() - 22, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            storage,
        )
        if video.get("favorite"):
            painter.setPen(QColor(COLORS["accent"]))
            painter.drawText(
                QRect(rect.right() - 31, poster_rect.bottom() + 32, 20, 20),
                Qt.AlignCenter,
                "◆",
            )
        painter.restore()


class SectionHeader(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 4)
        layout.setSpacing(6)
        self.label = QLabel(title.upper())
        self.label.setProperty("section", True)
        layout.addWidget(self.label)
        layout.addStretch(1)

    def add_action(self, widget: QWidget) -> None:
        self.layout().addWidget(widget)
