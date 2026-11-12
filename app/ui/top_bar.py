from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
)

from ..constants import ALL_LIBRARIES
from .icons import svg_icon


class TopBar(QFrame):
    add_library_requested = Signal()
    scan_requested = Signal()
    relocate_library_requested = Signal()
    remove_library_requested = Signal()
    settings_requested = Signal()
    security_requested = Signal()
    isolated_visibility_requested = Signal()
    logs_requested = Signal()
    tasks_requested = Signal()
    open_data_requested = Signal()
    detail_toggle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(46)
        self._current_library: int | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(4)

        self.library_button = self._menu_button("资源库")
        self.library_button.setIcon(svg_icon("library.svg"))
        self.library_button.setIconSize(QSize(16, 16))
        self.library_menu = QMenu(self.library_button)
        self.library_button.setMenu(self.library_menu)
        layout.addWidget(self.library_button)

        self.tools_button = self._menu_button("工具")
        self.tools_button.setIcon(svg_icon("tools.svg"))
        self.tools_button.setIconSize(QSize(16, 16))
        self.tools_menu = QMenu(self.tools_button)
        self.tools_button.setMenu(self.tools_menu)
        layout.addWidget(self.tools_button)

        self.tasks_button = QToolButton()
        self.tasks_button.setText("任务中心")
        self.tasks_button.setIcon(svg_icon("log.svg"))
        self.tasks_button.setIconSize(QSize(16, 16))
        self.tasks_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.tasks_button.setCursor(Qt.PointingHandCursor)
        self.tasks_button.clicked.connect(lambda _checked=False: self.tasks_requested.emit())
        layout.addWidget(self.tasks_button)
        layout.addStretch(1)

        self.scope_label = QLabel("全部资源库")
        self.scope_label.setProperty("muted", True)
        layout.addWidget(self.scope_label)
        self.local_label = QLabel("本地索引")
        self.local_label.setProperty("state", True)
        layout.addWidget(self.local_label)
        self.detail_toggle = QToolButton()
        self.detail_toggle.setFixedSize(30, 30)
        self.detail_toggle.setCursor(Qt.PointingHandCursor)
        self.detail_toggle.setIconSize(QSize(16, 16))
        self.detail_toggle.clicked.connect(self.detail_toggle_requested)
        layout.addWidget(self.detail_toggle)

        self._build_tools_menu()
        self.set_detail_visible(True)

    def _menu_button(self, text: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setPopupMode(QToolButton.InstantPopup)
        button.setCursor(Qt.PointingHandCursor)
        return button

    def _build_tools_menu(self) -> None:
        self.tools_menu.clear()
        tasks = self.tools_menu.addAction("任务中心")
        tasks.setIcon(svg_icon("log.svg"))
        self.tools_menu.addSeparator()
        settings = self.tools_menu.addAction("设置")
        settings.setIcon(svg_icon("settings.svg"))
        security = self.tools_menu.addAction("密码与隔离")
        security.setIcon(svg_icon("shield.svg"))
        self.isolated_action = self.tools_menu.addAction("显示隔离资源库")
        self.isolated_action.setIcon(svg_icon("eye.svg"))
        logs = self.tools_menu.addAction("操作日志")
        logs.setIcon(svg_icon("log.svg"))
        open_data = self.tools_menu.addAction("打开数据目录")
        open_data.setIcon(svg_icon("folder-open.svg"))
        tasks.triggered.connect(self.tasks_requested)
        settings.triggered.connect(self.settings_requested)
        security.triggered.connect(self.security_requested)
        self.isolated_action.triggered.connect(self.isolated_visibility_requested)
        logs.triggered.connect(self.logs_requested)
        open_data.triggered.connect(self.open_data_requested)

    def set_libraries(
        self,
        libraries: list[dict[str, Any]],
        current_library_id: int | None,
    ) -> None:
        self._current_library = current_library_id
        self.library_menu.clear()
        add = self.library_menu.addAction("添加资源库")
        add.setIcon(svg_icon("plus.svg"))
        scan = self.library_menu.addAction("扫描当前资源库")
        scan.setIcon(svg_icon("refresh.svg"))
        relocate = self.library_menu.addAction("迁移资源库目录...")
        relocate.setIcon(svg_icon("folder-open.svg"))
        self.library_menu.addSeparator()
        remove = self.library_menu.addAction("删除当前资源库...")
        remove.setIcon(svg_icon("trash.svg"))
        single_library = (
            current_library_id is not None
            and current_library_id != ALL_LIBRARIES
        )
        scan.setEnabled(single_library)
        relocate.setEnabled(single_library)
        remove.setEnabled(single_library)
        add.triggered.connect(self.add_library_requested)
        scan.triggered.connect(self.scan_requested)
        relocate.triggered.connect(self.relocate_library_requested)
        remove.triggered.connect(self.remove_library_requested)

        if current_library_id == ALL_LIBRARIES:
            self.scope_label.setText("全部资源库")
        else:
            library = next(
                (item for item in libraries if item["id"] == current_library_id),
                None,
            )
            self.scope_label.setText(library["name"] if library else "未选择资源库")

    def set_isolated_state(self, show: bool, has_isolated: bool) -> None:
        self.isolated_action.setEnabled(has_isolated)
        self.isolated_action.setText(
            "隐藏隔离资源库" if show else "显示隔离资源库"
        )

    def set_detail_visible(self, visible: bool) -> None:
        self.detail_toggle.setIcon(
            svg_icon("chevron-right.svg" if visible else "chevron-left.svg")
        )
        self.detail_toggle.setToolTip(
            "收起右侧侧边栏" if visible else "展开右侧侧边栏"
        )
