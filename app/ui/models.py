from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from ..constants import runtime_artwork_path


class VideoRoles:
    VideoId = Qt.UserRole + 1
    Video = Qt.UserRole + 2
    ThumbPath = Qt.UserRole + 3
    Progress = Qt.UserRole + 4
    Masked = Qt.UserRole + 5


class VideoListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._videos: list[dict[str, Any]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._videos)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._videos):
            return None
        video = self._videos[index.row()]
        if role == Qt.DisplayRole:
            return video.get("title") or video.get("file_name")
        if role == VideoRoles.VideoId:
            return video["id"]
        if role == VideoRoles.Video:
            return video
        if role == VideoRoles.ThumbPath:
            portrait = int(video.get("height") or 0) > int(video.get("width") or 0)
            if portrait and video.get("poster_path"):
                poster = runtime_artwork_path(video.get("poster_path"))
                if poster:
                    return str(poster)
            if video.get("thumb_path"):
                resolved = runtime_artwork_path(video["thumb_path"])
                return str(resolved) if resolved else None
            poster = runtime_artwork_path(video.get("poster_path"))
            return str(poster) if poster else None
        if role == VideoRoles.Progress:
            duration = int(video.get("duration_ms") or 0)
            return (
                min(1.0, int(video.get("last_position_ms") or 0) / duration)
                if duration
                else 0.0
            )
        if role == VideoRoles.Masked:
            return video.get("logical_ext") != video.get("physical_ext")
        return None

    def set_videos(self, videos: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._videos = videos
        self.endResetModel()

    def replace_range(
        self,
        start: int,
        remove_count: int,
        videos: list[dict[str, Any]],
    ) -> None:
        if remove_count:
            self.beginRemoveRows(QModelIndex(), start, start + remove_count - 1)
            del self._videos[start : start + remove_count]
            self.endRemoveRows()
        if videos:
            self.beginInsertRows(
                QModelIndex(),
                start,
                start + len(videos) - 1,
            )
            self._videos[start:start] = videos
            self.endInsertRows()

    def videos(self) -> list[dict[str, Any]]:
        return list(self._videos)

    def video_at(self, row: int) -> dict[str, Any] | None:
        return self._videos[row] if 0 <= row < len(self._videos) else None

    def video_by_id(self, video_id: int) -> dict[str, Any] | None:
        return next(
            (video for video in self._videos if video.get("id") == video_id),
            None,
        )

    def row_for_id(self, video_id: int) -> int:
        return next(
            (
                row
                for row, video in enumerate(self._videos)
                if video.get("id") == video_id
            ),
            -1,
        )
