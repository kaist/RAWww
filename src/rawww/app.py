from __future__ import annotations

import os
import sys
import math
import base64
import json
import ctypes
from hashlib import sha1
import plistlib
import subprocess
from collections import OrderedDict, deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from PySide6.QtCore import QBuffer, QDir, QEvent, QFileInfo, QFileSystemWatcher, QPoint, QRect, QRectF, QIODevice, QSettings, QSize, Qt, QTimer, Signal, QObject, QStorageInfo, QItemSelectionModel, QUrl
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QPolygon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileSystemModel,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QButtonGroup,
    QToolButton,
    QStyledItemDelegate,
    QStyle,
    QSplitter,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
    QInputDialog,
    QFileDialog,
    QMenu,
    QMessageBox,
)

from .cache import FolderCache
from .shotsync_client import ShotSyncClient
from .shotsync_hub import shotsync_hub
from .shotsync_panel import ShotSyncPanel
from .shotsync_selection import SelectionMarkSyncer
from .ai import AiPipeline
from .exif import MetadataPipeline
from .imaging import DecodedImage, PixelImage, decode_pixels, decode_thumbnail_pixels, is_supported_image, is_supported_media, is_supported_video, pixel_to_decoded
from .workspace import WorkspaceRequest, WorkspaceState


THUMB_SIZE = 256
CARD_HARD_MIN_WIDTH = 96
CARD_TARGET_WIDTH = 200
CARD_MAX_WIDTH = 280
CARD_SIZE_TARGETS = (120, 150, CARD_TARGET_WIDTH, 280)
CARD_ASPECT = 3 / 2
RAM_CACHE_LIMIT = 96
THUMBNAIL_RAM_CACHE_LIMIT_BYTES = 700 * 1024 * 1024
FULL_PRELOAD_RADIUS = 10
FULL_RAM_CACHE_LIMIT = FULL_PRELOAD_RADIUS * 2 + 1
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
DETAIL_ROLE = PREVIEW_ROLE + 1
SERIES_ROLE = DETAIL_ROLE + 1
CURRENT_DECODE_WORKERS = 2
BACKGROUND_DECODE_WORKERS = 3
VISIBLE_THUMB_DECODE_WORKERS = 1
MAX_PENDING_THUMBS = 2
VISIBLE_THUMB_LOOKUP_WORKERS = 2
# The visible decoder has one worker. Keeping more than one job submitted
# would put stale viewport work in an executor queue where it cannot be
# reprioritized after a scroll.
MAX_VISIBLE_THUMB_PENDING = 1
# Keep startup work below one frame.  A zero-interval timer can otherwise
# repeatedly win the event loop and make the native window look hung.
POPULATE_BATCH = 48
THUMB_SUBMIT_BATCH = 1
WORK_PUMP_INTERVAL_MS = 16
FLUSH_INTERVAL_MS = 30_000
FOLDER_CHANGE_DEBOUNCE_MS = 1_000
VOLUME_REFRESH_INTERVAL_MS = 2_000
SHOTSYNC_BASE_URL = "https://shotsync.ru"
SHOTSYNC_VOLUME_KEY = "__shotsync__"
ENABLE_EXIF_METADATA = True

FOMANTIC_ICON_CODES = {
    "images": "\uf302", "user": "\uf007", "brush": "\uf1fc", "media": "\uf87c",
    "sort": "\uf160", "search": "\uf002", "star": "\uf005", "ban": "\uf05e",
    "chevron-down": "\uf078", "chevron-up": "\uf077", "bookmark": "\uf02e",
    "step-forward": "\uf051", "keyboard": "\uf11c", "folder": "\uf07c",
    "filter": "\uf0b0", "lightbulb": "\uf0eb", "volume": "\uf028", "close": "\uf00d",
    "plus": "\uf067", "trash": "\uf1f8",
    "expand": "\uf065", "zoom": "\uf00e", "zoom-out": "\uf010", "play": "\uf04b", "pause": "\uf04c", "film": "\uf008",
    "cloud": "\uf0c2", "sign-out": "\uf08b", "lock": "\uf023", "sync": "\uf021",
}
FOMANTIC_ICON_FAMILY = ""


class DecodeBridge(QObject):
    decoded = Signal(object)
    failed = Signal(str, str)
    cacheLoaded = Signal(int, object)
    directoryScanned = Signal(object, Path, object)
    metadataUpdated = Signal(object)


class VideoThumbnailer(QObject):
    """Decode one representative video frame at a time through Qt Multimedia."""

    previewReady = Signal(Path, QImage)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._queue: deque[Path] = deque()
        self._queued: set[Path] = set()
        self._current: Path | None = None
        self._current_source: QUrl | None = None
        self._ready_for_frame = False
        self._generation = 0
        self._active = True
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setVideoOutput(self._sink)
        self._sink.videoFrameChanged.connect(self._frame_ready)
        self._player.mediaStatusChanged.connect(self._media_status_changed)
        self._player.errorOccurred.connect(lambda *_args: self._finish_current())

    def request(self, path: Path) -> None:
        if not self._active:
            return
        if path == self._current or path in self._queued:
            return
        self._queue.append(path)
        self._queued.add(path)
        self._maybe_start()

    def set_active(self, active: bool) -> None:
        self._active = active
        if not active:
            self._queue.clear()
            self._queued.clear()
            self._reset_current()

    def _reset_current(self) -> None:
        # Bump the generation so any late status/frame callbacks from the clip
        # we are abandoning are ignored instead of being attributed elsewhere.
        self._generation += 1
        self._player.stop()
        self._current = None
        self._current_source = None
        self._ready_for_frame = False

    def _maybe_start(self) -> None:
        # Decoding is strictly serialized: never begin a new clip while one is
        # already in flight, otherwise overlapping starts mislabel thumbnails.
        if self._current is not None:
            return
        if not self._active or not self._queue:
            return
        self._begin(self._queue.popleft())

    def _begin(self, path: Path) -> None:
        self._generation += 1
        self._current = path
        self._queued.discard(path)
        # A frame must not be accepted until the media for this exact source
        # has finished loading. Otherwise buffered frames from the previous
        # clip can arrive first and be stored under the new path, mixing up
        # thumbnails.
        self._ready_for_frame = False
        self._current_source = QUrl.fromLocalFile(str(path))
        self._player.setSource(self._current_source)
        # Playing briefly is the portable way to make all Qt backends deliver
        # a frame; the first frame is enough for a grid thumbnail.
        self._player.play()

    def _is_current_source(self) -> bool:
        return self._current is not None and self._player.source() == self._current_source

    def _media_status_changed(self, status) -> None:
        # Only open the frame gate for a status that belongs to the clip we are
        # currently decoding; stale events from a previous source are ignored.
        if not self._is_current_source():
            return
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._ready_for_frame = True

    def _frame_ready(self, frame) -> None:
        if not self._ready_for_frame or not frame.isValid():
            return
        # Reject frames that do not belong to the source we are decoding now.
        if not self._is_current_source():
            return
        image = frame.toImage()
        if image.isNull():
            return
        path = self._current
        self._reset_current()
        self.previewReady.emit(path, image)
        QTimer.singleShot(0, self._maybe_start)

    def _finish_current(self) -> None:
        if self._current is None:
            return
        self._reset_current()
        QTimer.singleShot(0, self._maybe_start)


class FolderNameEditor(QLineEdit):
    accepted = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accepted.emit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.accepted.emit()


