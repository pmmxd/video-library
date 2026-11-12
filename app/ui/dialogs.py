from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QSize, QStringListModel, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..translation import SOURCE_LANGUAGES
from .icons import svg_icon
from .player_view import VideoSurface
from .widgets import ClickableSlider, configure_filter_combo

from ..constants import (
    DATA_DIR,
    DB_PATH,
    DEFAULT_MASK_MAP,
    MASKABLE_EXTENSIONS,
)
from ..media_tools import format_timecode, parse_timecode


class AddLibraryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加资源库")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)
        title = QLabel("添加资源库")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel("软件只读取和操作这个目录内的文件，不会改变未添加的目录。")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：收藏、项目归档、普通视频")
        form.addRow("名称", self.name_edit)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择资源库根目录")
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("目录", path_row)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("自动识别（NFO / 普通目录）", "auto")
        self.kind_combo.addItem("NFO 影视项目（NFO、海报、字幕）", "nfo")
        self.kind_combo.addItem("摄影拍摄项目（日期、相机、镜头）", "shooting")
        self.kind_combo.addItem("纯视频文件夹", "plain")
        form.addRow("库类型", self.kind_combo)

        self.mask_checkbox = QCheckBox("启用后缀伪装模式")
        self.mask_checkbox.setToolTip("扫描完成后切换当前库中已登记文件的后缀")
        form.addRow("", self.mask_checkbox)
        self.isolation_checkbox = QCheckBox("从“全部资源库”中隔离")
        self.isolation_checkbox.setToolTip("隔离库不参与全部资源库汇总")
        form.addRow("", self.isolation_checkbox)

        layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("添加")
        self.buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择资源库目录")
        if path:
            self.path_edit.setText(path)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(path).name)

    def _accept(self) -> None:
        path = Path(self.path_edit.text().strip())
        name = self.name_edit.text().strip()
        if not path.is_dir():
            QMessageBox.warning(self, "目录无效", "请选择一个存在的文件夹。")
            return
        if not name:
            QMessageBox.warning(self, "缺少名称", "请为资源库填写名称。")
            return
        self.accept()

    def values(self) -> tuple[str, str, str, bool, bool]:
        return (
            self.name_edit.text().strip(),
            str(Path(self.path_edit.text().strip()).resolve()),
            str(self.kind_combo.currentData()),
            self.mask_checkbox.isChecked(),
            self.isolation_checkbox.isChecked(),
        )


