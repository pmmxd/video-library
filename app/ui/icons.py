from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap

from ..constants import bundled_root


def icon_path(name: str) -> Path:
    return bundled_root() / "assets" / "icons" / name


def svg_icon(name: str) -> QIcon:
    return QIcon(str(icon_path(name)))


def svg_pixmap(name: str, width: int, height: int | None = None) -> QPixmap:
    return svg_icon(name).pixmap(width, height or width)