class PhotoGrid(QListWidget):
    openRequested = Signal(Path)
    viewportChanged = Signal()
    cardSizeChanged = Signal(int)
    seriesToggleRequested = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("photoGrid")
        self._last_icon_size = QSize()
        self._last_grid_size = QSize()
        self._last_spacing = -1
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        # A stable scrollbar gutter prevents QListView from laying out against
        # a width that changes depending on the resulting number of rows.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.card_size = 1
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        # Adjacent columns can differ by one pixel so their total always equals
        # the viewport width.  A uniform grid cannot represent that remainder.
        self.setUniformItemSizes(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSpacing(0)
        self.setItemDelegate(PhotoCardDelegate(self))
        self.itemActivated.connect(self._emit_open)
        self.verticalScrollBar().rangeChanged.connect(self._queue_card_size_update)
        self._update_card_size()

    def _queue_card_size_update(self, _minimum: int, _maximum: int) -> None:
        # The vertical scrollbar changes the viewport width after QListView has
        # laid out its contents.  Recalculate once that geometry has settled.
        QTimer.singleShot(0, self._update_card_size)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_card_size()
        self.viewportChanged.emit()

    def _emit_open(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.openRequested.emit(Path(path))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                series = item.data(SERIES_ROLE) or {}
                rect = self.visualItemRect(item)
                badge = QRect(rect.right() - 36, rect.top() + 4, 32, 12)
                if series.get("count", 0) > 1 and badge.contains(event.position().toPoint()):
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value:
                        self.seriesToggleRequested.emit(Path(value))
                        event.accept()
                        return
        super().mouseReleaseEvent(event)

    def _update_card_size(self) -> None:
        available = max(CARD_HARD_MIN_WIDTH, self.viewport().width())
        target_width = CARD_SIZE_TARGETS[self.card_size]
        min_columns = max(1, math.ceil(available / CARD_MAX_WIDTH))
        max_columns = max(1, available // CARD_HARD_MIN_WIDTH)
        columns = max(min_columns, min(max_columns, round(available / target_width)))
        # QListView's icon layout reserves its rightmost viewport coordinate
        # when deciding whether to wrap.  Keep two layout pixels free; the
        # delegate extends the last column over them when painting.
        layout_width = max(1, available - 2)
        width, remainder = divmod(layout_width, columns)
        height = int((available / columns) / CARD_ASPECT)
        icon_size = QSize(width + bool(remainder), height)
        # An invalid grid size tells QListView to use the per-item size hints.
        grid_size = QSize()
        spacing = 0
        self._column_count = columns
        self._column_width = width
        self._wide_column_count = remainder
        self._card_height = height + 32
        if (
            icon_size == self._last_icon_size
            and grid_size == self._last_grid_size
            and spacing == self._last_spacing
        ):
            return
        self._last_icon_size = icon_size
        self._last_grid_size = grid_size
        self._last_spacing = spacing
        self.setSpacing(spacing)
        self.setViewportMargins(0, 0, 0, 0)
        self.setIconSize(icon_size)
        self.setGridSize(grid_size)
        self.scheduleDelayedItemsLayout()

    def card_size_hint(self, row: int) -> QSize:
        column = row % self._column_count
        width = self._column_width + (1 if column < self._wide_column_count else 0)
        return QSize(width, self._card_height)

    def is_last_grid_column(self, row: int) -> bool:
        return row % self._column_count == self._column_count - 1

    def change_card_size(self, delta: int) -> None:
        new_size = max(0, min(3, self.card_size + delta))
        if new_size == self.card_size:
            return
        self.card_size = new_size
        self._last_icon_size = QSize()
        self._update_card_size()
        self.cardSizeChanged.emit(self.card_size)


class PhotoCardDelegate(QStyledItemDelegate):
    """The single card renderer shared by grid and fullscreen strips."""

    def __init__(self, parent=None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.compact = compact

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        inset = 2 if self.compact else 2
        item_rect = option.rect
        if isinstance(option.widget, PhotoGrid) and option.widget.is_last_grid_column(index.row()):
            item_rect = item_rect.adjusted(0, 0, 2, 0)
        rect = item_rect.adjusted(inset, inset, -inset, -inset)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        detail = index.data(DETAIL_ROLE) or {}
        series = index.data(SERIES_ROLE) or {}
        expanded_series = bool(series.get("expanded") or series.get("member"))
        label = str(detail.get("color_label") or "")
        colors = {"red": "#a25555", "yellow": "#af9440", "green": "#4d9660", "blue": "#537fc2", "purple": "#9760b2"}
        tints = {
            "red": QColor(170, 88, 88, 86), "yellow": QColor(181, 151, 63, 84),
            "green": QColor(75, 154, 97, 82), "blue": QColor(83, 127, 194, 88),
            "purple": QColor(151, 96, 178, 84),
        }

        if expanded_series:
            bg = QColor("#a0a0a0") if selected else QColor("#888888" if hovered else "#747474")
        else:
            bg = QColor("#c4c4c4") if selected else QColor("#b3b3b3" if hovered else "#a7a7a7")
        painter.fillRect(rect, bg)
        if label in tints:
            painter.fillRect(rect, tints[label])
        painter.setPen(QPen(QColor(colors.get(label, "#767676")), 1))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        if selected:
            painter.setPen(QPen(QColor("#ececec"), 2))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))

        top, side, bottom = (14, 4, 15) if self.compact else (20, 4, 16)
        image_rect = rect.adjusted(side, top, -side, -bottom)
        # Check if this item is a directory
        path = index.data(Qt.ItemDataRole.UserRole)
        path_obj = Path(path) if path else None
        if path_obj and path_obj.is_dir():
             text_height = 20 if self.compact else 24
             text_rect = QRect(rect.left() + 8, rect.bottom() - text_height - 1, rect.width() - 16, text_height)
             folder_rect = QRect(
                 image_rect.left(),
                 image_rect.top(),
                 image_rect.width(),
                 max(24, text_rect.top() - image_rect.top() - 2),
             )
             # Use system folder icon from Qt file icon provider
             icon_provider = QFileIconProvider()
             folder_icon = icon_provider.icon(QFileInfo(str(path_obj)))
             if not folder_icon.isNull():
                 # Clear any background first (remove old gray background)
                 painter.fillRect(folder_rect, Qt.GlobalColor.transparent)
                 # Keep the folder icon above the caption instead of using the full card height.
                 scaled = folder_icon.pixmap(folder_rect.size()).size().scaled(
                     folder_rect.size(),
                     Qt.AspectRatioMode.KeepAspectRatio
                 )
                 target = QRect(
                     folder_rect.left() + (folder_rect.width() - scaled.width()) // 2,
                     folder_rect.top() + (folder_rect.height() - scaled.height()) // 2,
                     scaled.width(),
                     scaled.height(),
                 )
                 painter.drawPixmap(target, folder_icon.pixmap(scaled))
        else:
            painter.fillRect(image_rect, QColor("#8f8f8f"))

            preview = index.data(PREVIEW_ROLE)
            if isinstance(preview, QImage) and not preview.isNull():
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                scaled = preview.size().scaled(image_rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
                target = QRect(
                    image_rect.left() + (image_rect.width() - scaled.width()) // 2,
                    image_rect.top() + (image_rect.height() - scaled.height()) // 2,
                    scaled.width(),
                    scaled.height(),
                )
                painter.drawImage(target, preview)

            if path_obj and is_supported_video(path_obj):
                video_badge = QRect(image_rect.left() + 5, image_rect.bottom() - 20, 22, 16)
                painter.fillRect(video_badge, QColor(20, 20, 20, 190))
                painter.setPen(QColor("#f1f1f1"))
                icon_font = QFont(FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(10)
                painter.setFont(icon_font)
                painter.drawText(video_badge, Qt.AlignmentFlag.AlignCenter, FOMANTIC_ICON_CODES["film"] if FOMANTIC_ICON_FAMILY else "▶")

            # A series is a temporary expanded context, not a normal row in
            # the timeline. Darken its preview as well as the card chrome so
            # the complete group remains recognisable in grids and strips.
            if expanded_series:
                painter.fillRect(image_rect, QColor(0, 0, 0, 76))

        # For folders: full width text, no ratings/badges.
        caption_rect = QRect(rect.left() + 5, rect.bottom() - bottom + 2, rect.width() - 10, bottom - 2)
        if path_obj and path_obj.is_dir():
            text_rect = QRect(rect.left() + 8, rect.bottom() - (20 if self.compact else 24) - 1, rect.width() - 16, 20 if self.compact else 24)
        # For folders always use just the folder name, never full path
        if path_obj and path_obj.is_dir():
            text = path_obj.name
        else:
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = QColor("#242424")
        painter.setPen(color)
        font = QFont(option.font)
        font.setPointSizeF(6.5 if self.compact else 7.5)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        rating = int(detail.get("rating") or 0) if not (path_obj and path_obj.is_dir()) else 0
        rating_text = "★" * rating
        if not (path_obj and path_obj.is_dir()):
            # Always reserve five stars, regardless of the current rating.
            # This keeps the filename clear of the rating area and guarantees
            # that a later five-star mark cannot be clipped.
            rating_width = min(painter.fontMetrics().horizontalAdvance("★" * 5) + 5, caption_rect.width())
            text_rect = QRect(caption_rect)
            text_rect.setWidth(max(0, caption_rect.width() - rating_width - 3))
            rating_rect = QRect(caption_rect.right() - rating_width + 1, caption_rect.top(), rating_width, caption_rect.height())
        else:
            text_rect = QRect(caption_rect)
        display_text = text
        font_metrics = painter.fontMetrics()
        if path_obj and path_obj.is_file() and font_metrics.horizontalAdvance(text) > text_rect.width():
            # The extension is secondary information. When a narrow card
            # cannot fit both, spend all available width on the filename.
            display_text = path_obj.stem
        painter.drawText(
            text_rect,
            (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            if path_obj and path_obj.is_dir()
            else (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            font_metrics.elidedText(display_text, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        # Only render ratings and series badges for photos, not folders
        if not (path_obj and path_obj.is_dir()):
            if rating_text:
                painter.setPen(QColor("#3a3123"))
                painter.drawText(rating_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rating_text)
            count = int(series.get("count", 0) or 0)
            if count > 1:
                badge_width = 26 if self.compact else 32
                badge_height = 10 if self.compact else 12
                badge_rect = QRect(rect.right() - badge_width - 4, rect.top() + 4, badge_width, badge_height)
                painter.setPen(QColor("#262626"))
                icon_font = QFont(FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(7 if self.compact else 8)
                painter.setFont(icon_font)
                icon_width = 8 if self.compact else 10
                painter.drawText(QRect(badge_rect.left(), badge_rect.top(), icon_width, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, FOMANTIC_ICON_CODES["images"] if FOMANTIC_ICON_FAMILY else "▣")
                font = painter.font()
                font.setPixelSize(7 if self.compact else 8)
                painter.setFont(font)
                marker = "−" if series.get("expanded") else "+"
                badge_text = str(count) if self.compact else f"{count} {marker}"
                painter.drawText(QRect(badge_rect.left() + icon_width, badge_rect.top(), badge_width - icon_width, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, badge_text)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        if isinstance(option.widget, PhotoGrid):
            return option.widget.card_size_hint(index.row())
        return option.widget.gridSize() if isinstance(option.widget, QListWidget) else super().sizeHint(option, index)


class ViewerStrip(QListWidget):
    pathActivated = Signal(Path)
    seriesToggleRequested = Signal(Path)
    viewportChanged = Signal()

    def __init__(self, *, vertical: bool = False) -> None:
        super().__init__()
        self.vertical = vertical
        self._paths: list[Path] = []
        self._items_by_path: dict[Path, QListWidgetItem] = {}
        self.setObjectName("seriesStrip" if vertical else "photoStrip")
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setMovement(QListWidget.Movement.Static)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setSpacing(3)
        self.setItemDelegate(PhotoCardDelegate(self, compact=True))
        if vertical:
            self.setFlow(QListWidget.Flow.TopToBottom)
            self.setWrapping(False)
            # Keep series cards the same size as the cards in the bottom
            # strip.  The extra width leaves room for the vertical scrollbar.
            self.setGridSize(QSize(118, 104))
            self.setIconSize(QSize(112, 98))
            self.setFixedWidth(136)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        else:
            self.setFlow(QListWidget.Flow.LeftToRight)
            self.setWrapping(False)
            self.setGridSize(QSize(118, 104))
            self.setIconSize(QSize(112, 98))
            self.setFixedHeight(108)
            # A horizontal filmstrip must never reserve space for a vertical
            # scrollbar: its one row is deliberately clipped horizontally.
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.itemClicked.connect(self._activate)
        scroll_bar = self.verticalScrollBar() if vertical else self.horizontalScrollBar()
        scroll_bar.valueChanged.connect(self.viewportChanged)

    def _activate(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self.setCurrentItem(item)
            self.pathActivated.emit(Path(value))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                series = item.data(SERIES_ROLE) or {}
                rect = self.visualItemRect(item).adjusted(2, 2, -2, -2)
                badge = QRect(rect.right() - 30, rect.top() + 4, 26, 10)
                if int(series.get("count", 0) or 0) > 1 and badge.contains(event.position().toPoint()):
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value:
                        self.seriesToggleRequested.emit(Path(value))
                        event.accept()
                        return
        super().mousePressEvent(event)

    def set_paths(
        self,
        paths: list[Path],
        current: Path | None,
        details: dict[str, dict],
        previews: dict[Path, QImage],
        series_cards: dict[Path, dict] | None = None,
    ) -> None:
        series_cards = series_cards or {}
        if paths != self._paths:
            self.clear()
            self._items_by_path.clear()
            self._paths = list(paths)
            for path in paths:
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setData(DETAIL_ROLE, details.get(path.name, {}))
                item.setData(SERIES_ROLE, series_cards.get(path, {}))
                preview = previews.get(path)
                if preview is not None:
                    item.setData(PREVIEW_ROLE, preview)
                self.addItem(item)
                self._items_by_path[path] = item
        else:
            for path, preview in previews.items():
                item = self._items_by_path.get(path)
                if item is not None:
                    item.setData(PREVIEW_ROLE, preview)
            for path, series in series_cards.items():
                item = self._items_by_path.get(path)
                if item is not None:
                    item.setData(SERIES_ROLE, series)
        self.set_current(current)

    def set_current(self, current: Path | None) -> None:
        current_item = self._items_by_path.get(current) if current is not None else None
        if current_item is not None:
            self.setCurrentItem(current_item)
            self.scrollToItem(current_item, QListWidget.ScrollHint.EnsureVisible)

    def update_details(self, path: Path, detail: dict) -> None:
        item = self._items_by_path.get(path)
        if item is not None:
            item.setData(DETAIL_ROLE, detail)
            self.update(self.visualItemRect(item))

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.vertical:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() - delta)
                event.accept()
                return
        super().wheelEvent(event)

    def update_preview(self, path: Path, preview: QImage) -> None:
        item = self._items_by_path.get(path)
        if item is not None:
            item.setData(PREVIEW_ROLE, preview)
            self.update(self.visualItemRect(item))

    def visible_paths(self) -> list[Path]:
        """Return items intersecting the viewport, in display order."""
        viewport_rect = self.viewport().rect()
        return [
            path
            for path in self._paths
            if (item := self._items_by_path.get(path)) is not None
            and self.visualItemRect(item).intersects(viewport_rect)
        ]

    def item_for_path(self, path: Path) -> QListWidgetItem | None:
        return self._items_by_path.get(path)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.viewportChanged.emit()


class VideoSeekSlider(QSlider):
    """A seek slider that jumps to the point clicked on its track."""

    seekRequested = Signal(int)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            ratio = min(1.0, max(0.0, event.position().x() / self.width()))
            position = self.minimum() + round(ratio * (self.maximum() - self.minimum()))
            current_ratio = (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())
            if abs(event.position().x() - current_ratio * self.width()) > 8:
                self.setValue(position)
                self.seekRequested.emit(position)
                event.accept()
                return
        super().mousePressEvent(event)


class ColorLabelButton(QToolButton):
    """Color swatch with a selection outline painted inside its bounds."""

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not (self.isChecked() or self.underMouse()):
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#e5e5e5"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))


class ViewerMetaBar(QWidget):
    """Shared rating, label, quick-mark and comment controls for grid/full view."""

    ratingRequested = Signal(object)
    colorRequested = Signal(str)
    quickMarkRequested = Signal()
    quickMarkConfigured = Signal(str, object)
    autoAdvanceChanged = Signal(bool)
    commentSubmitted = Signal(str)

    def __init__(self, *, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setObjectName("viewerMeta")
        self.settings = settings or QSettings("RAWww", "RAWww")
        self._quick_mark = ("rating", 5)
        layout = QHBoxLayout(self)
        # The bar is 30px tall including its top/bottom borders. Leave an
        # exact 24px content lane so every fixed-height control is centred
        # identically in grid and full-view.
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(5)

        self.quick_mark_button = QToolButton()
        self.quick_mark_button.setObjectName("fullQuickMark")
        self.quick_mark_button.setIcon(_fomantic_icon("bookmark", 13))
        self.quick_mark_button.setText("быстр. метка")
        self.quick_mark_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.quick_mark_button.setToolTip("На��троить быструю метку; M — применить")
        self.quick_mark_button.setFixedSize(96, 24)
        self.quick_mark_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.quick_mark_button.clicked.connect(self._show_quick_mark_menu)
        layout.addWidget(self.quick_mark_button)

        self.auto_advance_button = QToolButton()
        self.auto_advance_button.setObjectName("fullAutoAdvance")
        self.auto_advance_button.setIcon(_fomantic_icon("step-forward", 13))
        self.auto_advance_button.setCheckable(True)
        self.auto_advance_button.setToolTip("Автоперелистывание после метки")
        self.auto_advance_button.setFixedSize(28, 24)
        self.auto_advance_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.auto_advance_button.toggled.connect(self.autoAdvanceChanged)
        layout.addWidget(self.auto_advance_button)

        self.color_group = QWidget()
        self.color_group.setObjectName("viewerColorRow")
        color_layout = QHBoxLayout(self.color_group)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(0)
        self.color_buttons: dict[str, QToolButton] = {}
        for color in ("", "red", "yellow", "green", "blue", "purple"):
            button = ColorLabelButton()
            button.setObjectName("viewerColor")
            button.setProperty("colorLabel", color or "none")
            button.setCheckable(True)
            if not color:
                button.setIcon(_fomantic_icon("ban", 11, "#959595"))
            button.setToolTip("Сбросить цвет" if not color else color)
            button.setFixedSize(24, 24)
            button.clicked.connect(lambda _checked=False, value=color: self.colorRequested.emit(value))
            color_layout.addWidget(button)
            self.color_buttons[color] = button
        layout.addWidget(self.color_group)
        self.color_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.rating_buttons: dict[int, QPushButton] = {}
        self.rating_group = QWidget()
        self.rating_group.setObjectName("viewerRatingRow")
        # Use explicit integer geometry here. QHBoxLayout may distribute its
        # border-adjusted contents rect fractionally under Windows DPI scaling,
        # which makes adjacent fixed-size buttons paint over one another.
        self.rating_group.setFixedSize(149, 24)
        self.rating_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        for rating in range(0, 6):
            button = QPushButton()
            button.setObjectName("viewerRating")
            button.setProperty("ratingClear", rating == 0)
            button.setFlat(True)
            button.setCheckable(True)
            button.setFixedSize(24, 24)
            button.setIconSize(QSize(18, 18))
            button.setIcon(_fomantic_icon("ban" if rating == 0 else "star", 17, "#777777"))
            button.setToolTip("Сбросить рейтинг" if rating == 0 else f"Рейтинг {rating}")
            button.clicked.connect(lambda _checked=False, value=rating: self.ratingRequested.emit(value or None))
            button.setParent(self.rating_group)
            button.move(rating * 25, 0)
            self.rating_buttons[rating] = button
        layout.addWidget(self.rating_group)

        self.comment_edit = QLineEdit()
        self.comment_edit.setObjectName("fullComment")
        self.comment_edit.setPlaceholderText("Комментарий")
        self.comment_edit.setFixedHeight(24)
        self.comment_edit.setMinimumWidth(72)
        self.comment_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.comment_edit.editingFinished.connect(lambda: self.commentSubmitted.emit(self.comment_edit.text().strip()))
        layout.addWidget(self.comment_edit, 1)

        self.exif_label = QLabel()
        self.exif_label.setObjectName("viewerExif")
        self.exif_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.exif_label.setMinimumWidth(0)
        self.exif_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.exif_label, 1)

    def _show_quick_mark_menu(self) -> None:
        # A popup must use the actual top-level widget as its transient
        # parent. Parenting it to this embedded bar produces QWidgetWindow
        # "must be a top level window" warnings on Windows.
        menu = QMenu(self.window())
        menu.setToolTipsVisible(True)
        title = menu.addAction("Настроить быструю метку")
        title.setEnabled(False)
        menu.addSeparator()

        def add_visual_action(*, selected: bool, visual: str | QIcon, tooltip: str, callback) -> None:
            action = QWidgetAction(menu)
            row = QPushButton()
            row.setObjectName("quickMarkMenuItem")
            row.setFlat(True)
            row.setFixedHeight(26)
            row.setToolTip(tooltip)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 2, 12, 2)
            row_layout.setSpacing(7)
            check = QLabel("✓" if selected else "")
            check.setObjectName("quickMarkMenuCheck")
            check.setFixedWidth(14)
            check.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(check)
            value = QLabel()
            value.setObjectName("quickMarkMenuValue")
            if isinstance(visual, QIcon):
                value.setPixmap(visual.pixmap(18, 18))
            else:
                value.setText(visual)
            row_layout.addWidget(value)
            row_layout.addStretch(1)
            row.clicked.connect(callback)
            row.clicked.connect(menu.close)
            action.setDefaultWidget(row)
            menu.addAction(action)

        for rating in range(5, 0, -1):
            selected = self._quick_mark == ("rating", rating)
            add_visual_action(
                selected=selected,
                visual="★" * rating,
                tooltip=f"Рейтинг {rating}",
                callback=lambda _checked=False, value=rating: self._configure_quick_mark("rating", value),
            )
        menu.addSeparator()
        color_options = (
            ("red", "Красная метка", "#7a5555"),
            ("yellow", "Жёлтая метка", "#7f7556"),
            ("green", "Зелёная метка", "#5d7560"),
            ("blue", "Синяя метка", "#596b82"),
            ("purple", "Фиолетовая метка", "#71607d"),
        )
        for color, tooltip, swatch in color_options:
            selected = self._quick_mark == ("color_label", color)
            add_visual_action(
                selected=selected,
                visual=(
                    _color_swatch_icon(swatch)
                    if swatch is not None
                    else _fomantic_icon("ban", 14, "#a0a0a0")
                ),
                tooltip=tooltip,
                callback=lambda _checked=False, value=color: self._configure_quick_mark("color_label", value),
            )
        menu.exec(self.quick_mark_button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    def _configure_quick_mark(self, kind: str, value: object) -> None:
        self._quick_mark = (kind, value)
        self.quickMarkConfigured.emit(kind, value)

    def set_quick_mark(self, kind: str, value: object) -> None:
        self._quick_mark = (kind, value)

    def set_auto_advance(self, enabled: bool) -> None:
        self.auto_advance_button.blockSignals(True)
        self.auto_advance_button.setChecked(enabled)
        self.auto_advance_button.blockSignals(False)

    def set_comment(self, comment: str) -> None:
        self.comment_edit.blockSignals(True)
        self.comment_edit.setText(comment)
        self.comment_edit.blockSignals(False)

    def set_metadata(self, detail: dict) -> None:
        color = str(detail.get("color_label") or "")
        rating = int(detail.get("rating") or 0)
        for value, button in self.color_buttons.items():
            button.blockSignals(True)
            button.setChecked(value == color)
            button.blockSignals(False)
        for value, button in self.rating_buttons.items():
            button.blockSignals(True)
            button.setChecked((value == 0 and rating == 0) or (value > 0 and value <= rating))
            button.blockSignals(False)
        for value, button in self.rating_buttons.items():
            if value == 0:
                button.setIcon(_fomantic_icon("ban", 17, "#d0d0d0" if rating == 0 else "#777777"))
            else:
                button.setIcon(_fomantic_icon("star", 17, "#d0d0d0" if value <= rating else "#777777"))
        self.set_comment(str(detail.get("comment") or ""))
        capture = detail.get("capture_settings") or {}
        camera = detail.get("camera") or {}
        parts = []
        if camera.get("model"):
            parts.append(str(camera["model"]))
        if capture.get("aperture") is not None:
            parts.append(f"f/{capture['aperture']:g}")
        if capture.get("exposure_display"):
            parts.append(str(capture["exposure_display"]))
        if capture.get("iso") is not None:
            parts.append(f"ISO {capture['iso']}")
        if capture.get("focal_length_mm") is not None:
            parts.append(f"{capture['focal_length_mm']:g}mm")
        self.exif_label.setText(" · ".join(parts))


class FullView(QFrame):
    exitRequested = Signal()
    nextRequested = Signal()
    previousRequested = Signal()
    pathRequested = Signal(Path)
    ratingRequested = Signal(object)
    colorRequested = Signal(str)
    faceShowRequested = Signal(object)
    faceAddRequested = Signal(object)
    faceFilterClearRequested = Signal()
    seriesToggleRequested = Signal(Path)
    quickMarkRequested = Signal()
    quickMarkConfigured = Signal(str, object)
    autoAdvanceChanged = Signal(bool)
    commentSubmitted = Signal(str)
    stripViewportChanged = Signal()
    videoPlaybackChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("fullView")
        self._pixmap: QPixmap | None = None
        self._path: Path | None = None
        self._is_fallback = False
        self._photo_generation = -1
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setSingleShot(True)
        self._smooth_timer.timeout.connect(self._smooth_fit)

        self.image_view = FullImageView()
        self.image_view.setObjectName("fullImageView")
        self.image_view.faceClicked.connect(self._show_face_actions)
        self.video_widget = QVideoWidget()
        self.video_widget.setObjectName("fullVideoView")
        self.media_stack = QStackedWidget()
        self.media_stack.addWidget(self.image_view)
        self.media_stack.addWidget(self.video_widget)
        self.video_player = QMediaPlayer(self)
        self.video_audio = QAudioOutput(self)
        self.video_audio.setVolume(1.0)
        self.video_player.setAudioOutput(self.video_audio)
        self.video_player.setVideoOutput(self.video_widget)
        self.video_player.positionChanged.connect(self._video_position_changed)
        self.video_player.durationChanged.connect(self._video_duration_changed)
        self.video_player.playbackStateChanged.connect(self._video_state_changed)
        self._is_video = False
        self.video_controls = QFrame()
        self.video_controls.setObjectName("videoControls")
        # QVideoWidget may use a native child surface on Windows. Make the
        # controls native too, otherwise that surface can cover them once the
        # first frame arrives.
        self.video_controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_controls.setFixedWidth(360)
        self.video_controls_layout = QHBoxLayout(self.video_controls)
        self.video_controls_layout.setContentsMargins(8, 5, 8, 5)
        self.video_controls_layout.setSpacing(7)
        self.video_controls.hide()

        self.info_label = QLabel()
        self.info_label.setObjectName("overlayLabel")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.photo_strip = ViewerStrip()
        self.photo_strip.pathActivated.connect(self.pathRequested)
        self.photo_strip.seriesToggleRequested.connect(self.seriesToggleRequested)
        self.photo_strip.viewportChanged.connect(self.stripViewportChanged)
        self.series_strip = ViewerStrip(vertical=True)
        self.series_strip.pathActivated.connect(self.pathRequested)
        self.series_strip.viewportChanged.connect(self.stripViewportChanged)
        self._series_paths: list[Path] = []

        self.series_panel = QFrame()
        self.series_panel.setObjectName("seriesPanel")
        series_layout = QVBoxLayout(self.series_panel)
        series_layout.setContentsMargins(0, 5, 0, 5)
        series_layout.setSpacing(4)
        self.series_up = QToolButton()
        self.series_up.setObjectName("seriesNav")
        self.series_up.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.series_up.setIcon(_fomantic_icon("chevron-up", 13))
        self.series_up.clicked.connect(lambda: self._move_series(-1))
        self.series_down = QToolButton()
        self.series_down.setObjectName("seriesNav")
        self.series_down.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.series_down.setIcon(_fomantic_icon("chevron-down", 13))
        self.series_down.clicked.connect(lambda: self._move_series(1))
        series_layout.addWidget(self.series_up)
        series_layout.addWidget(self.series_strip, 1)
        series_layout.addWidget(self.series_down)
        self.series_panel.hide()

        stage = QWidget()
        stage.setObjectName("photoStage")
        stage_layout = QHBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(12)
        stage_layout.addWidget(self.series_panel)
        self.media_panel = QWidget()
        media_layout = QVBoxLayout(self.media_panel)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(0)
        media_layout.addWidget(self.media_stack, 1)
        stage_layout.addWidget(self.media_panel, 1)
        # The floating video controls are positioned manually on top of their
        # host. Reposition them whenever the host resizes (e.g. when the bottom
        # strip is collapsed/expanded) so they always stay pinned to the bottom.
        self.media_panel.installEventFilter(self)
        self.video_widget.installEventFilter(self)

        self.face_filter_chip = QFrame(self.media_panel)
        self.face_filter_chip.setObjectName("fullFaceFilterChip")
        face_chip_layout = QHBoxLayout(self.face_filter_chip)
        face_chip_layout.setContentsMargins(5, 3, 4, 3)
        face_chip_layout.setSpacing(4)
        self.face_filter_avatar = QLabel()
        self.face_filter_avatar.setFixedSize(26, 26)
        self.face_filter_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        face_chip_layout.addWidget(self.face_filter_avatar)
        self.face_filter_clear = QToolButton()
        self.face_filter_clear.setObjectName("fullFaceFilterClear")
        self.face_filter_clear.setIcon(_fomantic_icon("close", 12))
        self.face_filter_clear.setFixedSize(20, 20)
        self.face_filter_clear.setIconSize(QSize(12, 12))
        self.face_filter_clear.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.face_filter_clear.setAutoRaise(True)
        self.face_filter_clear.setToolTip("Сбросить фильтр по лицу")
        self.face_filter_clear.clicked.connect(self.faceFilterClearRequested)
        face_chip_layout.addWidget(self.face_filter_clear)
        self.face_filter_chip.hide()

        strip_header = QWidget()
        strip_header.setObjectName("stripHeader")
        strip_header_layout = QHBoxLayout(strip_header)
        strip_header_layout.setContentsMargins(9, 4, 9, 4)
        strip_header_layout.setSpacing(5)
        self.strip_toggle = QToolButton()
        self.strip_toggle.setObjectName("stripToggle")
        self.strip_toggle.setIcon(_fomantic_icon("chevron-down", 12))
        self.strip_toggle.setToolTip("Свернуть ленту пр��вью")
        self.strip_toggle.clicked.connect(self.toggle_strip)
        strip_header_layout.addWidget(self.strip_toggle)
        self.video_play_button = QToolButton()
        self.video_play_button.setObjectName("videoPlay")
        self.video_play_button.setIcon(_fomantic_icon("play", 12))
        self.video_play_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.video_play_button.setToolTip("Воспроизвести / пауза (Пробел)")
        self.video_play_button.clicked.connect(self._toggle_video_playback)
        self.video_seek = VideoSeekSlider(Qt.Orientation.Horizontal)
        self.video_seek.setObjectName("videoSeek")
        self.video_seek.setRange(0, 0)
        self.video_seek.setMinimumWidth(150)
        self.video_seek.sliderMoved.connect(self.video_player.setPosition)
        self.video_seek.seekRequested.connect(self.video_player.setPosition)
        self.video_time_label = QLabel("0:00 / 0:00")
        self.video_time_label.setObjectName("videoTime")
        self.video_controls.setParent(self.media_panel)
        self.video_controls_layout.addWidget(self.video_play_button)
        self.video_controls_layout.addWidget(self.video_seek, 1)
        self.video_controls_layout.addWidget(self.video_time_label)
        self.meta_bar = ViewerMetaBar()
        self.meta_bar.ratingRequested.connect(self.ratingRequested)
        self.meta_bar.colorRequested.connect(self.colorRequested)
        self.meta_bar.quickMarkRequested.connect(self.quickMarkRequested)
        self.meta_bar.quickMarkConfigured.connect(self.quickMarkConfigured)
        self.meta_bar.autoAdvanceChanged.connect(self.autoAdvanceChanged)
        self.meta_bar.commentSubmitted.connect(self.commentSubmitted)
        self.color_buttons = self.meta_bar.color_buttons
        self.rating_buttons = self.meta_bar.rating_buttons
        self.full_comment_edit = self.meta_bar.comment_edit
        strip_header_layout.addWidget(self.meta_bar, 1)

        self.strip_panel = QFrame()
        self.strip_panel.setObjectName("stripPanel")
        strip_layout = QVBoxLayout(self.strip_panel)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(0)
        strip_layout.addWidget(strip_header)
        strip_layout.addWidget(self.photo_strip)
        if QSettings("RAWww", "RAWww").value("viewer_strip_collapsed", False, bool):
            self.photo_strip.hide()
            self.strip_toggle.setIcon(_fomantic_icon("chevron-up", 12))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(stage, 1)
        layout.addWidget(self.strip_panel)

    def toggle_strip(self) -> None:
        visible = self.photo_strip.isVisible()
        self.photo_strip.setVisible(not visible)
        self.strip_toggle.setIcon(_fomantic_icon("chevron-up" if visible else "chevron-down", 12))
        self.strip_toggle.setToolTip("Развернуть ленту превью" if visible else "Свернуть ленту превью")
        QSettings("RAWww", "RAWww").setValue("viewer_strip_collapsed", visible)
        # The host's resize (handled in eventFilter) keeps the controls pinned,
        # but reposition once more after the layout settles as a safety net.
        QTimer.singleShot(0, self._position_video_controls)

    def stop_video(self) -> None:
        """Fully stop playback so nothing keeps running after leaving full view."""
        if not self._is_video:
            return
        self.video_player.stop()
        self.video_play_button.setIcon(_fomantic_icon("play", 12))

    def set_quick_mark(self, kind: str, value: object) -> None:
        self.meta_bar.set_quick_mark(kind, value)

    def set_auto_advance(self, enabled: bool) -> None:
        self.meta_bar.set_auto_advance(enabled)

    def set_navigation(
        self,
        paths: list[Path],
        current: Path | None,
        details: dict[str, dict],
        previews: dict[Path, QImage],
        series: list[Path],
        generation: int,
        *,
        series_current: Path | None = None,
        strip_series_cards: dict[Path, dict] | None = None,
        show_series_strip: bool = True,
    ) -> None:
        # The bottom strip represents collapsed series and therefore selects
        # its leader. The vertical strip represents every member and must keep
        # the actual opened frame selected.
        series_current = current if series_current is None else series_current
        if generation != self._photo_generation:
            self.photo_strip.set_paths(paths, current, details, previews, strip_series_cards)
            self._photo_generation = generation
        else:
            self.photo_strip.set_current(current)
            for path, preview in previews.items():
                self.photo_strip.update_preview(path, preview)
            for path, series_card in (strip_series_cards or {}).items():
                item = self.photo_strip.item_for_path(path)
                if item is not None:
                    item.setData(SERIES_ROLE, series_card)
        series_cards = (
            {path: {"expanded": index == 0, "member": index > 0} for index, path in enumerate(series)}
            if len(series) > 1 else None
        )
        self.series_strip.set_paths(series, series_current, details, previews, series_cards)
        self._series_paths = list(series)
        self.series_panel.setVisible(show_series_strip and len(series) > 1)
        self._update_series_navigation(series_current)

    def update_preview(self, path: Path, preview: QImage) -> None:
        self.photo_strip.update_preview(path, preview)
        self.series_strip.update_preview(path, preview)

    def set_faces(self, faces: list[dict] | None) -> None:
        self.image_view.set_faces(faces)

    def face_avatar(self, face: dict, size: int = 40) -> QPixmap:
        return self.image_view.face_avatar(face, size)

    def set_face_filter(self, avatar: QPixmap | None) -> None:
        if avatar is not None and not avatar.isNull():
            self.face_filter_avatar.setPixmap(avatar.scaled(
                26, 26, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            ))
        else:
            self.face_filter_avatar.setPixmap(_fomantic_icon("user", 13).pixmap(16, 16))
        self.face_filter_chip.show()
        self._position_face_filter_chip()

    def clear_face_filter(self) -> None:
        self.face_filter_chip.hide()

    def _show_face_actions(self, face: object, position: object) -> None:
        if not isinstance(face, dict) or not isinstance(position, QPoint):
            return
        # FullView is embedded in a stacked page. A popup needs the actual
        # top-level parent on Windows, otherwise Qt creates an invalid child
        # QWidgetWindow and emits a warning for every click.
        menu = QMenu(self.window())
        menu.setObjectName("faceActionMenu")
        action = QWidgetAction(menu)
        row = QWidget(menu)
        layout = QVBoxLayout(row)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        show_button = QToolButton(row)
        show_button.setObjectName("faceActionButton")
        show_button.setIcon(_fomantic_icon("images", 14))
        show_button.setText("Показать фото с этим лицом")
        show_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_button = QToolButton(row)
        add_button.setObjectName("faceActionButton")
        add_button.setIcon(_fomantic_icon("plus", 14))
        add_button.setText("Добавить лицо в набор")
        add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for button in (show_button, add_button):
            button.setFixedWidth(250)
            layout.addWidget(button)
        show_button.clicked.connect(lambda: self.faceShowRequested.emit(face))
        show_button.clicked.connect(menu.close)
        add_button.clicked.connect(lambda: self.faceAddRequested.emit(face))
        add_button.clicked.connect(menu.close)
        action.setDefaultWidget(row)
        menu.addAction(action)
        menu.popup(self.image_view.mapToGlobal(position))

    def set_comment(self, comment: str) -> None:
        self.full_comment_edit.blockSignals(True)
        self.full_comment_edit.setText(comment)
        self.full_comment_edit.blockSignals(False)

    def set_metadata(self, detail: dict, paths: tuple[Path, ...] = ()) -> None:
        self.meta_bar.set_metadata(detail)
        for path in paths or ((self._path,) if self._path is not None else ()):
            self.photo_strip.update_details(path, detail)
            self.series_strip.update_details(path, detail)

    def _move_series(self, delta: int) -> None:
        if not self._series_paths:
            return
        current = self.series_strip.currentItem()
        current_path = Path(current.data(Qt.ItemDataRole.UserRole)) if current is not None else None
        try:
            row = self._series_paths.index(current_path) + delta
        except ValueError:
            row = 0 if delta > 0 else len(self._series_paths) - 1
        if 0 <= row < len(self._series_paths):
            path = self._series_paths[row]
            item = self.series_strip.item_for_path(path)
            if item is not None:
                self.series_strip.setCurrentItem(item)
                self.series_strip.scrollToItem(item, QListWidget.ScrollHint.EnsureVisible)
                self._update_series_navigation(path)
                self.pathRequested.emit(path)

    def _update_series_navigation(self, current: Path | None) -> None:
        try:
            row = self._series_paths.index(current) if current is not None else -1
        except ValueError:
            row = -1
        self.series_up.setEnabled(row > 0)
        self.series_down.setEnabled(0 <= row < len(self._series_paths) - 1)

    def set_image(self, decoded: DecodedImage, *, fallback: bool = False) -> None:
        self.video_player.stop()
        self._is_video = False
        self.video_controls.hide()
        self.media_stack.setCurrentWidget(self.image_view)
        self._path = decoded.path
        self._is_fallback = fallback
        self._pixmap = QPixmap.fromImage(decoded.image)
        suffix = "  -  preview" if fallback else ""
        self.info_label.setText(f"{decoded.path.name}  ·  {decoded.width} × {decoded.height}{suffix}")
        self.image_view.set_pixmap(self._pixmap, smooth=False)
        self._schedule_smooth_fit()

    def set_video(self, path: Path, preview: QImage | None = None) -> None:
        self._path = path
        self._is_video = True
        self._is_fallback = True
        self.video_player.stop()
        self.video_player.setSource(QUrl.fromLocalFile(str(path)))
        self.video_seek.setRange(0, 0)
        self.video_time_label.setText("0:00 / 0:00")
        self.video_play_button.setIcon(_fomantic_icon("play", 12))
        self.video_controls.setParent(self.media_panel)
        self.video_controls.show()
        self._position_video_controls()
        self.media_stack.setCurrentWidget(self.image_view)
        if preview is not None and not preview.isNull():
            self._pixmap = QPixmap.fromImage(preview)
            self.image_view.set_pixmap(self._pixmap, smooth=False)
        self.info_label.setText(path.name)

    def set_video_preview(self, path: Path, preview: QImage) -> None:
        if self._is_video and self._path == path and self.media_stack.currentWidget() is self.image_view:
            self._pixmap = QPixmap.fromImage(preview)
            self.image_view.set_pixmap(self._pixmap, smooth=False)

    def _toggle_video_playback(self) -> None:
        if not self._is_video:
            return
        if self.video_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.video_player.pause()
        else:
            self.video_controls.setParent(self.video_widget)
            self.media_stack.setCurrentWidget(self.video_widget)
            self._position_video_controls()
            self.video_controls.show()
            self.video_player.play()

    def _video_position_changed(self, position: int) -> None:
        if not self.video_seek.isSliderDown():
            self.video_seek.setValue(position)
        self._update_video_time(position, self.video_player.duration())
        if self.video_controls.isVisible():
            self.video_controls.raise_()

    def _video_duration_changed(self, duration: int) -> None:
        self.video_seek.setRange(0, max(0, duration))
        self._update_video_time(self.video_player.position(), duration)

    def _update_video_time(self, position: int, duration: int) -> None:
        def format_time(milliseconds: int) -> str:
            seconds = max(0, milliseconds // 1000)
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

        self.video_time_label.setText(f"{format_time(position)} / {format_time(duration)}")

    def _video_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.video_play_button.setIcon(_fomantic_icon("pause" if playing else "play", 12))
        self.videoPlaybackChanged.emit(playing)

    @property
    def is_fallback(self) -> bool:
        return self._is_fallback

    @property
    def has_image(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def _position_video_controls(self) -> None:
        host = self.video_controls.parentWidget()
        if host is not self.media_panel and host is not self.video_widget:
            return
        height = self.video_controls.sizeHint().height()
        self.video_controls.resize(self.video_controls.width(), height)
        self.video_controls.move(
            max(8, (host.width() - self.video_controls.width()) // 2),
            max(8, host.height() - height - 14),
        )
        self.video_controls.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize and obj is self.video_controls.parentWidget():
            self._position_video_controls()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.image_view.update()
        QTimer.singleShot(0, self._position_video_controls)
        QTimer.singleShot(0, self._position_face_filter_chip)

    def _position_face_filter_chip(self) -> None:
        if not self.face_filter_chip.isVisible():
            return
        self.face_filter_chip.adjustSize()
        self.face_filter_chip.move(
            max(8, self.media_panel.width() - self.face_filter_chip.width() - 12), 12
        )
        self.face_filter_chip.raise_()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in {Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.exitRequested.emit()
        elif key == Qt.Key.Key_Down and self.series_panel.isVisible():
            self._move_series(1)
        elif key == Qt.Key.Key_Up and self.series_panel.isVisible():
            self._move_series(-1)
        elif key == Qt.Key.Key_Space and self._is_video:
            self._toggle_video_playback()
        elif key in {Qt.Key.Key_Right, Qt.Key.Key_Space}:
            self.nextRequested.emit()
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Backspace}:
            self.previousRequested.emit()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.exitRequested.emit()

    def _fast_fit(self) -> None:
        self.image_view.set_smooth(False)
        
    def _smooth_fit(self) -> None:
        self.image_view.set_smooth(True)

    def _schedule_smooth_fit(self) -> None:
        self._smooth_timer.start(140)

    def begin_fast_resize(self) -> None:
        self._smooth_timer.stop()
        self.image_view.set_smooth(False)

    def finish_fast_resize(self) -> None:
        self._schedule_smooth_fit()


class FullImageView(QWidget):
    faceClicked = Signal(object, QPoint)

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._smooth = False
        self._faces: list[dict] = []
        self._hovered_face = -1
        self.setMinimumSize(1, 1)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)

    def set_pixmap(self, pixmap: QPixmap, *, smooth: bool) -> None:
        self._pixmap = pixmap
        self._smooth = smooth
        self.update()

    def set_smooth(self, smooth: bool) -> None:
        if self._smooth == smooth:
            return
        self._smooth = smooth
        self.update()

    def set_faces(self, faces: list[dict] | None) -> None:
        self._faces = [face for face in (faces or []) if isinstance(face, dict)]
        self._hovered_face = -1
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._face_at(event.position())
        if hit != self._hovered_face:
            self._hovered_face = hit
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hovered_face >= 0:
            self._hovered_face = -1
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._face_at(event.position())
            if hit >= 0:
                self.faceClicked.emit(self._faces[hit], event.position().toPoint())
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def _face_at(self, position) -> int:
        if self._pixmap is None or self._pixmap.isNull():
            return -1
        image_rect = _fit_rect(self._pixmap.size(), self.size())
        for index, face in enumerate(self._faces):
            rect = self._face_rect(face, image_rect)
            if rect.contains(position):
                return index
        return -1

    @staticmethod
    def _face_rect(face: dict, image_rect: QRect) -> QRectF:
        bbox = face.get("bbox") or {}
        try:
            x, y = float(bbox["x"]), float(bbox["y"])
            width, height = float(bbox["width"]), float(bbox["height"])
        except (KeyError, TypeError, ValueError):
            return QRectF()
        return QRectF(
            image_rect.left() + x * image_rect.width(), image_rect.top() + y * image_rect.height(),
            width * image_rect.width(), height * image_rect.height(),
        )

    def face_avatar(self, face: dict, size: int) -> QPixmap:
        """Return a circular crop of a detected face for chips and face sets."""
        if self._pixmap is None or self._pixmap.isNull():
            return QPixmap()
        return self.face_avatar_from_pixmap(self._pixmap, face, size)

    @staticmethod
    def face_avatar_from_pixmap(pixmap: QPixmap, face: dict, size: int) -> QPixmap:
        """Crop a face from the decoded source instead of a UI thumbnail."""
        if pixmap.isNull():
            return QPixmap()
        bbox = face.get("bbox") or {}
        try:
            source = QRectF(
                float(bbox["x"]) * pixmap.width(),
                float(bbox["y"]) * pixmap.height(),
                float(bbox["width"]) * pixmap.width(),
                float(bbox["height"]) * pixmap.height(),
            ).toAlignedRect().intersected(pixmap.rect())
        except (KeyError, TypeError, ValueError):
            return QPixmap()
        if source.isEmpty():
            return QPixmap()
        crop = pixmap.copy(source).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        result = QPixmap(size, size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(0, 0, size, size)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, crop)
        painter.end()
        return result

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101010"))
        if self._pixmap is None or self._pixmap.isNull():
            painter.end()
            return
        if self._smooth:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        target = _fit_rect(self._pixmap.size(), self.size())
        painter.drawPixmap(target, self._pixmap)
        if 0 <= self._hovered_face < len(self._faces):
            face_rect = self._face_rect(self._faces[self._hovered_face], target)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(138, 180, 248, 242), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(face_rect, 4, 4)
        painter.end()


class ChromeTitleBar(QFrame):
    """Draggable client-side title strip used by the tab host."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self.window = window
        self._drag_offset: QPoint | None = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self.window.isMaximized():
            self._drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()
        super().mouseDoubleClickEvent(event)


class ChromeTabBar(QTabBar):
    """Chrome-like tab widths instead of Qt's full-row expansion."""
    closeRequested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._tab_width = 220

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802
        return QSize(self._tab_width, 38)

    def set_tab_width(self, width: int) -> None:
        width = max(72, width)
        if width == self._tab_width:
            return
        self._tab_width = width
        self.updateGeometry()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#000000"))
        for index in range(self.count()):
            rect = QRectF(self.tabRect(index)).adjusted(1, 0, -1, 0)
            selected = index == self.currentIndex()
            if selected:
                top_radius = 12.0
                path = QPainterPath()
                path.moveTo(rect.left() + top_radius, rect.top())
                path.lineTo(rect.right() - top_radius, rect.top())
                path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + top_radius)
                path.lineTo(rect.right(), rect.bottom())
                path.lineTo(rect.left(), rect.bottom())
                path.lineTo(rect.left(), rect.top() + top_radius)
                path.quadTo(rect.left(), rect.top(), rect.left() + top_radius, rect.top())
                path.closeSubpath()
                painter.fillPath(path, QColor("#1f1f1f"))
            elif index:
                painter.setPen(QPen(QColor("#303030"), 1))
                painter.drawLine(int(rect.left()), 8, int(rect.left()), self.height() - 8)
            has_close = self.count() > 1
            text_rect = rect.toRect().adjusted(14, 0, -25 if has_close else -14, 0)
            painter.setPen(QColor("#f0f0f0") if selected else QColor("#b5b5b5"))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.fontMetrics().elidedText(self.tabText(index), Qt.TextElideMode.ElideRight, text_rect.width()))
            if has_close:
                close_rect = self._close_rect(index)
                pen = QPen(QColor("#e0e0e0") if selected else QColor("#a8a8a8"), 1.5)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(close_rect.left() + 3, close_rect.top() + 3, close_rect.right() - 3, close_rect.bottom() - 3)
                painter.drawLine(close_rect.right() - 3, close_rect.top() + 3, close_rect.left() + 3, close_rect.bottom() - 3)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.count() > 1 and event.button() == Qt.MouseButton.LeftButton:
            for index in range(self.count()):
                if self._close_rect(index).contains(event.position().toPoint()):
                    self.closeRequested.emit(index)
                    event.accept()
                    return
        super().mouseReleaseEvent(event)

    def _close_rect(self, index: int) -> QRect:
        rect = self.tabRect(index)
        return QRect(rect.right() - 20, rect.center().y() - 6, 12, 12)


def _fit_rect(source: QSize, bounds: QSize) -> QRect:
    if source.width() <= 0 or source.height() <= 0 or bounds.width() <= 0 or bounds.height() <= 0:
        return QRect()
    scale = min(bounds.width() / source.width(), bounds.height() / source.height())
    width = max(1, round(source.width() * scale))
    height = max(1, round(source.height() * scale))
    return QRect((bounds.width() - width) // 2, (bounds.height() - height) // 2, width, height)


class WindowsTaskbarProgress:
    """Optional Windows 7+ ITaskbarList3 progress integration."""

    _TBPF_NORMAL = 0x2
    _TBPF_NOPROGRESS = 0x0

    def __init__(self) -> None:
        self._taskbar = None
        self._com_initialized = False
        if sys.platform != "win32":
            return
        try:
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
                ]

            def guid(value: str) -> GUID:
                import uuid
                return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)

            ole32 = ctypes.OleDLL("ole32")
            ole32.CoInitialize.argtypes = [ctypes.c_void_p]
            self._com_initialized = ole32.CoInitialize(None) >= 0
            ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
            ]
            taskbar_base = ctypes.c_void_p()
            result = ole32.CoCreateInstance(
                ctypes.byref(guid("56FDF344-FD6D-11D0-958A-006097C9A090")), None, 1,
                ctypes.byref(guid("56FDF342-FD6D-11D0-958A-006097C9A090")), ctypes.byref(taskbar_base),
            )
            if result >= 0:
                base_vtable = ctypes.cast(taskbar_base, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                query_interface = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
                )(base_vtable[0])
                taskbar = ctypes.c_void_p()
                result = query_interface(
                    taskbar_base, ctypes.byref(guid("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF")), ctypes.byref(taskbar)
                )
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(base_vtable[2])
                release(taskbar_base)
                if result < 0:
                    return
                vtable = ctypes.cast(taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                initialize = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[3])
                if initialize(taskbar) >= 0:
                    self._taskbar = taskbar
        except (AttributeError, OSError):
            pass

    def close(self) -> None:
        if self._taskbar is not None:
            vtable = ctypes.cast(self._taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            release(self._taskbar)
            self._taskbar = None
        if self._com_initialized:
            ctypes.OleDLL("ole32").CoUninitialize()
            self._com_initialized = False

    def set_progress(self, window_id: int, value: int, total: int) -> None:
        if self._taskbar is None or not window_id:
            return
        try:
            vtable = ctypes.cast(self._taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            set_value = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong
            )(vtable[9])
            set_state = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong
            )(vtable[10])
            window = ctypes.c_void_p(window_id)
            if total > 0:
                set_state(self._taskbar, window, self._TBPF_NORMAL)
                set_value(self._taskbar, window, max(0, value), total)
            else:
                set_state(self._taskbar, window, self._TBPF_NOPROGRESS)
        except (OSError, ValueError):
            self._taskbar = None


class MacDockProgress:
    """Use the native Dock tile badge when the optional PyObjC bridge exists."""

    def __init__(self) -> None:
        self._tile = None
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
            self._tile = NSApp().dockTile()
        except (ImportError, AttributeError):
            pass

    def set_progress(self, value: int, total: int) -> None:
        if self._tile is None:
            return
        self._tile.setBadgeLabel_(f"{round(value / total * 100)}%" if total else None)
        self._tile.display()


def _safe_folder_name(title: str) -> str:
    """Turn a shooting title into a filesystem-safe folder name."""
    cleaned = "".join(c if c not in '<>:"/\\|?*' else "_" for c in str(title or "").strip())
    cleaned = cleaned.rstrip(". ").strip()
    return cleaned or "shotsync"


def _shotsync_photo_filename(photo: dict) -> str:
    """Basename of a ShotSync photo payload, without any path components."""
    raw = str(photo.get("name") or "").replace("\\", "/")
    return Path(raw).name.strip()


class Workspace(QMainWindow):
    fullViewRequested = Signal(object)
    fullscreenRequested = Signal(object)
    gridRequested = Signal()
    openFolderRequested = Signal(object)   # Path: open (or focus) a folder tab

    def __init__(self, initial_directory: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("RAWww")
        self.resize(1440, 920)
        self.closing = False
        self._taskbar_progress = WindowsTaskbarProgress()
        self._dock_progress = MacDockProgress()

        self.current_decode_executor: ProcessPoolExecutor | None = None
        self.background_decode_executor: ProcessPoolExecutor | None = None
        self.visible_thumb_decode_executor: ProcessPoolExecutor | None = None
        self.background_cache_lookup_executor = ThreadPoolExecutor(max_workers=1)
        self.visible_thumb_cache_lookup_executor = ThreadPoolExecutor(max_workers=VISIBLE_THUMB_LOOKUP_WORKERS)
        self.directory_scan_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_load_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_flush_executor = ThreadPoolExecutor(max_workers=1)
        self.bridge = DecodeBridge()
        self.bridge.decoded.connect(self._on_decoded)
        self.bridge.failed.connect(self._on_decode_failed)
        self.bridge.cacheLoaded.connect(self._on_cache_loaded)
        self.bridge.directoryScanned.connect(self._on_directory_scanned)
        self.bridge.metadataUpdated.connect(self._on_metadata_updated)
        self.video_thumbnailer = VideoThumbnailer(self)
        self.video_thumbnailer.previewReady.connect(self._on_video_preview)
        self.pending: dict[tuple[Path, int], Future] = {}
        self.foreground_full_futures: dict[tuple[Path, int], Future] = {}
        self.last_navigation_at = 0.0
        self.pending_full_request: Path | None = None
        self.pending_grid_full_request: Path | None = None
        self.populate_index = 0
        # ``paths`` is the current view order.  Thumbnail scheduling only uses
        # this order and never owns it, so sorting/filtering can replace the
        # view later without changing the scheduler.
        self.thumb_index = 0
        self.thumb_priority: deque[Path] = deque()
        self.thumb_priority_set: set[Path] = set()
        self.visible_thumb_pending: set[tuple[Path, int]] = set()
        self.cache_ready = False
        self.cache_load_generation = 0
        self.directory_generation = 0
        self.memory_cache: OrderedDict[tuple[Path, int], DecodedImage] = OrderedDict()
        self.thumbnail_cache: OrderedDict[Path, QImage] = OrderedDict()
        self.thumbnail_cache_bytes = 0
        self.items_by_path: dict[Path, QListWidgetItem] = {}
        self.all_paths: list[Path] = []
        # ``view_paths`` is the complete filtered order. ``paths`` is only
        # the grid representation and may collapse adjacent AI series.
        self.view_paths: list[Path] = []
        self.view_generation = 0
        self.paths: list[Path] = []
        self.series_cards: dict[Path, dict] = {}
        self.expanded_series: set[Path] = set()
        self.photo_details: dict[str, dict] = {}
        self.image_embeddings: dict[str, bytes] = {}
        self.settings = QSettings("RAWww", "RAWww")

        # ShotSync cloud integration. The key is remembered between launches
        # and validated lazily the first time the ShotSync disk is opened.
        self.shotsync_active = False
        self._shotsync_checked = False
        self.shotsync_client = ShotSyncClient(SHOTSYNC_BASE_URL, self)
        self.shotsync_client.set_api_key(self.settings.value("shotsync/api_key", "", str))
        self.shotsync_client.loginSucceeded.connect(self._shotsync_login_succeeded)
        self.shotsync_client.loginFailed.connect(self._shotsync_login_failed)
        self.shotsync_client.sessionVerified.connect(self._shotsync_session_verified)
        self.shotsync_client.sessionInvalid.connect(self._shotsync_session_invalid)
        self.shotsync_client.shootingsLoaded.connect(self._shotsync_shootings_loaded)
        self.shotsync_client.shootingsFailed.connect(self._shotsync_shootings_failed)
        self.shotsync_client.avatarLoaded.connect(self._shotsync_avatar_loaded)

        # A single socket is shared by every tab (see shotsync_hub). The hub
        # keeps a live connection whenever a key is stored and pushes photo
        # arrivals / mark changes back to whichever tab shows the folder.
        self.shotsync = shotsync_hub(SHOTSYNC_BASE_URL)
        self.shotsync.set_api_key(self.shotsync_client.api_key)
        self.shotsync.photoDownloaded.connect(self._on_shotsync_photo_downloaded)
        self.shotsync.markUpdated.connect(self._on_shotsync_mark_updated)
        self.shotsync.receivingChanged.connect(self._refresh_shotsync_receiving)
        self.shotsync.downloader.finished.connect(self._on_shotsync_selection_ready)
        self.shotsync.downloader.failed.connect(self._on_shotsync_selection_failed)
        self.shotsync.downloader.progress.connect(self._on_shotsync_selection_progress)

        # Mark-syncer for the currently open folder, if it is a ShotSync
        # selection copy. Recreated whenever the folder changes.
        self._shotsync_syncer = None

        quick_kind = self.settings.value("quick_mark_kind", "rating", str)
        quick_value = (
            self.settings.value("quick_mark_value", 5, int)
            if quick_kind == "rating"
            else self.settings.value("quick_mark_value", "", str)
        )

        if quick_kind not in {"rating", "color_label"}:
            quick_kind, quick_value = "rating", 5
        self.quick_mark: tuple[str, object] = (quick_kind, quick_value)
        self.auto_advance = self.settings.value("auto_advance", False, bool)
        stored_face_filter = self.settings.value("face_filter_embedding", "", str)
        try:
            stored_embedding = json.loads(stored_face_filter) if stored_face_filter else None
        except (TypeError, ValueError):
            stored_embedding = None
        self.face_reference: list[float] | None = stored_embedding if isinstance(stored_embedding, list) else None
        self.face_filter_avatar = self._face_avatar_from_entry({
            "avatar": self.settings.value("face_filter_avatar", "", str),
        })
        self.face_sets = self._load_face_sets()
        self.last_move_direction = 1
        self.current_dir = initial_directory or self._initial_directory()
        thumbnail_size = max(0, min(3, self.settings.value("thumbnail_size", 1, int)))
        self.workspace_state = WorkspaceState(self.current_dir, thumbnail_size=thumbnail_size)
        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_watcher.directoryChanged.connect(self._folder_changed)
        self.folder_change_timer = QTimer(self)
        self.folder_change_timer.setSingleShot(True)
        self.folder_change_timer.timeout.connect(self._reload_changed_folder)
        self.current_path: Path | None = None
        self.workspace_active = False
        self._folder_context_active = False
        self._pending_folder_grid_context: tuple[list[str], int] | None = None
        self.folder_cache: FolderCache | None = None
        self.ai_pipeline = AiPipeline()
        self.metadata_pipeline = MetadataPipeline()
        self.ai_progress_total = 0
        self.preview_progress_total = 0
        self.fast_fullscreen = False
        self.normal_geometry = None
        self.normal_window_flags = self.windowFlags()
        self.normal_window_state = self.windowState()

        self.stack = QStackedWidget()
        self.grid_page = self._build_grid_page()
        self.full_view = FullView()
        self.full_view.exitRequested.connect(self.show_grid)
        self.full_view.nextRequested.connect(self.next_image)
        self.full_view.previousRequested.connect(self.previous_image)
        self.full_view.pathRequested.connect(self.open_full)
        self.full_view.videoPlaybackChanged.connect(self._video_playback_changed)
        self.full_view.stripViewportChanged.connect(self._prioritize_visible_full_strip_thumbs)
        self.full_view.ratingRequested.connect(self._set_selected_rating)
        self.full_view.colorRequested.connect(self._set_selected_color)
        self.full_view.faceShowRequested.connect(self._filter_face_from_full_view)
        self.full_view.faceAddRequested.connect(self._add_face_to_set)
        self.full_view.faceFilterClearRequested.connect(self._clear_face_search)
        self.full_view.seriesToggleRequested.connect(self._toggle_grid_series)
        self.full_view.quickMarkRequested.connect(self._apply_quick_mark)
        self.full_view.quickMarkConfigured.connect(self._configure_quick_mark)
        self.full_view.autoAdvanceChanged.connect(self._set_auto_advance)
        self.full_view.set_quick_mark(*self.quick_mark)
        self.full_view.set_auto_advance(self.auto_advance)
        self.full_view.commentSubmitted.connect(self._save_full_comment)
        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.full_view)
        self.setCentralWidget(self.stack)
        self._install_grid_page_key_filters()

        # Qt exposes mounted volumes on all supported platforms. Polling is
        # intentional here: QStorageInfo has no cross-platform mount-change
        # signal, while a short interval also catches card insertion into an
        # already connected reader.
        self.volume_refresh_timer = QTimer(self)
        self.volume_refresh_timer.setInterval(VOLUME_REFRESH_INTERVAL_MS)
        self.volume_refresh_timer.timeout.connect(self._refresh_volume_buttons)
        self.volume_refresh_timer.start()

        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self.flush_timer.timeout.connect(lambda: self._flush_folder_cache(wait=False))
        self.flush_timer.start()
        self.full_request_timer = QTimer(self)
        self.full_request_timer.setSingleShot(True)
        self.full_request_timer.timeout.connect(self._submit_pending_full_request)
        self.grid_full_request_timer = QTimer(self)
        self.grid_full_request_timer.setSingleShot(True)
        self.grid_full_request_timer.timeout.connect(self._submit_pending_grid_full_request)
        self.populate_timer = QTimer(self)
        self.populate_timer.setInterval(WORK_PUMP_INTERVAL_MS)
        self.populate_timer.timeout.connect(self._populate_next_items)
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setInterval(WORK_PUMP_INTERVAL_MS)
        self.thumb_timer.timeout.connect(self._submit_next_thumbs)
        self.visible_thumb_timer = QTimer(self)
        self.visible_thumb_timer.setSingleShot(True)
        self.visible_thumb_timer.timeout.connect(self._prioritize_visible_thumbs)
        self.ai_progress_timer = QTimer(self)
        self.ai_progress_timer.setInterval(250)
        self.ai_progress_timer.timeout.connect(self._update_ai_progress)

        self._create_actions()
        QTimer.singleShot(0, lambda: self.load_directory(self.current_dir))
        QTimer.singleShot(0, self._restore_face_filter_chip)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Future callbacks run on worker threads. Mark shutdown before stopping
        # executors so a completed cache lookup cannot enqueue a decode into an
        # executor that has already been shut down.
        self.closing = True
        self._set_taskbar_progress(0, 0)
        self._taskbar_progress.close()
        self._save_folder_grid_context()
        self.workspace_state.close()
        self.flush_timer.stop()
        self.full_request_timer.stop()
        self.grid_full_request_timer.stop()
        self.populate_timer.stop()
        self.thumb_timer.stop()
        self.ai_progress_timer.stop()
        self.folder_change_timer.stop()
        self.volume_refresh_timer.stop()
        self._flush_folder_cache(wait=False, close=True)
        self._detach_shotsync_syncer()
        self.folder_cache = None
        self.cache_ready = False
        if self.current_decode_executor is not None:
            self.current_decode_executor.shutdown(wait=False, cancel_futures=True)
        if self.background_decode_executor is not None:
            self.background_decode_executor.shutdown(wait=False, cancel_futures=True)
        if self.visible_thumb_decode_executor is not None:
            self.visible_thumb_decode_executor.shutdown(wait=False, cancel_futures=True)
        self.background_cache_lookup_executor.shutdown(wait=False, cancel_futures=True)
        self.visible_thumb_cache_lookup_executor.shutdown(wait=False, cancel_futures=True)
        self.directory_scan_executor.shutdown(wait=False, cancel_futures=True)
        self.cache_load_executor.shutdown(wait=False, cancel_futures=True)
        self.cache_flush_executor.shutdown(wait=False, cancel_futures=False)
        self.metadata_pipeline.shutdown()
        self.ai_pipeline.shutdown()
        super().closeEvent(event)

    def set_workspace_active(self, active: bool) -> None:
        """Run preview generation only for the tab currently on screen."""
        if self.workspace_active == active:
            return
        self.workspace_active = active
        self.video_thumbnailer.set_active(active)
        if not active:
            self.populate_timer.stop()
            self.thumb_timer.stop()
            self.visible_thumb_timer.stop()
            self.grid_full_request_timer.stop()
            self.full_request_timer.stop()
            self.pending_full_request = None
            self.pending_grid_full_request = None
            for future in self.pending.values():
                future.cancel()
            self.pending.clear()
            self.foreground_full_futures.clear()
            self.visible_thumb_pending.clear()
            self.full_view.video_player.pause()
            return
        if self.populate_index < len(self.paths):
            self.populate_timer.start()
        self._schedule_visible_thumb_priority()
        if self.thumb_priority or self.thumb_index < len(self.paths):
            self.thumb_timer.start()

    def _video_playback_changed(self, playing: bool) -> None:
        """Never decode grid-video frames while a full video is playing."""
        self.video_thumbnailer.set_active(self.workspace_active and not playing)
        if not playing and self.workspace_active:
            self._schedule_visible_thumb_priority()

    def _build_grid_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.dir_model = QFileSystemModel(self)
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath(QDir.rootPath())

        # Create a custom model that correctly reports children only for folders with subdirectories
        class CleanDirModel(QFileSystemModel):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._new_folder_path: Path | None = None

            def hasChildren(self, parent=None):
                # Only show expand arrows if the folder actually contains other folders
                if not parent or not parent.isValid():
                    return super().hasChildren(parent)
                path = self.filePath(parent)
                if not path:
                    return super().hasChildren(parent)
                qdir = QDir(path)
                subdirs = qdir.entryList(QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot)
                if not subdirs:
                    return False
                return super().hasChildren(parent)

            def flags(self, index):
                default_flags = super().flags(index)
                if not index.isValid():
                    return default_flags
                # Разрешаем редактирование только для свежесозданной папки
                if self._new_folder_path and self.filePath(index) == str(self._new_folder_path):
                    return default_flags | Qt.ItemFlag.ItemIsEditable
                # Для всех остальных запрещаем
                return default_flags & ~Qt.ItemFlag.ItemIsEditable

            def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
                if role == Qt.ItemDataRole.EditRole:
                    old_path_str = self.filePath(index)
                    old_path = Path(old_path_str)
                    new_name = str(value).strip()

                    # Если имя не изменилось или пустое, отменяем
                    if not new_name or new_name == old_path.name:
                        self._new_folder_path = None # Сбрасываем в любом случае
                        return False

                    new_path = old_path.parent / new_name
                    if new_path.exists():
                        QMessageBox.warning(None, "Ошибка", "Папка с таким именем уже существует.")
                        self._new_folder_path = None # Сбрасываем
                        return False

                    # Переименовываем
                    if QDir().rename(old_path_str, str(new_path)):
                        self._new_folder_path = None # Успех, сбрасываем
                        return True

                    self._new_folder_path = None # Ошибка, сбрасываем
                    return False
                return super().setData(index, value, role)
        
        self.dir_model = CleanDirModel(self)
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath(QDir.rootPath())
        
        self.dir_tree = QTreeView()
        self.dir_tree.setModel(self.dir_model)
        # Включаем возможность редактирования элементов дерева
        self.dir_tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._folder_name_editor: FolderNameEditor | None = None
        self._set_tree_root_for_path(self.current_dir.anchor or QDir.rootPath())
        for column in range(1, self.dir_model.columnCount()):
            self.dir_tree.hideColumn(column)
        self.dir_tree.clicked.connect(self._directory_selected)
        self.dir_tree.setHeaderHidden(True)
        self.dir_tree.setMinimumWidth(260)

        self.grid = PhotoGrid()
        self.grid.card_size = self.workspace_state.thumbnail_size
        self.grid._last_icon_size = QSize()
        self.grid._update_card_size()
        self.grid.cardSizeChanged.connect(self._remember_thumbnail_size)
        self.grid.openRequested.connect(self.open_full)
        self.grid.seriesToggleRequested.connect(self._toggle_grid_series)
        self.grid.currentItemChanged.connect(self._grid_current_item_changed)
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_visible_thumb_priority)
        self.grid.viewportChanged.connect(self._schedule_visible_thumb_priority)

        splitter = QSplitter()
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(8)
        self.drive_buttons = QButtonGroup(self)
        self.drive_buttons.setExclusive(True)
        self.volume_icon_provider = QFileIconProvider()
        self.removable_volume_icon = _removable_volume_icon()
        self.volume_removability: dict[str, bool] = {}
        self.drive_button_layout = QHBoxLayout()
        self.drive_button_layout.setContentsMargins(0, 0, 0, 0)
        self.drive_button_layout.setSpacing(4)
        self.drive_button_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        sidebar_layout.addLayout(self.drive_button_layout)

        # Persistent "disk" that opens the ShotSync cloud instead of a local
        # volume. It shares the exclusive drive-button group so selecting it
        # visually deselects the local volumes and vice versa.
        self.shotsync_button = QToolButton()
        self.shotsync_button.setObjectName("driveButton")
        self.shotsync_button.setCheckable(True)
        self.shotsync_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.shotsync_button.setIconSize(QSize(20, 20))
        self.shotsync_button.setProperty("volumeKey", SHOTSYNC_VOLUME_KEY)
        self.shotsync_button.setText("ShotSync")
        self.shotsync_button.setToolTip("Съёмки ShotSync (shotsync.ru)")
        self.shotsync_button.setIcon(self._shotsync_button_icon())
        self.shotsync_button.clicked.connect(lambda: self._activate_shotsync())
        self.drive_buttons.addButton(self.shotsync_button)
        self.drive_button_layout.addWidget(self.shotsync_button)
        self._register_grid_page_focus_widget(self.shotsync_button)

        self._refresh_volume_buttons()

        # Создаем тулбар с кнопками навигации над деревом папок
        folder_toolbar = QWidget()
        folder_toolbar.setObjectName("folderToolbar")
        folder_toolbar_layout = QHBoxLayout(folder_toolbar)
        folder_toolbar_layout.setContentsMargins(0, 0, 0, 8)
        folder_toolbar_layout.setSpacing(4)
        
        # Кнопка "На уровень вверх"
        self.up_button = QToolButton()
        self.up_button.setObjectName("folderToolButton")
        self.up_button.setIcon(_sidebar_tool_icon("up"))
        self.up_button.setIconSize(QSize(16, 16))
        self.up_button.setToolTip("На уровень вверх")
        self.up_button.clicked.connect(self._go_up_directory)
        folder_toolbar_layout.addWidget(self.up_button)
        
        # Кнопка "Создать папку"
        self.new_folder_button = QToolButton()
        self.new_folder_button.setObjectName("folderToolButton")
        self.new_folder_button.setIcon(_sidebar_tool_icon("new-folder"))
        self.new_folder_button.setIconSize(QSize(16, 16))
        self.new_folder_button.setToolTip("Создать папку")
        self.new_folder_button.clicked.connect(self._create_new_folder)
        folder_toolbar_layout.addWidget(self.new_folder_button)
        
        # Выравниваем кнопки по левому краю
        folder_toolbar_layout.addStretch()
        
        # The sidebar body swaps between the local folder browser and the
        # ShotSync cloud panel depending on which "disk" is selected.
        self.sidebar_stack = QStackedWidget()

        local_page = QWidget()
        local_layout = QVBoxLayout(local_page)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(8)
        local_layout.addWidget(folder_toolbar)
        local_layout.addWidget(self.dir_tree, 1)

        self.shotsync_panel = ShotSyncPanel(icon_provider=_fomantic_icon)
        self.shotsync_panel.loginSubmitted.connect(self._shotsync_login)
        self.shotsync_panel.logoutRequested.connect(self._shotsync_logout)
        self.shotsync_panel.receiveRequested.connect(self._shotsync_receive_requested)
        self.shotsync_panel.selectRequested.connect(self._shotsync_select_requested)

        self.sidebar_stack.addWidget(local_page)
        self.sidebar_stack.addWidget(self.shotsync_panel)
        self.sidebar_stack.setCurrentWidget(local_page)
        self._sidebar_local_page = local_page

        sidebar_layout.addWidget(self.sidebar_stack, 1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("viewerToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(7)

        filter_panel = QWidget()
        filter_panel.setObjectName("viewerFiltersPanel")
        filter_layout = QHBoxLayout(filter_panel)
        filter_layout.setContentsMargins(7, 4, 7, 4)
        filter_layout.setSpacing(6)
        filter_icon = QLabel()
        filter_icon.setPixmap(_fomantic_icon("filter", 14, "#a8b0bd").pixmap(QSize(14, 14)))
        filter_layout.addWidget(filter_icon)

        
        self.rating_filter = QComboBox()
        self.rating_filter.addItem("Все рейтинги", None)
        self.rating_filter.setItemIcon(0, _fomantic_icon("star", 12, "#a8b0bd"))
        for rating in range(5, 0, -1):
            self.rating_filter.addItem("★" * rating, rating)
            self.rating_filter.setItemIcon(self.rating_filter.count() - 1, _fomantic_icon("star", 12, "#a8b0bd"))
        self.rating_filter.setFixedWidth(148)
        self.color_filter = QComboBox()
        for label, value in (("Все цвета", None), ("Без цвета", ""), ("Красный", "red"), ("Жёлтый", "yellow"), ("Зелёный", "green"), ("Синий", "blue"), ("Фиолетовый", "purple")):
            self.color_filter.addItem(label, value)
            if value is not None:
                self.color_filter.setItemIcon(self.color_filter.count() - 1, _color_swatch_icon(value or None))
        self.color_filter.setItemIcon(0, _fomantic_icon("brush", 12, "#a8b0bd"))
        self.color_filter.setFixedWidth(148)
        self.media_filter = QComboBox()
        for label, value in (("Фото и видео", None), ("Фото", "image"), ("Видео", "video")):
            self.media_filter.addItem(label, value)
        self.media_filter.setItemIcon(0, _fomantic_icon("media", 12, "#a8b0bd"))
        self.media_filter.setItemIcon(1, _fomantic_icon("images", 12, "#a8b0bd"))
        self.media_filter.setItemIcon(2, _fomantic_icon("film", 12, "#a8b0bd"))
        self.media_filter.setFixedWidth(148)
        self.camera_filter = QComboBox()
        self.camera_filter.addItem("Все камеры", None)
        self.camera_filter.setItemIcon(0, _fomantic_icon("images", 12, "#a8b0bd"))
        self.camera_filter.setFixedWidth(170)
        self.shot_filter = QComboBox()
        for label, value in (("Все планы", None), ("Крупный", "closeup"), ("Средний", "medium"), ("Общий", "wide"), ("Без лиц", "no_face")):
            self.shot_filter.addItem(label, value)
        self.shot_filter.hide()
        self.sort_combo = QComboBox()
        for label, value in (("По имени ↑", "name"), ("По имени ↓", "name_desc"), ("По времени ↑", "time"), ("По времени ↓", "time_desc"), ("По рейтингу", "rating")):
            self.sort_combo.addItem(label, value)
        self.sort_combo.setItemIcon(0, _fomantic_icon("sort", 12, "#a8b0bd"))
        for index in range(1, self.sort_combo.count()):
            self.sort_combo.setItemIcon(index, _fomantic_icon("sort", 12, "#a8b0bd"))
        self.sort_combo.setCurrentIndex(self.sort_combo.findData("time"))
        self.sort_combo.setFixedWidth(148)
        search_box = QWidget()
        search_box.setObjectName("viewerSearchBox")
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(5, 0, 5, 0)
        search_layout.setSpacing(3)
        search_icon = QLabel()
        search_icon.setObjectName("viewerSearchIcon")
        search_icon.setPixmap(_fomantic_icon("search", 14, "#a8b0bd").pixmap(QSize(14, 14)))
        search_layout.addWidget(search_icon)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по имени или комментарию")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(148)
        search_layout.addWidget(self.search_edit)
        for control in (self.rating_filter, self.color_filter, self.media_filter, self.camera_filter, self.shot_filter, self.sort_combo):
            control.currentIndexChanged.connect(self._apply_view)
            filter_layout.addWidget(control)
        self.search_edit.textChanged.connect(self._apply_view)
        filter_layout.addWidget(search_box)

        self.face_filter_chip = QFrame()
        self.face_filter_chip.setObjectName("fullFaceFilterChip")
        chip_layout = QHBoxLayout(self.face_filter_chip)
        chip_layout.setContentsMargins(5, 3, 4, 3)
        chip_layout.setSpacing(4)
        self.face_filter_avatar_label = QLabel()
        self.face_filter_avatar_label.setFixedSize(26, 26)
        self.face_filter_avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip_layout.addWidget(self.face_filter_avatar_label)
        self.face_clear_button = QToolButton()
        self.face_clear_button.setObjectName("fullFaceFilterClear")
        self.face_clear_button.setIcon(_fomantic_icon("close", 12))
        self.face_clear_button.setFixedSize(20, 20)
        self.face_clear_button.setIconSize(QSize(12, 12))
        self.face_clear_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.face_clear_button.setAutoRaise(True)
        self.face_clear_button.setToolTip("Сбросить фильтр по лицу")
        self.face_clear_button.clicked.connect(self._clear_face_search)
        chip_layout.addWidget(self.face_clear_button)
        self.face_filter_chip.hide()
        filter_layout.addWidget(self.face_filter_chip)

        self.ai_button = QPushButton("Обработать новые фото")
        self.ai_button.clicked.connect(self._start_ai_analysis)
        toolbar_layout.addWidget(self.ai_button)
        for icon, delta in (("zoom-out", -1), ("zoom", 1)):
            button = QToolButton()
            button.setIcon(_fomantic_icon(icon, 13))
            button.setToolTip("Размер превью")
            button.clicked.connect(lambda _checked=False, d=delta: self.grid.change_card_size(d))
            toolbar_layout.addWidget(button)

        self.status_panel = QWidget()
        self.status_panel.setObjectName("viewerStatusPanel")
        self.status_panel.setMinimumWidth(0)
        self.status_panel.setMaximumWidth(300)
        self.status_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        status_layout = QVBoxLayout(self.status_panel)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(2)
        self.status_progress = QProgressBar()
        self.status_progress.setObjectName("viewerStatusProgress")
        self.status_progress.setFixedHeight(14)
        self.status_progress.setTextVisible(True)
        progress_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        progress_policy.setRetainSizeWhenHidden(True)
        self.status_progress.setSizePolicy(progress_policy)
        self.status_progress.hide()
        status_layout.addWidget(self.status_progress)
        self.status_label = QLabel()
        self.status_label.setObjectName("viewerStatusText")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        status_layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignBottom)
        self.status_panel.installEventFilter(self)
        toolbar_layout.addWidget(self.status_panel, 1)
        toolbar_layout.addWidget(filter_panel)

        self.ai_panel = QWidget()
        self.ai_panel.setObjectName("viewerAiPanel")
        ai_layout = QHBoxLayout(self.ai_panel)
        ai_layout.setContentsMargins(10, 4, 10, 4)
        ai_layout.setSpacing(18)
        self.series_faces_group = QWidget()
        self.series_faces_group.setObjectName("aiPanelGroup")
        series_faces_layout = QHBoxLayout(self.series_faces_group)
        series_faces_layout.setContentsMargins(0, 0, 0, 0)
        series_faces_layout.setSpacing(5)
        series_faces_title = QLabel("СЕРИИ И ЛИЦА")
        series_faces_title.setObjectName("aiPanelTitle")
        series_faces_layout.addWidget(series_faces_title)
        self.series_toggle = QToolButton()
        self.series_toggle.setObjectName("aiFilter")
        self.series_toggle.setIcon(_fomantic_icon("images", 13))
        self.series_toggle.setText("Серии")
        self.series_toggle.setCheckable(True)
        self.series_toggle.setChecked(True)
        self.series_toggle.toggled.connect(self._series_toggle_changed)
        series_faces_layout.addWidget(self.series_toggle)
        self.faces_panel_button = QToolButton()
        self.faces_panel_button.setObjectName("aiFilter")
        self.faces_panel_button.setIcon(_fomantic_icon("user", 13))
        self.faces_panel_button.setText("Лица")
        self.faces_panel_button.clicked.connect(self._show_face_sets)
        series_faces_layout.addWidget(self.faces_panel_button)
        ai_layout.addWidget(self.series_faces_group)

        self.shot_group = QWidget()
        self.shot_group.setObjectName("aiPanelGroup")
        shot_layout = QHBoxLayout(self.shot_group)
        shot_layout.setContentsMargins(0, 0, 0, 0)
        shot_layout.setSpacing(5)
        self.ai_panel_title = QLabel("КРУПНОСТЬ ПЛАНА")
        self.ai_panel_title.setObjectName("aiPanelTitle")
        shot_layout.addWidget(self.ai_panel_title)
        self.shot_buttons: dict[object, QToolButton] = {}
        for label, value in (("Все", None), ("Крупный", "closeup"), ("Средний", "medium"), ("Общий", "wide"), ("Без лиц", "no_face")):
            button = QToolButton()
            button.setObjectName("shotFilter")
            button.setText(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, target=value: self._set_shot_filter(target))
            self.shot_buttons[value] = button
            shot_layout.addWidget(button)
        ai_layout.addWidget(self.shot_group)
        ai_layout.addStretch(1)
        self.ai_panel.hide()

        self.meta_bar = ViewerMetaBar(settings=self.settings)
        self.meta_bar.ratingRequested.connect(self._set_selected_rating)
        self.meta_bar.colorRequested.connect(self._set_selected_color)
        self.meta_bar.quickMarkRequested.connect(self._apply_quick_mark)
        self.meta_bar.quickMarkConfigured.connect(self._configure_quick_mark)
        self.meta_bar.autoAdvanceChanged.connect(self._set_auto_advance)
        self.meta_bar.commentSubmitted.connect(self._save_comment)
        self.comment_edit = self.meta_bar.comment_edit
        self.meta_bar.set_quick_mark(*self.quick_mark)
        self.meta_bar.set_auto_advance(self.auto_advance)
        meta = self.meta_bar

        content_layout.addWidget(self.grid, 1)
        content_layout.addWidget(meta)
        splitter.addWidget(sidebar)
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1120])
        layout.addWidget(toolbar)
        layout.addWidget(self.ai_panel)
        layout.addWidget(splitter, 1)
        return page

    def _create_actions(self) -> None:
        full = QAction("Full View", self)
        full.setShortcut(QKeySequence(Qt.Key.Key_F))
        full.triggered.connect(self._open_selected)
        self.addAction(full)

        grid = QAction("Grid", self)
        grid.setShortcut(QKeySequence(Qt.Key.Key_G))
        grid.triggered.connect(self.show_grid)
        self.addAction(grid)

        escape = QAction("Back", self)
        escape.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        escape.triggered.connect(self._handle_escape)
        self.addAction(escape)

        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence.Refresh)
        refresh.triggered.connect(lambda: self.load_directory(self.current_dir))
        self.addAction(refresh)

        fullscreen = QAction("Toggle Fullscreen", self)
        fullscreen.setShortcut(QKeySequence(Qt.Key.Key_F11))
        fullscreen.triggered.connect(self.toggle_fullscreen)
        self.addAction(fullscreen)

        quick = QAction("Quick mark", self)
        quick.setShortcut(QKeySequence(Qt.Key.Key_M))
        quick.triggered.connect(self._apply_quick_mark)
        self.addAction(quick)

        comment = QAction("Comment", self)
        comment.setShortcut(QKeySequence(Qt.Key.Key_C))
        comment.triggered.connect(lambda: (self.comment_edit.setFocus(), self.comment_edit.selectAll()))
        self.addAction(comment)

        for rating in range(0, 6):
            action = QAction(f"Rating {rating}", self)
            action.setShortcut(QKeySequence(str(rating)))
            action.triggered.connect(lambda _checked=False, value=rating: self._set_selected_rating(value or None))
            self.addAction(action)

    def _install_grid_page_key_filters(self) -> None:
        self._register_grid_page_focus_widget(self.grid_page)
        for widget in self.grid_page.findChildren(QWidget):
            self._register_grid_page_focus_widget(widget)

    def _register_grid_page_focus_widget(self, widget: QWidget) -> None:
        widget.installEventFilter(self)

    def _is_grid_page_widget(self, widget: QWidget | None) -> bool:
        return widget is not None and (widget is self.grid_page or self.grid_page.isAncestorOf(widget))

    def _is_directory_focus_widget(self, widget: QWidget | None) -> bool:
        return widget is not None and (widget is self.dir_tree or self.dir_tree.isAncestorOf(widget))

    def _focus_directory_panel(self) -> None:
        index = self.dir_tree.currentIndex()
        if not index.isValid():
            index = self.dir_model.index(str(self.current_dir))
            if index.isValid():
                self.dir_tree.setCurrentIndex(index)
        self.dir_tree.setFocus(Qt.FocusReason.TabFocusReason)

    def _focus_grid_panel(self) -> None:
        if self.grid.currentItem() is None and self.grid.count() > 0:
            self.grid.setCurrentRow(0)
        self.grid.setFocus(Qt.FocusReason.TabFocusReason)

    def _toggle_primary_panel_focus(self) -> None:
        focus_widget = QApplication.focusWidget()
        if self._is_directory_focus_widget(focus_widget):
            self._focus_grid_panel()
            return
        self._focus_directory_panel()

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "status_panel", None) and event.type() == QEvent.Type.Resize:
            self._fit_status_text()
        if event.type() == QEvent.Type.KeyPress and self.stack.currentWidget() is self.grid_page:
            focus_widget = watched if isinstance(watched, QWidget) else QApplication.focusWidget()
            if self._is_grid_page_widget(focus_widget):
                if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    self._toggle_primary_panel_focus()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._is_directory_focus_widget(focus_widget):
                    index = self.dir_tree.currentIndex()
                    if index.isValid():
                        self._directory_selected(index)
                        return True
        return super().eventFilter(watched, event)


    def _handle_escape(self) -> None:
        if self.stack.currentWidget() is self.full_view:
            self.show_grid()
            return
        if self.stack.currentWidget() is self.grid_page:
            self._go_up_directory()

    def _go_up_directory(self) -> None:
        """Перейти на уровень вверх от текущей директории"""
        if self.current_dir and self.current_dir.parent != self.current_dir:
            self.load_directory(self.current_dir.parent)

    def _expand_tree_path(self, index) -> None:
        """Раскрыть все родительские узлы, чтобы индекс точно стал видимым."""
        parents = []
        current = index.parent()
        while current.isValid():
            parents.append(current)
            current = current.parent()
        for parent in reversed(parents):
            self.dir_tree.expand(parent)

    def _begin_directory_inline_rename(self, path: Path, index=None, attempts_left: int = 20) -> None:
        """Запустить inline-rename по индексу модели с несколькими повторами."""
        if index is None or not index.isValid():
            index = self.dir_model.index(str(path))
        if not index.isValid():
            if attempts_left <= 0:
                self.dir_model._new_folder_path = None
                return
            QTimer.singleShot(
                50,
                lambda target=path, attempts=attempts_left - 1: self._begin_directory_inline_rename(target, None, attempts),
            )
            return

        self._expand_tree_path(index)
        selection_model = self.dir_tree.selectionModel()
        if selection_model is not None:
            selection_model.setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )
        self.dir_tree.setCurrentIndex(index)
        self.dir_tree.scrollTo(index, QTreeView.ScrollHint.EnsureVisible)
        self.dir_tree.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda target=path, idx=index: self._show_folder_name_editor(target, idx))

    def _show_folder_name_editor(self, path: Path, index) -> None:
        if self.dir_model._new_folder_path != path or not index.isValid():
            return
        if self._folder_name_editor is not None:
            self._folder_name_editor.deleteLater()

        rect = self.dir_tree.visualRect(index)
        editor = FolderNameEditor(self.dir_tree.viewport())
        editor.setText(path.name)
        editor.selectAll()
        editor.setGeometry(rect.adjusted(22, 0, -2, 0))
        editor.setMinimumWidth(80)
        editor.setProperty("folderPath", str(path))
        editor.accepted.connect(self._commit_folder_name)
        editor.cancelled.connect(self._cancel_folder_name)
        self._folder_name_editor = editor
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _commit_folder_name(self) -> None:
        editor = self._folder_name_editor
        if editor is None:
            return
        old_path = Path(editor.property("folderPath"))
        new_name = editor.text().strip()
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\\" in new_name:
            editor.setStyleSheet("border: 1px solid #c43d2f;")
            editor.setToolTip("Введите корректное имя папки")
            QTimer.singleShot(0, editor.setFocus)
            return

        new_path = old_path.parent / new_name
        if new_path != old_path:
            if new_path.exists():
                editor.setStyleSheet("border: 1px solid #c43d2f;")
                editor.setToolTip("Папка с таким именем уже существует")
                QTimer.singleShot(0, editor.setFocus)
                return
            try:
                old_path.rename(new_path)
            except OSError as error:
                editor.setStyleSheet("border: 1px solid #c43d2f;")
                editor.setToolTip(str(error))
                QTimer.singleShot(0, editor.setFocus)
                return
        self._finish_folder_name_editor()

    def _cancel_folder_name(self) -> None:
        self._finish_folder_name_editor()

    def _finish_folder_name_editor(self) -> None:
        editor = self._folder_name_editor
        self._folder_name_editor = None
        self.dir_model._new_folder_path = None
        if editor is not None:
            editor.hide()
            editor.deleteLater()

    def _create_new_folder(self) -> None:
        """Создать новую папку в текущей директории с inline-редактированием."""
        if not self.current_dir:
            return

        # Создаем временное имя для папки
        i = 1
        while True:
            temp_name = f"Новая папка {i}"
            temp_path = self.current_dir / temp_name
            if not temp_path.exists():
                break
            i += 1

        try:
            # Устанавливаем путь к новой папке в модели ПЕРЕД ее созданием
            self.dir_model._new_folder_path = temp_path

            parent_index = self.dir_model.index(str(self.current_dir))
            if not parent_index.isValid():
                raise OSError("Текущая директория еще не готова в модели дерева.")

            new_index = self.dir_model.mkdir(parent_index, temp_name)
            if not new_index.isValid():
                raise OSError("QFileSystemModel не смог создать папку.")

            QTimer.singleShot(
                0,
                lambda target=temp_path, idx=new_index: self._begin_directory_inline_rename(target, idx),
            )

        except OSError as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создат�� папку: {e}")
            self.dir_model._new_folder_path = None # Очищаем в случае ошибки

    def _directory_editor_closed(self, _editor, _hint) -> None:
        """Сбрасывать состояние новой папки даже при отмене inline-rename."""
        if self.dir_model._new_folder_path is None:
            return
        self.dir_model._new_folder_path = None

    def _directory_selected(self, index) -> None:
        path = Path(self.dir_model.filePath(index))
        self.load_directory(path)

    def _refresh_volume_buttons(self) -> None:
        """Synchronize the sidebar with volumes that are mounted and ready.

        Empty card-reader slots have no mounted filesystem, so QStorageInfo
        deliberately omits them. This also avoids showing inaccessible media.
        """
        volumes = _mounted_volume_paths()
        volume_keys = {_drive_key(path) for path in volumes}
        existing = {
            button.property("volumeKey"): button
            for button in self.drive_buttons.buttons()
        }

        for key, button in existing.items():
            if key == SHOTSYNC_VOLUME_KEY:
                # The ShotSync "disk" is persistent and never tied to a volume.
                continue
            if key not in volume_keys:
                self.drive_buttons.removeButton(button)
                self.drive_button_layout.removeWidget(button)
                self.volume_removability.pop(key, None)
                button.deleteLater()

        for path in volumes:
            key = _drive_key(path)
            button = existing.get(key)
            if button is None:
                button = QToolButton()
                button.setObjectName("driveButton")
                button.setCheckable(True)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                button.setIconSize(QSize(20, 20))
                button.setProperty("volumeKey", key)
                button.clicked.connect(lambda _checked=False, root=path: self._drive_selected(root))
                self.drive_buttons.addButton(button)
                self.drive_button_layout.addWidget(button)
                self._register_grid_page_focus_widget(button)
            if key not in self.volume_removability:
                self.volume_removability[key] = _is_removable_volume(path)
            removable = self.volume_removability[key]
            button.setProperty("removable", removable)
            button.setText(_volume_button_text(path))
            button.setIcon(
                self.removable_volume_icon
                if removable
                else self.volume_icon_provider.icon(QFileInfo(str(path)))
            )
            tooltip = _volume_label(path)
            button.setToolTip(f"Съёмный носитель\n{tooltip}" if removable else tooltip)

        current_root = _volume_root_for_path(self.current_dir, volumes)
        for button in self.drive_buttons.buttons():
            key = button.property("volumeKey")
            if key == SHOTSYNC_VOLUME_KEY:
                button.setChecked(self.shotsync_active)
                continue
            if self.shotsync_active:
                button.setChecked(False)
            else:
                button.setChecked(key == _drive_key(current_root) if current_root else False)

        # If removable media containing the open folder was unplugged, return
        # to a valid local location instead of retaining a dead tree root.
        if not self.closing and not self.current_dir.is_dir():
            fallback = Path.home()
            self._set_tree_root_for_path(fallback)
            self.load_directory(fallback)

    def _drive_selected(self, drive_path: Path) -> None:
        self._deactivate_shotsync()
        if drive_path.is_dir():
            self._set_tree_root_for_path(drive_path)
            self.load_directory(drive_path)

    def _set_tree_root_for_path(self, path: str | Path) -> None:
        path_text = str(path)
        model_index = self.dir_model.index(path_text)
        if model_index.isValid():
            self.dir_tree.setRootIndex(model_index)
            root = _volume_root_for_path(Path(path_text), _mounted_volume_paths())
            if root is not None and hasattr(self, "drive_buttons") and not self.shotsync_active:
                root_key = _drive_key(root)
                for button in self.drive_buttons.buttons():
                    button.setChecked(button.property("volumeKey") == root_key)

    # ----- ShotSync cloud disk ------------------------------------------
    def _shotsync_button_icon(self) -> QIcon:
        """Load the ShotSync logo bundled with the app assets."""
        logo_path = Path(__file__).parent / "assets" / "shotsync.png"
        if logo_path.exists():
            px = QPixmap(str(logo_path)).scaled(
                20, 20,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if not px.isNull():
                return QIcon(px)
        return _fomantic_icon("cloud", 18, "#8fb8ff")

    def _activate_shotsync(self) -> None:
        """Switch the sidebar to the ShotSync cloud panel."""
        self.shotsync_active = True
        self.shotsync_button.setChecked(True)
        for button in self.drive_buttons.buttons():
            if button is not self.shotsync_button:
                button.setChecked(False)
        self.sidebar_stack.setCurrentWidget(self.shotsync_panel)

        if self.shotsync_client.has_key():
            # Validate the remembered key once per session, then reuse it.
            if not self._shotsync_checked:
                self.shotsync_panel.show_checking()
                self.shotsync_client.verify_session()
            else:
                self.shotsync_panel.set_shootings_loading()
                self.shotsync_client.fetch_shootings()
        else:
            self.shotsync_panel.show_login()

    def _deactivate_shotsync(self) -> None:
        if not self.shotsync_active:
            return
        self.shotsync_active = False
        self.shotsync_button.setChecked(False)
        if hasattr(self, "sidebar_stack"):
            self.sidebar_stack.setCurrentWidget(self._sidebar_local_page)

    def _shotsync_login(self, login: str, password: str) -> None:
        self.shotsync_client.login(login, password)

    def _shotsync_logout(self) -> None:
        self.shotsync_client.logout()
        self._shotsync_checked = False
        self.settings.remove("shotsync/api_key")
        self.shotsync.set_api_key("")
        self.shotsync_panel.show_login()

    def _shotsync_login_succeeded(self, user: dict, key: str) -> None:
        self._shotsync_checked = True
        self.settings.setValue("shotsync/api_key", key)
        self.shotsync.set_api_key(key)
        self.shotsync_panel.show_logged_in(user)
        avatar_url = user.get("avatar_url")
        if avatar_url:
            self.shotsync_client.fetch_avatar(avatar_url)
        self.shotsync_panel.set_shootings_loading()
        self.shotsync_client.fetch_shootings()

    def _shotsync_login_failed(self, error: str) -> None:
        self.shotsync_panel.show_login_error(error)

    def _shotsync_session_verified(self, user: dict) -> None:
        self._shotsync_checked = True
        self.shotsync.set_api_key(self.shotsync_client.api_key)
        self.shotsync_panel.show_logged_in(user)
        avatar_url = user.get("avatar_url")
        if avatar_url:
            self.shotsync_client.fetch_avatar(avatar_url)
        self.shotsync_panel.set_shootings_loading()
        self.shotsync_client.fetch_shootings()

    def _shotsync_session_invalid(self, error: str) -> None:
        self._shotsync_checked = False
        self.settings.remove("shotsync/api_key")
        self.shotsync.set_api_key("")
        if self.shotsync_active:
            self.shotsync_panel.show_login()

    def _shotsync_shootings_loaded(self, shootings: list) -> None:
        self.shotsync_panel.set_shootings(shootings)
        self._refresh_shotsync_receiving()

    def _shotsync_shootings_failed(self, error: str) -> None:
        self.shotsync_panel.set_shootings_error(error)

    def _shotsync_avatar_loaded(self, image) -> None:
        self.shotsync_panel.set_avatar(image)

    # ----- live receive (feature 1) -------------------------------------
    def _refresh_shotsync_receiving(self) -> None:
        """Reflect which shootings are currently being received in the panel."""
        self.shotsync_panel.set_receiving_ids(self.shotsync.receiving_ids())

    def _shotsync_receive_requested(self, shooting: dict) -> None:
        """Toggle live receiving for a shooting, choosing a target folder."""
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id:
            return
        if self.shotsync.is_receiving(shooting_id):
            self.shotsync.stop_receiving(shooting_id)
            return
        title = shooting.get("title") or "Съёмка ShotSync"
        base = QFileDialog.getExistingDirectory(
            self, f"Куда сохранять фото «{title}»", str(self.current_dir)
        )
        if not base:
            return
        folder = Path(base) / _safe_folder_name(title)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "ShotSync", f"Не удалось создать папку:\n{exc}")
            return
        self.shotsync.start_receiving(shooting_id, folder, title)
        self.openFolderRequested.emit(folder)

    def _on_shotsync_photo_downloaded(self, shooting_id: int, folder: str, filename: str) -> None:
        """A new original landed on disk; refresh the tab showing that folder."""
        if Path(folder) == self.current_dir:
            self.load_directory(self.current_dir)

    def _on_shotsync_mark_updated(self, shooting_id: int, folder: str, photo: dict) -> None:
        """Mirror an owner mark that arrived over the socket into this folder."""
        if Path(folder) != self.current_dir:
            return
        name = _shotsync_photo_filename(photo)
        if not name:
            return
        self._apply_external_selection(
            name,
            rating=photo.get("rating"),
            color_label=photo.get("color_label") or "",
            comment=photo.get("comment") or "",
        )

    def _apply_external_selection(
        self, name: str, *, rating: int | None, color_label: str, comment: str
    ) -> None:
        """Apply rating/color/comment to a single file by name and repaint it."""
        detail = self.photo_details.setdefault(name, {})
        detail.update(rating=rating, color_label=color_label, comment=comment)
        for path, item in self.items_by_path.items():
            if path.name == name:
                item.setData(DETAIL_ROLE, dict(detail))
                break
        if self.folder_cache is not None and self.cache_ready:
            self.folder_cache.store_photo_selection(
                name, rating=rating, color_label=color_label, comment=comment
            )
        self.grid.viewport().update()
        if self.current_path is not None and self.current_path.name == name:
            if self.stack.currentWidget() is self.full_view:
                self.full_view.set_metadata(dict(detail), (self.current_path,))

    # ----- selection: take a shooting locally (feature 2) ----------------
    def _shotsync_select_requested(self, shooting: dict) -> None:
        """Download a shooting's previews into a local folder for selection."""
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id:
            return
        if self.shotsync.downloader.is_running(shooting_id):
            self.statusBar().showMessage("ShotSync: загрузка уже идёт…", 4000)
            return
        title = shooting.get("title") or "Съёмка ShotSync"
        self.statusBar().showMessage(f"ShotSync: загрузка «{title}»…")
        self.shotsync.downloader.start(shooting_id, title)

    def _on_shotsync_selection_progress(self, shooting_id: int, done: int, total: int) -> None:
        if total:
            self.statusBar().showMessage(f"ShotSync: загружено {done}/{total}…", 4000)

    def _on_shotsync_selection_ready(self, shooting_id: int, folder: str) -> None:
        self.statusBar().showMessage("ShotSync: съёмка готова к отбору.", 4000)
        self.openFolderRequested.emit(Path(folder))

    def _on_shotsync_selection_failed(self, shooting_id: int, message: str) -> None:
        QMessageBox.warning(self, "ShotSync", f"Не удалось загрузить съёмку:\n{message}")

    def _attach_shotsync_syncer(self) -> None:
        """If the open folder is a ShotSync selection, start syncing its marks."""
        self._detach_shotsync_syncer()
        if self.folder_cache is None or not self.cache_ready:
            return
        session = self.folder_cache.shotsync_session()
        if not session:
            return
        shooting_id, _title = session
        self._shotsync_syncer = SelectionMarkSyncer(
            self.shotsync, self.folder_cache, shooting_id, self
        )
        self._shotsync_syncer.pendingChanged.connect(self._on_shotsync_pending_changed)

    def _detach_shotsync_syncer(self) -> None:
        if getattr(self, "_shotsync_syncer", None) is not None:
            self._shotsync_syncer.detach()
            self._shotsync_syncer.deleteLater()
            self._shotsync_syncer = None

    def _on_shotsync_pending_changed(self, count: int) -> None:
        if count:
            self.statusBar().showMessage(f"ShotSync: меток в очереди — {count}", 3000)

    def load_directory(self, directory: Path) -> None:
        if self._folder_context_active:
            self._save_folder_grid_context()
        self._folder_context_active = False
        watched = self.folder_watcher.directories()
        if watched:
            self.folder_watcher.removePaths(watched)
        if directory.is_dir():
            self.folder_watcher.addPath(str(directory))
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()
        self.memory_cache.clear()
        self.thumbnail_cache.clear()
        self.thumbnail_cache_bytes = 0
        self.populate_timer.stop()
        self.thumb_timer.stop()
        self.ai_progress_timer.stop()
        self.ai_progress_total = 0
        self.preview_progress_total = 0
        if hasattr(self, "ai_button"):
            self.ai_button.setEnabled(False)
            self._refresh_status_panel()
        self.cache_load_generation += 1
        self.directory_generation += 1
        self._flush_folder_cache(wait=False, close=True)
        self._detach_shotsync_syncer()
        self.folder_cache = None
        self.cache_ready = False
        self.current_dir = directory
        self._restore_series_mode(directory)
        self._pending_folder_grid_context = self._load_folder_grid_context(directory)
        self.settings.setValue("last_directory", str(directory))
        self.setWindowTitle(_workspace_title(directory))
        self.all_paths = []
        self.view_paths = []
        self.paths = []
        self.photo_details = {}
        self.image_embeddings = {}
        self.ai_progress_total = 0
        self.items_by_path.clear()
        self.grid.clear()
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self.visible_thumb_pending.clear()
        
        # Synchronize the left tree view with the current directory
        index = self.dir_model.index(str(directory))
        if index.isValid():
            # Expand all parent directories to show the current folder in the tree
            self.dir_tree.expand(index.parent())
            # Select and scroll to the current directory
            self.dir_tree.setCurrentIndex(index)
            self.dir_tree.scrollTo(index)
            
        request = self.workspace_state.begin_directory(directory)
        future = self.directory_scan_executor.submit(_scan_directory, directory)
        future.add_done_callback(lambda done, r=request, d=directory: self._directory_scanned(r, d, done))

    @staticmethod
    def _folder_settings_prefix(directory: Path) -> str:
        # Keep the setting portable and avoid QSettings treating path
        # separators as nested groups.
        normalized = str(directory.expanduser().resolve()).casefold()
        return f"folder_settings/{sha1(normalized.encode()).hexdigest()}"

    @classmethod
    def _series_mode_setting_key(cls, directory: Path) -> str:
        return f"{cls._folder_settings_prefix(directory)}/series_enabled"

    def _load_folder_grid_context(self, directory: Path) -> tuple[list[str], int] | None:
        prefix = self._folder_settings_prefix(directory)
        if not self.settings.contains(f"{prefix}/grid_scroll"):
            return None
        selected = self.settings.value(f"{prefix}/selected_paths", [], list)
        if isinstance(selected, (list, tuple)):
            selected_names = [str(name) for name in selected]
        else:
            selected_names = [str(selected)] if selected else []
        return selected_names, max(0, self.settings.value(f"{prefix}/grid_scroll", 0, int))

    def _save_folder_grid_context(self) -> None:
        if not self._folder_context_active:
            return
        prefix = self._folder_settings_prefix(self.current_dir)
        selected = [path.name for path in self._selected_paths()]
        self.settings.setValue(f"{prefix}/selected_paths", selected)
        self.settings.setValue(f"{prefix}/grid_scroll", self.grid.verticalScrollBar().value())

    def _restore_folder_grid_context(self) -> None:
        context = self._pending_folder_grid_context
        if context is None:
            return
        self._pending_folder_grid_context = None
        selected_names, scroll_value = context
        selected_items = [self.items_by_path[path] for path in self.paths if path.name in selected_names and path in self.items_by_path]
        if selected_items:
            self.grid.setCurrentItem(selected_items[0])
            for item in selected_items:
                item.setSelected(True)
        scroll_bar = self.grid.verticalScrollBar()
        scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))

    def _restore_series_mode(self, directory: Path) -> None:
        enabled = self.settings.value(self._series_mode_setting_key(directory), True, bool)
        self.series_toggle.blockSignals(True)
        self.series_toggle.setChecked(enabled)
        self.series_toggle.blockSignals(False)

    def _series_toggle_changed(self, enabled: bool) -> None:
        self.settings.setValue(self._series_mode_setting_key(self.current_dir), enabled)
        self._apply_view()

    def _directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        if self.closing:
            return
        self.bridge.directoryScanned.emit(request, directory, future)

    def _on_directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        if self.closing or not self.workspace_state.accepts(request):
            return
        self._folder_context_active = True
        try:
            self.all_paths = future.result()
            # Separate subdirectories and image files
            subfolders = [p for p in self.all_paths if p.is_dir()]
            images = [p for p in self.all_paths if p.is_file()]
            # Sort both groups alphabetically
            sorted_subfolders = sorted(subfolders, key=lambda p: p.name.lower())
            sorted_images = sorted(images, key=lambda p: p.name.lower())
            # Combine: folders first, then images
            self.paths = sorted_subfolders + sorted_images
            self.view_paths = list(self.paths)
            self.preview_progress_total = len(images)
            self.view_generation += 1
        except Exception as exc:
            self.bridge.failed.emit(str(directory), str(exc))
            self.all_paths = []
            self.view_paths = []
            self.paths = []
            self.preview_progress_total = 0
        scannable_paths = [path for path in self.paths if path.is_file()]
        if scannable_paths:
            self.folder_cache = FolderCache(
                directory,
                {path.name for path in scannable_paths},
                eager_variants={THUMB_SIZE},
                load_from_disk=False,
            )
            self.cache_ready = False
            generation = self.cache_load_generation
            cache = self.folder_cache
            future = self.cache_load_executor.submit(cache.load_from_disk)
            future.add_done_callback(lambda done, g=generation: self._cache_loaded(g, done))
        else:
            self.folder_cache = None
            self.cache_ready = True
            self._update_analysis_controls()
            self._reset_ai_status()
        self.items_by_path.clear()
        self.grid.clear()
        self.populate_index = 0
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self.visible_thumb_pending.clear()
        self._refresh_status_panel()
        self.populate_timer.start()
        # Do not decode the first files before the in-memory cache has been
        # deserialized.  Otherwise the sequential queue races cached previews.

    def _populate_next_items(self) -> None:
        if not self.workspace_active:
            self.populate_timer.stop()
            return
        end = min(len(self.paths), self.populate_index + POPULATE_BATCH)
        for path in self.paths[self.populate_index : end]:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setToolTip(str(path))
            item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
            item.setData(SERIES_ROLE, self.series_cards.get(path, {}))
            preview = self._thumbnail_cache_get(path)
            if preview is None:
                cached = self._cache_get((path, THUMB_SIZE))
                preview = cached.image if cached is not None else None
            if preview is not None:
                item.setData(PREVIEW_ROLE, preview)
            self.grid.addItem(item)
            self.items_by_path[path] = item
        self.populate_index = end
        self._schedule_visible_thumb_priority()
        self._refresh_status_panel()
        if self.populate_index >= len(self.paths):
            self.populate_timer.stop()
            if self.cache_ready:
                QTimer.singleShot(0, self._restore_folder_grid_context)

    def _submit_next_thumbs(self) -> None:
        if not self.workspace_active:
            self.thumb_timer.stop()
            return
        if self.folder_cache is None or not self.cache_ready:
            return
        if self.pending_full_request is not None or self.foreground_full_futures:
            return
        pending_thumbs = sum(1 for _, size in self.pending if size == THUMB_SIZE)
        # Never keep feeding the top-to-bottom background scan while viewport
        # work is waiting.  Also keep executor queues short so a scroll can
        # affect the next submitted job instead of waiting behind stale work.
        if self.thumb_priority:
            if len(self.visible_thumb_pending) >= MAX_VISIBLE_THUMB_PENDING:
                return
        elif self.visible_thumb_pending or pending_thumbs >= MAX_PENDING_THUMBS:
            return
        submitted = 0
        while submitted < THUMB_SUBMIT_BATCH:
            next_path = self._next_thumb_path()
            if next_path is None:
                break
            path, visible_priority = next_path
            if is_supported_video(path):
                self._submit_video_thumbnail(path, visible_priority=visible_priority)
            else:
                self._submit_decode(path, THUMB_SIZE, full_priority=False, visible_priority=visible_priority)
            submitted += 1
        if self.thumb_index >= len(self.paths) and not self.thumb_priority:
            self.thumb_timer.stop()
        self._refresh_status_panel()

    def _schedule_visible_thumb_priority(self) -> None:
        if not self.workspace_active:
            return
        if not self.visible_thumb_timer.isActive():
            self.visible_thumb_timer.start(0)

    def _start_ai_analysis(self) -> None:
        if self.closing or not self.cache_ready or self.folder_cache is None:
            return
        analysis_paths = [path for path in self.view_paths if is_supported_image(path)]
        embedding_missing = self.folder_cache.missing_ai_paths(analysis_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(analysis_paths, "face_analysis")
        self.ai_progress_total = len(set(embedding_missing) | set(face_missing))
        if not self.ai_progress_total:
            self._refresh_status_panel()
            return
        self.ai_button.setEnabled(False)
        self.ai_pipeline.scan(analysis_paths, self.folder_cache, self._background_decode_executor())
        self.ai_progress_timer.start()
        self._refresh_status_panel()

    def _update_ai_progress(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            self.ai_progress_timer.stop()
            return
        analysis_paths = [path for path in self.view_paths if is_supported_image(path)]
        embedding_missing = self.folder_cache.missing_ai_paths(analysis_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(analysis_paths, "face_analysis")
        remaining = len(set(embedding_missing) | set(face_missing))
        if self.ai_pipeline.pending_count() == 0:
            self.ai_progress_timer.stop()
            self.ai_pipeline.release_analysis_workers()
            self._reload_photo_details()
            self.ai_button.setEnabled(remaining > 0)
        self._refresh_status_panel()

    def _folder_changed(self, path: str) -> None:
        if not self.closing and Path(path) == self.current_dir:
            self.folder_change_timer.start(FOLDER_CHANGE_DEBOUNCE_MS)

    def _reload_changed_folder(self) -> None:
        if not self.closing and self.current_dir.is_dir():
            self.load_directory(self.current_dir)

    def _refresh_ai_status(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            self._reset_ai_status()
            return
        analysis_paths = [path for path in self.view_paths if is_supported_image(path)]
        embedding_missing = self.folder_cache.missing_ai_paths(analysis_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(analysis_paths, "face_analysis")
        waiting = len(set(embedding_missing) | set(face_missing))
        self.ai_button.setEnabled(waiting > 0 and self.ai_pipeline.pending_count() == 0)
        self._refresh_status_panel()

    def _reset_ai_status(self) -> None:
        if not hasattr(self, "ai_button"):
            return
        self.ai_button.setEnabled(False)
        self._refresh_status_panel()

    def _refresh_status_panel(self) -> None:
        """Show one active operation and keep folder/selection counts in the toolbar."""
        if not hasattr(self, "status_label"):
            return
        # Directory scanning already established these collections. Avoid
        # thousands of filesystem stat calls whenever progress is repainted.
        total = self.preview_progress_total
        filtered = sum(1 for path in self.view_paths if is_supported_media(path))
        selected = len(self._selected_paths())
        text = f"{filtered}/{total}"
        if selected > 1:
            text += f" (выделено: {selected})"
        self._status_text = text
        self._fit_status_text()
        self.status_label.setToolTip(text)

        if self.ai_pipeline.pending_count() > 0:
            analysis_paths = [path for path in self.view_paths if is_supported_image(path)]
            remaining = 0
            if self.folder_cache is not None and self.cache_ready:
                embedding_missing = self.folder_cache.missing_ai_paths(analysis_paths, "image_embeddings")
                face_missing = self.folder_cache.missing_ai_paths(analysis_paths, "face_analysis")
                remaining = len(set(embedding_missing) | set(face_missing))
            completed = max(0, self.ai_progress_total - remaining)
            self.status_progress.setRange(0, max(1, self.ai_progress_total))
            self.status_progress.setValue(completed)
            self.status_progress.setFormat(f"Анализ: {completed}/{self.ai_progress_total}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(completed, self.ai_progress_total)
            return

        thumbnail_pending = any(size == THUMB_SIZE for _, size in self.pending)
        if self.preview_progress_total and (self.thumb_timer.isActive() or thumbnail_pending):
            loaded = sum(
                1 for path in self.paths
                if is_supported_media(path) and self.items_by_path.get(path) is not None
                and isinstance(self.items_by_path[path].data(PREVIEW_ROLE), QImage)
            )
            self.status_progress.setRange(0, self.preview_progress_total)
            self.status_progress.setValue(loaded)
            self.status_progress.setFormat(f"Превью: {loaded}/{self.preview_progress_total}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(loaded, self.preview_progress_total)
            return

        self.status_progress.hide()
        self._set_taskbar_progress(0, 0)

    def _fit_status_text(self) -> None:
        if not hasattr(self, "status_label"):
            return
        text = getattr(self, "_status_text", "")
        self.status_label.setText(self.status_label.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, max(0, self.status_label.width())
        ))

    def _set_taskbar_progress(self, value: int, total: int) -> None:
        self._taskbar_progress.set_progress(int(self.window().winId()), value, total)
        self._dock_progress.set_progress(value, total)

    def _reload_photo_details(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            return
        self.photo_details = self.folder_cache.load_photo_details(
            include_metadata=ENABLE_EXIF_METADATA
        )
        self.image_embeddings = self.folder_cache.load_image_embeddings()
        self._refresh_camera_filter()
        for path, item in self.items_by_path.items():
            item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        self.grid.viewport().update()
        self._update_analysis_controls()
        self._apply_view()

    def _on_metadata_updated(self, results: object) -> None:
        """Apply thumbnail-worker EXIF without reloading the whole folder cache."""
        if self.closing or not self.cache_ready or not isinstance(results, list):
            return
        changed_current = False
        changed_camera_keys = set()
        for name, payload in results:
            try:
                metadata = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            path = Path(name)
            detail = self.photo_details.setdefault(path.name, {})
            detail.update(metadata)
            camera_key = self._camera_filter_key(detail)
            if camera_key is not None:
                changed_camera_keys.add(camera_key)
            item = self.items_by_path.get(path)
            if item is not None:
                item.setData(DETAIL_ROLE, detail)
            changed_current |= path == self.current_path
        self._refresh_camera_filter()
        if self.camera_filter.currentData() in changed_camera_keys:
            self._apply_view()
        if changed_current and self.current_path is not None:
            detail = self.photo_details.get(self.current_path.name, {})
            self.full_view.set_metadata(detail)
            if self.stack.currentWidget() is self.grid_page and hasattr(self, "meta_bar"):
                self.meta_bar.set_metadata(detail)

    @staticmethod
    def _camera_filter_key(detail: dict) -> str | None:
        camera = detail.get("camera") or {}
        model = str(camera.get("model") or "").strip()
        if not model:
            return None
        serial = str(camera.get("serial_number") or "").strip()
        return f"serial:{serial}" if serial else f"model:{model}"

    def _refresh_camera_filter(self) -> None:
        if not hasattr(self, "camera_filter"):
            return
        selected = self.camera_filter.currentData()
        cameras: dict[str, dict[str, object]] = {}
        for detail in self.photo_details.values():
            key = self._camera_filter_key(detail)
            if key is None:
                continue
            camera = detail.get("camera") or {}
            entry = cameras.setdefault(key, {"model": str(camera.get("model") or ""), "count": 0})
            entry["count"] = int(entry["count"]) + 1
        self.camera_filter.blockSignals(True)
        self.camera_filter.clear()
        self.camera_filter.addItem("Все камеры", None)
        self.camera_filter.setItemIcon(0, _fomantic_icon("images", 12, "#a8b0bd"))
        for key, entry in sorted(cameras.items(), key=lambda item: (str(item[1]["model"]).casefold(), item[0])):
            self.camera_filter.addItem(f"{entry['model']} ({entry['count']})", key)
        index = self.camera_filter.findData(selected)
        self.camera_filter.setCurrentIndex(index if index >= 0 else 0)
        self.camera_filter.blockSignals(False)
        self.camera_filter.setVisible(len(cameras) > 1)

    def _update_analysis_controls(self) -> None:
        if not hasattr(self, "faces_panel_button"):
            return
        has_faces = any(detail.get("faces") for detail in self.photo_details.values())
        has_series = self._has_available_series(self.view_paths or self.all_paths)
        if hasattr(self, "ai_panel"):
            self.ai_panel.setVisible(has_faces or has_series)
            self.series_faces_group.setVisible(has_faces or has_series)
            self.series_toggle.setVisible(True)
            self.faces_panel_button.setVisible(has_faces)
            self.shot_group.setVisible(has_faces)
            counts = {value: 0 for value in self.shot_buttons}
            rating = self.rating_filter.currentData()
            color = self.color_filter.currentData()
            media = self.media_filter.currentData()
            camera_key = self.camera_filter.currentData()
            needle = self.search_edit.text().strip().casefold()
            matching_paths: list[Path] = []
            for path in self.all_paths:
                if not path.is_file():
                    continue
                if media == "video" and not is_supported_video(path):
                    continue
                if media == "image" and is_supported_video(path):
                    continue
                detail = self.photo_details.get(path.name, {})
                if camera_key is not None and self._camera_filter_key(detail) != camera_key:
                    continue
                if rating is not None and detail.get("rating") != rating:
                    continue
                if self.color_filter.currentIndex() > 0 and detail.get("color_label", "") != color:
                    continue
                if self.face_reference is not None and not any(
                    self._face_similarity(self.face_reference, face.get("embedding", [])) >= 0.42
                    for face in detail.get("faces", []) if isinstance(face, dict)
                ):
                    continue
                if needle and needle not in path.name.casefold() and needle not in str(detail.get("comment", "")).casefold():
                    continue
                matching_paths.append(path)
            for path in matching_paths:
                detail = self.photo_details.get(path.name, {})
                counts[self._shot_size(detail)] = counts.get(self._shot_size(detail), 0) + 1
            for value, button in self.shot_buttons.items():
                button.setChecked(self.shot_filter.currentData() == value)
                count = len(matching_paths) if value is None else counts.get(value, 0)
                label = button.property("shotLabel") or button.text().split("  ")[0]
                button.setProperty("shotLabel", label)
                button.setText(f"{label}  {count}")

    def _set_shot_filter(self, value: str | None) -> None:
        index = self.shot_filter.findData(value)
        if index >= 0:
            self.shot_filter.setCurrentIndex(index)

    def _has_available_series(self, paths: list[Path]) -> bool:
        photos = [path for path in paths if path.is_file()]
        return any(
            self._embedding_similarity(left, right) >= 0.92
            for left, right in zip(photos, photos[1:])
        )

    def _prioritize_visible_thumbs(self) -> None:
        if not self.workspace_active:
            return
        if self.folder_cache is None or not self.cache_ready or self.grid.count() == 0:
            return
        # PhotoGrid intentionally keeps QListView's gridSize invalid so the
        # delegate can distribute remainder pixels across columns. Use its
        # actual per-card hint for viewport sampling; otherwise this routine
        # returns early and thumbnail work falls back to top-to-bottom only.
        cell = self.grid.card_size_hint(0)
        if cell.width() <= 0 or cell.height() <= 0:
            return

        viewport = self.grid.viewport()
        visible_rows: set[int] = set()
        # Sample the centre of every visible grid cell. This follows Qt's
        # actual wrapping/layout without walking every item in a large folder.
        for y in range(cell.height() // 2, viewport.height(), cell.height()):
            for x in range(cell.width() // 2, viewport.width(), cell.width()):
                item = self.grid.itemAt(QPoint(x, y))
                if item is not None:
                    visible_rows.add(self.grid.row(item))

        if not visible_rows:
            return

        first = min(visible_rows)
        last = max(visible_rows)
        span = max(1, last - first + 1)
        # One viewport of overscan in both directions prevents blank cards
        # during ordinary scrolling. Visible cards are ordered from the centre
        # out; overscan follows by distance from the visible range.
        centre = (first + last) / 2
        visible_order = sorted(visible_rows, key=lambda row: abs(row - centre))
        before = range(first - 1, max(-1, first - span - 1), -1)
        after = range(last + 1, min(self.grid.count(), last + span + 1))
        overscan_order = sorted(
            [row for row in (*before, *after) if 0 <= row < self.grid.count()],
            key=lambda row: min(abs(row - first), abs(row - last)),
        )

        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        for row in [*visible_order, *overscan_order]:
            item = self.grid.item(row)
            if item is None or item.data(PREVIEW_ROLE) is not None:
                continue
            value = item.data(Qt.ItemDataRole.UserRole)
            if not value:
                continue
            path = Path(value)
            key = (path, THUMB_SIZE)
            if key not in self.pending and path not in self.thumb_priority_set:
                self.thumb_priority.append(path)
                self.thumb_priority_set.add(path)

        if self.thumb_priority:
            self.thumb_timer.start()

    def _next_thumb_path(self) -> tuple[Path, bool] | None:
        while self.thumb_priority:
            path = self.thumb_priority.popleft()
            self.thumb_priority_set.discard(path)
            if (path, THUMB_SIZE) not in self.pending:
                return path, True
        # Background traversal is deliberately lazy and consults the current
        # view order each time. It skips cards already loaded or pending, which
        # also makes it safe for a future sort/filter rebuild to reset the
        # cursor without duplicating work.
        while self.thumb_index < len(self.paths):
            path = self.paths[self.thumb_index]
            self.thumb_index += 1
            item = self.items_by_path.get(path)
            if item is not None and item.data(PREVIEW_ROLE) is not None:
                continue
            if (path, THUMB_SIZE) in self.pending:
                continue
            return path, False
        return None

    def _cache_loaded(self, generation: int, future: Future) -> None:
        if self.closing:
            return
        self.bridge.cacheLoaded.emit(generation, future)

    def _on_cache_loaded(self, generation: int, future: Future) -> None:
        if self.closing or generation != self.cache_load_generation:
            return
        try:
            future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(self.current_dir), str(exc))
        self.cache_ready = True
        if self.folder_cache is not None:
            self.photo_details = self.folder_cache.load_photo_details(
                include_metadata=ENABLE_EXIF_METADATA
            )
            self.image_embeddings = self.folder_cache.load_image_embeddings()
            for path, item in self.items_by_path.items():
                item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        self._refresh_camera_filter()
        # Series membership depends on embeddings, which are only available
        # after the folder cache has loaded. Rebuild the grid now so a restored
        # checked state reflects the actual collapsed-series view.
        self._apply_view()
        self._update_analysis_controls()
        self._refresh_ai_status()
        if ENABLE_EXIF_METADATA and self.folder_cache is not None:
            self.metadata_pipeline.scan(
                [path for path in self.view_paths if is_supported_image(path)],
                self.folder_cache,
                self.bridge.metadataUpdated.emit,
            )
        self.thumb_index = 0
        self._schedule_visible_thumb_priority()
        self.thumb_timer.start()
        self._attach_shotsync_syncer()

    def _apply_view(self, *_args) -> None:
        if not hasattr(self, "rating_filter"):
            return
        rating = self.rating_filter.currentData()
        color = self.color_filter.currentData()
        media = self.media_filter.currentData()
        camera_key = self.camera_filter.currentData()
        shot = self.shot_filter.currentData()
        needle = self.search_edit.text().strip().casefold()

        def visible(path: Path) -> bool:
            # Always keep directories - never filter out folders, no matter what
            if path.is_dir():
                return True
            # All filters only apply to actual image files
            if media == "video" and not is_supported_video(path):
                return False
            if media == "image" and is_supported_video(path):
                return False
            detail = self.photo_details.get(path.name, {})
            if camera_key is not None and self._camera_filter_key(detail) != camera_key:
                return False
            if rating is not None and detail.get("rating") != rating:
                return False
            if self.color_filter.currentIndex() > 0 and detail.get("color_label", "") != color:
                return False
            faces = detail.get("faces") or []
            if shot is not None and self._shot_size(detail) != shot:
                return False
            if self.face_reference is not None and not any(
                self._face_similarity(self.face_reference, face.get("embedding", [])) >= 0.42
                for face in faces if isinstance(face, dict)
            ):
                return False
            return not needle or needle in path.name.casefold() or needle in str(detail.get("comment", "")).casefold()

        order = self.sort_combo.currentData()
        if order == "rating":
            key = lambda path: (-(self.photo_details.get(path.name, {}).get("rating") or 0), path.name.casefold())
            reverse = False
        elif order and order.startswith("time"):
            key = lambda path: path.stat().st_mtime_ns
            reverse = order.endswith("desc")
        else:
            key = lambda path: path.name.casefold()
            reverse = order == "name_desc"
        self.view_paths = _build_photo_view(self.all_paths, predicate=visible, sort_key=key, reverse=reverse)
        self.paths = self._grid_paths_with_series(self.view_paths)
        self.view_generation += 1
        self.populate_timer.stop()
        self.grid.clear()
        self.items_by_path.clear()
        self.populate_index = 0
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self._update_analysis_controls()
        self._refresh_status_panel()
        self.populate_timer.start()

    def _collapse_series_paths(self, paths: list[Path]) -> list[Path]:
        if not paths:
            return []
        collapsed = [paths[0]]
        for previous, path in zip(paths, paths[1:]):
            if self._embedding_similarity(previous, path) < 0.92:
                collapsed.append(path)
        return collapsed

    def _grid_paths_with_series(self, paths: list[Path]) -> list[Path]:
        self.series_cards = {}
        if not self.series_toggle.isChecked():
            return list(paths)
        # Folders are navigation targets, not photos. Keep them at the top
        # and exclude them completely from AI-series grouping.
        result: list[Path] = [path for path in paths if path.is_dir()]
        group: list[Path] = []

        def flush() -> None:
            if not group:
                return
            leader = group[0]
            if len(group) == 1:
                result.append(leader)
            elif leader in self.expanded_series:
                for index, path in enumerate(group):
                    result.append(path)
                    self.series_cards[path] = {
                        "count": len(group) if index == 0 else 0,
                        "expanded": index == 0,
                        "member": index > 0,
                        "leader": leader,
                    }
            else:
                result.append(leader)
                self.series_cards[leader] = {"count": len(group), "expanded": False, "leader": leader}
            group.clear()

        for path in paths:
            if path.is_dir():
                continue
            if group and self._embedding_similarity(group[-1], path) < 0.92:
                flush()
            group.append(path)
        flush()
        self.expanded_series.intersection_update(self.series_cards)
        return result

    def _toggle_grid_series(self, path: Path) -> None:
        series = self.series_cards.get(path) or {}
        leader = series.get("leader", path)
        if leader in self.expanded_series:
            self.expanded_series.remove(leader)
        else:
            self.expanded_series.clear()
            self.expanded_series.add(leader)
        self._apply_view()
        if self.stack.currentWidget() is self.full_view and self.current_path is not None:
            self._refresh_full_view_navigation(self.current_path)

    def _selected_paths(self) -> list[Path]:
        return [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.grid.selectedItems()]

    def _selection_changed(self) -> None:
        selected = self._selected_paths()
        self._refresh_status_panel()
        if len(selected) == 1:
            self.comment_edit.setText(str(self.photo_details.get(selected[0].name, {}).get("comment", "")))
        elif not selected:
            self.comment_edit.clear()

    def _update_selection(self, **changes) -> None:
        selected_paths = self._selected_paths()
        paths = list(selected_paths)
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            # The grid retains the series leader as its selection. In the
            # viewer, metadata belongs exclusively to the opened series frame.
            paths = [self.current_path]
        elif {"rating", "color_label"}.intersection(changes):
            # A collapsed series is represented by one leader card, so a mark
            # on that card belongs to every hidden frame. Once expanded, each
            # visible card is an independent target.
            targets: list[Path] = []
            for path in paths:
                series = self.series_cards.get(path) or {}
                if int(series.get("count", 0) or 0) > 1 and not series.get("expanded"):
                    targets.extend(self._series_for_path(path))
                else:
                    targets.append(path)
            paths = list(dict.fromkeys(targets))
        for path in paths:
            detail = self.photo_details.setdefault(path.name, {})
            detail.update(changes)
            item = self.items_by_path.get(path)
            if item is not None:
                item.setData(DETAIL_ROLE, dict(detail))
            if self.folder_cache is not None and self.cache_ready:
                self.folder_cache.store_photo_selection(
                    path.name,
                    rating=detail.get("rating"),
                    color_label=detail.get("color_label", ""),
                    comment=detail.get("comment", ""),
                )
            # If this folder is a ShotSync selection, ship the mark back to the
            # server (durably queued; retried automatically while offline).
            if self._shotsync_syncer is not None:
                self._shotsync_syncer.queue_mark(path.name, detail=dict(detail), changes=changes)
        self.grid.viewport().update()
        if self.stack.currentWidget() is self.grid_page:
            self.meta_bar.set_metadata(
                self.photo_details.get(selected_paths[0].name, {}) if len(selected_paths) == 1 else {}
            )
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            self.full_view.set_metadata(
                self.photo_details.get(self.current_path.name, {}),
                (self.current_path,),
            )
            self._refresh_full_view_navigation(self.current_path)
        if self.auto_advance and len(selected_paths) == 1:
            if self.stack.currentWidget() is self.full_view:
                self._move(1)
            else:
                item = self.items_by_path.get(selected_paths[0])
                row = self.grid.row(item) if item is not None else -1
                if 0 <= row + 1 < self.grid.count():
                    self.grid.clearSelection()
                    self.grid.setCurrentRow(row + 1)

    def _set_selected_rating(self, rating: int | None) -> None:
        self._update_selection(rating=rating)

    def _set_selected_color(self, color: str) -> None:
        self._update_selection(color_label=color)

    def _save_comment(self) -> None:
        self._update_selection(comment=self.comment_edit.text().strip())

    def _save_full_comment(self, comment: str) -> None:
        self._update_selection(comment=comment)

    def _configure_quick_mark(self, kind: str, value: object) -> None:
        self.quick_mark = (kind, value)
        self.settings.setValue("quick_mark_kind", kind)
        self.settings.setValue("quick_mark_value", value)
        self.full_view.set_quick_mark(kind, value)

    def _set_auto_advance(self, enabled: bool) -> None:
        self.auto_advance = enabled
        self.settings.setValue("auto_advance", enabled)

    def _apply_quick_mark(self) -> None:
        kind, value = self.quick_mark
        paths = self._selected_paths()
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            paths = [self.current_path]
        if not paths:
            return
        current = self.photo_details.get(paths[0].name, {}).get(kind)
        self._update_selection(**{kind: None if current == value else value})

    def _load_face_sets(self) -> list[dict]:
        """Load globally saved people independently of the current folder cache."""
        raw = self.settings.value("face_sets", "", str)
        try:
            entries = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []
        return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("embedding"), list)]

    def _save_face_sets(self) -> None:
        self.settings.setValue("face_sets", json.dumps(self.face_sets, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _pixmap_to_base64(pixmap: QPixmap) -> str:
        if pixmap.isNull():
            return ""
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.toImage().save(buffer, "PNG")
        return bytes(buffer.data().toBase64()).decode("ascii")

    @staticmethod
    def _face_avatar_from_entry(entry: dict, size: int = 40) -> QPixmap:
        try:
            image = QImage.fromData(base64.b64decode(str(entry.get("avatar") or "")))
        except (ValueError, TypeError):
            image = QImage()
        if image.isNull():
            return QPixmap()
        return QPixmap.fromImage(image).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _current_face_avatar(self, face: dict, size: int = 80) -> QPixmap:
        """Prefer the largest decoded frame; the on-screen image can be 640px fallback."""
        if self.current_path is not None:
            candidates = [
                (variant, decoded) for (path, variant), decoded in self.memory_cache.items()
                if path == self.current_path and variant > THUMB_SIZE
            ]
            if candidates:
                _variant, decoded = max(candidates, key=lambda candidate: candidate[0])
                return FullImageView.face_avatar_from_pixmap(QPixmap.fromImage(decoded.image), face, size)
        return self.full_view.face_avatar(face, size)

    def _add_face_to_set(self, face: object) -> None:
        if not isinstance(face, dict) or not isinstance(face.get("embedding"), list):
            return
        embedding = face["embedding"]
        if any(self._face_similarity(embedding, item.get("embedding", [])) >= 0.98 for item in self.face_sets):
            return
        avatar = self._current_face_avatar(face, 80)
        self.face_sets.append({
            "id": sha1(json.dumps(embedding).encode()).hexdigest()[:12],
            "name": "Без имени",
            "embedding": embedding,
            "avatar": self._pixmap_to_base64(avatar),
        })
        self._save_face_sets()
        self._update_analysis_controls()

    def _face_set_by_id(self, face_id: str) -> dict | None:
        return next((entry for entry in self.face_sets if entry.get("id") == face_id), None)

    def _show_face_sets(self) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("faceSetsDialog")
        dialog.setWindowTitle("Наборы лиц")
        dialog.resize(560, 420)
        layout = QVBoxLayout(dialog)
        title = QLabel("Наборы лиц")
        title.setObjectName("faceSetsTitle")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(6, 6, 6, 6)
        body_layout.setSpacing(6)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        close = QPushButton("Закрыть")
        close.setFixedHeight(34)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

        def rebuild() -> None:
            while body_layout.count():
                child = body_layout.takeAt(0)
                if child.widget() is not None:
                    child.widget().deleteLater()
            if not self.face_sets:
                body_layout.addWidget(QLabel("Добавьте лицо, нажав «В набор» на фотографии."))
                body_layout.addStretch(1)
                return
            for entry in self.face_sets:
                face_id = str(entry["id"])
                row = QFrame()
                row.setObjectName("faceSetRow")
                row_layout = QHBoxLayout(row)
                avatar = QLabel()
                avatar.setFixedSize(44, 44)
                avatar.setObjectName("faceSetAvatar")
                avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pixmap = self._face_avatar_from_entry(entry, 40)
                avatar.setPixmap(pixmap if not pixmap.isNull() else _fomantic_icon("user", 18).pixmap(22, 22))
                row_layout.addWidget(avatar)
                name = QLineEdit(str(entry.get("name") or ""))
                name.setPlaceholderText("Имя")
                name.setFixedHeight(34)
                name.editingFinished.connect(lambda face_id=face_id, edit=name: self._rename_face_set(face_id, edit.text()))
                row_layout.addWidget(name, 1)
                mark = QToolButton()
                mark.setText("Быстрая метка")
                mark.setIcon(_fomantic_icon("bookmark", 13))
                mark.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                mark.setFixedHeight(34)
                mark.clicked.connect(lambda _checked=False, button=mark, face_id=face_id: self._show_face_mark_menu(button, face_id))
                row_layout.addWidget(mark)
                show = QToolButton()
                show.setIcon(_fomantic_icon("images", 13))
                show.setToolTip("Показать фото с этим лицом")
                show.setFixedSize(34, 34)
                show.clicked.connect(lambda _checked=False, face_id=face_id: self._show_face_set(face_id, dialog))
                row_layout.addWidget(show)
                delete = QToolButton()
                delete.setIcon(_fomantic_icon("trash", 13))
                delete.setToolTip("Удалить из набора")
                delete.setFixedSize(34, 34)
                delete.clicked.connect(lambda _checked=False, face_id=face_id: self._delete_face_set(face_id, rebuild))
                row_layout.addWidget(delete)
                body_layout.addWidget(row)
            body_layout.addStretch(1)

        rebuild()
        dialog.exec()

    def _rename_face_set(self, face_id: str, name: str) -> None:
        entry = self._face_set_by_id(face_id)
        if entry is not None:
            entry["name"] = name.strip() or "Без имени"
            self._save_face_sets()

    def _delete_face_set(self, face_id: str, rebuild: Callable[[], None]) -> None:
        self.face_sets = [entry for entry in self.face_sets if entry.get("id") != face_id]
        self._save_face_sets()
        rebuild()

    def _show_face_set(self, face_id: str, dialog: QDialog) -> None:
        entry = self._face_set_by_id(face_id)
        if entry is None:
            return
        self._set_face_reference(entry["embedding"], self._face_avatar_from_entry(entry))
        dialog.accept()

    def _show_face_mark_menu(self, button: QToolButton, face_id: str) -> None:
        menu = QMenu(button)
        for rating in range(5, 0, -1):
            action = menu.addAction("★" * rating)
            action.triggered.connect(lambda _checked=False, value=rating: self._apply_mark_to_face(face_id, "rating", value))
        menu.addSeparator()
        for label, value in (("Красная", "red"), ("Жёлтая", "yellow"), ("Зелёная", "green"), ("Синяя", "blue"), ("Фиолетовая", "purple")):
            action = menu.addAction(label)
            action.setIcon(_color_swatch_icon(value))
            action.triggered.connect(lambda _checked=False, value=value: self._apply_mark_to_face(face_id, "color_label", value))
        menu.addSeparator()
        remove_rating = menu.addAction("У��рать рейтинг")
        remove_rating.setIcon(_fomantic_icon("ban", 12))
        remove_rating.triggered.connect(lambda: self._apply_mark_to_face(face_id, "rating", None))
        remove = menu.addAction("Убрать метку")
        remove.setIcon(_fomantic_icon("ban", 12))
        remove.triggered.connect(lambda: self._apply_mark_to_face(face_id, "color_label", ""))
        menu.popup(button.mapToGlobal(QPoint(0, button.height())))

    def _apply_mark_to_face(self, face_id: str, kind: str, value: object) -> None:
        entry = self._face_set_by_id(face_id)
        if entry is None:
            return
        embedding = entry["embedding"]
        paths = [
            path for path in self.all_paths if path.is_file() and any(
                self._face_similarity(embedding, face.get("embedding", [])) >= 0.42
                for face in self.photo_details.get(path.name, {}).get("faces", []) if isinstance(face, dict)
            )
        ]
        for path in paths:
            detail = self.photo_details.setdefault(path.name, {})
            detail[kind] = value
            if self.folder_cache is not None and self.cache_ready:
                self.folder_cache.store_photo_selection(
                    path.name, rating=detail.get("rating"), color_label=detail.get("color_label", ""),
                    comment=detail.get("comment", ""),
                )
        self.grid.viewport().update()
        self._apply_view()

    @staticmethod
    def _face_similarity(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
        return dot / norm if norm else -1.0

    @staticmethod
    def _shot_size(detail: dict) -> str:
        faces = detail.get("faces") or []
        if not faces:
            return "no_face"
        try:
            ratio = max(
                max(float(face.get("bbox", {}).get("width", 0)), float(face.get("bbox", {}).get("height", 0)))
                for face in faces if isinstance(face, dict)
            )
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio >= 0.32:
            return "closeup"
        if ratio >= 0.14:
            return "medium"
        return "wide"

    def _filter_face_from_full_view(self, face: object) -> None:
        embedding = face.get("embedding") if isinstance(face, dict) else None
        if isinstance(embedding, list) and embedding:
            self._set_face_reference(embedding, self._current_face_avatar(face))

    def _set_face_reference(self, embedding: list[float], avatar: QPixmap | None = None) -> None:
        self.face_reference = embedding
        self.face_filter_avatar = avatar or QPixmap()
        self.settings.setValue("face_filter_embedding", json.dumps(embedding, separators=(",", ":")))
        self.settings.setValue("face_filter_avatar", self._pixmap_to_base64(self.face_filter_avatar))
        if not self.face_filter_avatar.isNull():
            self.face_filter_avatar_label.setPixmap(self.face_filter_avatar.scaled(
                24, 24, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            ))
        else:
            self.face_filter_avatar_label.setPixmap(_fomantic_icon("user", 13).pixmap(16, 16))
        self.face_filter_chip.show()
        self.full_view.set_face_filter(self.face_filter_avatar)
        self._apply_view()
        if self.stack.currentWidget() is self.full_view and self.current_path is not None:
            self._refresh_full_view_navigation(self.current_path)

    def _clear_face_search(self) -> None:
        self.face_reference = None
        self.face_filter_avatar = QPixmap()
        self.settings.remove("face_filter_embedding")
        self.settings.remove("face_filter_avatar")
        self.face_filter_chip.hide()
        self.full_view.clear_face_filter()
        self._update_analysis_controls()
        self._apply_view()
        if self.stack.currentWidget() is self.full_view and self.current_path is not None:
            self._refresh_full_view_navigation(self.current_path)

    def _restore_face_filter_chip(self) -> None:
        if self.face_reference:
            self._set_face_reference(self.face_reference, self.face_filter_avatar)

    def _submit_decode(
        self,
        path: Path,
        max_size: int,
        *,
        full_priority: bool,
        visible_priority: bool = False,
    ) -> None:
        if self.closing:
            return
        key = (path, max_size)
        cached = self._cache_get(key)
        if cached is not None:
            self.bridge.decoded.emit((cached, max_size))
            return
        if key in self.pending:
            return
        if self.folder_cache is None:
            return

        if max_size == THUMB_SIZE:
            cache = self.folder_cache
            executor = self.visible_thumb_cache_lookup_executor if visible_priority else self.background_cache_lookup_executor
            future = executor.submit(cache.load, path, max_size)
            self.pending[key] = future
            if visible_priority:
                self.visible_thumb_pending.add(key)
            future.add_done_callback(
                lambda done, p=path, s=max_size, fp=full_priority, vp=visible_priority: self._cache_lookup_done(
                    p, s, fp, vp, done
                )
            )
            return
        # Full-view images deliberately bypass the disk cache. They are decoded
        # from the source on demand and live only in the bounded RAM LRU.
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def _submit_video_thumbnail(self, path: Path, *, visible_priority: bool) -> None:
        """Use RAM, then SQLite, before falling back to Qt frame decoding."""
        key = (path, THUMB_SIZE)
        preview = self._thumbnail_cache_get(path)
        if preview is not None:
            self.bridge.decoded.emit(
                (DecodedImage(path=path, image=preview, width=preview.width(), height=preview.height()), THUMB_SIZE)
            )
            return
        cached = self._cache_get(key)
        if cached is not None:
            self.bridge.decoded.emit((cached, THUMB_SIZE))
            return
        if key in self.pending or self.folder_cache is None:
            return
        executor = self.visible_thumb_cache_lookup_executor if visible_priority else self.background_cache_lookup_executor
        future = executor.submit(self.folder_cache.load, path, THUMB_SIZE)
        self.pending[key] = future
        if visible_priority:
            self.visible_thumb_pending.add(key)
        future.add_done_callback(
            lambda done, p=path, vp=visible_priority: self._video_thumbnail_cache_lookup_done(p, vp, done)
        )

    def _video_thumbnail_cache_lookup_done(self, path: Path, visible_priority: bool, future: Future) -> None:
        key = (path, THUMB_SIZE)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self.closing or future.cancelled():
            return
        try:
            decoded = future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(path), str(exc))
            return
        if decoded is not None:
            self._cache_put(key, decoded)
            self.bridge.decoded.emit((decoded, THUMB_SIZE))
            return
        if self.workspace_active:
            self.video_thumbnailer.request(path)

    def _submit_process_decode(
        self,
        path: Path,
        max_size: int,
        *,
        full_priority: bool,
        visible_priority: bool = False,
    ) -> None:
        if self.closing:
            return
        key = (path, max_size)
        if key in self.pending:
            return
        is_foreground = False
        if visible_priority:
            executor = self._visible_thumb_decode_executor()
        elif full_priority:
            executor = self._current_decode_executor() if path == self.current_path else self._background_decode_executor()
            if path == self.current_path:
                is_foreground = True
        else:
            executor = self._background_decode_executor()
        try:
            decoder = decode_pixels if full_priority else decode_thumbnail_pixels
            future = executor.submit(decoder, path, max_size)
        except RuntimeError:
            # Shutdown may begin between the guard above and submit because
            # cache callbacks execute on worker threads.
            if self.closing:
                return
            raise
        self.pending[key] = future
        if is_foreground:
            self.foreground_full_futures[key] = future
        future.add_done_callback(lambda done, p=path, s=max_size: self._decode_done(p, s, done))

    def _cache_lookup_done(
        self,
        path: Path,
        max_size: int,
        full_priority: bool,
        visible_priority: bool,
        future: Future,
    ) -> None:
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self.closing:
            return
        if future.cancelled():
            return
        try:
            decoded = future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(path), str(exc))
            return
        if decoded is not None:
            self._cache_put((path, max_size), decoded)
            self.bridge.decoded.emit((decoded, max_size))
            return
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def _decode_done(self, path: Path, max_size: int, future: Future) -> None:
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self.foreground_full_futures.get(key) is future:
            self.foreground_full_futures.pop(key, None)
        if self.closing:
            return
        if future.cancelled():
            return
        try:
            result = future.result()
            if isinstance(result, PixelImage):
                if self.folder_cache is not None and max_size == THUMB_SIZE:
                    self.folder_cache.store_pixels(result, max_size)
                decoded = pixel_to_decoded(result)
            else:
                decoded = result
            self._cache_put((path, max_size), decoded)
            self.bridge.decoded.emit((decoded, max_size))
        except Exception as exc:
            self.bridge.failed.emit(str(path), str(exc))

    def _on_decoded(self, payload: object) -> None:
        decoded, max_size = payload
        self.visible_thumb_pending.discard((decoded.path, max_size))
        if not self.workspace_active:
            return
        item = self.items_by_path.get(decoded.path)
        if max_size == THUMB_SIZE:
            self._thumbnail_cache_put(decoded.path, decoded.image)
            if item is not None:
                item.setData(PREVIEW_ROLE, decoded.image)
                self.grid.update(self.grid.visualItemRect(item))
            self.full_view.update_preview(decoded.path, decoded.image)
            if is_supported_video(decoded.path):
                self.full_view.set_video_preview(decoded.path, decoded.image)
        if (
            self.stack.currentWidget() is self.full_view
            and decoded.path == self.current_path
            and not is_supported_video(decoded.path)
        ):
            if max_size > THUMB_SIZE or not self.full_view.has_image or self.full_view.is_fallback:
                self.full_view.set_image(decoded, fallback=max_size == THUMB_SIZE)
        if max_size > THUMB_SIZE and decoded.path == self.current_path:
            self.thumb_timer.start()
        # Full-size frames and their preloaded neighbours do not change
        # thumbnail progress. Updating it here used to scan the whole folder
        # and call the OS taskbar API after every full-view decode.
        if max_size == THUMB_SIZE and self.stack.currentWidget() is not self.full_view:
            self._refresh_status_panel()

    def _on_video_preview(self, path: Path, preview: QImage) -> None:
        if self.closing or not self.workspace_active or preview.isNull():
            return
        preview = preview.scaled(THUMB_SIZE, THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        if self.folder_cache is not None and self.cache_ready:
            rgba = preview.convertToFormat(QImage.Format.Format_RGBA8888)
            self.folder_cache.store_pixels(
                PixelImage(path=path, pixels=bytes(rgba.bits()), width=rgba.width(), height=rgba.height()),
                THUMB_SIZE,
            )
        self._thumbnail_cache_put(path, preview)
        item = self.items_by_path.get(path)
        if item is not None:
            item.setData(PREVIEW_ROLE, preview)
            self.grid.update(self.grid.visualItemRect(item))
        self.full_view.update_preview(path, preview)
        self.full_view.set_video_preview(path, preview)
        if self.stack.currentWidget() is not self.full_view:
            self._refresh_status_panel()

    def _on_decode_failed(self, path: str, message: str) -> None:
        self.visible_thumb_pending.discard((Path(path), THUMB_SIZE))
        item = self.items_by_path.get(Path(path))
        if item is not None:
            item.setText(f"{Path(path).name}\n{message}")
        if Path(path) == self.current_path:
            self.thumb_timer.start()
        if self.stack.currentWidget() is not self.full_view:
            self._refresh_status_panel()

    def _open_selected(self) -> None:
        item = self.grid.currentItem()
        if item:
            self.open_full(Path(item.data(Qt.ItemDataRole.UserRole)))

    def _grid_current_item_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.pending_grid_full_request = None
            self.grid_full_request_timer.stop()
            return
        value = current.data(Qt.ItemDataRole.UserRole)
        if not value:
            return
        path = Path(value)
        self.current_path = path
        self.workspace_state.current_photo = path
        if self.stack.currentWidget() is self.grid_page and hasattr(self, "meta_bar"):
            self.meta_bar.set_metadata(self.photo_details.get(path.name, {}))
        self.pending_grid_full_request = path
        self.grid_full_request_timer.start(70)

    def open_full(self, path: Path) -> None:
        # If this is a directory, navigate into it instead of opening it as an image
        if path.is_dir():
            self.load_directory(path)
            return
            
        now = monotonic()
        rapid_navigation = now - self.last_navigation_at < 0.14
        self.last_navigation_at = now
        self.current_path = path
        self.workspace_state.current_photo = path
        self.workspace_state.thumbnail_size = self.grid.card_size
        self.full_view.set_faces(self.photo_details.get(path.name, {}).get("faces") or [])
        self.full_view.set_metadata(
            self.photo_details.get(path.name, {}),
            (path,),
        )
        self.stack.setCurrentWidget(self.full_view)
        self._refresh_full_view_navigation(path)
        self.fullViewRequested.emit(self)
        self.full_view.setFocus(Qt.FocusReason.OtherFocusReason)
        if is_supported_video(path):
            item = self.items_by_path.get(path)
            preview = item.data(PREVIEW_ROLE) if item is not None else self._thumbnail_cache_get(path)
            self.full_view.set_video(path, preview if isinstance(preview, QImage) else None)
            if not isinstance(preview, QImage) or preview.isNull():
                self.video_thumbnailer.request(path)
            return
        full_size = self._full_preview_size()
        self._suspend_thumbnail_work()
        self._cancel_outdated_full_tasks(path, full_size)
        self._show_best_cached_full(path, full_size)
        in_series = len(self._series_for_path(path)) > 1
        if in_series:
            # Show the compact decode first. In a series this gives immediate
            # visual feedback while the full-resolution frame is still being
            # decoded or read from a slow card.
            self._submit_decode(path, THUMB_SIZE, full_priority=False, visible_priority=True)
            self.pending_full_request = path
            self.full_request_timer.start(55)
        elif rapid_navigation:
            self.pending_full_request = path
            self.full_request_timer.start(55)
        else:
            self.pending_full_request = None
            self._promote_current_full_task(path, full_size)
            self._submit_decode(path, full_size, full_priority=True)
            self._preload_neighbors(path)

    def show_grid(self) -> None:
        # Leaving full view must not keep a video playing in the background.
        self.full_view.stop_video()
        self.stack.setCurrentWidget(self.grid_page)
        self._restore_grid_context()
        self._refresh_status_panel()
        self.gridRequested.emit()

    def _remember_thumbnail_size(self, size: int) -> None:
        self.workspace_state.thumbnail_size = size
        self.settings.setValue("thumbnail_size", size)

    def _restore_grid_context(self) -> None:
        """Return to the card reached in full view without changing its scale."""
        path = self.workspace_state.current_photo or self.current_path
        if path is None:
            return
        item = self.items_by_path.get(path)
        if item is None:
            return
        self.grid.setCurrentItem(item)
        self.grid.scrollToItem(item, QListWidget.ScrollHint.PositionAtCenter)

    def toggle_fullscreen(self) -> None:
        self.fullscreenRequested.emit(self)

    def _enter_fast_fullscreen(self) -> None:
        self.fast_fullscreen = True
        self.normal_geometry = self.geometry()
        self.normal_window_flags = self.windowFlags()
        self.normal_window_state = self.windowState()
        self.full_view.begin_fast_resize()
        self.setUpdatesEnabled(False)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.show()
        self.setUpdatesEnabled(True)
        self.full_view.finish_fast_resize()

    def _leave_fast_fullscreen(self) -> None:
        self.fast_fullscreen = False
        self.full_view.begin_fast_resize()
        self.setUpdatesEnabled(False)
        self.setWindowFlags(self.normal_window_flags)
        self.setWindowState(self.normal_window_state)
        if self.normal_geometry is not None:
            self.setGeometry(self.normal_geometry)
        self.show()
        self.setUpdatesEnabled(True)
        self.full_view.finish_fast_resize()

    def next_image(self) -> None:
        self.last_move_direction = 1
        self._move(1)

    def previous_image(self) -> None:
        self.last_move_direction = -1
        self._move(-1)

    def _move(self, direction: int) -> None:
        if not self.current_path or self.current_path not in self.view_paths:
            return
        targets = self._photo_mode_paths()
        current = self.current_path
        if current not in targets:
            current = self._series_for_path(current)[0]
        index = targets.index(current) + direction
        if 0 <= index < len(targets):
            self.open_full(targets[index])

    def _refresh_full_view_navigation(self, current: Path) -> None:
        strip_paths = self._photo_mode_paths()
        series = self._series_for_path(current)
        strip_current = current if current in strip_paths else series[0]
        try:
            strip_index = strip_paths.index(strip_current)
        except ValueError:
            strip_index = 0
        useful_paths = [*series, *strip_paths[max(0, strip_index - 12) : strip_index + 13]]
        previews: dict[Path, QImage] = {}
        for path in dict.fromkeys(useful_paths):
            item = self.items_by_path.get(path)
            preview = item.data(PREVIEW_ROLE) if item is not None else None
            if isinstance(preview, QImage) and not preview.isNull():
                previews[path] = preview
                continue
            preview = self._thumbnail_cache_get(path)
            if preview is not None:
                previews[path] = preview
                continue
            cached = self._cache_get((path, THUMB_SIZE))
            if cached is not None:
                previews[path] = cached.image
        self.full_view.set_navigation(
            strip_paths,
            strip_current,
            self.photo_details,
            previews,
            series,
            self.view_generation,
            series_current=current,
            strip_series_cards={path: self.series_cards.get(path, {}) for path in strip_paths},
            show_series_strip=not (len(series) > 1 and series[0] in self.expanded_series),
        )
        self._prioritize_full_strip_thumbs(current, strip_paths, series)

    def _prioritize_full_strip_thumbs(self, current: Path, strip_paths: list[Path], series: list[Path]) -> None:
        """Use the existing grid thumbnail queue for the currently useful strips."""
        try:
            index = strip_paths.index(current)
        except ValueError:
            index = 0
        nearby = [*series, *strip_paths[max(0, index - 4) : index + 5]]
        self._prioritize_strip_thumbs(nearby)

    def _prioritize_visible_full_strip_thumbs(self) -> None:
        """Schedule previews as soon as either viewer strip is scrolled."""
        if self.stack.currentWidget() is not self.full_view:
            return
        self._prioritize_strip_thumbs(
            [*self.full_view.photo_strip.visible_paths(), *self.full_view.series_strip.visible_paths()]
        )

    def _prioritize_strip_thumbs(self, paths: list[Path]) -> None:
        """Put strip items ahead of the background scan, preserving UI order."""
        if not self.cache_ready:
            return
        for path in reversed(list(dict.fromkeys(paths))):
            if not path.is_file():
                continue
            key = (path, THUMB_SIZE)
            if key not in self.pending and path not in self.thumb_priority_set:
                self.thumb_priority.appendleft(path)
                self.thumb_priority_set.add(path)
        if self.thumb_priority:
            self.thumb_timer.start()

    def _suspend_thumbnail_work(self) -> None:
        """Give the current full-size decode exclusive scheduling priority."""
        self.thumb_timer.stop()
        self.visible_thumb_timer.stop()
        for key, future in list(self.pending.items()):
            if key[1] == THUMB_SIZE:
                future.cancel()
                self.visible_thumb_pending.discard(key)

    def _photo_mode_paths(self) -> list[Path]:
        """Build the strip order, expanding the same series as the grid."""
        # Filter out directories - only show actual image files in the viewer strip
        image_only_paths = [p for p in self.view_paths if p.is_file()]
        if not self.series_toggle.isChecked():
            return list(image_only_paths)
        result: list[Path] = []
        group: list[Path] = []

        def flush() -> None:
            if not group:
                return
            if group[0] in self.expanded_series:
                result.extend(group)
            else:
                result.append(group[0])
            group.clear()

        for path in image_only_paths:
            if group and self._embedding_similarity(group[-1], path) < 0.92:
                flush()
            group.append(path)
        flush()
        return result

    def _series_for_path(self, path: Path) -> list[Path]:
        if not self.series_toggle.isChecked() or path not in self.view_paths or path.name not in self.image_embeddings:
            return [path]
        index = self.view_paths.index(path)
        start = index
        end = index
        while start > 0 and self._embedding_similarity(self.view_paths[start - 1], self.view_paths[start]) >= 0.92:
            start -= 1
        while end + 1 < len(self.view_paths) and self._embedding_similarity(self.view_paths[end], self.view_paths[end + 1]) >= 0.92:
            end += 1
        return self.view_paths[start : end + 1]

    def _embedding_similarity(self, left: Path, right: Path) -> float:
        a = self.image_embeddings.get(left.name, b"")
        b = self.image_embeddings.get(right.name, b"")
        if not a or len(a) != len(b):
            return -1.0
        av = [value - 128 for value in a]
        bv = [value - 128 for value in b]
        dot = sum(x * y for x, y in zip(av, bv))
        norm = math.sqrt(sum(x * x for x in av) * sum(y * y for y in bv))
        return dot / norm if norm else -1.0

    def _preload_neighbors(self, path: Path) -> None:
        navigation_paths = self._full_preload_paths(path)
        if path not in navigation_paths:
            return
        index = navigation_paths.index(path)
        full_size = self._full_preview_size()
        before = list(reversed(navigation_paths[max(0, index - FULL_PRELOAD_RADIUS) : index]))
        after = navigation_paths[index + 1 : index + FULL_PRELOAD_RADIUS + 1]
        if self.last_move_direction >= 0:
            primary, secondary = after, before
        else:
            primary, secondary = before, after
        # Alternate around the current image while giving the navigation
        # direction the first slot at every distance.
        neighbors = [
            neighbor
            for distance in range(FULL_PRELOAD_RADIUS)
            for group in (primary, secondary)
            for neighbor in group[distance : distance + 1]
        ]
        for neighbor in neighbors:
            self._submit_decode(neighbor, full_size, full_priority=True)

    def _full_preload_paths(self, path: Path) -> list[Path]:
        """Prefer adjacent frames while navigating inside an expanded series."""
        series = self._series_for_path(path)
        if len(series) > 1 and path in series:
            return series
        navigation_paths = self._photo_mode_paths()
        if path not in navigation_paths:
            series_leader = series[0]
            if series_leader in navigation_paths:
                return navigation_paths
        return navigation_paths

    def _cancel_outdated_full_tasks(self, path: Path, full_size: int) -> None:
        keep = {path}
        navigation_paths = self._full_preload_paths(path)
        if path in navigation_paths:
            index = navigation_paths.index(path)
            keep.update(
                navigation_paths[
                    max(0, index - FULL_PRELOAD_RADIUS) : index + FULL_PRELOAD_RADIUS + 1
                ]
            )

        for key, future in list(self.pending.items()):
            pending_path, pending_size = key
            stale_foreground = key in self.foreground_full_futures and pending_path != path
            if pending_size > THUMB_SIZE and (
                stale_foreground or pending_size != full_size or pending_path not in keep
            ):
                future.cancel()

    def _promote_current_full_task(self, path: Path, full_size: int) -> None:
        """Move a queued neighbour decode onto the current-image worker."""
        key = (path, full_size)
        future = self.pending.get(key)
        if future is not None:
            # A running background decode cannot move between executors. Start
            # a foreground duplicate rather than making the current photo wait.
            future.cancel()
            if self.pending.get(key) is future:
                self.pending.pop(key, None)

    def _show_best_cached_full(self, path: Path, full_size: int) -> None:
        full = self._cache_get((path, full_size))
        if full is not None:
            self.full_view.set_image(full)
            return
        thumb = self._cache_get((path, THUMB_SIZE))
        if thumb is not None:
            self.full_view.set_image(thumb, fallback=True)
            return
        if self.folder_cache is None:
            return
        thumb = self.folder_cache.load(path, THUMB_SIZE)
        if thumb is not None:
            self._cache_put((path, THUMB_SIZE), thumb)
            self.full_view.set_image(thumb, fallback=True)

    def _full_preview_size(self) -> int:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 2560
        size = screen.geometry().size()
        ratio = screen.devicePixelRatio()
        return max(THUMB_SIZE, int(max(size.width(), size.height()) * ratio))

    def _submit_pending_full_request(self) -> None:
        path = self.pending_full_request
        self.pending_full_request = None
        if path is None or path != self.current_path:
            return
        full_size = self._full_preview_size()
        self._promote_current_full_task(path, full_size)
        self._submit_decode(path, full_size, full_priority=True)
        self._preload_neighbors(path)

    def _submit_pending_grid_full_request(self) -> None:
        path = self.pending_grid_full_request
        self.pending_grid_full_request = None
        item = self.grid.currentItem()
        if (
            path is None
            or self.stack.currentWidget() is not self.grid_page
            or item is None
            or item.data(Qt.ItemDataRole.UserRole) != str(path)
        ):
            return
        full_size = self._full_preview_size()
        self._cancel_outdated_full_tasks(path, full_size)
        self._promote_current_full_task(path, full_size)
        self._submit_decode(path, full_size, full_priority=True)

    def _cache_get(self, key: tuple[Path, int]) -> DecodedImage | None:
        decoded = self.memory_cache.get(key)
        if decoded is None:
            return None
        self.memory_cache.move_to_end(key)
        return decoded

    def _thumbnail_cache_get(self, path: Path) -> QImage | None:
        image = self.thumbnail_cache.get(path)
        if image is not None:
            self.thumbnail_cache.move_to_end(path)
        return image

    def _thumbnail_cache_put(self, path: Path, image: QImage) -> None:
        previous = self.thumbnail_cache.pop(path, None)
        if previous is not None:
            self.thumbnail_cache_bytes -= previous.sizeInBytes()
        self.thumbnail_cache[path] = image
        self.thumbnail_cache_bytes += image.sizeInBytes()
        while self.thumbnail_cache and self.thumbnail_cache_bytes > THUMBNAIL_RAM_CACHE_LIMIT_BYTES:
            _path, expired = self.thumbnail_cache.popitem(last=False)
            self.thumbnail_cache_bytes -= expired.sizeInBytes()

    def _cache_put(self, key: tuple[Path, int], decoded: DecodedImage) -> None:
        self.memory_cache[key] = decoded
        self.memory_cache.move_to_end(key)
        self._trim_memory_cache()

    def _trim_memory_cache(self) -> None:
        full_keys = [key for key in self.memory_cache if key[1] > THUMB_SIZE]
        while len(full_keys) > FULL_RAM_CACHE_LIMIT:
            self.memory_cache.pop(full_keys.pop(0), None)
        while len(self.memory_cache) > RAM_CACHE_LIMIT:
            self.memory_cache.popitem(last=False)

    def _current_decode_executor(self) -> ProcessPoolExecutor:
        if self.current_decode_executor is None:
            self.current_decode_executor = ProcessPoolExecutor(max_workers=CURRENT_DECODE_WORKERS)
        return self.current_decode_executor

    def _background_decode_executor(self) -> ProcessPoolExecutor:
        if self.background_decode_executor is None:
            self.background_decode_executor = ProcessPoolExecutor(max_workers=BACKGROUND_DECODE_WORKERS)
        return self.background_decode_executor

    def _visible_thumb_decode_executor(self) -> ProcessPoolExecutor:
        if self.visible_thumb_decode_executor is None:
            self.visible_thumb_decode_executor = ProcessPoolExecutor(max_workers=VISIBLE_THUMB_DECODE_WORKERS)
        return self.visible_thumb_decode_executor

    def _flush_folder_cache(self, *, wait: bool, close: bool = False) -> None:
        cache = self.folder_cache
        if cache is None:
            return
        # Preview rows are committed as they are written.  Closing still runs
        # off the UI thread because SQLite may checkpoint its WAL file.
        future = self.cache_flush_executor.submit(_flush_and_close, cache, close)
        if wait:
            future.result()

    def _initial_directory(self) -> Path:
        saved = self.settings.value("last_directory", "", str)
        if saved:
            path = Path(saved)
            if path.exists() and path.is_dir():
                return path
        return Path.home()


class MainWindow(QMainWindow):
    """Application shell that owns independently stateful folder workspaces."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("RAWww", "RAWww")
        self.setWindowTitle("RAWww")
        self.resize(1440, 920)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = ChromeTitleBar(self)
        self.title_bar.setObjectName("chromeTitleBar")
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(7, 0, 5, 0)
        title_layout.setSpacing(3)
        app_icon = QLabel()
        app_icon.setObjectName("appIcon")
        app_icon.setToolTip("RAWww")
        app_icon.setPixmap(_chrome_icon("app").pixmap(16, 16))
        title_layout.addWidget(app_icon)
        self.tabs = ChromeTabBar()
        self.tabs.setObjectName("workspaceTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(False)
        self.tabs.setMovable(True)
        self.tabs.setExpanding(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.tabs.setFixedHeight(38)
        self.tabs.currentChanged.connect(self._select_workspace)
        self.tabs.closeRequested.connect(self._close_workspace)
        title_layout.addWidget(self.tabs)
        add_tab = QToolButton()
        add_tab.setObjectName("titleAction")
        add_tab.setIcon(_chrome_icon("plus"))
        add_tab.setIconSize(QSize(16, 16))
        add_tab.setToolTip("Новая вкладка")
        add_tab.clicked.connect(self._add_workspace)
        title_layout.addWidget(add_tab)
        title_layout.addStretch(1)
        settings = QToolButton()
        settings.setObjectName("titleAction")
        settings.setIcon(_chrome_icon("settings"))
        settings.setIconSize(QSize(16, 16))
        settings.setToolTip("Настройки")
        title_layout.addWidget(settings)
        for icon, tooltip, callback in (
            ("minimize", "Свернуть", self.showMinimized),
            ("maximize", "Развернуть", self._toggle_maximized),
            ("close", "Закрыть", self.close),
        ):
            button = QToolButton()
            button.setObjectName("windowControl")
            button.setIcon(_chrome_icon(icon))
            button.setIconSize(QSize(16, 16))
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            title_layout.addWidget(button)

        self.workspace_stack = QStackedWidget()
        root_layout.addWidget(self.title_bar)
        root_layout.addWidget(self.workspace_stack, 1)
        self.setCentralWidget(root)
        self._create_actions()
        self._restore_workspaces()

    def _add_workspace(self, directory: Path | None = None) -> None:
        workspace = Workspace(directory)
        # A workspace is a regular widget inside the tab host, not a second
        # native top-level window.
        workspace.setWindowFlags(Qt.WindowType.Widget)
        index = self.workspace_stack.addWidget(workspace)
        self.tabs.addTab(_workspace_title(workspace.current_dir))
        workspace.windowTitleChanged.connect(
            lambda title, view=workspace: self._update_workspace_title(view, title)
        )
        workspace.fullViewRequested.connect(self._show_full_view)
        workspace.fullscreenRequested.connect(self._toggle_workspace_fullscreen)
        workspace.gridRequested.connect(self._leave_full_view)
        workspace.openFolderRequested.connect(self._open_folder_tab)
        self.tabs.setCurrentIndex(index)
        self._select_workspace(self.tabs.currentIndex())
        self._update_tab_geometry()

    def _open_folder_tab(self, folder: Path) -> None:
        """Focus an existing tab for ``folder`` or open a new one."""
        folder = Path(folder)
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and workspace.current_dir == folder:
                self.tabs.setCurrentIndex(index)
                self._select_workspace(index)
                return
        self._add_workspace(folder)

    def _create_actions(self) -> None:
        next_tab = QAction("Next workspace", self)
        next_tab.setShortcut(QKeySequence("Ctrl+Right"))
        next_tab.triggered.connect(lambda: self._select_relative_workspace(1))
        self.addAction(next_tab)

        previous_tab = QAction("Previous workspace", self)
        previous_tab.setShortcut(QKeySequence("Ctrl+Left"))
        previous_tab.triggered.connect(lambda: self._select_relative_workspace(-1))
        self.addAction(previous_tab)

    def _select_relative_workspace(self, step: int) -> None:
        count = self.tabs.count()
        if count <= 1:
            return
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % count)

    def _select_workspace(self, index: int) -> None:
        if index >= 0:
            self.workspace_stack.setCurrentIndex(index)
        for workspace_index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(workspace_index)
            if isinstance(workspace, Workspace):
                workspace.set_workspace_active(workspace_index == index)

    def _update_workspace_title(self, workspace: Workspace, title: str) -> None:
        index = self.workspace_stack.indexOf(workspace)
        if index >= 0:
            self.tabs.setTabText(index, title)

    def _close_workspace(self, index: int) -> None:
        workspace = self.workspace_stack.widget(index)
        if workspace is None:
            return
        self.tabs.removeTab(index)
        self.workspace_stack.removeWidget(workspace)
        workspace.close()
        workspace.deleteLater()
        if self.tabs.count() == 0:
            self._add_workspace()
        else:
            self._update_tab_geometry()

    def closeEvent(self, event) -> None:  # noqa: N802
        directories = [
            str(workspace.current_dir)
            for index in range(self.workspace_stack.count())
            if (workspace := self.workspace_stack.widget(index)) is not None and workspace.current_dir.is_dir()
        ]
        self.settings.setValue("open_workspaces", directories)
        self.settings.setValue("active_workspace", self.tabs.currentIndex())
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if workspace is not None:
                workspace.close()
        super().closeEvent(event)

    def _toggle_maximized(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_tab_geometry()

    def _update_tab_geometry(self) -> None:
        if not hasattr(self, "tabs") or self.tabs.count() == 0:
            return
        # App mark, new-tab action, spacer, settings and three window buttons
        # occupy the rest of the title strip. The tabs themselves shrink before
        # any scrolling affordance is introduced.
        available = max(72, self.width() - 220)
        tab_width = max(72, min(220, available // self.tabs.count()))
        self.tabs.set_tab_width(tab_width)
        self.tabs.setFixedWidth(tab_width * self.tabs.count())

    def _restore_workspaces(self) -> None:
        stored = self.settings.value("open_workspaces", [], list)
        directories = stored if isinstance(stored, list) else [stored]
        for value in directories:
            directory = Path(str(value))
            if directory.is_dir():
                self._add_workspace(directory)
        if self.tabs.count() == 0:
            self._add_workspace()
        active = self.settings.value("active_workspace", 0, int)
        self.tabs.setCurrentIndex(max(0, min(active, self.tabs.count() - 1)))

    def _show_full_view(self, workspace: Workspace) -> None:
        index = self.workspace_stack.indexOf(workspace)
        if index >= 0:
            self.tabs.setCurrentIndex(index)
        if getattr(self, "_fast_full_view", False):
            return
        self._fast_full_view = True
        self._was_maximized_before_full_view = self.isMaximized()
        self._geometry_before_full_view = self.geometry()
        self.title_bar.hide()
        has_full_view = workspace.stack.currentWidget() is workspace.full_view
        if has_full_view:
            workspace.full_view.begin_fast_resize()
        # Let the hidden title bar lay out and present the first frame before
        # Windows performs the (unavoidable) native fullscreen transition.
        QTimer.singleShot(0, lambda view=workspace, full=has_full_view: self._commit_full_view(view, full))

    def _leave_full_view(self) -> None:
        if not getattr(self, "_fast_full_view", False):
            return
        self._fast_full_view = False
        self.showNormal()
        if getattr(self, "_was_maximized_before_full_view", False):
            self.showMaximized()
        elif getattr(self, "_geometry_before_full_view", None) is not None:
            self.setGeometry(self._geometry_before_full_view)
        self.title_bar.show()

    def _commit_full_view(self, workspace: Workspace, has_full_view: bool) -> None:
        if not self._fast_full_view or self.workspace_stack.currentWidget() is not workspace:
            return
        self.showFullScreen()
        if has_full_view:
            QTimer.singleShot(0, workspace.full_view.finish_fast_resize)

    def _toggle_workspace_fullscreen(self, workspace: Workspace) -> None:
        if getattr(self, "_fast_full_view", False):
            self._leave_full_view()
            return
        if getattr(self, "_grid_fullscreen", False):
            self._leave_grid_fullscreen()
            return
        self._grid_fullscreen = True
        self._was_maximized_before_grid_fullscreen = self.isMaximized()
        self._geometry_before_grid_fullscreen = self.geometry()
        # The grid keeps all of its UI, including tabs and side panels; only
        # the native window enters fullscreen so Windows hides the taskbar.
        self.showFullScreen()

    def _leave_grid_fullscreen(self) -> None:
        self._grid_fullscreen = False
        self.showNormal()
        if getattr(self, "_was_maximized_before_grid_fullscreen", False):
            self.showMaximized()
        elif getattr(self, "_geometry_before_grid_fullscreen", None) is not None:
            self.setGeometry(self._geometry_before_grid_fullscreen)


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    _load_viewer_fonts()
    app.setStyleSheet(
        """
        QMainWindow, QWidget {
            background: #1f1f1f;
            color: #d6d6d6;
            font-size: 12px;
        }
        QFrame#chromeTitleBar {
            background: #000000;
            border: 0;
            min-height: 38px;
        }
        QLabel#appIcon {
            color: #d0d0d0;
            font-size: 12px;
            font-weight: 700;
            min-width: 24px;
            qproperty-alignment: AlignCenter;
        }
        QTabBar#workspaceTabs {
            background: transparent;
        }
        QTabBar#workspaceTabs::tab {
            background: transparent;
            border: 0;
            border-radius: 0;
            color: #b5b5b5;
            margin: 0 1px;
            padding: 0 8px;
            font-size: 12px;
            font-weight: 400;
        }
        QTabBar#workspaceTabs::tab:hover:!selected {
            background: #171717;
            color: #e0e0e0;
        }
        QTabBar#workspaceTabs::tab:selected {
            background: transparent;
            color: #f2f2f2;
        }
        QTabBar#workspaceTabs::close-button:hover {
            background: #505050;
            border-radius: 7px;
        }
        QTabBar#workspaceTabs::close-button {
            background: transparent;
            border: 0;
            width: 16px;
            height: 16px;
        }
        QToolButton#titleAction {
            color: #c9c9c9;
            background: transparent;
            border: 0;
            border-radius: 5px;
            min-width: 26px;
            min-height: 30px;
            font-size: 16px;
        }
        QToolButton#titleAction:hover {
            background: #2b2b2b;
        }
        QToolButton#windowControl {
            border: 0;
            border-radius: 0;
            min-width: 32px;
            min-height: 28px;
            font-size: 15px;
        }
        QToolButton#windowControl:hover {
            background: #303030;
        }
        QTreeView, QListWidget {
            background: #252525;
            border: 1px solid #161616;
            color: #d8d8d8;
            alternate-background-color: #202020;
            outline: 0;
        }
        QComboBox {
            background: #2b2b2b;
            border: 1px solid #151515;
            color: #d8d8d8;
            padding: 5px 8px;
        }
        QComboBox:hover {
            background: #333333;
        }
        QComboBox::drop-down {
            border: 0;
            width: 24px;
        }
        QComboBox QAbstractItemView {
            background: #252525;
            border: 1px solid #111111;
            selection-background-color: #3f6db5;
            color: #d8d8d8;
        }
        QToolButton#driveButton {
            background: #292929;
            border: 1px solid #3a3a3a;
            border-radius: 7px;
            color: #e8e8e8;
            font-weight: 600;
            min-height: 26px;
            padding: 1px 6px 1px 5px;
        }
        QToolButton#driveButton:hover {
            background: #363636;
            border-color: #5689d6;
        }
        QToolButton#driveButton:checked {
            background: #315b92;
            border-color: #79aaff;
            color: white;
        }
        QWidget#folderToolbar {
            background: transparent;
        }
        QToolButton#folderToolButton {
            background: #262626;
            border: 1px solid #151515;
            border-radius: 3px;
            min-width: 28px;
            max-width: 28px;
            min-height: 26px;
            max-height: 26px;
            padding: 0;
        }
        QToolButton#folderToolButton:hover {
            background: #303030;
            border-color: #4b4b4b;
        }
        QToolButton#folderToolButton:pressed {
            background: #1b1b1b;
        }
        QToolButton#folderToolButton:disabled {
            background: #202020;
            border-color: #121212;
        }
        QWidget#shotsyncPanel {
            background: transparent;
        }
        QLabel#shotsyncTitle {
            color: #f0f0f0;
            font-size: 15px;
            font-weight: 700;
        }
        QLabel#shotsyncHint {
            color: #8a8a8a;
            font-size: 12px;
        }
        QLabel#shotsyncError {
            color: #e2726e;
            font-size: 12px;
        }
        QLabel#shotsyncSection {
            color: #8a8a8a;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            padding: 2px 2px 0 2px;
        }
        QLineEdit#shotsyncField {
            background: #202020;
            border: 1px solid #3a3a3a;
            border-radius: 6px;
            padding: 7px 9px;
            color: #ededed;
        }
        QLineEdit#shotsyncField:focus {
            border-color: #5689d6;
        }
        QPushButton#shotsyncPrimaryButton {
            background: #315b92;
            border: 1px solid #79aaff;
            border-radius: 6px;
            padding: 8px 12px;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#shotsyncPrimaryButton:hover {
            background: #396bab;
        }
        QPushButton#shotsyncPrimaryButton:disabled {
            background: #2a2a2a;
            border-color: #3a3a3a;
            color: #8a8a8a;
        }
        QWidget#shotsyncProfile {
            background: transparent;
            border: none;
        }
        QLabel#shotsyncProfileName {
            color: #f0f0f0;
            font-size: 13px;
            font-weight: 600;
        }
        QToolButton#shotsyncLogoutButton {
            background: transparent;
            border: none;
            border-radius: 5px;
            padding: 4px;
        }
        QToolButton#shotsyncLogoutButton:hover {
            background: #3a3a3a;
        }
        QListWidget#shotsyncShootingList {
            background: #1c1c1c;
            border: 1px solid #2c2c2c;
            border-radius: 8px;
        }
        QTreeView::item, QListWidget::item {
            padding: 6px;
        }
        QTreeView::item:selected, QListWidget::item:selected {
            background: #3f6db5;
            color: #ffffff;
        }
        QListWidget::item:hover, QTreeView::item:hover {
            background: #333333;
        }
        QSplitter::handle {
            background: #111111;
        }
        QLabel {
            color: #d6d6d6;
        }
        QLabel#overlayLabel {
            background: #191919;
            border-bottom: 1px solid #111111;
            min-height: 30px;
            color: #cfcfcf;
        }
        QScrollBar:vertical, QScrollBar:horizontal {
            background: #202020;
            border: 0;
            width: 6px;
            height: 6px;
        }
        QScrollBar::handle {
            background: #555555;
            border-radius: 2px;
        }
        QScrollBar::handle:hover {
            background: #6a6a6a;
        }
        QScrollBar::add-line, QScrollBar::sub-line {
            width: 0;
            height: 0;
        }
        /* Visual language shared with the ShotSync /v viewer. */
        QWidget#viewerToolbar {
            min-height: 42px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #3d3d3d, stop:0.48 #303030, stop:1 #272727);
            border-bottom: 1px solid #111111;
            border-top: 1px solid #505050;
        }
        QWidget#viewerToolbar QComboBox, QWidget#viewerToolbar QLineEdit,
        QWidget#viewerToolbar QPushButton, QWidget#viewerToolbar QToolButton {
            min-height: 24px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #515151, stop:1 #404040);
            border: 1px solid #1b1b1b;
            border-radius: 2px;
            color: #ececec;
            padding: 2px 8px;
        }
        QWidget#viewerToolbar QLineEdit {
            background: #303030;
            color: #ededed;
            padding-left: 9px;
        }
        QWidget#viewerStatusPanel, QLabel#viewerStatusText {
            background: transparent;
            border: 0;
        }
        QLabel#viewerStatusText {
            color: #c4c4c4;
            font-size: 11px;
            padding: 0;
        }
        QProgressBar#viewerStatusProgress {
            min-height: 14px;
            max-height: 14px;
            border: 1px solid #595959;
            border-radius: 6px;
            background: #111111;
            color: #f0f0f0;
            font-size: 9px;
            padding: 0;
        }
        QProgressBar#viewerStatusProgress::chunk {
            border-radius: 5px;
            background: #707070;
        }
        QWidget#viewerFiltersPanel {
            min-height: 32px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(46, 46, 46, 0.96), stop:1 rgba(35, 35, 35, 0.96));
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 10px;
            padding: 0;
        }
        QWidget#viewerFiltersPanel QLabel {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QWidget#viewerSearchBox {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QLabel#viewerSearchIcon {
            min-width: 14px;
            max-width: 14px;
            min-height: 14px;
            max-height: 14px;
        }
        QWidget#viewerFiltersPanel QComboBox,
        QWidget#viewerFiltersPanel QLineEdit {
            min-height: 24px;
            max-height: 24px;
        }
        QFrame#faceFilterChip {
            background: #343434;
            border: 1px solid #5a5a5a;
            border-radius: 16px;
        }
        QFrame#faceFilterChip QLabel, QFrame#fullFaceFilterChip QLabel,
        QLabel#faceSetAvatar {
            background: transparent;
            border: none;
        }
        QToolButton#faceFilterClear {
            border: none;
            background: transparent;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QFrame#fullFaceFilterChip {
            background: #343434;
            border: 1px solid #5a5a5a;
            border-radius: 16px;
        }
        QToolButton#fullFaceFilterClear {
            border: none;
            background: transparent;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear,
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear:hover,
        QWidget#viewerToolbar QToolButton#fullFaceFilterClear:pressed {
            background: transparent;
            border: none;
            border-radius: 0;
            padding: 0;
            min-width: 0;
            min-height: 0;
        }
        QMenu#faceActionMenu { padding: 0; }
        QToolButton#faceActionButton {
            min-height: 30px;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 3px 7px;
        }
        QToolButton#faceActionButton:hover { background: #3d3d3d; }
        QDialog#faceSetsDialog { background: #292929; }
        QLabel#faceSetsTitle { font-size: 16px; font-weight: 600; }
        QFrame#faceSetRow { background: transparent; border: none; }
        QWidget#viewerFiltersPanel QComboBox {
            padding-left: 7px;
            padding-right: 4px;
        }
        QWidget#viewerFiltersPanel QLineEdit {
            background: #303030;
            padding-left: 9px;
            padding-right: 9px;
        }
        QWidget#viewerToolbar QComboBox:hover, QWidget#viewerToolbar QPushButton:hover,
        QWidget#viewerToolbar QToolButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #606060, stop:1 #4b4b4b);
            border-color: #707070;
        }
        QWidget#viewerAiPanel {
            min-height: 36px;
            background: transparent;
            border-bottom: 1px solid #131313;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
        }
        QLabel#aiPanelTitle {
            color: #a8b0bd;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.7px;
            padding-right: 4px;
        }
        QToolButton#aiFilter {
            min-height: 24px;
            border: 1px solid #363636;
            border-radius: 12px;
            background: #242424;
            color: #c9c9c9;
            padding: 0 9px;
            font-size: 11px;
        }
        QToolButton#aiFilter:hover { background: #303030; color: #f0f0f0; }
        QToolButton#aiFilter:checked {
            background: #315b80;
            border-color: #79aaff;
            color: #ffffff;
        }
        QToolButton#shotFilter {
            min-height: 24px;
            border: 1px solid #363636;
            border-radius: 12px;
            background: #242424;
            color: #c9c9c9;
            padding: 0 9px;
            font-size: 11px;
        }
        QToolButton#shotFilter:hover { background: #303030; color: #f0f0f0; }
        QToolButton#shotFilter:checked {
            background: #315b80;
            border-color: #79aaff;
            color: #ffffff;
        }
        QProgressBar {
            border: 1px solid #171717;
            border-radius: 2px;
            background: #2a2a2a;
            color: #c9c9c9;
            text-align: center;
        }
        QProgressBar::chunk { background: #5284bd; }
        QListWidget#photoGrid {
            background: #666666;
            border: 0;
            color: #252525;
            padding: 3px;
        }
        QListWidget#photoGrid::item { background: transparent; padding: 0; }
        QListWidget#photoGrid::item:selected, QListWidget#photoGrid::item:hover {
            background: transparent;
            color: #252525;
        }
        QWidget#viewerMeta {
            min-height: 30px;
            max-height: 30px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #303030, stop:1 #272727);
            border-top: 1px solid #111111;
            border-bottom: 1px solid #454545;
        }
        QWidget#viewerMeta QPushButton, QWidget#viewerMeta QToolButton {
            color: #c9c9c9;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #4a4a4a, stop:1 #3c3c3c);
            border: 1px solid #171717;
            border-radius: 0;
            padding: 0 6px;
            font-size: 11px;
        }
        QWidget#viewerMeta QPushButton:hover, QWidget#viewerMeta QToolButton:hover {
            background: #505050;
            color: #f4f4f4;
        }
        QWidget#viewerMeta QLineEdit {
            background: #1f1f1f;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 0;
            padding: 0 7px;
            font-size: 11px;
        }
        QTreeView {
            background: #252525;
            border: 0;
        }
        QFrame#fullView, QWidget#fullImageView { background: #1f1f1f; }
        QLabel#overlayLabel {
            background: #252525;
            border: 0;
            border-bottom: 1px solid #111111;
            color: #9fa7b3;
            min-height: 28px;
        }
        QFrame#stripPanel {
            border-top: 1px solid #171717;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #373737, stop:1 #2a2a2a);
        }
        QWidget#stripHeader {
            min-height: 24px;
            background: transparent;
        }
        QToolButton#stripToggle {
            min-width: 46px;
            max-width: 46px;
            min-height: 16px;
            max-height: 16px;
            border: 1px solid #333333;
            border-radius: 8px;
            background: #181818;
            color: #f4f4f5;
            font-size: 13px;
        }
        QToolButton#stripToggle:hover { background: #242424; }
        QToolButton#fullQuickMark {
            min-width: 96px;
            max-width: 96px;
            min-height: 24px;
            max-height: 24px;
            border: 1px solid #1a1a1a;
            border-radius: 0;
            background: #3c3c3c;
        }
        QToolButton#fullQuickMark:hover { background: #505050; }
        QToolButton#fullAutoAdvance {
            min-width: 28px;
            max-width: 28px;
            min-height: 24px;
            max-height: 24px;
            border: 1px solid #1a1a1a;
            border-radius: 0;
            background: #3c3c3c;
        }
        QToolButton#fullAutoAdvance:hover { background: #505050; }
        QToolButton#fullAutoAdvance:checked {
            color: #9fc3f5;
            background: #38495f;
            border-color: #607fa8;
        }
        QToolButton#videoPlay {
            min-width: 28px;
            max-width: 28px;
            min-height: 22px;
            max-height: 22px;
            border: 1px solid #1a1a1a;
            border-radius: 3px;
            background: #3c3c3c;
        }
        QFrame#videoControls {
            background: #252525;
            border: 1px solid #121212;
            border-radius: 7px;
        }
        QLabel#videoTime { min-width: 82px; background: transparent; color: #d5d5d5; font-size: 11px; font-weight: 600; }
        QSlider#videoSeek { background: transparent; }
        QSlider#videoSeek::groove:horizontal { height: 4px; background: #1b1b1b; border-radius: 2px; }
        QSlider#videoSeek::handle:horizontal { width: 10px; margin: -4px 0; background: #c8c8c8; border-radius: 5px; }
        QLineEdit#fullComment {
            min-width: 72px;
            max-width: 420px;
            min-height: 24px;
            max-height: 24px;
            background: #202020;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 0;
            padding: 2px 8px;
        }
        QListWidget#photoStrip {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #373737, stop:1 #2a2a2a);
            border: 0;
            outline: 0;
            padding: 1px 4px 2px 4px;
        }
        QListWidget#seriesStrip {
            background: transparent;
            border: 0;
            outline: 0;
            padding: 0;
        }
        QListWidget#photoStrip::item, QListWidget#seriesStrip::item,
        QListWidget#photoStrip::item:selected, QListWidget#seriesStrip::item:selected,
        QListWidget#photoStrip::item:hover, QListWidget#seriesStrip::item:hover {
            background: transparent;
            padding: 0;
        }
        QFrame#seriesPanel {
            min-width: 136px;
            max-width: 136px;
            border: 1px solid #383838;
            border-radius: 10px;
            background: #151515;
        }
        QToolButton#seriesNav {
            min-height: 26px;
            max-height: 26px;
            border: 1px solid #2f2f2f;
            border-radius: 6px;
            background: #383838;
            color: #ececec;
            font-size: 15px;
        }
        QToolButton#seriesNav:hover { background: #484848; }
        QToolButton#seriesNav:disabled { color: #666666; background: #292929; }
        QWidget#viewerMeta QToolButton#viewerColor {
            min-width: 23px;
            max-width: 23px;
            min-height: 22px;
            max-height: 22px;
            padding: 0;
            border: 1px solid #181818;
            border-left: 0;
            border-radius: 0;
            background: #4e4e4e;
            color: #b8b8b8;
            font-size: 10px;
        }
        QWidget#viewerRatingRow {
            min-width: 149px;
            max-width: 149px;
            min-height: 24px;
            max-height: 24px;
            border: 0;
            border-radius: 0;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #4a4a4a, stop:1 #3b3b3b);
        }
        QWidget#viewerMeta QPushButton#viewerRating {
            min-width: 24px;
            max-width: 24px;
            min-height: 24px;
            max-height: 24px;
            padding: 0;
            border: 0;
            border-left: 0;
            border-radius: 0;
            background: transparent;
            color: rgba(230, 230, 230, 0.22);
            font-size: 10px;
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"] { border-left: 0; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="red"] { background: #7a5555; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="yellow"] { background: #7f7556; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="green"] { background: #5d7560; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="blue"] { background: #596b82; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="purple"] { background: #71607d; }
        QWidget#viewerMeta QToolButton#viewerColor:hover {
            border-color: #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"] {
            min-width: 22px;
            max-width: 22px;
            border-left: 1px solid #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"]:hover { background: #696969; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="red"]:hover { background: #a96a6a; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="yellow"]:hover { background: #aa9a65; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="green"]:hover { background: #719477; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="blue"]:hover { background: #708caa; }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="purple"]:hover { background: #9175a2; }
        QWidget#viewerMeta QToolButton#viewerColor:checked {
            border-color: #181818;
        }
        QWidget#viewerMeta QToolButton#viewerColor[colorLabel="none"]:checked {
            border-left-color: #181818;
        }
        QWidget#viewerMeta QPushButton#viewerRating:hover { background: rgba(255, 255, 255, 0.08); }
        QWidget#viewerMeta QPushButton#viewerRating:checked {
            color: #d8d8d8;
            background: rgba(255, 255, 255, 0.04);
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"] {
            color: rgba(220, 220, 220, 0.42);
        }
        QWidget#viewerMeta QPushButton#viewerRating[ratingClear="true"]:checked {
            color: #c8c8c8;
        }
        QLabel#viewerExif {
            min-width: 0;
            background: transparent;
            border: 0;
            color: #9fa7b3;
            font-size: 11px;
        }
        QMenu QPushButton#quickMarkMenuItem {
            min-width: 170px;
            min-height: 26px;
            max-height: 26px;
            padding: 0;
            border: 0;
            border-radius: 0;
            background: transparent;
            text-align: left;
        }
        QMenu QPushButton#quickMarkMenuItem:hover { background: #454545; }
        QMenu QLabel#quickMarkMenuCheck,
        QMenu QLabel#quickMarkMenuValue {
            background: transparent;
            border: 0;
            color: #dedede;
            font-size: 13px;
        }
        """
    )


def _drive_key(path: Path) -> str:
    anchor = path.anchor or str(path)
    return anchor.replace("\\", "/")


def _workspace_title(directory: Path) -> str:
    return directory.name or str(directory)


def _load_fomantic_icons() -> None:
    global FOMANTIC_ICON_FAMILY
    if FOMANTIC_ICON_FAMILY:
        return
    asset = Path(__file__).with_name("assets") / "fomantic-icons.ttf"
    font_id = QFontDatabase.addApplicationFont(str(asset))
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            FOMANTIC_ICON_FAMILY = families[0]


def _load_viewer_fonts() -> None:
    assets = Path(__file__).with_name("assets")
    for filename in ("Lato-Regular.ttf", "Lato-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(assets / filename))
    _load_fomantic_icons()


def _fomantic_icon(name: str, size: int = 18, color: str = "#d6d6d6") -> QIcon:
    """Render a glyph from the same Fomantic icon font as the /v viewer."""
    glyph = FOMANTIC_ICON_CODES.get(name, "")
    if not glyph or not FOMANTIC_ICON_FAMILY:
        return QIcon()
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(FOMANTIC_ICON_FAMILY)
    font.setPixelSize(size)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


def _color_swatch_icon(color: str | None) -> QIcon:
    """Return a compact colored square for the color filter choices."""
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#8c8c8c"), 1))
    painter.setBrush(QColor(color) if color else QColor("#686868"))
    painter.drawRect(QRect(3, 3, 12, 12))
    painter.end()
    return QIcon(pixmap)


def _chrome_icon(kind: str) -> QIcon:
    """Small, consistent icons inspired by the web viewer's camera avatar."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#d0d0d0"), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    if kind == "app":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2492c4"))
        painter.drawEllipse(QRect(4, 4, 24, 24))
        painter.setBrush(QColor("#f19b38"))
        painter.drawPolygon(QPolygon([QPoint(16, 5), QPoint(25, 10), QPoint(18, 16)]))
        painter.setBrush(QColor("#f5f5f5"))
        painter.drawEllipse(QRect(9, 9, 14, 14))
        painter.setBrush(QColor("#273746"))
        painter.drawEllipse(QRect(12, 12, 8, 8))
        painter.setBrush(QColor("#58b9dc"))
        painter.drawEllipse(QRect(14, 14, 4, 4))
    elif kind == "plus":
        painter.drawLine(10, 16, 22, 16)
        painter.drawLine(16, 10, 16, 22)
    elif kind == "minimize":
        painter.drawLine(9, 20, 23, 20)
    elif kind == "maximize":
        painter.drawRect(QRect(10, 10, 12, 12))
    elif kind == "close":
        painter.drawLine(11, 11, 21, 21)
        painter.drawLine(21, 11, 11, 21)
    elif kind == "settings":
        painter.drawEllipse(QRect(10, 10, 12, 12))
        painter.drawEllipse(QRect(14, 14, 4, 4))
        for start, end in (((16, 6), (16, 9)), ((16, 23), (16, 26)), ((6, 16), (9, 16)), ((23, 16), (26, 16))):
            painter.drawLine(*start, *end)
    painter.end()
    return QIcon(pixmap)


