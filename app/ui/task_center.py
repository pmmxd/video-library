from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..constants import DATA_DIR
from ..theme import COLORS


class TaskCenterDialog(QDialog):
    cancel_requested = Signal(str)

    # 任务记录存到 data 下的小文件，重启后还能看到上一次跑到哪
    _HISTORY_FILE = DATA_DIR / "task_history.json"
    _MAX_HISTORY = 50

    _STATUS = {
        "queued": ("排队中", COLORS["muted"]),
        "running": ("运行中", COLORS["accent"]),
        "completed": ("已完成", COLORS["teal"]),
        "failed": ("失败", COLORS["danger"]),
        "cancelled": ("已取消", COLORS["muted"]),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("任务中心")
        self.setWindowModality(Qt.NonModal)
        self.resize(900, 460)
        self._tasks: dict[str, dict] = self._load_history()
        self._rows: dict[str, int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("任务中心")
        title.setProperty("heading", True)
        layout.addWidget(title)
        hint = QLabel("扫描、字幕和文件处理任务串行执行；关闭此窗口不会停止任务。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["状态", "任务", "类型", "进度", "详情"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 82)
        self.table.setColumnWidth(2, 92)
        self.table.setColumnWidth(3, 168)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_selected)
        buttons.addWidget(self.cancel_button)
        self.open_subtitle_button = QPushButton("打开字幕")
        self.open_subtitle_button.setEnabled(False)
        self.open_subtitle_button.clicked.connect(self._open_selected_subtitle)
        buttons.addWidget(self.open_subtitle_button)
        self.open_folder_button = QPushButton("打开所在文件夹")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_selected_folder)
        buttons.addWidget(self.open_folder_button)
        self.clear_button = QPushButton("清除已完成")
        self.clear_button.clicked.connect(self.clear_finished)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.button(QDialogButtonBox.Close).setText("关闭")
        close_box.rejected.connect(self.close)
        buttons.addWidget(close_box)
        layout.addLayout(buttons)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.table.rowCount() == 0 and self._tasks:
            self._rebuild()
        self.raise_()
        self.activateWindow()

    def _load_history(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, list):
            return {}
        tasks: dict[str, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "")
            if not task_id:
                continue
            state = str(item.get("state") or "failed")
            # 上次进程已经没了，标成"运行中"会让人以为还在跑
            if state in {"running", "queued"}:
                state = "failed" if state == "running" else "cancelled"
            tasks[task_id] = {
                "kind": str(item.get("kind") or ""),
                "title": str(item.get("title") or ""),
                "state": state if state in self._STATUS else "failed",
                "progress": max(0, min(100, int(item.get("progress") or 0))),
                "detail": str(item.get("detail") or ""),
                "outputs": [str(value) for value in item.get("outputs") or []],
            }
        return tasks

    def _save_history(self) -> None:
        items = [
            {"id": task_id, **task}
            for task_id, task in list(self._tasks.items())[-self._MAX_HISTORY :]
        ]
        try:
            self._HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = self._HISTORY_FILE.with_suffix(".tmp")
            temp.write_text(
                json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(temp, self._HISTORY_FILE)
        except OSError:
            pass

    def start_task(
        self,
        task_id: str,
        kind: str,
        title: str,
        detail: str = "",
        outputs: list[str] | None = None,
        state: str = "running",
    ) -> None:
        self._tasks[task_id] = {
            "kind": kind,
            "title": title,
            "state": state if state in self._STATUS else "failed",
            "progress": 0,
            "detail": detail,
            "outputs": list(outputs or []),
        }
        self._save_history()
        self._render(task_id)

    def start_tasks(self, tasks: list[dict], state: str = "queued") -> None:
        for task in tasks:
            task_id = str(task["id"])
            self._tasks[task_id] = {
                "kind": str(task.get("kind") or ""),
                "title": str(task.get("title") or ""),
                "state": state if state in self._STATUS else "failed",
                "progress": 0,
                "detail": str(task.get("detail") or ""),
                "outputs": [str(value) for value in task.get("outputs") or []],
            }
        self._save_history()
        self._rebuild()

    def run_task(self, task_id: str, detail: str | None = None) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task["state"] = "running"
        if detail is not None:
            task["detail"] = detail
        self._save_history()
        self._render(task_id)

    def update_task(
        self,
        task_id: str,
        progress: int | None = None,
        detail: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        if progress is not None:
            task["progress"] = max(0, min(100, int(progress)))
        if detail is not None:
            task["detail"] = detail
        self._render(task_id, lightweight=True)

    def finish_task(self, task_id: str, state: str, detail: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task["state"] = state if state in self._STATUS else "failed"
        task["progress"] = 100 if state == "completed" else task["progress"]
        task["detail"] = detail
        self._save_history()
        self._render(task_id)
        if task_id == self._selected_task_id():
            self._selection_changed()

    def clear_finished(self) -> None:
        self._tasks = {
            task_id: task
            for task_id, task in self._tasks.items()
            if task["state"] in {"queued", "running"}
        }
        self._save_history()
        self._rebuild()

    def _render(self, task_id: str, lightweight: bool = False) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        row = self._rows.get(task_id)
        if row is None:
            row = 0
            self.table.insertRow(row)
            self._rows = {
                current_id: index + 1
                for current_id, index in self._rows.items()
            }
            self._rows[task_id] = row
        status, color = self._STATUS[task["state"]]
        status_item = self.table.item(row, 0)
        if status_item is None:
            status_item = QTableWidgetItem()
            self.table.setItem(row, 0, status_item)
        status_item.setText(status)
        status_item.setForeground(QColor(color))
        status_item.setData(Qt.UserRole, task_id)
        for column, value in ((1, task["title"]), (2, task["kind"]), (4, task["detail"])):
            item = self.table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, column, item)
            # 复用单元格对象，避免每次进度更新触发表格重新布局（翻译时卡死的根因）
            if item.text() != str(value):
                item.setText(str(value))
        # 进度条放进一个带垂直居中的容器：直接当 cellWidget 会被拉伸到整行，
        # 看着就贴在顶上，很不协调。
        holder = self.table.cellWidget(row, 3)
        progress = holder.findChild(QProgressBar) if holder else None
        if progress is None:
            holder = QWidget()
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setTextVisible(True)
            progress.setFixedHeight(18)
            layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignVCenter)
            self.table.setCellWidget(row, 3, holder)
        progress.setValue(int(task["progress"]))
        if not lightweight:
            self.table.setRowHeight(row, 42)

    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        for task_id in self._tasks:
            self._render(task_id)
        self._selection_changed()

    def _selected_task_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole)) if item and item.data(Qt.UserRole) else None

    def _selected_task(self) -> dict | None:
        return self._tasks.get(self._selected_task_id() or "")

    def _selected_output(self) -> Path | None:
        task = self._selected_task()
        if not task or task["state"] not in {"completed", "cancelled"}:
            return None
        for value in task.get("outputs", []):
            path = Path(str(value))
            if path.is_file():
                return path
        return None

    def _selection_changed(self) -> None:
        task = self._selected_task()
        self.cancel_button.setEnabled(
            bool(task and task["state"] in {"queued", "running"})
        )
        has_output = self._selected_output() is not None
        self.open_subtitle_button.setEnabled(has_output)
        self.open_folder_button.setEnabled(has_output)

    def _open_selected_subtitle(self) -> None:
        output = self._selected_output()
        if output and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(output))):
            QMessageBox.warning(self, "无法打开字幕", "系统没有可用的默认文本软件。")

    def _open_selected_folder(self) -> None:
        output = self._selected_output()
        if not output:
            return
        if os.name == "nt":
            try:
                subprocess.Popen(
                    ["explorer.exe", "/select,", str(output)],
                    creationflags=0x08000000,
                )
                return
            except OSError as exc:
                QMessageBox.critical(self, "打开文件夹失败", str(exc))
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))

    def _cancel_selected(self) -> None:
        task_id = self._selected_task_id()
        task = self._tasks.get(task_id or "")
        if task and task["state"] in {"queued", "running"}:
            self.update_task(task_id, detail="正在取消...")
            self.cancel_requested.emit(task_id)
