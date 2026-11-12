from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QStyleFactory,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..constants import ALL_LIBRARIES
from ..theme import COLORS
from .icons import svg_icon, svg_pixmap
from .widgets import SectionHeader


COUNT_ROLE = Qt.UserRole + 3


def _app_mark(size: int = 32) -> QPixmap:
    return svg_pixmap("app.svg", size)


def _line_icon(kind: str, color: str, size: int = 16) -> QIcon:
    name = "folder.svg" if kind in {"folder", "category"} else f"{kind}.svg"
    return svg_icon(name)


def _asset_icon(name: str) -> QIcon:
    return svg_icon(name)


class OrganizeTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 装饰列（根节点箭头）会出现一块多余的底色，且与自定义委托画的整行背景错位；
        # 直接关掉，箭头改由委托画在行右端。
        self.setRootIsDecorated(False)

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex) -> None:
        return


class SidebarTreeDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # 背景覆盖整行（含缩进/分支区），否则缩进子项的高亮块会缩在文字处，看上去和图标文案错位
        full = QRect(option.rect)
        view = self.parent()
        row = full
        tree = view if isinstance(view, QTreeWidget) else None
        if tree is not None:
            row = full.adjusted(-tree.indentation() * _depth(index), 0, 0, 0)
        background_rect = row.adjusted(0, 1, -2, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            background = QPainterPath()
            background.addRoundedRect(QRectF(background_rect), 6, 6)
            painter.fillPath(
                background,
                QColor(COLORS["surface_hover"] if selected else COLORS["surface_alt"]),
            )

        has_children = bool(tree is not None and tree.model().hasChildren(index))

        icon = index.data(Qt.DecorationRole)
        text_left = full.left() + 8
        if isinstance(icon, QIcon):
            mode = QIcon.Normal
            pixmap = icon.pixmap(16, 16, mode)
            # 图标必须画进固定的 16x16 逻辑矩形：高 DPI 下 pixmap 是物理像素（1.5 倍时高 24），
            # 用 pixmap.height()//2 居中会让图标比文字高 4 像素，看起来"歪"
            painter.drawPixmap(
                QRect(int(text_left), full.center().y() - 8, 16, 16),
                pixmap,
            )
            text_left += 24

        # 右侧从外到内依次是边距、折叠箭头、视频数量；依次扣减右边界，避免箭头压住数字
        right = full.right() - 6
        arrow_rect: QRect | None = None
        if has_children:
            arrow_rect = QRect(right - 14, full.center().y() - 7, 14, 14)
            right -= 18
        count = index.data(COUNT_ROLE)
        count_text = "" if count in {None, ""} else str(count)
        count_rect: QRectF | None = None
        if count_text:
            count_rect = QRectF(right - 30, full.top(), 30, full.height())
            right -= 34

        title_rect = QRectF(text_left, full.top(), max(0.0, right - text_left), full.height())
        font = option.font
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        title = painter.fontMetrics().elidedText(
            str(index.data(Qt.DisplayRole) or ""),
            Qt.ElideRight,
            int(title_rect.width()),
        )
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, title)

        if count_rect is not None:
            painter.setPen(QColor(COLORS["muted"] if selected else COLORS["faint"]))
            painter.drawText(count_rect, Qt.AlignRight | Qt.AlignVCenter, count_text)

        if arrow_rect is not None and tree is not None:
            # 折叠箭头用两条线段手绘：SVG 图标缩到 12px 会发虚、描边粗细不均，看起来是歪的
            pen = QPen(QColor(COLORS["muted"]), 1.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            center = arrow_rect.center()
            if tree.isExpanded(index):
                points = [
                    QPoint(center.x() - 4, center.y() - 2),
                    QPoint(center.x(), center.y() + 2),
                    QPoint(center.x() + 4, center.y() - 2),
                ]
            else:
                points = [
                    QPoint(center.x() - 2, center.y() - 4),
                    QPoint(center.x() + 2, center.y()),
                    QPoint(center.x() - 2, center.y() + 4),
                ]
            painter.drawPolyline(points)
        painter.restore()


def _depth(index: QModelIndex) -> int:
    depth = 0
    parent = index.parent()
    while parent.isValid():
        depth += 1
        parent = parent.parent()
    return depth


class Sidebar(QWidget):
    library_selected = Signal(int)
    view_selected = Signal(str)
    category_selected = Signal(object)
    add_category_requested = Signal()
    add_subcategory_requested = Signal(int)
    rename_category_requested = Signal(int)
    delete_category_requested = Signal(int)
    tag_selected = Signal(str)
    tag_rename_requested = Signal(str)
    tag_delete_requested = Signal(str)
    rating_selected = Signal(int)
    actor_selected = Signal(str)
    folder_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(260)
        self._categories: list[dict[str, Any]] = []
        self._view_mode = "all"
        self._special_items: dict[str, QTreeWidgetItem] = {}
        self._category_items: dict[int, QTreeWidgetItem] = {}
        self._tag_items: dict[str, QTreeWidgetItem] = {}
        self._tags: list[dict[str, Any]] = []
        self._counts: dict[str, int] = {}
        self._rating_items: dict[int, QTreeWidgetItem] = {}
        self._actors: list[dict[str, Any]] = []
        self._actor_total = 0
        self._actor_items: dict[str, QTreeWidgetItem] = {}
        self._folders: list[dict[str, Any]] = []
        self._folder_items: dict[str, QTreeWidgetItem] = {}
        self._expanded_category_ids: set[int] = set()
        self._tree_initialized = False
        self._rating_expanded = False
        self._rating_root: QTreeWidgetItem | None = None
        self._actor_expanded = False
        self._actor_root: QTreeWidgetItem | None = None
        self._folder_expanded = False
        self._folder_root: QTreeWidgetItem | None = None
        self._tag_expanded = False
        self._tag_root: QTreeWidgetItem | None = None
        self._selected_tag: str | None = None
        self._selected_actor: str | None = None
        self._selected_folder: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 18, 14, 16)
        layout.setSpacing(10)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(_app_mark(34))
        brand.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel("影库")
        name.setStyleSheet("font-size: 17px; font-weight: 700;")
        subtitle = QLabel("VIDEO LIBRARY")
        subtitle.setProperty("section", True)
        brand_text.addWidget(name)
        brand_text.addWidget(subtitle)
        brand.addLayout(brand_text)
        brand.addStretch(1)
        layout.addLayout(brand)

        self.library_combo = QComboBox()
        self.library_combo.setToolTip("切换当前资源库")
        self.library_combo.currentIndexChanged.connect(self._library_combo_changed)
        layout.addWidget(self.library_combo)

        organize_header = SectionHeader("浏览与分类")
        add_category = QPushButton()
        add_category.setIcon(_asset_icon("plus.svg"))
        add_category.setIconSize(QSize(16, 16))
        add_category.setFixedSize(24, 24)
        add_category.setProperty("quiet", True)
        add_category.setToolTip("新建分类")
        add_category.clicked.connect(self.add_category_requested)
        organize_header.add_action(add_category)
        layout.addWidget(organize_header)

        self.organize_tree = OrganizeTree()
        self.organize_tree.setObjectName("OrganizeTree")
        self.organize_tree.setColumnCount(1)
        self.organize_tree.setHeaderHidden(True)
        self.organize_tree.setIconSize(QSize(16, 16))
        self.organize_tree.setIndentation(18)
        self.organize_tree.setUniformRowHeights(True)
        self.organize_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.organize_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.organize_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.organize_tree.setItemDelegate(SidebarTreeDelegate(self.organize_tree))
        # 焦点框由自定义委托自己画，关掉视图默认的选中/焦点底条，否则缩进区会多出一块色块
        self.organize_tree.setFocusPolicy(Qt.NoFocus)
        self.organize_tree.setStyleSheet("QTreeWidget#OrganizeTree::item { outline: none; }")
        self.organize_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        palette = self.organize_tree.palette()
        palette.setColor(QPalette.Highlight, QColor(COLORS["surface_hover"]))
        palette.setColor(QPalette.HighlightedText, QColor(COLORS["text"]))
        self.organize_tree.setPalette(palette)
        self.organize_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.organize_tree.customContextMenuRequested.connect(self._category_menu)
        self.organize_tree.itemClicked.connect(self._organize_clicked)
        self.organize_tree.itemExpanded.connect(self._category_expanded)
        self.organize_tree.itemCollapsed.connect(self._category_collapsed)
        layout.addWidget(self.organize_tree, 1)

        self._rebuild_tree()

    def set_libraries(
        self,
        libraries: list[dict[str, Any]],
        selected_id: int | None = None,
        show_isolated: bool = False,
    ) -> None:
        self.library_combo.blockSignals(True)
        self.library_combo.clear()
        shared_count = sum(
            int(library.get("available_count") or 0)
            for library in libraries
            if not library.get("isolated")
        )
        self.library_combo.addItem(
            f"全部资源库  ·  {shared_count} 个视频",
            ALL_LIBRARIES,
        )
        for library in libraries:
            if library.get("isolated") and not show_isolated:
                continue
            label = library["name"]
            if library.get("isolated"):
                label += "  [隔离]"
            self.library_combo.addItem(
                f"{label}  ·  {library.get('available_count') or 0} 个视频",
                int(library["id"]),
            )
        index = self.library_combo.findData(selected_id)
        self.library_combo.setCurrentIndex(max(0, index))
        self.library_combo.blockSignals(False)

    def select_library_id(self, library_id: int) -> None:
        index = self.library_combo.findData(library_id)
        if index >= 0:
            self.library_combo.blockSignals(True)
            self.library_combo.setCurrentIndex(index)
            self.library_combo.blockSignals(False)

    def current_library_id(self) -> int:
        value = self.library_combo.currentData()
        return int(value) if value is not None else ALL_LIBRARIES

    def set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self._selected_tag = None
        self._selected_actor = None
        self._selected_folder = None
        item = self._special_items.get(mode)
        if item:
            self.organize_tree.setCurrentItem(item)

    def clear_category_selection(self) -> None:
        self._selected_tag = None
        self._selected_actor = None
        self._selected_folder = None
        item = self._special_items.get(self._view_mode)
        if item:
            self.organize_tree.setCurrentItem(item)

    def select_category_id(self, category_id: int) -> None:
        self._selected_tag = None
        self._selected_actor = None
        self._selected_folder = None
        item = self._category_items.get(category_id)
        if item:
            self.organize_tree.setCurrentItem(item)

    def select_tag_name(self, tag: str) -> None:
        self._selected_tag = tag
        self._selected_actor = None
        self._selected_folder = None
        item = self._tag_items.get(tag.casefold())
        if item:
            self.organize_tree.setCurrentItem(item)

    def select_actor_name(self, actor: str | None) -> None:
        self._selected_actor = actor or None
        if not actor:
            current = self.organize_tree.currentItem()
            if current and current.data(0, Qt.UserRole) == "actor":
                item = self._special_items.get(self._view_mode)
                if item:
                    self.organize_tree.setCurrentItem(item)
            return
        self._selected_tag = None
        self._selected_folder = None
        item = self._actor_items.get(actor.casefold())
        if item:
            self.organize_tree.setCurrentItem(item)

    def select_folder_name(self, folder: str | None) -> None:
        self._selected_folder = folder or None
        if not folder:
            return
        self._selected_tag = None
        self._selected_actor = None
        item = self._folder_items.get(folder.casefold())
        if item:
            self.organize_tree.setCurrentItem(item)

    def selected_category_id(self) -> int | None:
        item = self.organize_tree.currentItem()
        if not item or item.data(0, Qt.UserRole) != "category":
            return None
        return item.data(0, Qt.UserRole + 1)

    def set_categories(self, categories: list[dict[str, Any]]) -> None:
        self._categories = categories
        self._rebuild_tree()

    def set_tags(self, tags: list[dict[str, Any]]) -> None:
        self._tags = list(tags)
        self._rebuild_tree()

    def set_actors(self, actors: list[dict[str, Any]]) -> None:
        self._actors = list(actors[:30])
        self._actor_total = len(actors)
        self._rebuild_tree()

    def set_folders(self, folders: list[dict[str, Any]]) -> None:
        self._folders = list(folders)
        self._rebuild_tree()

    def set_counts(self, counts: dict[str, int]) -> None:
        self._counts = {str(key): int(value) for key, value in counts.items()}
        self._apply_counts()

    def adjust_rating_count(self, old_rating: object, new_rating: object) -> None:
        if not self._counts:
            return

        def bucket(value: object) -> int:
            try:
                return min(5, max(0, int(float(value or 0) + 0.5)))
            except (TypeError, ValueError):
                return 0

        old_bucket = bucket(old_rating)
        new_bucket = bucket(new_rating)
        if old_bucket == new_bucket:
            return
        old_key = f"rating:{old_bucket}"
        new_key = f"rating:{new_bucket}"
        self._counts[old_key] = max(0, int(self._counts.get(old_key, 0)) - 1)
        self._counts[new_key] = int(self._counts.get(new_key, 0)) + 1
        self._apply_counts()

    def _apply_counts(self) -> None:
        for mode, item in self._special_items.items():
            item.setData(0, COUNT_ROLE, int(self._counts.get(mode, 0)))
        for stars, item in self._rating_items.items():
            item.setData(
                0,
                COUNT_ROLE,
                int(self._counts.get(f"rating:{stars}", 0)),
            )
        if self._rating_root is not None:
            self._rating_root.setData(
                0,
                COUNT_ROLE,
                int(self._counts.get("all", 0)),
            )
        self.organize_tree.viewport().update()

    def _rebuild_tree(self) -> None:
        selected_category = self.selected_category_id()
        selected_tag = self._selected_tag
        for category_id, item in self._category_items.items():
            if item.isExpanded():
                self._expanded_category_ids.add(category_id)
        self.organize_tree.clear()
        self._special_items.clear()
        self._category_items.clear()
        self._tag_items.clear()
        self._rating_items.clear()
        self._actor_items.clear()
        self._actor_root = None
        self._folder_items.clear()
        self._folder_root = None

        specials = (
            ("全部视频", "all", "all"),
            ("继续观看", "continue", "continue"),
            ("最近加入", "recent", "log"),
            ("收藏", "favorite", "favorite"),
            ("未分类", "uncategorized", "folder"),
        )
        for label, mode, icon_kind in specials:
            item = QTreeWidgetItem([label])
            item.setIcon(0, _line_icon(icon_kind, COLORS["muted"]))
            item.setSizeHint(0, QSize(0, 32))
            item.setData(0, Qt.UserRole, "view")
            item.setData(0, Qt.UserRole + 2, mode)
            item.setData(0, COUNT_ROLE, int(self._counts.get(mode, 0)))
            self.organize_tree.addTopLevelItem(item)
            self._special_items[mode] = item

        rating_root = QTreeWidgetItem(["评级"])
        rating_root.setIcon(0, _line_icon("favorite", COLORS["muted"]))
        rating_root.setSizeHint(0, QSize(0, 32))
        rating_root.setData(0, Qt.UserRole, "rating_root")
        rating_root.setData(0, COUNT_ROLE, int(self._counts.get("all", 0)))
        rating_root.setToolTip(0, "按星级筛选视频")
        for stars in (5, 4, 3, 2, 1, 0):
            child = QTreeWidgetItem([f"{stars} 星" if stars else "未评级"])
            child.setIcon(
                0,
                _line_icon("favorite" if stars else "tag", COLORS["muted"]),
            )
            child.setSizeHint(0, QSize(0, 32))
            child.setData(0, Qt.UserRole, "rating")
            child.setData(0, Qt.UserRole + 2, stars)
            child.setData(0, COUNT_ROLE, int(self._counts.get(f"rating:{stars}", 0)))
            rating_root.addChild(child)
            self._rating_items[stars] = child
        rating_root.setExpanded(self._rating_expanded)
        self.organize_tree.addTopLevelItem(rating_root)
        self._rating_root = rating_root

        if self._actors:
            actor_root = QTreeWidgetItem(["演员"])
            actor_root.setIcon(0, _line_icon("actor", COLORS["muted"]))
            actor_root.setSizeHint(0, QSize(0, 32))
            actor_root.setData(0, Qt.UserRole, "actor_root")
            actor_root.setData(0, COUNT_ROLE, self._actor_total)
            actor_root.setToolTip(
                0,
                f"显示前 {len(self._actors)} 个高频演员\n"
                "完整演员筛选请使用顶部“NFO 筛选”",
            )
            for actor in self._actors:
                name = str(actor.get("value") or "").strip()
                if not name:
                    continue
                child = QTreeWidgetItem([name])
                child.setIcon(0, _line_icon("actor", COLORS["muted"]))
                child.setSizeHint(0, QSize(0, 32))
                child.setData(0, Qt.UserRole, "actor")
                child.setData(0, Qt.UserRole + 2, name)
                child.setData(
                    0,
                    COUNT_ROLE,
                    int(actor.get("count") or 0),
                )
                child.setToolTip(
                    0,
                    f"{name}\n{int(actor.get('count') or 0)} 个视频",
                )
                actor_root.addChild(child)
                self._actor_items[name.casefold()] = child
            actor_root.setExpanded(self._actor_expanded)
            self.organize_tree.addTopLevelItem(actor_root)
            self._actor_root = actor_root

        if self._folders:
            folder_root = QTreeWidgetItem(["文件夹"])
            folder_root.setIcon(0, _line_icon("folder", COLORS["muted"]))
            folder_root.setSizeHint(0, QSize(0, 32))
            folder_root.setData(0, Qt.UserRole, "folder_root")
            folder_root.setData(0, COUNT_ROLE, len(self._folders))
            folder_root.setToolTip(0, "按资源库一级子文件夹筛选")
            for folder in self._folders:
                name = str(folder.get("name") or "").strip()
                if not name:
                    continue
                child = QTreeWidgetItem([name])
                child.setIcon(0, _line_icon("folder", COLORS["muted"]))
                child.setSizeHint(0, QSize(0, 32))
                child.setData(0, Qt.UserRole, "folder")
                child.setData(0, Qt.UserRole + 2, name)
                child.setData(
                    0,
                    COUNT_ROLE,
                    int(folder.get("video_count") or 0),
                )
                child.setToolTip(
                    0,
                    f"{name}\n{int(folder.get('video_count') or 0)} 个视频",
                )
                folder_root.addChild(child)
                self._folder_items[name.casefold()] = child
            folder_root.setExpanded(self._folder_expanded)
            self.organize_tree.addTopLevelItem(folder_root)
            self._folder_root = folder_root

        nodes: dict[int, QTreeWidgetItem] = {}
        pending = list(self._categories)
        while pending:
            progressed = False
            for category in list(pending):
                parent_id = category.get("parent_id")
                if parent_id is not None and parent_id not in nodes:
                    continue
                label = str(category.get("display_name") or category["name"])
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.UserRole, "category")
                item.setData(0, Qt.UserRole + 1, category["id"])
                item.setData(0, Qt.UserRole + 2, category["name"])
                item.setData(0, COUNT_ROLE, int(category.get("video_count") or 0))
                item.setToolTip(0, label)
                item.setSizeHint(0, QSize(0, 32))
                color = QColor(category.get("color") or COLORS["accent"])
                item.setIcon(0, _line_icon("category", color.name()))
                parent = nodes.get(parent_id)
                if parent:
                    parent.addChild(item)
                else:
                    self.organize_tree.addTopLevelItem(item)
                nodes[category["id"]] = item
                self._category_items[category["id"]] = item
                pending.remove(category)
                progressed = True
            if not progressed:
                break

        tag_root = QTreeWidgetItem(["标签"])
        tag_root.setIcon(0, _line_icon("tag", COLORS["muted"]))
        tag_root.setSizeHint(0, QSize(0, 32))
        tag_root.setData(0, Qt.UserRole, "tag_root")
        tag_root.setData(0, COUNT_ROLE, len(self._tags))
        tag_root.setToolTip(0, f"{len(self._tags)} 个标签")
        for tag in self._tags:
            name = str(tag.get("name") or "")
            if not name:
                continue
            child = QTreeWidgetItem([name])
            child.setIcon(0, _line_icon("tag", COLORS["muted"]))
            child.setSizeHint(0, QSize(0, 32))
            child.setData(0, Qt.UserRole, "tag")
            child.setData(0, Qt.UserRole + 2, name)
            child.setData(0, COUNT_ROLE, int(tag.get("video_count") or 0))
            source = str(tag.get("source") or "manual")
            source_label = {
                "nfo": "NFO 标签",
                "both": "NFO 与软件标签",
                "manual": "软件标签",
            }.get(source, "软件标签")
            child.setToolTip(
                0,
                f"{name}\n{int(tag.get('video_count') or 0)} 个视频 · {source_label}",
            )
            tag_root.addChild(child)
            self._tag_items[name.casefold()] = child
        tag_root.setExpanded(self._tag_expanded)
        self.organize_tree.addTopLevelItem(tag_root)
        self._tag_root = tag_root

        if not self._tree_initialized and self._categories:
            self.organize_tree.expandAll()
            self._expanded_category_ids.update(
                category_id
                for category_id, item in self._category_items.items()
                if item.childCount()
            )
            self._tree_initialized = True
        elif self._tree_initialized:
            for category_id in self._expanded_category_ids:
                item = self._category_items.get(category_id)
                if item:
                    item.setExpanded(True)
        if self._rating_root is not None:
            self._rating_root.setExpanded(self._rating_expanded)
        if self._actor_root is not None:
            self._actor_root.setExpanded(self._actor_expanded)
        if self._folder_root is not None:
            self._folder_root.setExpanded(self._folder_expanded)
        selected_actor = (
            self._actor_items.get(self._selected_actor.casefold())
            if self._selected_actor
            else None
        )
        selected_folder = (
            self._folder_items.get(self._selected_folder.casefold())
            if self._selected_folder
            else None
        )
        target = (
            self._tag_items.get(selected_tag.casefold())
            if selected_tag is not None
            else self._category_items.get(selected_category)
            if selected_category is not None
            else selected_actor
            if selected_actor is not None
            else selected_folder
            if selected_folder is not None
            else self._special_items.get(self._view_mode)
        )
        if target:
            self.organize_tree.setCurrentItem(target)

    def _category_expanded(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, Qt.UserRole)
        if kind == "category":
            self._expanded_category_ids.add(int(item.data(0, Qt.UserRole + 1)))
        elif kind == "tag_root":
            self._tag_expanded = True
        elif kind == "actor_root":
            self._actor_expanded = True
        elif kind == "folder_root":
            self._folder_expanded = True

    def _category_collapsed(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, Qt.UserRole)
        if kind == "category":
            self._expanded_category_ids.discard(int(item.data(0, Qt.UserRole + 1)))
        elif kind == "tag_root":
            self._tag_expanded = False
        elif kind == "actor_root":
            self._actor_expanded = False
        elif kind == "folder_root":
            self._folder_expanded = False

    def _library_combo_changed(self, index: int) -> None:
        value = self.library_combo.itemData(index)
        if value is not None:
            self.library_selected.emit(int(value))

    def _organize_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        kind = item.data(0, Qt.UserRole)
        if kind == "rating_root":
            self._rating_expanded = not item.isExpanded()
            item.setExpanded(self._rating_expanded)
            return
        if kind == "actor_root":
            self._actor_expanded = not item.isExpanded()
            item.setExpanded(self._actor_expanded)
            return
        if kind == "folder_root":
            self._folder_expanded = not item.isExpanded()
            item.setExpanded(self._folder_expanded)
            return
        if kind == "rating":
            self._selected_tag = None
            self._selected_actor = None
            self.rating_selected.emit(int(item.data(0, Qt.UserRole + 2)))
            return
        if kind == "actor":
            self._selected_tag = None
            self._selected_actor = str(item.data(0, Qt.UserRole + 2))
            self._selected_folder = None
            self.actor_selected.emit(self._selected_actor)
            return
        if kind == "folder":
            self._selected_tag = None
            self._selected_actor = None
            self._selected_folder = str(item.data(0, Qt.UserRole + 2))
            self.folder_selected.emit(self._selected_folder)
            return
        if kind == "view":
            mode = str(item.data(0, Qt.UserRole + 2))
            self._view_mode = mode
            self._selected_tag = None
            self._selected_actor = None
            self._selected_folder = None
            self.view_selected.emit(mode)
        elif kind == "category":
            self._selected_tag = None
            self._selected_actor = None
            self._selected_folder = None
            self.category_selected.emit(int(item.data(0, Qt.UserRole + 1)))
        elif kind == "tag":
            tag = str(item.data(0, Qt.UserRole + 2))
            self._selected_tag = tag
            self._selected_actor = None
            self._selected_folder = None
            self.tag_selected.emit(tag)

    def _tag_menu(self, item: QTreeWidgetItem, position: QPoint) -> None:
        tag = str(item.data(0, Qt.UserRole + 2))
        menu = QMenu(self)
        rename = menu.addAction("重命名")
        delete = menu.addAction("删除标签")
        chosen = menu.exec(
            self.organize_tree.viewport().mapToGlobal(position)
        )
        if chosen == rename:
            self.tag_rename_requested.emit(tag)
        elif chosen == delete:
            self.tag_delete_requested.emit(tag)

    def _category_menu(self, position) -> None:
        item = self.organize_tree.itemAt(position)
        if not item:
            return
        if item.data(0, Qt.UserRole) == "tag":
            self._tag_menu(item, position)
            return
        if item.data(0, Qt.UserRole) != "category":
            return
        category_id = int(item.data(0, Qt.UserRole + 1))
        menu = QMenu(self)
        add_child = menu.addAction("新建子分类")
        rename = menu.addAction("重命名")
        delete = menu.addAction("删除分类")
        chosen = menu.exec(self.organize_tree.viewport().mapToGlobal(position))
        if chosen == add_child:
            self.add_subcategory_requested.emit(category_id)
        elif chosen == rename:
            self.rename_category_requested.emit(category_id)
        elif chosen == delete:
            self.delete_category_requested.emit(category_id)
