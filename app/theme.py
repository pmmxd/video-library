from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from .constants import bundled_root


COLORS = {
    "bg": "#0F1216",
    "surface": "#161A20",
    "surface_alt": "#1C2129",
    "surface_hover": "#242B35",
    "line": "#2A313C",
    "text": "#EDF1F5",
    "muted": "#8F9AA8",
    "faint": "#5F6A77",
    "accent": "#E4B363",
    "accent_hover": "#F0C77C",
    "teal": "#4FB3A8",
    "danger": "#E06C75",
    "player": "#07090C",
}

ICON_DIR = (bundled_root() / "assets" / "icons").resolve().as_posix()


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(COLORS["bg"]))
    palette.setColor(QPalette.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.AlternateBase, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ToolTipBase, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ToolTipText, QColor(COLORS["text"]))
    palette.setColor(QPalette.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.Button, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.Highlight, QColor(COLORS["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor("#19140C"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(COLORS["faint"]))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(COLORS["faint"]))
    app.setPalette(palette)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
* {{
    outline: none;
}}
QMainWindow, QWidget#Root {{
    background: {COLORS["bg"]};
    color: {COLORS["text"]};
}}
QFrame#TopBar {{
    background: #12161B;
    border-bottom: 1px solid {COLORS["line"]};
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 11px;
    color: #D7DEE7;
    font-weight: 550;
}}
QToolButton:hover {{
    background: {COLORS["surface_hover"]};
    border-color: {COLORS["line"]};
}}
QToolButton:pressed, QToolButton:checked {{
    background: {COLORS["surface_alt"]};
    border-color: #3A4452;
}}
QToolButton::menu-indicator {{
    image: none;
    width: 0;
}}
QWidget {{
    color: {COLORS["text"]};
}}
QSplitter::handle {{
    background: {COLORS["bg"]};
    width: 1px;
}}
QToolTip {{
    background: {COLORS["surface_alt"]};
    color: {COLORS["text"]};
    border: 1px solid {COLORS["line"]};
    padding: 6px;
}}
QPushButton {{
    background: {COLORS["surface_alt"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 7px;
    padding: 7px 12px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {COLORS["surface_hover"]};
    border-color: #3A4452;
}}
QPushButton:pressed {{
    background: #151A20;
}}
QPushButton[tab="true"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    min-width: 54px;
    padding: 5px 12px;
    color: {COLORS["muted"]};
}}
QPushButton[tab="true"]:hover {{
    background: {COLORS["surface_alt"]};
    color: {COLORS["text"]};
}}
QPushButton[tab="true"]:checked {{
    background: {COLORS["surface_alt"]};
    border-color: {COLORS["line"]};
    color: {COLORS["text"]};
    font-weight: 600;
}}
QPushButton:disabled {{
    background: #161A20;
    border-color: #202630;
}}
QPushButton[accent="true"] {{
    background: {COLORS["accent"]};
    color: #20180B;
    border: 1px solid {COLORS["accent"]};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {COLORS["accent_hover"]};
}}
QPushButton[quiet="true"] {{
    background: transparent;
    border-color: transparent;
    padding: 6px 9px;
}}
QPushButton[quiet="true"]:hover {{
    background: {COLORS["surface_hover"]};
}}
QPushButton[danger="true"] {{
    color: {COLORS["danger"]};
}}
QLineEdit, QTextEdit, QTextBrowser, QComboBox, QSpinBox {{
    background: {COLORS["surface_alt"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: {COLORS["accent"]};
    selection-color: #20180B;
}}
QLineEdit:focus, QTextEdit:focus, QTextBrowser:focus, QComboBox:focus {{
    border-color: {COLORS["accent"]};
}}
QLineEdit[role="search"] {{
    padding-left: 32px;
    background: {COLORS["surface_alt"]};
}}
QComboBox {{
    padding-right: 24px;
}}
QComboBox::drop-down {{
    width: 22px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background: {COLORS["surface_alt"]};
    border: 1px solid {COLORS["line"]};
    selection-background-color: {COLORS["surface_hover"]};
    padding: 4px;
}}
QListWidget, QTreeWidget {{
    background: transparent;
    border: none;
}}
QListWidget::item, QTreeWidget::item {{
    border-radius: 6px;
    padding: 7px 9px;
    margin: 1px 0;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: {COLORS["surface_alt"]};
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {COLORS["surface_hover"]};
    color: {COLORS["text"]};
}}
QTreeWidget::branch {{
    background: transparent;
}}
QTreeWidget#OrganizeTree {{
    background: transparent;
    padding-top: 2px;
    show-decoration-selected: 1;
}}
QTreeWidget#OrganizeTree::branch {{
    background: transparent;
    border: none;
    image: none;
}}
QTreeWidget#OrganizeTree::item {{
    min-height: 30px;
    padding: 0 7px;
    margin: 1px 0;
    border-radius: 6px;
}}
QTreeWidget#OrganizeTree::item:hover {{
    background: {COLORS["surface_alt"]};
}}
QTreeWidget#OrganizeTree::item:selected {{
    background: {COLORS["surface_hover"]};
    color: {COLORS["text"]};
}}
QListWidget#TagList {{
    background: transparent;
}}
QListWidget#TagList::item {{
    padding: 0 9px;
    margin: 0;
    border-radius: 6px;
}}
QListWidget#TagList::item:hover {{
    background: {COLORS["surface_alt"]};
}}
QListWidget#TagList::item:selected {{
    background: {COLORS["surface_hover"]};
    color: {COLORS["text"]};
}}
QHeaderView::section {{
    background: {COLORS["surface"]};
    border: none;
    padding: 8px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #343D49;
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #45505F;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: #343D49;
    border-radius: 4px;
    min-width: 28px;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: #2C333E;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {COLORS["accent"]};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 12px;
    margin: -4px 0;
    border-radius: 6px;
    background: #F5E7C9;
}}
QProgressBar {{
    border: none;
    background: #252C35;
    border-radius: 2px;
    height: 4px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {COLORS["accent"]};
    border-radius: 2px;
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid #46505D;
    border-radius: 4px;
    background: {COLORS["surface_alt"]};
}}
QCheckBox::indicator:checked {{
    background: {COLORS["accent"]};
    border-color: {COLORS["accent"]};
}}
QMenu {{
    background: {COLORS["surface_alt"]};
    border: 1px solid {COLORS["line"]};
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 22px 7px 10px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {COLORS["surface_hover"]};
}}
QDialog {{
    background: {COLORS["bg"]};
}}
QFrame[card="true"] {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 8px;
}}
QFrame[statCard="true"] {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 7px;
}}
QLabel[muted="true"] {{
    color: {COLORS["muted"]};
}}
QLabel[faint="true"] {{
    color: {COLORS["faint"]};
}}
QLabel[heading="true"] {{
    font-size: 20px;
    font-weight: 650;
}}
QLabel[section="true"] {{
    color: {COLORS["muted"]};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel[badge="true"] {{
    background: {COLORS["surface_alt"]};
    border: 1px solid {COLORS["line"]};
    border-radius: 5px;
    padding: 3px 7px;
    color: #C9D1DB;
    font-size: 9px;
}}
QLabel[accentBadge="true"] {{
    background: #332B1D;
    border: 1px solid #5C492B;
    border-radius: 5px;
    padding: 3px 7px;
    color: {COLORS["accent"]};
    font-size: 9px;
}}
QLabel[state="true"] {{
    color: {COLORS["teal"]};
    font-size: 9px;
    font-weight: 600;
}}
QListView {{
    background: transparent;
    border: none;
    padding: 2px;
}}
QStatusBar {{
    background: {COLORS["surface"]};
    border-top: 1px solid {COLORS["line"]};
    color: {COLORS["muted"]};
}}
QStatusBar::item {{
    border: none;
}}
QMessageBox {{
    background: {COLORS["bg"]};
}}
"""