class RelocateLibraryDialog(QDialog):
    def __init__(
        self,
        library_name: str,
        current_root: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("迁移资源库目录")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        title = QLabel("迁移资源库目录")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel(
            "请先在文件管理器中把整个资源库移动到新位置，再在这里选择新根目录。"
            "软件只更新数据库链接，不复制或删除视频文件。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        old_path = QLabel(current_root)
        old_path.setWordWrap(True)
        old_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        old_path.setProperty("muted", True)
        form.addRow("当前目录", old_path)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择已经迁移完成的新资源库根目录")
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("新目录", path_row)
        layout.addLayout(form)

        warning = QLabel(
            f"将更新资源库“{library_name}”的全部视频、NFO、字幕、海报和附属文件路径。"
            "视频 ID、分类、标签、收藏、评分和播放进度保持不变。"
            "执行前会自动备份数据库，旧目录不会被删除。"
        )
        warning.setWordWrap(True)
        warning.setProperty("muted", True)
        layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("校验并迁移")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "选择迁移后的资源库根目录",
        )
        if path:
            self.path_edit.setText(path)

    def _accept(self) -> None:
        path = Path(self.path_edit.text().strip())
        if not path.is_dir():
            QMessageBox.warning(
                self,
                "目录无效",
                "请选择已经迁移完成并且存在的资源库文件夹。",
            )
            return
        self.accept()

    def new_root(self) -> str:
        return str(Path(self.path_edit.text().strip()).resolve())


class RenameDialog(QDialog):
    def __init__(self, current_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重命名")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        title = QLabel("重命名视频")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel("只修改文件名，保留当前后缀；同名前缀的字幕和附属资源会一起更新。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        self.edit = QLineEdit(current_name)
        self.edit.selectAll()
        layout.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> str:
        return self.edit.text().strip()


class TagsDialog(QDialog):
    def __init__(
        self,
        existing: list[str],
        parent=None,
        batch_count: int | None = None,
        available: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("批量打标签" if batch_count else "编辑标签")
        self.setMinimumSize(680, 460)
        self._tags = self._normalized(existing)
        self._available = self._normalized(available or [])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        title = QLabel("批量打标签" if batch_count else "编辑标签")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel(
            f"给选中的 {batch_count} 个视频追加标签，各视频原有标签会保留。"
            if batch_count
            else "输入标签后按回车添加，也可以从已有标签中选择。"
        )
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        input_label = QLabel("添加标签")
        input_label.setProperty("section", True)
        layout.addWidget(input_label)

        input_row = QHBoxLayout()
        self.tag_combo = QComboBox()
        self.tag_combo.setEditable(True)
        self.tag_combo.setInsertPolicy(QComboBox.NoInsert)
        self.tag_combo.setMinimumWidth(420)
        self.tag_combo.setMinimumHeight(34)
        self.tag_combo.setToolTip("输入新标签，或点击右侧“已有标签”展开选择")
        self.tag_combo.setStyleSheet(
            "QComboBox QAbstractItemView {"
            " selection-background-color: #332B1D;"
            " selection-color: #F0C77C;"
            "}"
            "QComboBox QAbstractItemView::item:hover {"
            " background: #332B1D; color: #F0C77C;"
            "}"
        )
        self.tag_edit = self.tag_combo.lineEdit()
        self.tag_edit.setPlaceholderText("输入新标签后按回车，或从下拉框选择已有标签")
        self.tag_edit.returnPressed.connect(self._add_from_input)
        self.tag_combo.activated.connect(self._add_combo_item)
        self._tag_completion = QCompleter(self.tag_combo)
        self._tag_completion.setCaseSensitivity(Qt.CaseInsensitive)
        self._tag_completion.setFilterMode(Qt.MatchContains)
        self._tag_completion.setCompletionMode(QCompleter.PopupCompletion)
        self.tag_edit.setCompleter(self._tag_completion)
        add_button = QPushButton("添加")
        add_button.clicked.connect(self._add_from_input)
        self.existing_button = QPushButton("已有标签")
        self.existing_button.setIcon(svg_icon("chevron-down.svg"))
        self.existing_button.setIconSize(QSize(14, 14))
        self.existing_button.setToolTip("展开已有标签列表")
        self.existing_button.clicked.connect(self.tag_combo.showPopup)
        remove_button = QPushButton("移除选中")
        remove_button.clicked.connect(self._remove_selected)
        input_row.addWidget(self.tag_combo, 1)
        input_row.addWidget(self.existing_button)
        input_row.addWidget(add_button)
        input_row.addWidget(remove_button)
        layout.addLayout(input_row)

        current_label = QLabel("已选标签")
        current_label.setProperty("section", True)
        layout.addWidget(current_label)
        self.tags_list = QListWidget()
        self.tags_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tags_list.setStyleSheet(
            "QListWidget::item:selected {"
            " background: #332B1D; color: #F0C77C;"
            "}"
        )
        self.tags_list.itemDoubleClicked.connect(
            lambda item: self._remove_items([item])
        )
        layout.addWidget(self.tags_list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setAutoDefault(False)
        buttons.button(QDialogButtonBox.Cancel).setAutoDefault(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_choices()
        self.remove_shortcut = QShortcut(QKeySequence.Delete, self.tags_list)
        self.remove_shortcut.activated.connect(self._remove_selected)
        self.tag_edit.setFocus()

    @staticmethod
    def _normalized(tags: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            name = str(tag).strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def _refresh_choices(self) -> None:
        self.tags_list.clear()
        for tag in self._tags:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, tag)
            item.setSizeHint(QSize(0, 38))
            self.tags_list.addItem(item)
            self.tags_list.setItemWidget(item, self._tag_row(tag))

        selected = {tag.casefold() for tag in self._tags}
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        for tag in self._available:
            is_selected = tag.casefold() in selected
            self.tag_combo.addItem(
                f"{tag}  ·  已设置" if is_selected else tag,
                tag,
            )
            if is_selected:
                item = self.tag_combo.model().item(self.tag_combo.count() - 1)
                if item is not None:
                    item.setEnabled(False)
        self.tag_combo.setCurrentIndex(-1)
        self.tag_combo.blockSignals(False)
        available = [
            tag for tag in self._available if tag.casefold() not in selected
        ]
        self._tag_completion.setModel(
            QStringListModel(available, self._tag_completion)
        )
        self.tag_edit.clear()

    def _tag_row(self, tag: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(8)
        label = QLabel(tag)
        label.setToolTip(tag)
        remove = QToolButton()
        remove.setObjectName("TagRemoveButton")
        remove.setIcon(svg_icon("x.svg"))
        remove.setIconSize(QSize(14, 14))
        remove.setToolTip(f"移除标签“{tag}”")
        remove.setAutoRaise(True)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setFixedSize(24, 24)
        remove.setStyleSheet(
            "QToolButton {"
            " padding: 0; border: none; border-radius: 0;"
            " background: transparent;"
            "}"
            "QToolButton:hover {"
            " background: transparent; border: none;"
            "}"
        )
        remove.clicked.connect(
            lambda _checked=False, value=tag: self._remove_tag(value)
        )
        layout.addWidget(label, 1)
        layout.addWidget(remove)
        return row

    def _add_from_input(self) -> None:
        names = re.split(r"[,，;；\n]+", self.tag_edit.text())
        self._tags = self._normalized([*self._tags, *names])
        self._refresh_choices()
        self.tag_edit.setFocus()

    def _add_combo_item(self, index: int) -> None:
        tag = self.tag_combo.itemData(index)
        if not tag:
            return
        self._tags = self._normalized([*self._tags, str(tag)])
        self._refresh_choices()
        self.tag_edit.setFocus()

    def _remove_selected(self) -> None:
        self._remove_items(self.tags_list.selectedItems())

    def _remove_items(self, items: list[QListWidgetItem]) -> None:
        removed = [
            str(item.data(Qt.UserRole))
            for item in items
            if item.data(Qt.UserRole)
        ]
        self._remove_tags(removed)

    def _remove_tag(self, tag: str) -> None:
        self._remove_tags([tag])

    def _remove_tags(self, tags: list[str]) -> None:
        removed = {tag.casefold() for tag in tags}
        if not removed:
            return
        self._tags = [
            tag for tag in self._tags if tag.casefold() not in removed
        ]
        self._refresh_choices()
        self.tag_edit.setFocus()

    def _accept(self) -> None:
        if self.tag_edit.text().strip():
            self._add_from_input()
        self.accept()

    def values(self) -> list[str]:
        return list(self._tags)


class CategoryDialog(QDialog):
    def __init__(
        self,
        parents: list[tuple[int, str]],
        selected_parent_id: int | None = None,
        title_text: str = "新建分类",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        title = QLabel(title_text)
        title.setProperty("heading", True)
        layout.addWidget(title)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("分类名称")
        layout.addWidget(self.name_edit)
        self.parent_combo = QComboBox()
        self.parent_combo.addItem("顶层分类", None)
        for category_id, label in parents:
            self.parent_combo.addItem(label, category_id)
        selected_index = self.parent_combo.findData(selected_parent_id)
        if selected_index >= 0:
            self.parent_combo.setCurrentIndex(selected_index)
        layout.addWidget(self.parent_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, int | None]:
        return self.name_edit.text().strip(), self.parent_combo.currentData()


class SettingsDialog(QDialog):
    def __init__(
        self,
        library: dict | None,
        mask_enabled: bool,
        subtitle_font_size: int = 22,
        show_detail_panel: bool = True,
        playback_end_behavior: str = "stop",
        auto_lock_minutes: int = 15,
        mask_map: dict[str, str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(540)
        self.resize(620, 720)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 0, 24, 16)
        outer.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 22, 0, 22)
        layout.setSpacing(16)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        heading = QLabel("设置")
        heading.setProperty("heading", True)
        layout.addWidget(heading)

        database_label = QLabel("数据库")
        database_label.setProperty("section", True)
        layout.addWidget(database_label)
        self.database_path = QLabel(str(DB_PATH))
        self.database_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.database_path.setWordWrap(True)
        self.database_path.setProperty("muted", True)
        layout.addWidget(self.database_path)
        open_data = QPushButton("打开数据目录")
        open_data.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))
        )
        layout.addWidget(open_data, 0, Qt.AlignLeft)

        security_label = QLabel("隔离资源库安全")
        security_label.setProperty("section", True)
        layout.addWidget(security_label)
        security_form = QFormLayout()
        self.auto_lock_spin = QSpinBox()
        self.auto_lock_spin.setRange(0, 240)
        self.auto_lock_spin.setSuffix(" 分钟")
        self.auto_lock_spin.setSpecialValueText("关闭闲置锁定")
        self.auto_lock_spin.setValue(max(0, min(240, int(auto_lock_minutes))))
        security_form.addRow("闲置自动锁定", self.auto_lock_spin)
        layout.addLayout(security_form)
        self.lock_rules_label = QLabel(
            "锁定规则：\n"
            "1. 关闭软件：不保存解锁状态，下次启动重新验证两层密码。\n"
            "2. Windows 锁屏或注销：立即锁定并清除两层状态。\n"
            "3. 仅软件在前台且无操作时计算闲置；切到其他软件或最小化会暂停，设为 0 可关闭。\n"
            "4. 切换资源库：保留本次第一层解锁期间已通过的第二层状态。\n"
            "5. 主动隐藏隔离资源库：清除名称显示和第二层进入状态。\n"
            "6. 任务队列运行时不自动锁定，队列全部结束后再恢复锁定判断。"
        )
        self.lock_rules_label.setWordWrap(True)
        self.lock_rules_label.setProperty("muted", True)
        layout.addWidget(self.lock_rules_label)

        appearance_label = QLabel("界面与播放")
        appearance_label.setProperty("section", True)
        layout.addWidget(appearance_label)
        appearance_form = QFormLayout()
        self.subtitle_font_spin = QSpinBox()
        self.subtitle_font_spin.setRange(14, 48)
        self.subtitle_font_spin.setSuffix(" px")
        self.subtitle_font_spin.setValue(max(14, min(48, int(subtitle_font_size))))
        appearance_form.addRow("字幕字号", self.subtitle_font_spin)
        self.detail_panel_checkbox = QCheckBox("显示右侧侧边栏")
        self.detail_panel_checkbox.setChecked(bool(show_detail_panel))
        appearance_form.addRow("", self.detail_panel_checkbox)
        self.playback_end_combo = QComboBox()
        self.playback_end_combo.addItem("播放结束后停止", "stop")
        self.playback_end_combo.addItem("循环当前视频", "loop")
        self.playback_end_combo.addItem("自动播放下一个视频", "next")
        behavior_index = self.playback_end_combo.findData(
            str(playback_end_behavior or "stop")
        )
        self.playback_end_combo.setCurrentIndex(max(0, behavior_index))
        appearance_form.addRow("播放结束", self.playback_end_combo)
        layout.addLayout(appearance_form)

        mask_map_label = QLabel("伪装后缀对应关系（全局）")
        mask_map_label.setProperty("section", True)
        layout.addWidget(mask_map_label)
        mask_map_hint = QLabel(
            "每行左侧是源格式，右侧是启用伪装后使用的后缀。"
            "修改后会按现有串行任务队列更新已启用的伪装资源库。"
        )
        mask_map_hint.setWordWrap(True)
        mask_map_hint.setProperty("muted", True)
        layout.addWidget(mask_map_hint)
        active_mask_map = dict(mask_map or DEFAULT_MASK_MAP)
        mask_map_form = QFormLayout()
        mask_map_form.setHorizontalSpacing(14)
        mask_map_form.setVerticalSpacing(8)
        self.mask_extension_edits: dict[str, QLineEdit] = {}
        for source_ext in sorted(MASKABLE_EXTENSIONS):
            edit = QLineEdit(
                str(
                    active_mask_map.get(
                        source_ext,
                        DEFAULT_MASK_MAP[source_ext],
                    )
                )
            )
            edit.setMaxLength(17)
            edit.setFixedWidth(170)
            edit.setPlaceholderText(DEFAULT_MASK_MAP[source_ext])
            self.mask_extension_edits[source_ext] = edit
            mask_map_form.addRow(f"{source_ext} →", edit)
        layout.addLayout(mask_map_form)

        library_label = QLabel("当前资源库")
        library_label.setProperty("section", True)
        layout.addWidget(library_label)
        library_form = QFormLayout()
        library_form.setHorizontalSpacing(14)
        library_form.setVerticalSpacing(12)
        self.library_name_edit = QLineEdit(
            str(library.get("name") or "") if library else ""
        )
        self.library_name_edit.setPlaceholderText("未选择资源库")
        self.library_name_edit.setEnabled(library is not None)
        library_form.addRow("资源库名称", self.library_name_edit)
        self.library_name = QLabel(
            str(library.get("root_path") or "") if library else "未选择资源库"
        )
        self.library_name.setWordWrap(True)
        self.library_name.setProperty("muted", True)
        self.library_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        library_form.addRow("目录", self.library_name)
        layout.addLayout(library_form)

        self.project_kind_combo = QComboBox()
        self.project_kind_combo.addItem("自动识别", "auto")
        self.project_kind_combo.addItem("NFO 影视项目", "nfo")
        self.project_kind_combo.addItem("摄影拍摄项目", "shooting")
        self.project_kind_combo.addItem("普通视频文件夹", "plain")
        current_kind = str((library or {}).get("kind") or "auto")
        if current_kind == "standard":
            current_kind = "nfo"
        kind_index = self.project_kind_combo.findData(current_kind)
        self.project_kind_combo.setCurrentIndex(max(0, kind_index))
        self.project_kind_combo.setEnabled(library is not None)
        project_row = QFormLayout()
        project_row.addRow("项目类型", self.project_kind_combo)
        layout.addLayout(project_row)

        mask_label = QLabel("当前资源库行为")
        mask_label.setProperty("section", True)
        layout.addWidget(mask_label)
        self.mask_checkbox = QCheckBox("启用后缀伪装模式")
        self.mask_checkbox.setChecked(mask_enabled)
        self.mask_checkbox.setEnabled(library is not None)
        layout.addWidget(self.mask_checkbox)

        self.isolated_checkbox = QCheckBox("从“全部资源库”中隔离")
        self.isolated_checkbox.setChecked(
            bool(library and library.get("isolated"))
        )
        self.isolated_checkbox.setEnabled(library is not None)
        layout.addWidget(self.isolated_checkbox)
        explanation = QLabel(
            "只对当前资源库中已登记的视频和关联图片生效。\n"
            "这是后缀遮蔽，不加密文件内容，也不影响其他资源库或未登记文件。\n"
            "隔离后，该资源库仍可单独切换查看，但不会参与“全部资源库”汇总。"
        )
        explanation.setWordWrap(True)
        explanation.setProperty("muted", True)
        layout.addWidget(explanation)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _normalized_mask_extension_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        used: set[str] = set()
        for source_ext, edit in self.mask_extension_edits.items():
            target = edit.text().strip().lower()
            if target and not target.startswith("."):
                target = f".{target}"
            if (
                not re.fullmatch(r"\.[a-z0-9_-]{1,16}", target)
                or target in MASKABLE_EXTENSIONS
                or target in used
            ):
                raise ValueError(f"伪装后缀无效或重复：{target or '(空)'}")
            result[source_ext] = target
            used.add(target)
        return result

    def _accept(self) -> None:
        try:
            self._normalized_mask_extension_map()
        except ValueError as exc:
            QMessageBox.warning(self, "伪装后缀无效", str(exc))
            return
        self.accept()

    def mask_extension_map(self) -> dict[str, str]:
        return self._normalized_mask_extension_map()

    def mask_enabled(self) -> bool:
        return self.mask_checkbox.isChecked()

    def isolated(self) -> bool:
        return self.isolated_checkbox.isChecked()

    def project_kind(self) -> str:
        return str(self.project_kind_combo.currentData() or "auto")

    def subtitle_font_size(self) -> int:
        return int(self.subtitle_font_spin.value())

    def show_detail_panel(self) -> bool:
        return self.detail_panel_checkbox.isChecked()

    def playback_end_behavior(self) -> str:
        return str(self.playback_end_combo.currentData() or "stop")

    def auto_lock_minutes(self) -> int:
        return int(self.auto_lock_spin.value())

    def library_name_value(self) -> str:
        return self.library_name_edit.text().strip()


class NfoFilterDialog(QDialog):
    ROW_HEIGHT = 34
    FIELDS = (
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

    def __init__(
        self,
        options: dict[str, list[dict]],
        current: dict[str, str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("筛选")
        self.resize(820, 580)
        self.setMinimumSize(760, 520)
        self.setMaximumHeight(680)
        self._options = options
        self._selected = dict(current or {})
        self._labels = dict(self.FIELDS)
        self._active_key = next(
            (key for key, _ in self.FIELDS if key in self._selected),
            self.FIELDS[0][0],
        )
        self._rendering_options = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading_row = QHBoxLayout()
        heading = QLabel("按 movie.nfo 筛选")
        heading.setProperty("heading", True)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        self.selection_label = QLabel()
        self.selection_label.setProperty("muted", True)
        heading_row.addWidget(self.selection_label)
        layout.addLayout(heading_row)

        hint = QLabel("选择左侧字段，再在右侧选择条件。选项按视频数量倒序排列。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.field_list = QListWidget()
        self.field_list.setFixedWidth(190)
        self.field_list.setMinimumHeight(360)
        self.field_list.setUniformItemSizes(True)
        for key, label in self.FIELDS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setSizeHint(QSize(0, self.ROW_HEIGHT))
            self.field_list.addItem(item)
        body.addWidget(self.field_list)

        options_panel = QVBoxLayout()
        options_panel.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索当前筛选字段的内容")
        options_panel.addWidget(self.search_edit)
        self.options_label = QLabel()
        self.options_label.setProperty("faint", True)
        options_panel.addWidget(self.options_label)
        self.option_list = QListWidget()
        self.option_list.setMinimumHeight(330)
        self.option_list.setUniformItemSizes(True)
        self.option_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.option_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.option_list.setTextElideMode(Qt.ElideRight)
        options_panel.addWidget(self.option_list, 1)
        body.addLayout(options_panel, 1)
        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.Reset
        )
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Reset).setText("清除筛选")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(self._clear)
        layout.addWidget(buttons)

        self.field_list.currentItemChanged.connect(self._load_field)
        self.search_edit.textChanged.connect(self._render_options)
        self.option_list.currentItemChanged.connect(self._select_option)

        active_row = next(
            (
                index
                for index in range(self.field_list.count())
                if self.field_list.item(index).data(Qt.UserRole) == self._active_key
            ),
            0,
        )
        self._refresh_field_items()
        self._update_summary()
        self.field_list.setCurrentRow(active_row)

    def _load_field(self, current, previous=None) -> None:
        if current is None:
            return
        self._active_key = str(current.data(Qt.UserRole))
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._render_options()

    def _refresh_field_items(self) -> None:
        for index in range(self.field_list.count()):
            item = self.field_list.item(index)
            key = str(item.data(Qt.UserRole))
            label = self._labels[key]
            item.setText(f"{label}  ●" if key in self._selected else label)
            if key in self._selected:
                item.setToolTip(f"已选：{self._selected[key]}")
            else:
                item.setToolTip("")

    def _render_options(self) -> None:
        if not hasattr(self, "option_list"):
            return
        key = self._active_key
        query = self.search_edit.text().strip().casefold()
        selected = self._selected.get(key)
        values = sorted(
            self._options.get(key, []),
            key=lambda item: -int(item.get("count") or 0),
        )

        self._rendering_options = True
        self.option_list.clear()
        all_item = QListWidgetItem("全部")
        all_item.setData(Qt.UserRole, None)
        all_item.setSizeHint(QSize(0, self.ROW_HEIGHT))
        self.option_list.addItem(all_item)
        if selected is None:
            self.option_list.setCurrentItem(all_item)

        visible = 0
        for option in values:
            value = str(option.get("value") or "")
            if not value or (query and query not in value.casefold()):
                continue
            count = int(option.get("count") or 0)
            item = QListWidgetItem(f"{value}    · {count}")
            item.setData(Qt.UserRole, value)
            item.setToolTip(f"{value}\n{count} 个视频")
            item.setSizeHint(QSize(0, self.ROW_HEIGHT))
            self.option_list.addItem(item)
            visible += 1
            if value == selected:
                self.option_list.setCurrentItem(item)
        self._rendering_options = False
        self.options_label.setText(f"{visible} 项 · 按视频数量倒序")

    def _select_option(self, current, previous=None) -> None:
        if self._rendering_options or current is None:
            return
        value = current.data(Qt.UserRole)
        if value:
            self._selected[self._active_key] = str(value)
        else:
            self._selected.pop(self._active_key, None)
        self._refresh_field_items()
        self._update_summary()

    def _update_summary(self) -> None:
        self.selection_label.setText(f"已选 {len(self._selected)} 项")

    def _clear(self) -> None:
        self._selected.clear()
        self._refresh_field_items()
        self._update_summary()
        self._render_options()

    def values(self) -> dict[str, str]:
        return dict(self._selected)


class MetadataFilterDialog(QDialog):
    FIELDS = (
        ("capture_year", "拍摄年份"),
        ("capture_month", "拍摄月份"),
        ("resolution", "分辨率"),
        ("duration", "视频时长"),
        ("size", "文件大小"),
        ("camera", "相机 / 设备"),
        ("lens", "镜头"),
        ("location", "位置"),
        ("rating", "评级"),
        ("tag", "标签"),
    )

    def __init__(
        self,
        options: dict[str, list[dict]],
        current: dict[str, str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("按元数据筛选")
        self.setMinimumWidth(520)
        self._combos: dict[str, QComboBox] = {}
        selected = dict(current or {})
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("按视频元数据筛选")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        hint = QLabel(
            "这里只筛选拍摄时间、分辨率、大小、设备、镜头、评级和标签，不读取 NFO 分类字段。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setVerticalSpacing(10)
        for key, label in self.FIELDS:
            combo = QComboBox()
            configure_filter_combo(combo)
            combo.addItem("全部", None)
            for option in options.get(key, []):
                value = str(option.get("value") or "")
                if not value:
                    continue
                combo.addItem(
                    f"{value}  ·  {int(option.get('count') or 0)}",
                    value,
                )
            index = combo.findData(selected.get(key))
            combo.setCurrentIndex(max(0, index))
            self._combos[key] = combo
            form.addRow(label, combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.Reset
        )
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Reset).setText("清除")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(self._clear)
        layout.addWidget(buttons)

    def _clear(self) -> None:
        for combo in self._combos.values():
            combo.setCurrentIndex(0)

    def values(self) -> dict[str, str]:
        return {
            key: str(combo.currentData())
            for key, combo in self._combos.items()
            if combo.currentData()
        }


class TrimDialog(QDialog):
    def __init__(self, video: dict, library_root: Path, parent=None):
        super().__init__(parent)
        self.video = video
        self.library_root = library_root.resolve()
        self.setWindowTitle("FFmpeg 裁剪")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("FFmpeg 裁剪")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        source = QLabel(str(video.get("path") or ""))
        source.setWordWrap(True)
        source.setProperty("faint", True)
        layout.addWidget(source)

        duration_ms = int(video.get("duration_ms") or 0)
        source_path = Path(str(video.get("path") or ""))
        self._syncing = False
        self.preview_player = QMediaPlayer(self)
        self.preview_audio = QAudioOutput(self)
        self.preview_audio.setMuted(True)
        self.preview_player.setAudioOutput(self.preview_audio)
        preview_frame = QFrame()
        preview_frame.setMinimumHeight(240)
        preview_frame.setStyleSheet(
            "background: #05070A; border-radius: 7px; border: none;"
        )
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.surface = VideoSurface()
        self.surface.set_video_rotation(int(video.get("rotation") or 0))
        self.preview_player.setVideoOutput(self.surface.video_item.videoSink())
        preview_layout.addWidget(self.surface)
        layout.addWidget(preview_frame)
        if source_path.exists():
            self.preview_player.setSource(QUrl.fromLocalFile(str(source_path)))
            self.preview_player.pause()

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.play_button = QPushButton("播放")
        self.play_button.setFixedWidth(64)
        self.play_button.clicked.connect(self._toggle_preview)
        seek_row.addWidget(self.play_button)
        self.seek_slider = ClickableSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, max(1, duration_ms))
        self.seek_label = QLabel(format_timecode(0))
        self.seek_label.setProperty("muted", True)
        self.seek_label.setFixedWidth(76)
        seek_row.addWidget(self.seek_slider, 1)
        seek_row.addWidget(self.seek_label)
        layout.addLayout(seek_row)
        self.seek_slider.sliderMoved.connect(self._preview_at)
        self.seek_slider.valueChanged.connect(self._preview_at)
        self.preview_player.positionChanged.connect(self._on_preview_position)
        self.preview_player.playbackStateChanged.connect(self._on_preview_state)

        mark_row = QHBoxLayout()
        mark_row.setSpacing(8)
        self.start_mark = QPushButton("起点 = 当前位置")
        self.end_mark = QPushButton("终点 = 当前位置")
        self.start_mark.clicked.connect(self._mark_start)
        self.end_mark.clicked.connect(self._mark_end)
        mark_row.addWidget(self.start_mark)
        mark_row.addWidget(self.end_mark)
        mark_row.addStretch(1)
        layout.addLayout(mark_row)

        form = QFormLayout()
        form.setVerticalSpacing(11)
        self.start_edit = QLineEdit("00:00:00")
        self.end_edit = QLineEdit(
            format_timecode(duration_ms) if duration_ms else ""
        )
        self.start_edit.setPlaceholderText("00:00:00 或秒数")
        self.end_edit.setPlaceholderText("00:00:00 或秒数")
        form.addRow("开始时间", self.start_edit)
        form.addRow("结束时间", self.end_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("无损快速裁剪", "copy")
        self.mode_combo.addItem("精确裁剪（H.264/AAC 重编码）", "precise")
        form.addRow("裁剪模式", self.mode_combo)

        output_row = QHBoxLayout()
        source_path = Path(str(video.get("path") or ""))
        logical_ext = str(video.get("logical_ext") or source_path.suffix or ".mp4")
        default_output = source_path.with_name(f"{source_path.stem} - 裁剪{logical_ext}")
        self.output_edit = QLineEdit(str(default_output))
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)
        form.addRow("输出文件", output_row)
        layout.addLayout(form)

        hint = QLabel(
            "无损模式速度最快，但起点会落在最近的关键帧；精确模式会重新编码视频。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始裁剪")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _preview_at(self, position_ms: int) -> None:
        if self._syncing:
            return
        position_ms = max(0, int(position_ms))
        self.seek_label.setText(format_timecode(position_ms))
        if self.preview_player.playbackState() == QMediaPlayer.PlayingState:
            self.preview_player.pause()
        self.preview_player.setPosition(position_ms)

    def _toggle_preview(self) -> None:
        if self.preview_player.playbackState() == QMediaPlayer.PlayingState:
            self.preview_player.pause()
        else:
            self.preview_player.play()

    def _on_preview_position(self, position_ms: int) -> None:
        # 播放时让时间条跟着走；_syncing 防止 setValue 又反弹回 _preview_at 把播放暂停
        if self._syncing:
            return
        self._syncing = True
        try:
            self.seek_slider.setValue(max(0, int(position_ms)))
            self.seek_label.setText(format_timecode(position_ms))
        finally:
            self._syncing = False

    def _on_preview_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.play_button.setText("暂停" if playing else "播放")

    def _mark_start(self) -> None:
        self.start_edit.setText(format_timecode(self.seek_slider.value()))

    def _mark_end(self) -> None:
        self.end_edit.setText(format_timecode(self.seek_slider.value()))

    def _browse_output(self) -> None:
        current = Path(self.output_edit.text().strip())
        suffix = current.suffix or ".mp4"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择裁剪输出文件",
            str(current),
            f"视频文件 (*{suffix});;所有文件 (*.*)",
        )
        if path:
            self.output_edit.setText(path)

    def _accept(self) -> None:
        start_ms = parse_timecode(self.start_edit.text())
        end_ms = parse_timecode(self.end_edit.text())
        duration_ms = int(self.video.get("duration_ms") or 0)
        if start_ms is None or end_ms is None:
            QMessageBox.warning(self, "时间格式无效", "请使用 HH:MM:SS 或秒数。")
            return
        if end_ms <= start_ms:
            QMessageBox.warning(self, "时间范围无效", "结束时间必须晚于开始时间。")
            return
        if duration_ms and end_ms > duration_ms + 1000:
            QMessageBox.warning(self, "时间超出范围", "结束时间超过视频时长。")
            return
        output = Path(self.output_edit.text().strip())
        try:
            output.resolve().relative_to(self.library_root)
        except ValueError:
            QMessageBox.warning(
                self,
                "输出位置无效",
                "裁剪结果只能保存到当前资源库目录内。",
            )
            return
        if output.exists():
            QMessageBox.warning(self, "文件已存在", "输出文件已存在，请更换名称。")
            return
        if output == Path(str(self.video.get("path") or "")):
            QMessageBox.warning(self, "输出位置无效", "不能覆盖原视频。")
            return
        self.accept()

    def values(self) -> tuple[int, int, Path, str]:
        self.preview_player.stop()
        self.preview_player.setSource(QUrl())
        return (
            int(parse_timecode(self.start_edit.text()) or 0),
            int(parse_timecode(self.end_edit.text()) or 0),
            Path(self.output_edit.text().strip()).resolve(),
            str(self.mode_combo.currentData()),
        )


class PasswordPromptDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        description = QLabel(message)
        description.setProperty("muted", True)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("输入密码")
        layout.addWidget(self.password_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确认")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if not self.password_edit.text():
            QMessageBox.warning(self, "密码为空", "请输入密码。")
            return
        self.accept()

    def password(self) -> str:
        return self.password_edit.text()


class SecurityDialog(QDialog):
    def __init__(self, db, show_library_names: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("密码与隔离")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("密码与隔离")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        note = QLabel(
            "密码只保存在本机数据库中。第一层密码用于显示隔离资源库名称，"
            "第二层密码用于进入具体隔离资源库。"
        )
        note.setProperty("muted", True)
        note.setWordWrap(True)
        layout.addWidget(note)

        first_label = QLabel("第一层 · 显示隔离资源库")
        first_label.setProperty("section", True)
        layout.addWidget(first_label)
        has_global = db.has_isolated_reveal_password()
        self.global_status = QLabel("已设置" if has_global else "未设置")
        self.global_status.setProperty("state" if has_global else "faint", True)
        layout.addWidget(self.global_status)
        global_form = QFormLayout()
        global_form.setVerticalSpacing(10)
        self.global_current = self._password_edit()
        self.global_current.setEnabled(has_global)
        self.global_new = self._password_edit()
        self.global_confirm = self._password_edit()
        global_form.addRow("当前密码", self.global_current)
        global_form.addRow("新密码", self.global_new)
        global_form.addRow("确认新密码", self.global_confirm)
        layout.addLayout(global_form)

        second_label = QLabel("第二层 · 进入隔离资源库")
        second_label.setProperty("section", True)
        layout.addWidget(second_label)
        self.library_combo = QComboBox()
        isolated = (
            [library for library in db.libraries() if library.get("isolated")]
            if show_library_names
            else []
        )
        for library in isolated:
            self.library_combo.addItem(library["name"], library["id"])
        self.library_combo.setEnabled(bool(isolated))
        self.library_combo.currentIndexChanged.connect(self._library_changed)
        layout.addWidget(self.library_combo)
        self.library_status = QLabel("")
        self.library_status.setProperty("muted", True)
        layout.addWidget(self.library_status)
        library_form = QFormLayout()
        library_form.setVerticalSpacing(10)
        self.library_current = self._password_edit()
        self.library_new = self._password_edit()
        self.library_confirm = self._password_edit()
        library_form.addRow("当前密码", self.library_current)
        library_form.addRow("新密码", self.library_new)
        library_form.addRow("确认新密码", self.library_confirm)
        layout.addLayout(library_form)
        if not isolated:
            self.library_status.setText(
                "隔离资源库名称已隐藏。先保存第一层密码，"
                "再通过侧栏“显示隔离”解锁后设置第二层密码。"
                if not show_library_names
                else "当前没有已隔离的资源库"
            )
            for edit in (self.library_current, self.library_new, self.library_confirm):
                edit.setEnabled(False)
        else:
            self._library_changed()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _password_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.Password)
        edit.setPlaceholderText("留空表示不修改")
        return edit

    def _library_changed(self) -> None:
        library_id = self.library_combo.currentData()
        if library_id is None:
            return
        has_password = self.db.has_library_access_password(int(library_id))
        self.library_status.setText(
            "进入密码已设置" if has_password else "进入密码未设置"
        )
        self.library_current.setEnabled(has_password)
        self.library_current.clear()
        self.library_new.clear()
        self.library_confirm.clear()

    def _validate_change(
        self,
        label: str,
        has_password: bool,
        current_edit: QLineEdit,
        new_edit: QLineEdit,
        confirm_edit: QLineEdit,
        verifier,
    ) -> str | None:
        new_password = new_edit.text()
        if not new_password:
            return None
        if new_password != confirm_edit.text():
            return f"{label}：两次新密码不一致。"
        if has_password and not verifier(current_edit.text()):
            return f"{label}：当前密码不正确。"
        return new_password

    def _save(self) -> None:
        has_global = self.db.has_isolated_reveal_password()
        global_password = self._validate_change(
            "第一层密码",
            has_global,
            self.global_current,
            self.global_new,
            self.global_confirm,
            self.db.verify_isolated_reveal_password,
        )
        if isinstance(global_password, str) and global_password.startswith("第一层密码："):
            QMessageBox.warning(self, "无法保存", global_password)
            return

        library_id = self.library_combo.currentData()
        library_password = None
        if library_id is not None:
            has_library = self.db.has_library_access_password(int(library_id))
            result = self._validate_change(
                "第二层密码",
                has_library,
                self.library_current,
                self.library_new,
                self.library_confirm,
                lambda password: self.db.verify_library_access_password(
                    int(library_id),
                    password,
                ),
            )
            if isinstance(result, str) and result.startswith("第二层密码："):
                QMessageBox.warning(self, "无法保存", result)
                return
            library_password = result

        if isinstance(global_password, str):
            self.db.set_isolated_reveal_password(global_password)
        if library_id is not None and isinstance(library_password, str):
            self.db.set_library_access_password(int(library_id), library_password)
        self.accept()


class DeleteDialog(QDialog):
    def __init__(
        self,
        title: str,
        sidecar_count: int,
        parent=None,
        missing: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("清理缺失记录" if missing else "移至回收站")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        heading = QLabel("清理缺失记录" if missing else "移至回收站")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        message = QLabel(
            f"“{title}”的视频文件已经不存在。"
            "确定从影库中移除这条记录？"
            if missing
            else f"确定把“{title}”移入 Windows 回收站？"
        )
        message.setWordWrap(True)
        layout.addWidget(message)
        self.sidecar_checkbox = QCheckBox(
            (
                f"同时把仍存在的 {sidecar_count} 个关联文件移入回收站"
                if missing
                else f"同时移入 {sidecar_count} 个关联文件"
            )
        )
        self.sidecar_checkbox.setChecked(sidecar_count > 0)
        self.sidecar_checkbox.setEnabled(sidecar_count > 0)
        layout.addWidget(self.sidecar_checkbox)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(
            "移除记录" if missing else "移至回收站"
        )
        buttons.button(QDialogButtonBox.Ok).setProperty("danger", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def include_sidecars(self) -> bool:
        return self.sidecar_checkbox.isChecked()


class SubtitleOptionsDialog(QDialog):
    def __init__(self, video_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成字幕")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 20)
        layout.setSpacing(14)

        title = QLabel("本地 AI 字幕")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel(
            f"将为选中的 {video_count} 个视频生成同目录 SRT。"
            "任务运行时才加载模型，全程离线。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        self.language_combo = QComboBox()
        for code, name in SOURCE_LANGUAGES:
            self.language_combo.addItem(name, code)
        form.addRow("语音语言", self.language_combo)

        self.model_combo = QComboBox()
        self.model_combo.addItem("large-v3 · 质量优先", "large-v3")
        self.model_combo.addItem("medium · 速度优先", "medium")
        form.addRow("本地模型", self.model_combo)

        self.translate_checkbox = QCheckBox("翻译为简体中文")
        self.translate_checkbox.setChecked(True)
        self.translate_checkbox.setToolTip("保留原始字幕，同时生成 .zh-CN.srt 中文版")
        form.addRow("字幕翻译", self.translate_checkbox)
        target = QLabel("简体中文（固定）")
        target.setProperty("muted", True)
        form.addRow("翻译目标", target)

        layout.addLayout(form)

        self.overwrite_checkbox = QCheckBox("覆盖已存在的同名 SRT")
        self.overwrite_checkbox.setToolTip("默认跳过已有字幕，不覆盖原文件")
        layout.addWidget(self.overwrite_checkbox)

        note = QLabel("进度可在顶部“任务中心”查看，运行中可以取消。")
        note.setProperty("faint", True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始生成")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, bool, bool]:
        return (
            str(self.language_combo.currentData() or ""),
            str(self.model_combo.currentData() or "large-v3"),
            self.overwrite_checkbox.isChecked(),
            self.translate_checkbox.isChecked(),
        )