def _sidebar_tool_icon(kind: str) -> QIcon:
    """Monochrome icons for the compact directory toolbar."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#e2e2e2"), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if kind == "up":
        painter.drawLine(7, 24, 15, 16)
        painter.drawLine(15, 16, 23, 24)
        painter.drawLine(15, 16, 15, 9)
    elif kind == "new-folder":
        painter.setBrush(QColor("#f0f0f0"))
        painter.drawRoundedRect(QRectF(5, 10, 22, 14), 2.5, 2.5)
        painter.drawRoundedRect(QRectF(7, 7, 8, 5), 1.8, 1.8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(16, 13, 16, 21)
        painter.drawLine(12, 17, 20, 17)

    painter.end()
    return QIcon(pixmap)


def _mounted_volume_paths() -> list[Path]:
    """Return only mounted, accessible filesystem roots reported by Qt."""
    paths: dict[str, Path] = {}
    for volume in QStorageInfo.mountedVolumes():
        if not volume.isValid() or not volume.isReady():
            continue
        root = Path(volume.rootPath())
        if root.is_dir():
            paths[_drive_key(root)] = root
    return sorted(paths.values(), key=lambda path: _drive_key(path).lower())


def _volume_root_for_path(path: Path, volumes: list[Path]) -> Path | None:
    """Find the mounted volume that owns *path*, including nested mountpoints."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    matching = []
    for volume in volumes:
        try:
            resolved.relative_to(volume)
        except ValueError:
            continue
        matching.append(volume)
    return max(matching, key=lambda volume: len(str(volume)), default=None)


def _volume_label(path: Path) -> str:
    """Use a filesystem label when present, otherwise a portable root label."""
    storage = QStorageInfo(str(path))
    name = storage.displayName().strip()
    return f"{name} ({path})" if name else str(path)


def _volume_button_text(path: Path) -> str:
    """Short root label for the compact volume-button row."""
    drive = path.drive.rstrip("\\/")
    return drive or path.name or str(path)


def _removable_volume_icon() -> QIcon:
    """Create a compact SD-card icon for removable volume buttons."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    card = QPolygon([QPoint(8, 3), QPoint(20, 3), QPoint(25, 8), QPoint(25, 28), QPoint(7, 28), QPoint(7, 4)])
    painter.setPen(QPen(QColor("#d5d5d5"), 1.4))
    painter.setBrush(QColor("#6d6d6d"))
    painter.drawPolygon(card)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#dddddd"))
    for x, width in ((10, 2), (13, 2), (16, 2), (19, 3)):
        painter.drawRect(QRect(x, 5, width, 7))
    painter.setBrush(QColor("#383838"))
    painter.drawRoundedRect(QRect(10, 16, 12, 7), 2, 2)
    painter.end()
    return QIcon(pixmap)


def _is_removable_volume(path: Path) -> bool:
    """Identify removable media with the native mechanism of each OS."""
    if sys.platform == "win32":
        # DRIVE_REMOVABLE covers USB sticks and cards exposed by card readers.
        return ctypes.windll.kernel32.GetDriveTypeW(str(path)) == 2
    if sys.platform.startswith("linux"):
        return _linux_volume_is_removable(path)
    if sys.platform == "darwin":
        return _macos_volume_is_removable(path)
    return False


def _linux_volume_is_removable(path: Path) -> bool:
    """Map a mount point to its sysfs block device and read ``removable``."""
    try:
        mount_path = str(path.resolve())
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            source = after.split()[1] if separator and len(after.split()) > 1 else ""
            if len(fields) < 5 or fields[4] != mount_path or not source.startswith("/dev/"):
                continue
            block = Path("/sys/class/block", Path(source).name)
            # Partition nodes (sdb1, mmcblk0p1) do not contain this property;
            # their resolved parent block device does.
            for device in (block, block.resolve().parent):
                removable = device / "removable"
                if removable.is_file():
                    return removable.read_text(encoding="utf-8").strip() == "1"
            return False
    except OSError:
        pass
    return False


def _macos_volume_is_removable(path: Path) -> bool:
    """Ask diskutil once when a volume button is created on macOS."""
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", str(path)],
            capture_output=True,
            check=True,
            timeout=1,
        )
        info = plistlib.loads(result.stdout)
        return bool(info.get("RemovableMediaOrExternalDevice"))
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return False


def _scan_directory(directory: Path) -> list[Path]:
    try:
        entries = []
        # Collect both subdirectories and supported image files
        for entry in directory.iterdir():
            if entry.is_dir() or (entry.is_file() and is_supported_media(entry)):
                entries.append(entry)
        return entries
    except OSError:
        return []


def _build_photo_view(
    paths: list[Path],
    *,
    predicate: Callable[[Path], bool] | None = None,
    sort_key: Callable[[Path], object] | None = None,
    reverse: bool = False,
) -> list[Path]:
    """Build the ordered, filtered view consumed by grid and navigation.

    The source collection stays untouched, allowing future UI controls to
    rebuild the view without rescanning the directory or coupling their rules
    to thumbnail scheduling.
    """
    # Folders form a permanent top section of the grid. Filtering, photo
    # sorting and series logic below apply to image files only.
    folders = sorted((path for path in paths if path.is_dir()), key=lambda path: path.name.casefold())
    photos = [path for path in paths if not path.is_dir()]
    visible = photos if predicate is None else [path for path in photos if predicate(path)]
    key = sort_key or (lambda path: path.name.lower())
    return [*folders, *sorted(visible, key=key, reverse=reverse)]


def _flush_and_close(cache: FolderCache, close: bool) -> None:
    try:
        cache.flush()
    finally:
        if close:
            cache.close(flush=False)


def main() -> None:
    # Mandatory for frozen (PyInstaller) Windows builds that use
    # ProcessPoolExecutor. Without this, each spawned worker re-launches the
    # GUI, producing an endless cascade of windows. It is a no-op in normal
    # (non-frozen) runs and when not a multiprocessing child.
    import multiprocessing

    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())
