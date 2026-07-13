from __future__ import annotations

import os
import sys
import math
import shutil
import base64
import json
import ctypes
import re
from datetime import datetime
from hashlib import sha1
from uuid import uuid4
import plistlib
import subprocess
import webbrowser
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from send2trash import send2trash

from PySide6.QtCore import QBuffer, QDir, QEvent, QFileInfo, QFileSystemWatcher, QLibraryInfo, QPoint, QPointF, QRect, QRectF, QIODevice, QMimeData, QSettings, QSize, QSizeF, Qt, QTimer, QTranslator, Signal, QObject, QStorageInfo, QItemSelectionModel, QStandardPaths, QUrl, QStringListModel
from PySide6.QtGui import QAction, QColor, QCursor, QDrag, QFont, QFontMetricsF, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygon, QTextCharFormat, QTextFormat, QTextObjectInterface
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QCheckBox,
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
    QSplitterHandle,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
    QFileDialog,
    QMenu,
    QMessageBox,
)

from .cache import FolderCache, cache_size, clear_cache, maintain_folder_caches, prune_folder_cache, relocate_folder_caches, remove_folder_cache
from .decode_cache import DecodeCache
from .decode_scheduler import DecodeScheduler
from .platform_profile import DECODE_USE_PROCESSES
from .shotsync_client import ShotSyncClient
from .shotsync_login import ShotSyncLoginDialog
from .shotsync_hub import shotsync_hub
from .shotsync_panel import ShotSyncPanel
from .shotsync_selection import SelectionMarkSyncer, selection_folder, selection_root
from .imaging import JPEG_EXTENSIONS, RAW_EXTENSIONS, DecodedImage, PixelImage, is_supported_image, is_supported_media, is_supported_video
from .launch import target_from_argv
from .runtime_paths import PORTABLE, data_path, work_path
from .subprocess_utils import no_window_kwargs
from . import theme
from .theme import (
    apply_theme,
    _application_icon,
    _chrome_icon,
    _color_swatch_icon,
    _fomantic_icon,
    _title_bar_icon,
)
from .hotkeys import HOTKEY_DEFAULTS, _hotkey_sequence
from .widgets import SettingsCheckBox
from .dialogs import (
    BatchRenameDialog,
    BatchResizeDialog,
    HelpDialog,
    QuickTransferDialog,
    SettingsDialog,
    ShrinkJpegDialog,
)
from .workspace import WorkspaceRequest, WorkspaceState
from .xmp import build_xmp, write_sidecar
from .updater import fetch_release_info, is_newer
from .version import __version__


THUMB_SIZE = 256
# A non-preview decode key: the complete source used by the 100% inspector.
ORIGINAL_SIZE = 0
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
APP_NAME = "Контролька"
APP_VERSION = __version__
SETTINGS_NAME = "ctrlka"


def _application_settings() -> QSettings:
    if PORTABLE:
        settings_path = work_path() / "settings"
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings_path))
        return QSettings(SETTINGS_NAME, SETTINGS_NAME, QSettings.Format.IniFormat)
    return QSettings(SETTINGS_NAME, SETTINGS_NAME)


def _resize_export_worker(job: tuple[str, str, int, bool, int, bool, float, int, int, bool, int]) -> tuple[str, str, str | None]:
    """Process-isolated JPEG export; RAW files prefer their embedded preview."""
    source_text, output_text, max_side, sharpen, sharpen_amount, unsharp, unsharp_radius, unsharp_amount, unsharp_threshold, keep_exif, raw_orientation = job
    source, output = Path(source_text), Path(output_text)
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp")
    try:
        from io import BytesIO
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        is_raw = source.suffix.lower() in RAW_EXTENSIONS
        if is_raw:
            import rawpy
            with rawpy.imread(str(source)) as raw:
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        image = Image.open(BytesIO(thumb.data))
                    else:
                        image = Image.fromarray(thumb.data)
                except rawpy.LibRawNoThumbnailError:
                    image = Image.fromarray(raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8))
        else:
            image = Image.open(source)
        embedded_orientation = image.getexif().get(274)
        image = ImageOps.exif_transpose(image)
        # Embedded RAW previews often lack their camera file's orientation
        # tag. The regular preview pipeline uses EXIF transpose; apply the
        # cached RAW orientation only when it was absent from that preview.
        if is_raw and not embedded_orientation:
            transforms = {
                2: Image.Transpose.FLIP_LEFT_RIGHT,
                3: Image.Transpose.ROTATE_180,
                4: Image.Transpose.FLIP_TOP_BOTTOM,
                5: Image.Transpose.TRANSPOSE,
                6: Image.Transpose.ROTATE_270,
                7: Image.Transpose.TRANSVERSE,
                8: Image.Transpose.ROTATE_90,
            }
            transform = transforms.get(int(raw_orientation or 1))
            if transform is not None:
                image = image.transpose(transform)
        # exif_transpose already re-serialised info["exif"] without the
        # orientation tag, so it keeps sub-IFDs (GPS, maker notes) that a bare
        # getexif().tobytes() would drop. The ICC profile always travels along.
        if keep_exif:
            exif = image.info.get("exif") or image.getexif().tobytes()
        else:
            exif = b""
        icc_profile = image.info.get("icc_profile")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if sharpen:
            image = ImageEnhance.Sharpness(image).enhance(sharpen_amount / 100)
        if unsharp:
            image = image.filter(ImageFilter.UnsharpMask(radius=unsharp_radius, percent=unsharp_amount, threshold=unsharp_threshold))
        image = image.convert("RGB")
        options = {"format": "JPEG", "quality": 95, "subsampling": 0}
        if exif:
            options["exif"] = exif
        if icc_profile:
            options["icc_profile"] = icc_profile
        image.save(temporary, **options)
        os.replace(temporary, output)
        return source_text, output_text, None
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return source_text, output_text, str(exc)


def _recompress_jpeg_worker(job: tuple[str, int, bool]) -> tuple[str, int, int, str | None]:
    """Re-encode a JPEG in place at a lower quality, keeping ICC and (optionally) EXIF."""
    source_text, quality, keep_exif = job
    source = Path(source_text)
    temporary = source.with_name(f".{source.stem}.{uuid4().hex}.tmp")
    try:
        from PIL import Image

        original_size = source.stat().st_size
        with Image.open(source) as opened:
            opened.load()
            # Orientation is left untouched: the pixels are not transposed, so
            # the original EXIF (incl. its orientation tag), ICC and sub-IFDs
            # stay valid as-is.
            exif = opened.info.get("exif") if keep_exif else None
            icc_profile = opened.info.get("icc_profile")
            image = opened if opened.mode in ("RGB", "L", "CMYK") else opened.convert("RGB")
            options = {"format": "JPEG", "quality": int(quality), "subsampling": "keep"}
            if exif:
                options["exif"] = exif
            if icc_profile:
                options["icc_profile"] = icc_profile
            try:
                image.save(temporary, **options)
            except (ValueError, OSError):
                # "keep" subsampling only works when the source is a JPEG whose
                # chroma layout Pillow can reuse; fall back to the encoder default.
                options.pop("subsampling", None)
                image.save(temporary, **options)
        new_size = temporary.stat().st_size
        os.replace(temporary, source)
        return source_text, original_size, new_size, None
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return source_text, 0, 0, str(exc)


class DecodeBridge(QObject):
    decoded = Signal(object)
    failed = Signal(str, str)
    cacheLoaded = Signal(int, object)
    directoryScanned = Signal(object, Path, object)
    metadataUpdated = Signal(object)
    xmpWritten = Signal(object)


def _write_xmp_task(path: Path, detail: dict, face_sets: list[dict], replacements: dict[str, str]) -> None:
    """Worker-thread entry point; never blocks the Qt event loop on disk I/O."""
    write_sidecar(path, build_xmp(detail, face_sets, replacements))


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

    def cancel(self) -> None:
        """Drop queued/current work while keeping the thumbnailer active."""
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


def _local_paths_from_mime(mime: QMimeData) -> list[Path]:
    """Return existing local file URLs, keeping their original drag order."""
    if not mime.hasUrls():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.exists() and path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


_PREFERRED_DROP_EFFECT_MIME = 'application/x-qt-windows-mime;value="Preferred DropEffect"'


def _mime_requests_move(mime: QMimeData) -> bool:
    """Recognise our cut marker and Windows Explorer's standard cut marker."""
    if mime.hasFormat("application/x-rawww-cut"):
        return True
    effect = bytes(mime.data(_PREFERRED_DROP_EFFECT_MIME))
    return bool(effect and int.from_bytes(effect[:4], "little") & 2)


class PhotoGrid(QListWidget):
    openRequested = Signal(Path)
    viewportChanged = Signal()
    cardSizeChanged = Signal(int)
    seriesToggleRequested = Signal(Path)
    audioRequested = Signal(Path)
    audioHoverChanged = Signal(object)
    deleteRequested = Signal(bool)  # permanent
    pathsDropped = Signal(object, object, object)  # paths, destination, action

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
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._hovered_audio_path: Path | None = None
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
                detail = item.data(DETAIL_ROLE) or {}
                audio_badge = self._audio_badge_rect(rect)
                if detail.get("audio_comment_path") and audio_badge.contains(event.position().toPoint()):
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value:
                        self.audioRequested.emit(Path(value))
                        event.accept()
                        return
                badge = QRect(rect.left() + 6, rect.top() + 6, 32, 12)
                if series.get("count", 0) > 1 and badge.contains(event.position().toPoint()):
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value:
                        self.seriesToggleRequested.emit(Path(value))
                        event.accept()
                        return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Delete:
            self.deleteRequested.emit(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        super().keyPressEvent(event)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        paths = [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.selectedItems()
                 if item.data(Qt.ItemDataRole.UserRole)]
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setData("application/x-rawww-drag", b"1")
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = _local_paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        destination = None
        if item is not None:
            candidate = item.data(Qt.ItemDataRole.UserRole)
            if candidate and Path(candidate).is_dir():
                destination = Path(candidate)
        action = event.proposedAction() if event.mimeData().hasFormat("application/x-rawww-drag") else Qt.DropAction.CopyAction
        self.pathsDropped.emit(paths, destination, action)
        event.acceptProposedAction()

    @staticmethod
    def _audio_badge_rect(rect: QRect) -> QRect:
        return QRect(rect.left() + 7, rect.bottom() - 43, 22, 22)

    def _update_audio_hover(self, position: QPoint) -> None:
        item = self.itemAt(position)
        path = None
        if item is not None and (item.data(DETAIL_ROLE) or {}).get("audio_comment_path"):
            if self._audio_badge_rect(self.visualItemRect(item)).contains(position):
                path = Path(item.data(Qt.ItemDataRole.UserRole))
        if path != self._hovered_audio_path:
            self._hovered_audio_path = path
            self.audioHoverChanged.emit(path)

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.MouseMove:
            self._update_audio_hover(event.position().toPoint())
        elif event.type() == QEvent.Type.Leave and self._hovered_audio_path is not None:
            self._hovered_audio_path = None
            self.audioHoverChanged.emit(None)
        return super().viewportEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._update_audio_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hovered_audio_path is not None:
            self._hovered_audio_path = None
            self.audioHoverChanged.emit(None)
        super().leaveEvent(event)

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


class AudioToggleButton(QToolButton):
    """Round full-view audio control with the web viewer's progress ring."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.progress = 0.0
        self.setAutoRaise(True)

    def set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(1.0, float(value)))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        radius = min(self.width(), self.height()) / 2 - 3
        painter.setPen(QPen(QColor(255, 255, 255, 70), 2))
        painter.drawEllipse(QPointF(center), radius, radius)
        painter.setPen(QPen(QColor(235, 238, 241), 3))
        painter.drawArc(QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2), 90 * 16, -round(self.progress * 360 * 16))
        icon = self.icon()
        if not icon.isNull():
            icon.paint(painter, self.rect().adjusted(10, 10, -10, -10), Qt.AlignmentFlag.AlignCenter)


class MarkIndicatorButton(QToolButton):
    """Antialiased circular quick-mark control for Full View."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = QColor("#4d535b")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_mark_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow = QRectF(self.rect()).adjusted(2.5, 3.5, -1.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawEllipse(shadow)
        rect = QRectF(self.rect()).adjusted(1.5, 1.0, -1.5, -2.0)
        fill = self._color.lighter(112) if self.underMouse() else self._color
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0, fill.lighter(116))
        gradient.setColorAt(0.48, fill)
        gradient.setColorAt(1, fill.darker(118))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 82), 1.0))
        painter.drawEllipse(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 65), 1.0))
        painter.drawEllipse(rect.adjusted(2.0, 2.0, -2.0, -2.0))
        if self.text():
            font = QFont(self.font())
            font.setPointSize(13)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            metrics = QFontMetricsF(font)
            glyph_bounds = metrics.tightBoundingRect(self.text())
            center = QRectF(self.rect()).center()
            painter.drawText(
                QPointF(
                    center.x() - glyph_bounds.width() / 2 - glyph_bounds.left(),
                    center.y() - glyph_bounds.height() / 2 - glyph_bounds.top(),
                ),
                self.text(),
            )


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
            text_height = 22 if self.compact else 28
            text_rect = QRect(rect.left() + 8, rect.bottom() - text_height - 5, rect.width() - 16, text_height)
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
                icon_font = QFont(theme.FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(10)
                painter.setFont(icon_font)
                painter.drawText(video_badge, Qt.AlignmentFlag.AlignCenter, theme.FOMANTIC_ICON_CODES["film"] if theme.FOMANTIC_ICON_FAMILY else "▶")

            if detail.get("audio_comment_path"):
                audio_badge = QRect(image_rect.left() + 5, image_rect.bottom() - 25, 22, 22)
                painter.setBrush(QColor(243, 245, 247, 235))
                painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
                painter.drawEllipse(audio_badge)
                icon_font = QFont(theme.FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(10)
                painter.setFont(icon_font)
                painter.setPen(QColor("#30363d"))
                painter.drawText(
                    audio_badge,
                    Qt.AlignmentFlag.AlignCenter,
                    theme.FOMANTIC_ICON_CODES.get("microphone", "M") if theme.FOMANTIC_ICON_FAMILY else "M",
                )

            # A series is a temporary expanded context, not a normal row in
            # the timeline. Darken its preview as well as the card chrome so
            # the complete group remains recognisable in grids and strips.
            if expanded_series:
                painter.fillRect(image_rect, QColor(0, 0, 0, 76))

        # For folders: full width text, no ratings/badges.
        caption_rect = QRect(rect.left() + 5, rect.bottom() - bottom + 2, rect.width() - 10, bottom - 2)
        text_rect = QRect(caption_rect)
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
        font.setPointSizeF(
            7.0 if self.compact and not (path_obj and path_obj.is_dir())
            else 8.5 if self.compact
            else 10.5 if path_obj and path_obj.is_dir()
            else 7.5
        )
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        rating = int(detail.get("rating") or 0) if not (path_obj and path_obj.is_dir()) else 0
        rating_text = "★" * rating
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
            count = int(series.get("count", 0) or 0)
            badge_height = 10 if self.compact else 12
            badge_top = rect.top() + 4
            badge_left = rect.left() + 4
            if count > 1:
                badge_width = 26 if self.compact else 32
                badge_rect = QRect(badge_left, badge_top, badge_width, badge_height)
                painter.setPen(QColor("#262626"))
                icon_font = QFont(theme.FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(7 if self.compact else 8)
                painter.setFont(icon_font)
                icon_width = 8 if self.compact else 10
                painter.drawText(QRect(badge_rect.left(), badge_rect.top(), icon_width, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, theme.FOMANTIC_ICON_CODES["images"] if theme.FOMANTIC_ICON_FAMILY else "▣")
                font = painter.font()
                font.setPixelSize(7 if self.compact else 8)
                painter.setFont(font)
                marker = "−" if series.get("expanded") else "+"
                badge_text = str(count) if self.compact else f"{count} {marker}"
                painter.drawText(QRect(badge_rect.left() + icon_width, badge_rect.top(), badge_width - icon_width, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, badge_text)
                badge_left += badge_width + 3
            if rating_text:
                rating_font = QFont(option.font)
                rating_font.setPointSizeF(7.0 if self.compact else 8.0)
                painter.setFont(rating_font)
                rating_width = painter.fontMetrics().horizontalAdvance(rating_text)
                rating_rect = QRect(badge_left, badge_top, rating_width + 2, badge_height)
                painter.setPen(QColor("#3a3123"))
                painter.drawText(rating_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, rating_text)
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
                badge = QRect(rect.left() + 4, rect.top() + 4, 26, 10)
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


_CODE_TOKEN_OBJECT = int(QTextFormat.ObjectTypes.UserObject) + 1
_CODE_TOKEN_RAW = 1025
_CODE_TOKEN_VALUE = 1026
_CODE_TOKEN_TAG = 1027


class CodeTokenObject(QObject, QTextObjectInterface):
    """An atomic, painted inline token stored as one object character."""

    def intrinsicSize(self, doc, _pos, fmt):  # noqa: N802
        metrics = QFontMetricsF(doc.defaultFont())
        return QSizeF(metrics.horizontalAdvance(str(fmt.property(_CODE_TOKEN_VALUE))) + 12, metrics.height() + 2)

    def drawObject(self, painter, rect, _doc, _pos, fmt):  # noqa: N802
        tag = bool(fmt.property(_CODE_TOKEN_TAG))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#765b9a") if tag else QColor("#3867a8"))
        painter.drawRoundedRect(rect.adjusted(0, 1, 0, -1), 4, 4)
        painter.setPen(QColor("#f7fbff"))
        painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(fmt.property(_CODE_TOKEN_VALUE)))


class RichCodeCommentEdit(QTextEdit):
    """Single-line rich editor: visible tokens retain their raw marker."""

    editingFinished = Signal()
    returnPressed = Signal()
    _RAW_TOKEN = _CODE_TOKEN_RAW
    _TOKEN_RE = re.compile(r"\{([^}]+)\}|\\([^\\]+)\\|=([^=]+)=|@([\w]+)|#([\w]+)")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lookup: dict[str, str] = {}
        self._rendering = False
        self._opener = "{"
        self._start = 0
        self._labels: dict[str, str] = {}
        self._model = QStringListModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setWidget(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.activated.connect(self._insert_code)
        self._suggestion_popup = QListWidget(self)
        self._suggestion_popup.setWindowFlags(Qt.WindowType.ToolTip)
        self._suggestion_popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._suggestion_popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._suggestion_popup.setObjectName("codeSuggestionPopup")
        self._suggestion_popup.itemClicked.connect(lambda item: self._insert_code(item.text()))
        self._suggestion_popup.hide()
        self._token_renderer = CodeTokenObject(self)
        self.document().documentLayout().registerHandler(_CODE_TOKEN_OBJECT, self._token_renderer)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self._retokenize)

    def set_codes(self, sets: list[dict], active_id: int) -> None:
        entries = [entry for group in sets if not active_id or group.get("id") == active_id for entry in group.get("codes", []) if isinstance(entry, dict)]
        self._lookup = {str(entry.get("code") or ""): str(entry.get("value") or "") for entry in entries}
        self._render(self.text())

    def text(self) -> str:  # noqa: N802
        output: list[str] = []
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                fmt = fragment.charFormat()
                output.append(str(fmt.property(self._RAW_TOKEN)) if fmt.hasProperty(self._RAW_TOKEN) else fragment.text())
                iterator += 1
            if block.next().isValid(): output.append("\n")
            block = block.next()
        return "".join(output)

    def setText(self, text: str) -> None:  # noqa: N802
        self._render(str(text or ""))

    def _render(self, raw: str, raw_cursor: int | None = None) -> None:
        if raw_cursor is None: raw_cursor = len(raw)
        self._rendering = True
        cursor = self.textCursor()
        cursor.select(cursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.beginEditBlock()
        last = 0
        for match in self._TOKEN_RE.finditer(raw):
            cursor.setCharFormat(QTextCharFormat())
            cursor.insertText(raw[last:match.start()], QTextCharFormat())
            marker = match.group(0)
            tag = match.group(5) is not None
            code = next(value for value in match.groups() if value is not None)
            value = marker if tag else self._lookup.get(code)
            if value is None:
                cursor.insertText(marker)
            else:
                fmt = cursor.charFormat()
                fmt.setProperty(_CODE_TOKEN_RAW, marker)
                fmt.setForeground(QColor("#f7fbff"))
                fmt.setBackground(QColor("#765b9a") if tag else QColor("#3867a8"))
                fmt.setFontWeight(QFont.Weight.DemiBold)
                cursor.insertText(value, fmt)
            last = match.end()
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(raw[last:], QTextCharFormat())
        cursor.endEditBlock()
        self._rendering = False
        self._set_raw_cursor(raw_cursor)
        self.setToolTip(raw)

    def _raw_cursor(self) -> int:
        position = self.textCursor().position()
        raw, visual = 0, 0
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment(); display = fragment.text(); length = len(display)
                marker = fragment.charFormat().property(self._RAW_TOKEN) if fragment.charFormat().hasProperty(self._RAW_TOKEN) else display
                if position <= visual + length:
                    return raw + (len(str(marker)) if fragment.charFormat().hasProperty(self._RAW_TOKEN) else position - visual)
                raw += len(str(marker)); visual += length; iterator += 1
            block = block.next()
        return raw

    def _set_raw_cursor(self, target: int) -> None:
        raw, visual = 0, 0
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment(); display = fragment.text(); length = len(display)
                token = fragment.charFormat().hasProperty(self._RAW_TOKEN)
                marker = str(fragment.charFormat().property(self._RAW_TOKEN)) if token else display
                if target <= raw + len(marker):
                    cursor = self.textCursor()
                    cursor.setPosition(visual + (length if token else target - raw))
                    # Never let following typing inherit a token's hidden raw
                    # marker; otherwise serialization would swallow it.
                    cursor.setCharFormat(QTextCharFormat())
                    self.setTextCursor(cursor)
                    return
                raw += len(marker); visual += length; iterator += 1
            block = block.next()

    def _retokenize(self) -> None:
        if self._rendering: return
        raw, position = self.text(), self._raw_cursor()
        self._render(raw, position)
        QTimer.singleShot(0, self._offer_codes)

    def _offer_codes(self) -> None:
        before = self.text()[:self._raw_cursor()]
        start, opener = max((before.rfind(mark), mark) for mark in ("{", "\\", "=", "@"))
        if start < 0: self._suggestion_popup.hide(); return
        fragment = before[start + 1:]
        if (opener == "@" and fragment and not fragment.replace("_", "a").isalnum()) or (opener != "@" and ("}" if opener == "{" else opener) in fragment):
            self._suggestion_popup.hide(); return
        if opener == "@" and fragment in self._lookup:
            self._suggestion_popup.hide(); return
        self._start, self._opener = start, opener
        self._labels = {f"{code} — {value}": code for code, value in self._lookup.items()}
        labels = [label for label in self._labels if fragment.casefold() in label.casefold()]
        self._suggestion_popup.clear()
        self._suggestion_popup.addItems(labels)
        if not labels:
            self._suggestion_popup.hide()
            return
        self._suggestion_popup.setCurrentRow(0)
        self._suggestion_popup.setFixedWidth(max(240, self.width()))
        self._suggestion_popup.setFixedHeight(min(180, self._suggestion_popup.sizeHintForRow(0) * len(labels) + 4))
        self._suggestion_popup.move(self.mapToGlobal(self.cursorRect().bottomLeft()))
        self._suggestion_popup.show()

    def _insert_code(self, label: str) -> None:
        code = self._labels.get(label)
        if not code: return
        raw = self.text(); end = self._raw_cursor()
        close = "}" if self._opener == "{" else ("" if self._opener == "@" else self._opener)
        insertion = f"{self._opener}{code}{close}"
        self._render(raw[:self._start] + insertion + raw[end:], self._start + len(insertion))
        self._suggestion_popup.hide()
        self._completer.popup().hide()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        # QCompleter releases its popup focus after the activation callback.
        QTimer.singleShot(0, lambda: self.setFocus(Qt.FocusReason.OtherFocusReason))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._suggestion_popup.isVisible():
            if event.key() == Qt.Key.Key_Down:
                self._suggestion_popup.setCurrentRow(min(self._suggestion_popup.count() - 1, self._suggestion_popup.currentRow() + 1)); event.accept(); return
            if event.key() == Qt.Key.Key_Up:
                self._suggestion_popup.setCurrentRow(max(0, self._suggestion_popup.currentRow() - 1)); event.accept(); return
            if event.key() == Qt.Key.Key_Tab:
                item = self._suggestion_popup.currentItem()
                if item: self._insert_code(item.text())
                event.accept(); return
            if event.key() == Qt.Key.Key_Escape:
                self._suggestion_popup.hide(); event.accept(); return
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                self._suggestion_popup.hide()
        if event.key() == Qt.Key.Key_Escape:
            dialog = self.window()
            if isinstance(dialog, QDialog):
                dialog.reject()
                event.accept()
                return
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.returnPressed.emit(); self.editingFinished.emit(); event.accept(); return
        cursor = self.textCursor()
        if not cursor.hasSelection() and event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            position = cursor.position()
            target = position - 1 if event.key() == Qt.Key.Key_Backspace else position
            probe = self.textCursor()
            probe.setPosition(max(0, target))
            probe.movePosition(probe.MoveOperation.Right, probe.MoveMode.KeepAnchor)
            if probe.charFormat().hasProperty(_CODE_TOKEN_RAW):
                block = self.document().begin()
                while block.isValid():
                    iterator = block.begin()
                    while not iterator.atEnd():
                        fragment = iterator.fragment()
                        if fragment.position() <= target < fragment.position() + fragment.length() and fragment.charFormat().hasProperty(_CODE_TOKEN_RAW):
                            token = self.textCursor()
                            token.setPosition(fragment.position())
                            token.setPosition(fragment.position() + fragment.length(), token.MoveMode.KeepAnchor)
                            token.removeSelectedText()
                            self.setTextCursor(token)
                            event.accept()
                            return
                        iterator += 1
                    block = block.next()
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._completer.popup().hide()
        self._suggestion_popup.hide()
        self.editingFinished.emit()


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
        self.settings = settings or _application_settings()
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

        self.comment_edit = RichCodeCommentEdit()
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
    originalRequested = Signal(object)
    markIndicatorRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("fullView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pixmap: QPixmap | None = None
        self._preview_pixmap: QPixmap | None = None
        self._path: Path | None = None
        self._is_fallback = False
        self._photo_generation = -1
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setSingleShot(True)
        self._smooth_timer.timeout.connect(self._smooth_fit)

        self.image_view = FullImageView()
        self.image_view.setObjectName("fullImageView")
        self.image_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.image_view.faceClicked.connect(self._show_face_actions)
        self.image_view.zoomPressed.connect(self._request_mouse_zoom)
        self.image_view.zoomReleased.connect(self._release_mouse_zoom)
        self.image_view.wheelScrolled.connect(self._navigate_wheel)
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
        self.counter_label = QLabel(self.media_panel)
        self.counter_label.setObjectName("fullViewCounter")
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter_label.hide()
        self.mark_indicator = MarkIndicatorButton(self.media_panel)
        self.mark_indicator.setObjectName("fullViewMarkIndicator")
        self.mark_indicator.setFixedSize(44, 44)
        self.mark_indicator.clicked.connect(self.markIndicatorRequested)
        self.mark_indicator.hide()
        self._mark_detail: dict = {}
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

        # Mirrors the web viewer: a round microphone control opens a compact
        # Compact audio player panel over the lower-left corner of the photo.
        self.audio_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(1.0)
        self.audio_player.setAudioOutput(self.audio_output)
        self.audio_player.positionChanged.connect(self._audio_position_changed)
        self.audio_player.durationChanged.connect(self._audio_duration_changed)
        self.audio_player.playbackStateChanged.connect(self._audio_state_changed)
        self.audio_path = ""
        self.audio_toggle = AudioToggleButton(self.media_panel)
        self.audio_toggle.setObjectName("audioToggle")
        self.audio_toggle.setFixedSize(48, 48)
        self.audio_toggle.setIcon(_fomantic_icon("microphone", 22))
        self.audio_toggle.setToolTip("Аудиокомментарий")
        self.audio_toggle.clicked.connect(self._toggle_audio)
        self.audio_toggle.hide()
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
        self._strip_level = self._load_strip_level()
        self._apply_strip_level()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(stage, 1)
        layout.addWidget(self.strip_panel)
        # The full-view frame often hands focus to a child control (the image,
        # strip, or metadata bar). Keep Z available throughout that subtree.
        self.zoom_action = QAction(self)
        self.zoom_action.setShortcut(QKeySequence("Z"))
        self.zoom_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.zoom_action.triggered.connect(self._toggle_zoom)
        self.addAction(self.zoom_action)

    # Full-view lower area has three states driven by Ctrl+Down / Ctrl+Up:
    # 0 = strip and metadata bar visible, 1 = only the metadata bar (thumbnail
    # strip collapsed), 2 = the whole panel hidden.
    STRIP_FULL = 0
    STRIP_COLLAPSED = 1
    STRIP_HIDDEN = 2

    def _load_strip_level(self) -> int:
        settings = _application_settings()
        if settings.contains("viewer_strip_level"):
            return max(self.STRIP_FULL, min(self.STRIP_HIDDEN, settings.value("viewer_strip_level", self.STRIP_FULL, int)))
        # Migrate the previous two-state collapsed flag.
        return self.STRIP_COLLAPSED if settings.value("viewer_strip_collapsed", False, bool) else self.STRIP_FULL

    def _apply_strip_level(self) -> None:
        self.strip_panel.setVisible(self._strip_level != self.STRIP_HIDDEN)
        self.photo_strip.setVisible(self._strip_level == self.STRIP_FULL)
        collapsed = self._strip_level != self.STRIP_FULL
        self.strip_toggle.setIcon(_fomantic_icon("chevron-up" if collapsed else "chevron-down", 12))
        self.strip_toggle.setToolTip("Развернуть ленту превью" if collapsed else "Свернуть ленту превью")

    def set_strip_level(self, level: int) -> None:
        level = max(self.STRIP_FULL, min(self.STRIP_HIDDEN, level))
        if level == self._strip_level:
            return
        self._strip_level = level
        self._apply_strip_level()
        _application_settings().setValue("viewer_strip_level", level)
        # The host's resize (handled in eventFilter) keeps the controls pinned,
        # but reposition once more after the layout settles as a safety net.
        QTimer.singleShot(0, self._position_video_controls)

    def cycle_strip(self, step: int) -> None:
        self.set_strip_level(self._strip_level + step)

    def toggle_strip(self) -> None:
        self.set_strip_level(self.STRIP_FULL if self._strip_level != self.STRIP_FULL else self.STRIP_COLLAPSED)

    def set_counter(self, text: str, visible: bool) -> None:
        self.counter_label.setText(text)
        self.counter_label.adjustSize()
        self.counter_label.setVisible(visible)
        if visible:
            self._position_counter()

    def _position_counter(self) -> None:
        if not self.counter_label.isVisible():
            return
        self.counter_label.adjustSize()
        self.counter_label.move(12, 12)
        self.counter_label.raise_()

    def stop_video(self) -> None:
        """Fully stop playback so nothing keeps running after leaving full view."""
        if not self._is_video:
            return
        self.video_player.stop()
        self.video_play_button.setIcon(_fomantic_icon("play", 12))

    def stop_audio(self) -> None:
        self.audio_player.stop()

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
        add_button.clicked.connect(menu.close)
        add_button.clicked.connect(lambda: self.faceAddRequested.emit(face))
        action.setDefaultWidget(row)
        menu.addAction(action)
        menu.popup(self.image_view.mapToGlobal(position))

    def set_comment(self, comment: str) -> None:
        self.full_comment_edit.blockSignals(True)
        self.full_comment_edit.setText(comment)
        self.full_comment_edit.blockSignals(False)

    def set_metadata(self, detail: dict, paths: tuple[Path, ...] = ()) -> None:
        self.meta_bar.set_metadata(detail)
        self._mark_detail = detail
        self._update_mark_indicator()
        self._set_audio_detail(detail)
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
        if self._path != decoded.path:
            self.stop_audio()
        self.video_player.stop()
        self._is_video = False
        self._update_mark_indicator()
        self.video_controls.hide()
        self.media_stack.setCurrentWidget(self.image_view)
        self._path = decoded.path
        self._is_fallback = fallback
        self._pixmap = QPixmap.fromImage(decoded.image)
        self._preview_pixmap = self._pixmap
        suffix = "  -  preview" if fallback else ""
        self.info_label.setText(f"{decoded.path.name}  ·  {decoded.width} × {decoded.height}{suffix}")
        # A late screen-sized decode must not replace an active original.
        if self.image_view.zoom_requested:
            return
        self.image_view.set_pixmap(self._pixmap, smooth=False)
        self._schedule_smooth_fit()

    def set_video(self, path: Path, preview: QImage | None = None) -> None:
        if self._path != path:
            self.stop_audio()
        self._reset_zoom()
        self._path = path
        self._is_video = True
        self._update_mark_indicator()
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

    def show_original(self, decoded: DecodedImage) -> None:
        """Enter 100% only after the original source has finished decoding."""
        if decoded.path != self._path or not self.image_view.zoom_requested:
            return
        self.image_view.set_original_pixmap(QPixmap.fromImage(decoded.image))

    def _request_mouse_zoom(self, position: object) -> None:
        if self._is_video or self.image_view.zoom_requested:
            return
        self.image_view.request_zoom(position, temporary=True)
        self._update_mark_indicator()
        self.originalRequested.emit(position)

    def _release_mouse_zoom(self) -> None:
        if self.image_view.temporary_zoom:
            self._reset_zoom()

    def _toggle_zoom(self) -> None:
        if self._is_video:
            return
        if self.image_view.zoom_requested:
            self._reset_zoom()
            return
        self.image_view.request_zoom(None, temporary=False)
        self._update_mark_indicator()
        self.originalRequested.emit(None)

    def _reset_zoom(self) -> None:
        self.image_view.reset_zoom(self._preview_pixmap)
        self._update_mark_indicator()

    def _navigate_wheel(self, direction: int) -> None:
        if direction > 0:
            self.previousRequested.emit()
        elif direction < 0:
            self.nextRequested.emit()

    def refresh_mark_indicator(self) -> None:
        """Apply a changed interface preference without reopening Full View."""
        self._update_mark_indicator()

    def _update_mark_indicator(self) -> None:
        detail = self._mark_detail
        rating = int(detail.get("rating") or 0)
        color_label = str(detail.get("color_label") or "")
        visible = (
            not self._is_video
            and not self.image_view.zoom_requested
            and _application_settings().value("interface/show_full_view_mark_indicator", True, bool)
        )
        if not visible:
            self.mark_indicator.hide()
            return
        colors = {
            "red": "#a25555", "yellow": "#af9440", "green": "#4d9660",
            "blue": "#537fc2", "purple": "#9760b2",
        }
        self.mark_indicator.setText(f"★ {rating}" if rating > 0 else "")
        has_mark = rating > 0 or bool(color_label)
        self.mark_indicator.setToolTip(
            "Снять все метки" if has_mark else "Применить быструю метку (M)"
        )
        self.mark_indicator.set_mark_color(colors.get(color_label, "#4d535b"))
        self.mark_indicator.show()
        self._position_mark_indicator()

    def cancel_zoom(self) -> None:
        """Discard a pending/active inspection before changing photos."""
        self._reset_zoom()

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

    def _set_audio_detail(self, detail: dict) -> None:
        path = str(detail.get("audio_comment_path") or "")
        available = bool(path and Path(path).is_file())
        self.audio_toggle.setVisible(available)
        if not available:
            self.stop_audio()
            self.audio_path = ""
            return
        self.audio_path = path

    def _toggle_audio(self) -> None:
        if not self.audio_path:
            return
        if self.audio_player.source().toLocalFile() != self.audio_path:
            self.audio_player.setSource(QUrl.fromLocalFile(self.audio_path))
        if self.audio_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.audio_player.stop()
            self.audio_player.setPosition(0)
            self.audio_toggle.set_progress(0)
        else:
            self.audio_player.setPosition(0)
            self.audio_player.play()

    def _audio_position_changed(self, position: int) -> None:
        duration = self.audio_player.duration()
        self.audio_toggle.set_progress(position / duration if duration > 0 else 0)

    def _audio_duration_changed(self, duration: int) -> None:
        del duration

    def _audio_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.audio_toggle.setIcon(_fomantic_icon("pause" if playing else "microphone", 22))
        if not playing and self.audio_player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self.audio_toggle.set_progress(1.0)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize and obj is self.video_controls.parentWidget():
            # Collapsing the strip/metadata panel resizes the media panel without
            # resizing the frame, so re-pin every floating overlay to its edge.
            self._position_video_controls()
            self._position_counter()
            self._position_face_filter_chip()
            self._position_mark_indicator()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.image_view.update()
        QTimer.singleShot(0, self._position_video_controls)
        QTimer.singleShot(0, self._position_face_filter_chip)
        QTimer.singleShot(0, self._position_counter)
        QTimer.singleShot(0, self._position_mark_indicator)

    def _position_face_filter_chip(self) -> None:
        if not self.face_filter_chip.isVisible():
            return
        self.face_filter_chip.adjustSize()
        self.face_filter_chip.move(
            max(8, self.media_panel.width() - self.face_filter_chip.width() - 12), 12
        )
        self.face_filter_chip.raise_()

    def _position_mark_indicator(self) -> None:
        if not self.mark_indicator.isVisible():
            return
        position = _application_settings().value(
            "interface/full_view_mark_indicator_position", "bottom", str
        )
        self.mark_indicator.move(
            max(8, self.media_panel.width() - self.mark_indicator.width() - 12),
            12 if position == "top" else max(8, self.media_panel.height() - self.mark_indicator.height() - 12),
        )
        self.mark_indicator.raise_()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in {Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.exitRequested.emit()
        elif key == Qt.Key.Key_Z:
            self._toggle_zoom()
        elif self.image_view.zoomed and key in {
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        }:
            self.image_view.pan_key(key)
        elif key == Qt.Key.Key_Down and self.series_panel.isVisible():
            self._move_series(1)
        elif key == Qt.Key.Key_Up and self.series_panel.isVisible():
            self._move_series(-1)
        elif key == Qt.Key.Key_Space and self._is_video:
            self._toggle_video_playback()
        elif key == Qt.Key.Key_Space and self.audio_path:
            self._toggle_audio()
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
    zoomPressed = Signal(object)
    zoomReleased = Signal()
    wheelScrolled = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._smooth = False
        self._faces: list[dict] = []
        self._hovered_face = -1
        self._zoomed = False
        self._zoom_requested = False
        self._temporary_zoom = False
        self._zoom_anchor: QPointF | None = None
        self._view_center = QPointF(0.5, 0.5)
        self._drag_position: QPointF | None = None
        self._drag_center: QPointF | None = None
        self._spinner_angle = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(50)
        self._spinner_timer.timeout.connect(self._advance_spinner)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(3000)
        self._cursor_timer.timeout.connect(self._hide_cursor)
        self._cursor_hidden = False
        self._wheel_delta = 0
        self.setMinimumSize(1, 1)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self._set_zoom_cursor()

    def set_pixmap(self, pixmap: QPixmap, *, smooth: bool) -> None:
        self._pixmap = pixmap
        self._smooth = smooth
        self._note_mouse_activity()
        self.update()

    @property
    def zoomed(self) -> bool:
        return self._zoomed

    @property
    def zoom_requested(self) -> bool:
        return self._zoom_requested

    @property
    def temporary_zoom(self) -> bool:
        return self._temporary_zoom

    def request_zoom(self, position: object, *, temporary: bool) -> None:
        self._zoom_requested = True
        self._temporary_zoom = temporary
        self._zoom_anchor = position if isinstance(position, QPointF) else None
        self._spinner_timer.start()
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.update()

    def set_original_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull() or not self._zoom_requested:
            return
        self._pixmap = pixmap
        self._zoomed = True
        self._spinner_timer.stop()
        self._view_center = self._zoom_focus()
        self._clamp_view_center()
        # A held mouse may have started while the original was loading. Make
        # that exact point the origin now, after the face/cursor focus is set.
        if self._drag_position is not None:
            self._drag_center = QPointF(self._view_center)
        self.update()

    def reset_zoom(self, preview: QPixmap | None) -> None:
        self._zoomed = self._zoom_requested = self._temporary_zoom = False
        self._zoom_anchor = self._drag_position = self._drag_center = None
        self._spinner_timer.stop()
        if preview is not None and not preview.isNull():
            self._pixmap = preview
        self._set_zoom_cursor()
        self.update()

    def _set_zoom_cursor(self) -> None:
        self.setCursor(QCursor(_fomantic_icon("zoom", 20).pixmap(20, 20), 10, 10))

    def _update_cursor(self) -> None:
        if self._cursor_hidden:
            return
        if self._zoom_requested and not self._zoomed:
            self.setCursor(Qt.CursorShape.BlankCursor)
        elif self._hovered_face >= 0:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self._set_zoom_cursor()

    def _note_mouse_activity(self) -> None:
        self._cursor_hidden = False
        self._update_cursor()
        self._cursor_timer.start()

    def _hide_cursor(self) -> None:
        self._cursor_hidden = True
        self.setCursor(Qt.CursorShape.BlankCursor)

    def _advance_spinner(self) -> None:
        self._spinner_angle = (self._spinner_angle + 24) % 360
        self.update()

    def _zoom_focus(self) -> QPointF:
        faces = []
        if _application_settings().value("interface/zoom_focus_face", True, bool):
            for face in self._faces:
                bbox = face.get("bbox") or {}
                try:
                    faces.append((float(bbox["width"]) * float(bbox["height"]), bbox))
                except (KeyError, TypeError, ValueError):
                    pass
        if faces:
            _area, bbox = max(faces, key=lambda item: item[0])
            return QPointF(float(bbox["x"]) + float(bbox["width"]) / 2, float(bbox["y"]) + float(bbox["height"]) / 2)
        if self._zoom_anchor is not None and self._pixmap is not None:
            rect = _fit_rect(self._pixmap.size(), self.size())
            if not rect.isEmpty() and rect.contains(self._zoom_anchor.toPoint()):
                return QPointF((self._zoom_anchor.x() - rect.left()) / rect.width(), (self._zoom_anchor.y() - rect.top()) / rect.height())
        return QPointF(0.5, 0.5)

    def _clamp_view_center(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        half_x = min(0.5, self.width() / max(1, self._pixmap.width()) / 2)
        half_y = min(0.5, self.height() / max(1, self._pixmap.height()) / 2)
        self._view_center.setX(min(1 - half_x, max(half_x, self._view_center.x())))
        self._view_center.setY(min(1 - half_y, max(half_y, self._view_center.y())))

    def pan_key(self, key) -> None:
        delta = {Qt.Key.Key_Left: (-.05, 0), Qt.Key.Key_Right: (.05, 0), Qt.Key.Key_Up: (0, -.05), Qt.Key.Key_Down: (0, .05)}.get(key)
        if not self._zoomed or delta is None:
            return
        self._view_center += QPointF(*delta)
        self._clamp_view_center()
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
        self._note_mouse_activity()
        if self._zoomed and self._drag_position is not None and self._pixmap is not None:
            # Map the pointer's displacement from the grab point to the whole
            # scene, while keeping the originally focused point under it.
            # This avoids a jump on the first tiny movement and never needs a
            # second grab to reach the edge of the image.
            start = self._drag_center or self._view_center
            self._view_center = QPointF(
                start.x() - (event.position().x() - self._drag_position.x()) / max(1, self.width()),
                start.y() - (event.position().y() - self._drag_position.y()) / max(1, self.height()),
            )
            self._clamp_view_center()
            self.update()
            event.accept()
            return
        hit = self._face_at(event.position())
        if hit != self._hovered_face:
            self._hovered_face = hit
            self._update_cursor()
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
            self._note_mouse_activity()
            self._drag_position = None
            if self._temporary_zoom:
                self.zoomReleased.emit()
                event.accept()
                return
            hit = self._face_at(event.position())
            if hit >= 0:
                self.faceClicked.emit(self._faces[hit], event.position().toPoint())
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._note_mouse_activity()
            # A detected face is an interactive target.  Check it before
            # starting the press-and-hold zoom, otherwise the temporary zoom
            # consumes the release event that opens the face menu.
            if self._face_at(event.position()) >= 0:
                event.accept()
                return
            if self._zoomed:
                self._drag_position = event.position()
                self._drag_center = QPointF(self._view_center)
            else:
                # Keep this point while the source is decoding. If the user
                # keeps holding the button, the first later move pans at once.
                self._drag_position = event.position()
                self._drag_center = None
                self.zoomPressed.emit(event.position())
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._zoom_requested:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            delta = event.pixelDelta().y() * 3
        if not delta:
            event.ignore()
            return
        self._wheel_delta += delta
        while abs(self._wheel_delta) >= 120:
            direction = 1 if self._wheel_delta > 0 else -1
            self.wheelScrolled.emit(direction)
            self._wheel_delta -= direction * 120
        event.accept()

    def _face_at(self, position) -> int:
        if self._pixmap is None or self._pixmap.isNull():
            return -1
        image_rect = self._image_rect()
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
        target = self._image_rect()
        painter.drawPixmap(target, self._pixmap)
        if self._zoom_requested and not self._zoomed:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            spinner = QRect(self.rect().center().x() - 16, self.rect().center().y() - 16, 32, 32)
            pen = QPen(QColor(235, 235, 235), 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(spinner, self._spinner_angle * 16, 250 * 16)
        if 0 <= self._hovered_face < len(self._faces):
            face_rect = self._face_rect(self._faces[self._hovered_face], target)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(138, 180, 248, 242), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(face_rect, 4, 4)
        painter.end()

    def _image_rect(self) -> QRect:
        if self._pixmap is None or self._pixmap.isNull():
            return QRect()
        if not self._zoomed:
            return _fit_rect(self._pixmap.size(), self.size())
        self._clamp_view_center()
        return QRect(
            round(self.width() / 2 - self._view_center.x() * self._pixmap.width()),
            round(self.height() / 2 - self._view_center.y() * self._pixmap.height()),
            self._pixmap.width(), self._pixmap.height(),
        )


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


# These shortcuts are deliberately kept in one place: SettingsDialog uses the
# same ids to edit them that Workspace uses to install them.
class DirectoryTree(QTreeView):
    """Folder tree that accepts local file URLs without letting its model move them."""

    pathsDropped = Signal(object, object, object)  # paths, destination, action

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("treeFocused", False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self.setProperty("treeFocused", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.setProperty("treeFocused", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()
        super().focusOutEvent(event)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        selection = self.selectionModel()
        if selection is None:
            return
        paths = [
            Path(self.model().filePath(index))
            for index in selection.selectedRows(0)
            if index.isValid()
        ]
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setData("application/x-rawww-drag", b"1")
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = _local_paths_from_mime(event.mimeData())
        index = self.indexAt(event.position().toPoint())
        destination = Path(self.model().filePath(index)) if index.isValid() else None
        if not paths or destination is None or not destination.is_dir():
            event.ignore()
            return
        action = event.proposedAction() if event.mimeData().hasFormat("application/x-rawww-drag") else Qt.DropAction.CopyAction
        self.pathsDropped.emit(paths, destination, action)
        event.acceptProposedAction()


class FavoritesList(QListWidget):
    """A reorderable list that exposes its folder path while being dragged."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("favoritesList")
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        mime = self.model().mimeData(self.selectedIndexes())
        mime.setData("application/x-rawww-favorite-path", item.data(Qt.ItemDataRole.UserRole).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)


class FavoritesSplitterHandle(QSplitterHandle):
    """Visible grip for resizing the favorites panel."""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#353535"))
        center = self.rect().center()
        color = QColor("#b0b0b0") if self.underMouse() else QColor("#858585")
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for offset in (-3, 0, 3):
            if self.orientation() == Qt.Orientation.Vertical:
                painter.drawLine(center.x() - 12, center.y() + offset, center.x() + 12, center.y() + offset)
            else:
                painter.drawLine(center.x() + offset, center.y() - 12, center.x() + offset, center.y() + 12)
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)


class FavoritesSplitter(QSplitter):
    def createHandle(self):  # noqa: N802
        return FavoritesSplitterHandle(self.orientation(), self)


class CenteredSearchEdit(QLineEdit):
    """Search field whose leading and clear actions stay vertically centered."""

    def _center_action_buttons(self) -> None:
        for button in self.findChildren(QToolButton):
            if not button.isVisible() and not button.isEnabled():
                continue
            height = button.sizeHint().height()
            if height <= 0:
                continue
            button.move(button.x(), max(0, (self.height() - height) // 2))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._center_action_buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._center_action_buttons)


class GridZoomControls(QFrame):
    """Compact overlay controls for changing the thumbnail size."""

    def __init__(self, changed: Callable[[int], None], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("gridZoomControls")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)
        for icon, delta, tooltip in (
            ("zoom-out", -1, "Уменьшить миниатюры"),
            ("zoom", 1, "Увеличить миниатюры"),
        ):
            button = QToolButton(self)
            button.setObjectName("gridZoomButton")
            button.setIcon(_fomantic_icon(icon, 18, "#aeb5bf"))
            button.setIconSize(QSize(18, 18))
            button.setFixedSize(22, 22)
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _checked=False, amount=delta: changed(amount))
            layout.addWidget(button)
        self.adjustSize()


class FavoritesTrashButton(QToolButton):
    """Drop target used to remove a folder from the favorites list."""

    favoriteDropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("favoritesTrash")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-rawww-favorite-path"):
            self.setProperty("dropActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dropActive", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # noqa: N802
        path = bytes(event.mimeData().data("application/x-rawww-favorite-path")).decode("utf-8", errors="replace")
        self.setProperty("dropActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        if path:
            self.favoriteDropped.emit(path)
            event.acceptProposedAction()
            return
        event.ignore()


class ChromeTabBar(QTabBar):
    """Chrome-like tab widths instead of Qt's full-row expansion."""
    closeRequested = Signal(int)
    pathsDropped = Signal(object, int, object)  # paths, tab index, action

    def __init__(self) -> None:
        super().__init__()
        self._tab_width = 220
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and self.tabAt(event.position().toPoint()) >= 0:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = _local_paths_from_mime(event.mimeData())
        index = self.tabAt(event.position().toPoint())
        if not paths or index < 0:
            event.ignore()
            return
        action = event.proposedAction() if event.mimeData().hasFormat("application/x-rawww-drag") else Qt.DropAction.CopyAction
        self.pathsDropped.emit(paths, index, action)
        event.acceptProposedAction()

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
    shotsyncFolderChanged = Signal(bool)   # current folder is linked to ShotSync
    _cache_maintenance_started = False

    def __init__(
        self,
        initial_directory: Path | None = None,
        *,
        defer_initial_scan: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 920)
        self.closing = False
        self._taskbar_progress = WindowsTaskbarProgress()
        self._dock_progress = MacDockProgress()

        self.directory_scan_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_load_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_flush_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_maintenance_executor = ThreadPoolExecutor(max_workers=1)
        self.xmp_executor = ThreadPoolExecutor(max_workers=1)
        self.bridge = DecodeBridge()
        self.bridge.decoded.connect(self._on_decoded)
        self.bridge.failed.connect(self._on_decode_failed)
        self.bridge.cacheLoaded.connect(self._on_cache_loaded)
        self.bridge.directoryScanned.connect(self._on_directory_scanned)
        self.bridge.metadataUpdated.connect(self._on_metadata_updated)
        self.bridge.xmpWritten.connect(self._on_xmp_written)
        self.video_thumbnailer = VideoThumbnailer(self)
        self.video_thumbnailer.previewReady.connect(self._on_video_preview)
        self._xmp_pending: dict[Path, tuple[dict, list[dict], dict[str, str]]] = {}
        self._xmp_running: set[Path] = set()
        self._xmp_export_after_cache_load = False
        self._ignore_folder_changes_until = 0.0
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
        self.cache_ready = False
        self.cache_load_generation = 0
        self.directory_generation = 0
        self.decode_cache = DecodeCache(
            ram_limit=RAM_CACHE_LIMIT,
            full_limit=FULL_RAM_CACHE_LIMIT,
            thumbnail_bytes_limit=THUMBNAIL_RAM_CACHE_LIMIT_BYTES,
            original_size=ORIGINAL_SIZE,
            thumb_size=THUMB_SIZE,
        )
        self.scheduler = DecodeScheduler(
            self,
            thumb_size=THUMB_SIZE,
            original_size=ORIGINAL_SIZE,
            current_workers=CURRENT_DECODE_WORKERS,
            background_workers=BACKGROUND_DECODE_WORKERS,
            visible_thumb_workers=VISIBLE_THUMB_DECODE_WORKERS,
            visible_thumb_lookup_workers=VISIBLE_THUMB_LOOKUP_WORKERS,
            use_processes=DECODE_USE_PROCESSES,
        )
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
        self.settings = _application_settings()
        self.destination_paths_provider: Callable[[], list[Path]] | None = None

        # ShotSync cloud integration. The key is remembered between launches
        # and validated lazily the first time the ShotSync disk is opened.
        self.shotsync_active = False
        self._shotsync_checked = False
        self._shotsync_shootings: list[dict] = []
        self._pending_shotsync_marks_for: int | None = None
        self._requested_shotsync_selections: set[int] = set()
        self._requested_shotsync_folders: dict[int, Path] = {}
        self._resuming_shotsync_selections: set[int] = set()
        self._deleting_shotsync_folders: dict[int, Path] = {}
        self.shotsync_client = ShotSyncClient(SHOTSYNC_BASE_URL, self)
        self.code_replacement_sets: list[dict] = self._local_code_replacement_sets()
        self.shotsync_login_dialog = ShotSyncLoginDialog(self)
        self.shotsync_login_dialog.loginSubmitted.connect(self._shotsync_login)
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
        self.shotsync.photoUpdated.connect(self._on_shotsync_photo_updated)
        self.shotsync.shootingDeleted.connect(self._on_shotsync_shooting_deleted)
        self.shotsync.receiveProgress.connect(self._on_shotsync_receive_progress)
        self.shotsync.receivingChanged.connect(self._refresh_shotsync_receiving)
        self.shotsync.downloader.finished.connect(self._on_shotsync_selection_ready)
        self.shotsync.downloader.failed.connect(self._on_shotsync_selection_failed)
        self.shotsync.downloader.progress.connect(self._on_shotsync_selection_progress)
        self.shotsync.uploader.progress.connect(self._on_shotsync_upload_progress)
        self.shotsync.uploader.finished.connect(self._on_shotsync_upload_finished)
        self.shotsync.uploader.failed.connect(self._on_shotsync_upload_failed)
        self.shotsync.uploader.deleteFinished.connect(self._on_shotsync_server_deleted)
        self.shotsync.uploader.deleteFailed.connect(self._on_shotsync_server_delete_failed)
        self.shotsync.marks_fetcher.finished.connect(self._on_shotsync_marks_fetched)
        self.shotsync.marks_fetcher.failed.connect(self._on_shotsync_marks_failed)

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
        # Native AI and metadata dependencies are loaded only on their first
        # use, keeping a direct file launch focused on first-frame latency.
        self._ai_pipeline = None
        self._metadata_pipeline = None
        self.ai_progress_total = 0
        self.preview_progress_total = 0
        # ShotSync upload progress, shown in the shared top status bar.
        self._upload_progress: tuple[int, int] | None = None
        self._receive_progress: tuple[int, int, int] | None = None
        self._selection_progress: tuple[int, int] | None = None
        self._shotsync_pending_marks = 0
        self._shotsync_marks_fetching = False
        # The lower QMainWindow status bar is intentionally unused; ShotSync
        # and all long-running work report through the top status panel.
        self.statusBar().hide()
        self.fast_fullscreen = False
        self.normal_geometry = None
        self.normal_window_flags = self.windowFlags()
        self.normal_window_state = self.windowState()

        self.stack = QStackedWidget()
        self.grid_page = self._build_grid_page()
        self.full_view = FullView()
        self.full_view.exitRequested.connect(self.show_grid)
        self.full_view.originalRequested.connect(self._request_original_zoom)
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
        self.full_view.markIndicatorRequested.connect(self._toggle_full_view_mark_indicator)
        self.full_view.quickMarkConfigured.connect(self._configure_quick_mark)
        self.full_view.autoAdvanceChanged.connect(self._set_auto_advance)
        self.full_view.set_quick_mark(*self.quick_mark)
        self.full_view.set_auto_advance(self.auto_advance)
        self.full_view.commentSubmitted.connect(self._save_full_comment)
        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.full_view)
        self.shotsync_action_page = self._build_shotsync_action_page()
        self.grid_content_stack.addWidget(self.shotsync_action_page)
        self.shotsync_upload_page = self._build_shotsync_upload_page()
        self.grid_content_stack.addWidget(self.shotsync_upload_page)
        self.setCentralWidget(self.stack)
        self._set_code_replacements(self.code_replacement_sets)
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
        # A direct OS file-open should spend its first CPU/I/O budget on the
        # requested frame, not on scanning every sibling and opening SQLite.
        # The grid catches up shortly afterwards, while folder launches retain
        # their existing immediate population behaviour.
        initial_scan_delay = 350 if defer_initial_scan else 0
        QTimer.singleShot(initial_scan_delay, lambda: self.load_directory(self.current_dir))
        QTimer.singleShot(0, self._focus_grid_panel)
        QTimer.singleShot(0, self._restore_face_filter_chip)
        # Maintenance is deliberately delayed and isolated from interactive
        # cache work so startup and first-folder rendering keep priority.
        QTimer.singleShot(5_000, self._start_cache_maintenance)

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
        self.scheduler.shutdown()
        self.directory_scan_executor.shutdown(wait=False, cancel_futures=True)
        self.cache_load_executor.shutdown(wait=False, cancel_futures=True)
        self.cache_flush_executor.shutdown(wait=False, cancel_futures=False)
        self.cache_maintenance_executor.shutdown(wait=False, cancel_futures=True)
        self.xmp_executor.shutdown(wait=False, cancel_futures=True)
        if self._metadata_pipeline is not None:
            self._metadata_pipeline.shutdown()
        if self._ai_pipeline is not None:
            self._ai_pipeline.shutdown()
        super().closeEvent(event)

    @property
    def ai_pipeline(self):
        if self._ai_pipeline is None:
            from .ai import AiPipeline

            self._ai_pipeline = AiPipeline()
        return self._ai_pipeline

    @property
    def metadata_pipeline(self):
        if self._metadata_pipeline is None:
            from .exif import MetadataPipeline

            self._metadata_pipeline = MetadataPipeline()
        return self._metadata_pipeline

    @property
    def pending(self) -> dict[tuple[Path, int], Future]:
        return self.scheduler.pending

    @property
    def foreground_full_futures(self) -> dict[tuple[Path, int], Future]:
        return self.scheduler.foreground_full_futures

    @property
    def visible_thumb_pending(self) -> set[tuple[Path, int]]:
        return self.scheduler.visible_thumb_pending

    def _start_cache_maintenance(self) -> None:
        """Queue startup cache cleanup after interactive startup has settled."""
        if self.closing or type(self)._cache_maintenance_started:
            return
        type(self)._cache_maintenance_started = True
        self.cache_maintenance_executor.submit(maintain_folder_caches)

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
            self.scheduler.cancel_pending()
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
        
        self.dir_tree = DirectoryTree()
        self.dir_tree.setModel(self.dir_model)
        self.dir_tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        # QFileSystemModel loads children asynchronously; enable its explicit
        # name-column sort instead of retaining the filesystem enumeration
        # order as folders arrive.
        self.dir_tree.setSortingEnabled(True)
        self.dir_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # Включаем возможность редактирования элементов дерева
        self.dir_tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._folder_name_editor: FolderNameEditor | None = None
        self._set_tree_root_for_path(self.current_dir.anchor or QDir.rootPath())
        for column in range(1, self.dir_model.columnCount()):
            self.dir_tree.hideColumn(column)
        self.dir_tree.clicked.connect(self._directory_selected)
        self.dir_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dir_tree.customContextMenuRequested.connect(self._show_directory_context_menu)
        self.dir_tree.pathsDropped.connect(self._receive_dropped_paths)
        self.dir_tree.setHeaderHidden(True)
        self.dir_tree.setMinimumWidth(260)

        self.grid = PhotoGrid()
        self.grid.card_size = self.workspace_state.thumbnail_size
        self.grid._last_icon_size = QSize()
        self.grid._update_card_size()
        self.grid.cardSizeChanged.connect(self._remember_thumbnail_size)
        self.grid.openRequested.connect(self.open_full)
        self.grid.audioRequested.connect(self._open_grid_audio)
        self.grid.audioHoverChanged.connect(self._set_grid_audio_hover)
        self.grid.seriesToggleRequested.connect(self._toggle_grid_series)
        self.grid.deleteRequested.connect(self._delete_grid_selection)
        self.grid.pathsDropped.connect(self._receive_dropped_paths)
        self.grid.currentItemChanged.connect(self._grid_current_item_changed)
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_visible_thumb_priority)
        self.grid.viewportChanged.connect(self._schedule_visible_thumb_priority)
        self.grid_audio_player = QMediaPlayer(self)
        self.grid_audio_output = QAudioOutput(self)
        self.grid_audio_player.setAudioOutput(self.grid_audio_output)
        self.grid_audio_path = ""
        splitter = FavoritesSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("panelSplitter")
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
        self.drive_button_layout.setSpacing(3)
        self.drive_button_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        sidebar_layout.addLayout(self.drive_button_layout)

        # Persistent "disk" that opens the ShotSync cloud instead of a local
        # volume. It shares the exclusive drive-button group so selecting it
        # visually deselects the local volumes and vice versa.
        self.shotsync_button = QToolButton()
        self.shotsync_button.setObjectName("driveButton")
        self.shotsync_button.setCheckable(True)
        self.shotsync_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.shotsync_button.setIconSize(QSize(16, 16))
        self.shotsync_button.setProperty("volumeKey", SHOTSYNC_VOLUME_KEY)
        self.shotsync_button.setText("ShotSync")
        self.shotsync_button.setToolTip("Съёмки ShotSync (shotsync.ru)")
        self.shotsync_button.setIcon(self._shotsync_button_icon())
        self.shotsync_button.clicked.connect(lambda: self._activate_shotsync())
        self.drive_buttons.addButton(self.shotsync_button)
        self.drive_button_layout.addWidget(self.shotsync_button)
        self._register_grid_page_focus_widget(self.shotsync_button)

        self._refresh_volume_buttons()

        # Панель дерева папок с действиями в заголовке.
        directory_panel = QWidget()
        directory_panel.setObjectName("directoryPanel")
        directory_layout = QVBoxLayout(directory_panel)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.setSpacing(4)
        directory_header = QHBoxLayout()
        directory_header.setContentsMargins(2, 0, 2, 0)
        directory_header.setSpacing(0)
        directory_title = QLabel("ПАПКИ")
        directory_title.setObjectName("directoryTitle")
        directory_header.addWidget(directory_title)
        directory_header.addStretch()

        # Кнопка "На уровень вверх"
        self.up_button = QToolButton()
        self.up_button.setObjectName("directoryAction")
        self.up_button.setIcon(_fomantic_icon("arrow-up", 20, "#e6e6e6"))
        self.up_button.setIconSize(QSize(20, 20))
        self.up_button.setToolTip("На уровень вверх")
        self.up_button.clicked.connect(self._go_up_directory)
        directory_header.addWidget(self.up_button)
        
        # Кнопка "Создать папку"
        self.new_folder_button = QToolButton()
        self.new_folder_button.setObjectName("directoryAction")
        self.new_folder_button.setIcon(_fomantic_icon("folder-plus", 20, "#e6e6e6"))
        self.new_folder_button.setIconSize(QSize(20, 20))
        self.new_folder_button.setToolTip("Создать папку")
        self.new_folder_button.clicked.connect(self._create_new_folder)
        directory_header.addWidget(self.new_folder_button)
        directory_layout.addLayout(directory_header)
        directory_layout.addWidget(self.dir_tree, 1)
        
        # The sidebar body swaps between the local folder browser and the
        # ShotSync cloud panel depending on which "disk" is selected.
        self.sidebar_stack = QStackedWidget()

        local_page = QWidget()
        local_layout = QVBoxLayout(local_page)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(8)
        favorites = QWidget()
        favorites.setObjectName("favoritesPanel")
        favorites.setMinimumHeight(48)
        favorites_layout = QVBoxLayout(favorites)
        favorites_layout.setContentsMargins(0, 2, 0, 0)
        favorites_layout.setSpacing(4)
        favorites_header = QHBoxLayout()
        favorites_header.setContentsMargins(2, 0, 2, 0)
        favorites_title = QLabel("ИЗБРАННОЕ")
        favorites_title.setObjectName("favoritesTitle")
        favorites_header.addWidget(favorites_title)
        favorites_header.addStretch()
        self.favorites_trash = FavoritesTrashButton()
        self.favorites_trash.setIcon(_fomantic_icon("trash", 13, "#a8a8a8"))
        self.favorites_trash.setIconSize(QSize(13, 13))
        self.favorites_trash.setToolTip("Удалить выбранную папку из избранного или перетащить её сюда")
        self.favorites_trash.clicked.connect(self._remove_selected_favorite)
        self.favorites_trash.favoriteDropped.connect(self._remove_favorite)
        favorites_header.addWidget(self.favorites_trash)
        self.add_favorite_button = QToolButton()
        self.add_favorite_button.setObjectName("favoritesAdd")
        self.add_favorite_button.setIcon(_fomantic_icon("plus", 13))
        self.add_favorite_button.setIconSize(QSize(13, 13))
        self.add_favorite_button.setToolTip("Добавить текущую папку в избранное")
        self.add_favorite_button.clicked.connect(self._add_current_directory_to_favorites)
        favorites_header.addWidget(self.add_favorite_button)
        favorites_layout.addLayout(favorites_header)

        self.favorites_list = FavoritesList()
        self.favorites_list.itemActivated.connect(self._open_favorite)
        self.favorites_list.itemClicked.connect(self._open_favorite)
        self.favorites_list.model().rowsMoved.connect(lambda *_: self._save_favorites())
        favorites_layout.addWidget(self.favorites_list, 1)
        self._load_favorites()
        self.favorites_panel = favorites
        self.favorites_splitter = FavoritesSplitter(Qt.Orientation.Vertical)
        self.favorites_splitter.setObjectName("favoritesSplitter")
        self.favorites_splitter.setChildrenCollapsible(False)
        self.favorites_splitter.addWidget(directory_panel)
        self.favorites_splitter.addWidget(favorites)
        self.favorites_splitter.setStretchFactor(0, 1)
        self.favorites_splitter.setStretchFactor(1, 0)
        self.favorites_splitter.splitterMoved.connect(lambda *_: self._save_favorites_height())
        local_layout.addWidget(self.favorites_splitter, 1)
        QTimer.singleShot(0, self._restore_favorites_height)

        self.shotsync_panel = ShotSyncPanel(icon_provider=_fomantic_icon)
        self.shotsync_panel.loginRequested.connect(self._show_shotsync_login)
        self.shotsync_panel.logoutRequested.connect(self._shotsync_logout)
        self.shotsync_panel.refreshRequested.connect(self._shotsync_refresh_requested)
        self.shotsync_panel.receiveRequested.connect(self._shotsync_receive_requested)
        self.shotsync_panel.selectRequested.connect(self._shotsync_select_requested)
        self.shotsync_panel.removeLocalRequested.connect(self._shotsync_remove_local_requested)
        self.shotsync_panel.deleteServerRequested.connect(self._shotsync_delete_server_requested)
        self.shotsync_panel.getMarksForRequested.connect(self._shotsync_get_marks_for_requested)
        self.shotsync_panel.shootingActivated.connect(self._shotsync_shooting_activated)
        self.shotsync_panel.sendFolderRequested.connect(self._shotsync_send_current_folder)

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
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(5)

        filter_panel = QWidget()
        filter_panel.setObjectName("viewerFiltersPanel")
        filter_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        filter_layout = QHBoxLayout(filter_panel)
        filter_layout.setContentsMargins(7, 4, 7, 4)
        filter_layout.setSpacing(3)
        filter_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        filter_icon = QLabel()
        filter_icon.setFixedSize(12, 12)
        filter_icon.setPixmap(_fomantic_icon("filter", 12, "#a8b0bd").pixmap(QSize(12, 12)))
        filter_layout.addWidget(filter_icon)

        
        self.rating_filter = QComboBox()
        self.rating_filter.addItem("Все рейтинги", None)
        self.rating_filter.setItemIcon(0, _fomantic_icon("star", 10, "#a8b0bd"))
        for rating in range(5, 0, -1):
            self.rating_filter.addItem("★" * rating, rating)
            self.rating_filter.setItemIcon(self.rating_filter.count() - 1, _fomantic_icon("star", 10, "#a8b0bd"))
        self.rating_filter.setFixedWidth(118)
        self.color_filter = QComboBox()
        for label, value in (("Все цвета", None), ("Без цвета", ""), ("Красный", "red"), ("Жёлтый", "yellow"), ("Зелёный", "green"), ("Синий", "blue"), ("Фиолетовый", "purple")):
            self.color_filter.addItem(label, value)
            if value is not None:
                self.color_filter.setItemIcon(self.color_filter.count() - 1, _color_swatch_icon(value or None))
        self.color_filter.setItemIcon(0, _fomantic_icon("brush", 10, "#a8b0bd"))
        self.color_filter.setFixedWidth(118)
        self.media_filter = QComboBox()
        for label, value in (("Фото и видео", None), ("Фото", "image"), ("Видео", "video")):
            self.media_filter.addItem(label, value)
        self.media_filter.setItemIcon(0, _fomantic_icon("media", 10, "#a8b0bd"))
        self.media_filter.setItemIcon(1, _fomantic_icon("images", 10, "#a8b0bd"))
        self.media_filter.setItemIcon(2, _fomantic_icon("film", 10, "#a8b0bd"))
        self.media_filter.setFixedWidth(118)
        self.file_type_filter = QComboBox()
        # The combined option is the neutral/default state: it must not hide
        # videos or other supported image formats. The dedicated JPG/RAW
        # options below are the actual file-type filters.
        for label, value in (("JPG и RAW", None), ("Только JPG", "jpg"), ("Только RAW", "raw")):
            self.file_type_filter.addItem(label, value)
        self.file_type_filter.setItemIcon(0, _fomantic_icon("images", 10, "#a8b0bd"))
        self.file_type_filter.setItemIcon(1, _fomantic_icon("file", 10, "#a8b0bd"))
        self.file_type_filter.setItemIcon(2, _fomantic_icon("camera", 10, "#a8b0bd"))
        self.file_type_filter.setFixedWidth(106)
        self.camera_filter = QComboBox()
        self.camera_filter.addItem("Все камеры", None)
        self.camera_filter.setItemIcon(0, _fomantic_icon("images", 10, "#a8b0bd"))
        self.camera_filter.setFixedWidth(132)
        self.shot_filter = QComboBox()
        for label, value in (("Все планы", None), ("Крупный", "closeup"), ("Средний", "medium"), ("Общий", "wide"), ("Без лиц", "no_face")):
            self.shot_filter.addItem(label, value)
        self.shot_filter.hide()
        self.sort_combo = QComboBox()
        for label, value in (("По имени ↑", "name"), ("По имени ↓", "name_desc"), ("По времени ↑", "time"), ("По времени ↓", "time_desc"), ("По рейтингу", "rating")):
            self.sort_combo.addItem(label, value)
        self.sort_combo.setItemIcon(0, _fomantic_icon("sort", 10, "#a8b0bd"))
        for index in range(1, self.sort_combo.count()):
            self.sort_combo.setItemIcon(index, _fomantic_icon("sort", 10, "#a8b0bd"))
        self.sort_combo.setCurrentIndex(self.sort_combo.findData("time"))
        self.sort_combo.setFixedWidth(118)
        self.search_edit = CenteredSearchEdit()
        self.search_edit.setObjectName("viewerSearchEdit")
        self.search_edit.addAction(
            _fomantic_icon("search", 12, "#a8b0bd"),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.search_edit.setPlaceholderText("Поиск")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(112)
        # Match the native height of the neighbouring combo-box controls.
        self.search_edit.setFixedHeight(self.media_filter.sizeHint().height())
        for control in (self.rating_filter, self.color_filter, self.media_filter, self.file_type_filter, self.camera_filter, self.shot_filter, self.sort_combo):
            control.currentIndexChanged.connect(self._apply_view)
            filter_layout.addWidget(control)
        self.search_edit.textChanged.connect(self._apply_view)
        filter_layout.addWidget(self.search_edit)

        self.face_filter_chip = QFrame()
        self.face_filter_chip.setObjectName("fullFaceFilterChip")
        chip_layout = QHBoxLayout(self.face_filter_chip)
        chip_layout.setContentsMargins(3, 2, 3, 2)
        chip_layout.setSpacing(2)
        self.face_filter_avatar_label = QLabel()
        self.face_filter_avatar_label.setFixedSize(20, 20)
        self.face_filter_avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip_layout.addWidget(self.face_filter_avatar_label)
        self.face_clear_button = QToolButton()
        self.face_clear_button.setObjectName("fullFaceFilterClear")
        self.face_clear_button.setIcon(_fomantic_icon("close", 10))
        self.face_clear_button.setFixedSize(17, 17)
        self.face_clear_button.setIconSize(QSize(10, 10))
        self.face_clear_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.face_clear_button.setAutoRaise(True)
        self.face_clear_button.setToolTip("Сбросить фильтр по лицу")
        self.face_clear_button.clicked.connect(self._clear_face_search)
        chip_layout.addWidget(self.face_clear_button)
        self.face_filter_chip.hide()
        filter_layout.addWidget(self.face_filter_chip)

        self.ai_button = QToolButton()
        self.ai_button.setObjectName("toolbarAction")
        self.ai_button.setText("AI")
        self.ai_button.setIcon(_fomantic_icon("magic", 20, "#c9ddff"))
        self.ai_button.setIconSize(QSize(20, 20))
        self.ai_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.ai_button.setToolTip("AI: серии и лица")
        self.ai_button.clicked.connect(self._show_ai_menu)
        self.ai_analysis_available = False
        self.ai_button.setFixedSize(52, 44)

        self.xmp_button = QToolButton()
        self.xmp_button.setObjectName("toolbarAction")
        self.xmp_button.setText("XMP")
        self.xmp_button.setIcon(_fomantic_icon("file", 20, "#d6d6d6"))
        self.xmp_button.setIconSize(QSize(20, 20))
        self.xmp_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.xmp_button.setToolTip("Экспорт метаданных в XMP")
        self.xmp_button.clicked.connect(self._show_xmp_menu)
        self.xmp_button.setFixedSize(52, 44)

        self.utilities_button = QToolButton()
        self.utilities_button.setObjectName("toolbarAction")
        self.utilities_button.setText("Утилиты")
        self.utilities_button.setIcon(_fomantic_icon("wrench", 20, "#d6d6d6"))
        self.utilities_button.setIconSize(QSize(20, 20))
        self.utilities_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.utilities_button.setToolTip("Утилиты")
        self.utilities_button.clicked.connect(self._show_utilities_menu)
        self.utilities_button.setFixedSize(52, 44)
        toolbar_actions = QWidget()
        toolbar_actions.setObjectName("toolbarActionsGroup")
        toolbar_actions.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        toolbar_actions_layout = QHBoxLayout(toolbar_actions)
        toolbar_actions_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_actions_layout.setSpacing(0)
        toolbar_actions_layout.addWidget(self.ai_button)
        toolbar_actions_layout.addWidget(self.xmp_button)
        toolbar_actions_layout.addWidget(self.utilities_button)
        toolbar_layout.addWidget(toolbar_actions)
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
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignBottom)
        self.status_panel.installEventFilter(self)
        toolbar_layout.addWidget(self.status_panel, 1)
        toolbar_layout.addWidget(filter_panel)

        self.ai_panel = QWidget()
        self.ai_panel.setObjectName("viewerAiPanel")
        ai_layout = QHBoxLayout(self.ai_panel)
        ai_layout.setContentsMargins(8, 2, 8, 2)
        ai_layout.setSpacing(10)
        self.series_faces_group = QWidget()
        self.series_faces_group.setObjectName("aiPanelGroup")
        series_faces_layout = QHBoxLayout(self.series_faces_group)
        series_faces_layout.setContentsMargins(0, 0, 0, 0)
        series_faces_layout.setSpacing(3)
        series_faces_title = QLabel("СЕРИИ И ЛИЦА")
        series_faces_title.setObjectName("aiPanelTitle")
        series_faces_layout.addWidget(series_faces_title)
        self.series_toggle = QToolButton()
        self.series_toggle.setObjectName("aiFilter")
        self.series_toggle.setIcon(_fomantic_icon("images", 11))
        self.series_toggle.setText("Серии")
        self.series_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.series_toggle.setToolTip("Включить или выключить группировку по сериям")
        self.series_toggle.setCheckable(True)
        self.series_toggle.setChecked(True)
        self.series_toggle.toggled.connect(self._series_toggle_changed)
        series_faces_layout.addWidget(self.series_toggle)
        self.faces_panel_button = QToolButton()
        self.faces_panel_button.setObjectName("aiFilter")
        self.faces_panel_button.setIcon(_fomantic_icon("user", 11))
        self.faces_panel_button.setText("Лица")
        self.faces_panel_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.faces_panel_button.setToolTip("Открыть наборы лиц")
        self.faces_panel_button.clicked.connect(self._show_face_sets)
        series_faces_layout.addWidget(self.faces_panel_button)
        ai_layout.addWidget(self.series_faces_group)

        self.shot_group = QWidget()
        self.shot_group.setObjectName("aiPanelGroup")
        shot_layout = QHBoxLayout(self.shot_group)
        shot_layout.setContentsMargins(0, 0, 0, 0)
        shot_layout.setSpacing(3)
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
        ai_layout.addStretch(1)
        ai_layout.addWidget(self.shot_group)
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

        self.grid_content_stack = QStackedWidget()
        self.grid_content_stack.addWidget(self.grid)
        self.grid_zoom_controls = GridZoomControls(self.grid.change_card_size, self.grid_content_stack)
        self.grid_content_stack.installEventFilter(self)
        self._position_grid_zoom_controls()
        content_layout.addWidget(self.grid_content_stack, 1)
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

    def _build_shotsync_action_page(self) -> QWidget:
        """Empty-state shown for a cloud shooting that has no local folder yet."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.addStretch(1)
        box = QWidget()
        box.setObjectName("shotsyncActionPage")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(28, 24, 28, 24)
        box_layout.setSpacing(10)
        self.shotsync_action_title = QLabel()
        self.shotsync_action_title.setObjectName("shotsyncTitle")
        self.shotsync_action_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(self.shotsync_action_title)
        self.shotsync_action_hint = QLabel("Выберите, как работать со съёмкой.")
        self.shotsync_action_hint.setObjectName("shotsyncHint")
        self.shotsync_action_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(self.shotsync_action_hint)
        self.shotsync_take_button = QPushButton("Взять на отбор")
        self.shotsync_take_button.clicked.connect(self._take_displayed_shotsync_shooting)
        box_layout.addWidget(self.shotsync_take_button)
        self.shotsync_watch_button = QPushButton("Получать оригиналы")
        self.shotsync_watch_button.clicked.connect(self._watch_displayed_shotsync_shooting)
        box_layout.addWidget(self.shotsync_watch_button)
        layout.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self._displayed_shotsync_shooting: dict | None = None
        return page

    def _build_shotsync_upload_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        self.shotsync_upload_title = QLabel("Загружаем съёмку на сервер…")
        self.shotsync_upload_title.setObjectName("shotsyncUploadStateTitle")
        self.shotsync_upload_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.shotsync_upload_title)
        self.shotsync_upload_status = QLabel("Подготавливаем фотографии…")
        self.shotsync_upload_status.setObjectName("shotsyncUploadStateHint")
        self.shotsync_upload_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.shotsync_upload_status)
        layout.addStretch(1)
        return page

    def _create_actions(self) -> None:
        self._hotkey_actions: dict[str, QAction] = {}

        def add_hotkey(identifier: str, callback: Callable, target: QWidget | None = None, context: Qt.ShortcutContext | None = None) -> None:
            host = target if target is not None else self
            action = QAction(HOTKEY_DEFAULTS[identifier][0], host)
            action.triggered.connect(callback)
            if context is not None:
                action.setShortcutContext(context)
            host.addAction(action)
            self._hotkey_actions[identifier] = action

        add_hotkey("full_view", self._open_selected)
        add_hotkey("open_in_editor", self._open_in_editor)
        add_hotkey("grid", self.show_grid)
        # Scoped to the full view so Shift+Arrow keeps extending the grid selection.
        add_hotkey("strip_collapse", lambda: self.full_view.cycle_strip(1), target=self.full_view, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        add_hotkey("strip_expand", lambda: self.full_view.cycle_strip(-1), target=self.full_view, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

        # Navigation keys are intentionally not exposed in the preferences.
        escape = QAction("Back", self)
        escape.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        escape.triggered.connect(self._handle_escape)
        self.addAction(escape)

        add_hotkey("refresh", lambda: self.load_directory(self.current_dir))
        add_hotkey("fullscreen", self.toggle_fullscreen)
        add_hotkey("quick_mark", self._apply_quick_mark)
        add_hotkey("comment", self._show_comment_dialog)
        add_hotkey("quick_copy", lambda: self._show_quick_transfer(move=False))
        add_hotkey("quick_move", lambda: self._show_quick_transfer(move=True))

        for rating in range(0, 6):
            add_hotkey(f"rating_{rating}", lambda _checked=False, value=rating: self._set_selected_rating(value or None))

        for index, color in enumerate(("", "red", "yellow", "green", "blue", "purple")):
            add_hotkey(f"color_{index}", lambda _checked=False, value=color: self._set_selected_color(value))

        self._reload_hotkeys()

    def _reload_hotkeys(self) -> None:
        for identifier, action in self._hotkey_actions.items():
            action.setShortcut(_hotkey_sequence(self.settings, identifier))

    def _quick_transfer_destinations(self) -> list[Path]:
        """Last used destination first, then open tabs and the remaining history."""
        raw_history = self.settings.value("quick_transfer/recent_destinations", [], list)
        history = [Path(value) for value in raw_history if isinstance(value, str) and Path(value).is_dir()]
        tabs = self.destination_paths_provider() if self.destination_paths_provider else []
        candidates = [*history, *tabs]
        destinations: list[Path] = []
        for path in candidates:
            if path.is_dir() and path not in destinations and path != self.current_dir:
                destinations.append(path)
            if len(destinations) == 9:
                break
        return destinations

    def _remember_quick_transfer_destination(self, destination: Path) -> None:
        history = [path for path in self._quick_transfer_destinations() if path != destination]
        self.settings.setValue("quick_transfer/recent_destinations", [str(destination), *(str(path) for path in history)][:9])

    def _show_quick_transfer(self, *, move: bool) -> None:
        sources = [path for path in self._file_panel_paths() if path.exists()]
        if not sources:
            return
        identifier = "quick_move" if move else "quick_copy"
        operation = "переместить" if move else "скопировать"
        dialog = QuickTransferDialog(
            operation,
            self._quick_transfer_destinations(),
            _hotkey_sequence(self.settings, identifier),
            lambda destination, update_recent, progress: self._quick_transfer_to(
                sources, destination, move, update_recent, progress
            ),
            self,
        )
        # A WindowShortcut QAction can otherwise consume the repeated shortcut
        # before this modal dialog receives its key press.  The dialog owns
        # both quick-transfer shortcuts until it closes.
        quick_actions = [self._hotkey_actions[name] for name in ("quick_copy", "quick_move")]
        enabled = [action.isEnabled() for action in quick_actions]
        for action in quick_actions:
            action.setEnabled(False)
        try:
            dialog.exec()
        finally:
            for action, was_enabled in zip(quick_actions, enabled):
                action.setEnabled(was_enabled)

    def _quick_transfer_to(self, sources: list[Path], destination: Path, move: bool, update_recent: bool, progress: Callable[[int, int], None]) -> None:
        if update_recent:
            self._remember_quick_transfer_destination(destination)
        self._receive_dropped_paths(
            sources,
            destination,
            Qt.DropAction.MoveAction if move else Qt.DropAction.CopyAction,
            progress=progress,
        )

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

    def _position_grid_zoom_controls(self) -> None:
        if not hasattr(self, "grid_zoom_controls"):
            return
        margin = 10
        controls = self.grid_zoom_controls
        controls.adjustSize()
        controls.move(
            max(margin, self.grid_content_stack.width() - controls.width() - margin),
            max(margin, self.grid_content_stack.height() - controls.height() - margin),
        )
        controls.raise_()

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "grid_content_stack", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._position_grid_zoom_controls)
        if watched is getattr(self, "status_panel", None) and event.type() == QEvent.Type.Resize:
            self._fit_status_text()
        if event.type() == QEvent.Type.KeyPress and self.stack.currentWidget() is self.grid_page:
            focus_widget = watched if isinstance(watched, QWidget) else QApplication.focusWidget()
            if self._is_grid_page_widget(focus_widget):
                file_panel_focused = self._is_directory_focus_widget(focus_widget) or focus_widget is self.grid or self.grid.isAncestorOf(focus_widget)
                if file_panel_focused and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    if event.key() == Qt.Key.Key_C:
                        self._copy_file_selection(cut=False)
                        return True
                    if event.key() == Qt.Key.Key_X:
                        self._copy_file_selection(cut=True)
                        return True
                    if event.key() == Qt.Key.Key_V:
                        self._paste_file_selection()
                        return True
                    if event.key() == Qt.Key.Key_D:
                        if self._is_directory_focus_widget(focus_widget):
                            self.dir_tree.clearSelection()
                        else:
                            self.grid.clearSelection()
                        return True
                if event.key() == Qt.Key.Key_Delete and self._is_directory_focus_widget(focus_widget):
                    index = self.dir_tree.currentIndex()
                    if index.isValid():
                        self._delete_paths([Path(self.dir_model.filePath(index))], permanent=bool(
                            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                        ))
                        return True
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
        if not index.isValid():
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
                current_relative = self.current_dir.relative_to(old_path)
            except ValueError:
                current_relative = None
            if current_relative is not None:
                # SQLite cannot reliably move an open database on Windows.
                # Close this workspace's cache before moving its path-hashed
                # database, then reopen it at the renamed location.
                self._flush_folder_cache(wait=True, close=True)
                self.folder_cache = None
                self.cache_ready = False
            try:
                old_path.rename(new_path)
            except OSError as error:
                editor.setStyleSheet("border: 1px solid #c43d2f;")
                editor.setToolTip(str(error))
                if current_relative is not None:
                    self.load_directory(old_path / current_relative)
                QTimer.singleShot(0, editor.setFocus)
                return
            try:
                relocate_folder_caches(old_path, new_path)
            except OSError as error:
                QMessageBox.warning(
                    self,
                    "Кэш папки",
                    f"Папка переименована, но не удалось перенести её кэш:\n{error}",
                )
            if current_relative is not None:
                self.load_directory(new_path / current_relative)
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

    def _create_new_folder(self, parent_dir: Path | None = None) -> None:
        """Создать новую папку в указанной или текущей директории."""
        parent_dir = parent_dir or self.current_dir
        if not parent_dir:
            return

        # Создаем временное имя для папки
        i = 1
        while True:
            temp_name = f"Новая папка {i}"
            temp_path = parent_dir / temp_name
            if not temp_path.exists():
                break
            i += 1

        try:
            # Устанавливаем путь к новой папке в модели ПЕРЕД ее созданием
            self.dir_model._new_folder_path = temp_path

            parent_index = self.dir_model.index(str(parent_dir))
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

    @staticmethod
    def _favorite_path_key(path: Path) -> str:
        try:
            return str(path.expanduser().resolve()).casefold()
        except OSError:
            return str(path.expanduser()).casefold()

    def _default_favorite_paths(self) -> list[Path]:
        locations = (
            QStandardPaths.StandardLocation.DocumentsLocation,
            QStandardPaths.StandardLocation.PicturesLocation,
        )
        paths: list[Path] = []
        for location in locations:
            value = QStandardPaths.writableLocation(location)
            path = Path(value) if value else None
            if path is not None and path.is_dir() and path not in paths:
                paths.append(path)
        return paths

    def _load_favorites(self) -> None:
        key = "sidebar/favorite_paths"
        if self.settings.contains(key):
            stored = self.settings.value(key, [], list)
            raw_paths = stored if isinstance(stored, (list, tuple)) else [stored]
        else:
            raw_paths = [str(path) for path in self._default_favorite_paths()]
            self.settings.setValue(key, raw_paths)
        paths: list[Path] = []
        seen: set[str] = set()
        for raw_path in raw_paths:
            path = Path(str(raw_path)).expanduser()
            path_key = self._favorite_path_key(path)
            if path.is_dir() and path_key not in seen:
                paths.append(path)
                seen.add(path_key)
        self._set_favorites(paths)

    def _set_favorites(self, paths: list[Path]) -> None:
        self.favorites_list.clear()
        for path in paths:
            item = QListWidgetItem(self.volume_icon_provider.icon(QFileInfo(str(path))), path.name or str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.favorites_list.addItem(item)

    def _save_favorites(self) -> None:
        self.settings.setValue(
            "sidebar/favorite_paths",
            [self.favorites_list.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.favorites_list.count())],
        )

    def _restore_favorites_height(self) -> None:
        if not hasattr(self, "favorites_splitter"):
            return
        requested = self.settings.value("sidebar/favorites_height", 144, int)
        available = max(48, self.favorites_splitter.height() - self.favorites_splitter.handleWidth())
        height = max(48, min(int(requested), max(48, available - 48)))
        self.favorites_splitter.setSizes([max(48, available - height), height])

    def _save_favorites_height(self) -> None:
        if hasattr(self, "favorites_panel") and self.favorites_panel.height() >= 48:
            self.settings.setValue("sidebar/favorites_height", self.favorites_panel.height())

    def _add_current_directory_to_favorites(self) -> None:
        path = self.current_dir
        if not path.is_dir():
            return
        path_key = self._favorite_path_key(path)
        for row in range(self.favorites_list.count()):
            item = self.favorites_list.item(row)
            if self._favorite_path_key(Path(item.data(Qt.ItemDataRole.UserRole))) == path_key:
                self.favorites_list.setCurrentItem(item)
                return
        item = QListWidgetItem(self.volume_icon_provider.icon(QFileInfo(str(path))), path.name or str(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setToolTip(str(path))
        self.favorites_list.addItem(item)
        self._save_favorites()

    def _open_favorite(self, item: QListWidgetItem) -> None:
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if path.is_dir():
            self.load_directory(path)
            self._reveal_favorite_in_tree(path)

    def _reveal_favorite_in_tree(self, path: Path, attempts_left: int = 20) -> None:
        """Expand and select a favorite after QFileSystemModel has loaded it."""
        index = self.dir_model.index(str(path))
        if not index.isValid():
            if attempts_left > 0:
                QTimer.singleShot(
                    50,
                    lambda target=path, attempts=attempts_left - 1: self._reveal_favorite_in_tree(target, attempts),
                )
            return
        self._expand_tree_path(index)
        self.dir_tree.setCurrentIndex(index)
        self.dir_tree.scrollTo(index, QTreeView.ScrollHint.EnsureVisible)

    def _remove_selected_favorite(self) -> None:
        item = self.favorites_list.currentItem()
        if item is not None:
            self._remove_favorite(str(item.data(Qt.ItemDataRole.UserRole)))

    def _remove_favorite(self, path_text: str) -> None:
        path_key = self._favorite_path_key(Path(path_text))
        for row in range(self.favorites_list.count()):
            item = self.favorites_list.item(row)
            if self._favorite_path_key(Path(item.data(Qt.ItemDataRole.UserRole))) == path_key:
                self.favorites_list.takeItem(row)
                self._save_favorites()
                break

    def _show_directory_context_menu(self, position: QPoint) -> None:
        index = self.dir_tree.indexAt(position)
        if not index.isValid():
            return
        path = Path(self.dir_model.filePath(index))
        if not path.is_dir():
            return
        self.dir_tree.setCurrentIndex(index)
        menu = QMenu(self.dir_tree)
        create = menu.addAction("Создать папку")
        create.setIcon(_fomantic_icon("folder-plus", 14))
        create.triggered.connect(
            lambda _checked=False, target=path: QTimer.singleShot(
                0, lambda: self._create_new_folder(target)
            )
        )
        rename = menu.addAction("Переименовать")
        rename.setIcon(_fomantic_icon("edit", 13))
        rename.triggered.connect(
            lambda _checked=False, target=path: QTimer.singleShot(
                0, lambda: self._begin_directory_inline_rename(target)
            )
        )
        delete = menu.addAction("Удалить")
        delete.setIcon(_fomantic_icon("trash", 13))
        delete.triggered.connect(lambda _checked=False, target=path: self._delete_paths([target], permanent=False))
        menu.exec(self.dir_tree.viewport().mapToGlobal(position))

    def _delete_grid_selection(self, permanent: bool) -> None:
        self._delete_paths(self._selected_paths(), permanent=permanent)

    def _file_panel_paths(self) -> list[Path]:
        """Return the selected filesystem entries from the active file panel."""
        focus = QApplication.focusWidget()
        if self._is_directory_focus_widget(focus):
            selection = self.dir_tree.selectionModel()
            if selection is None:
                return []
            return [
                Path(self.dir_model.filePath(index))
                for index in selection.selectedRows(0)
                if index.isValid()
            ]
        return self._selected_paths()

    def _paste_destination(self) -> Path:
        focus = QApplication.focusWidget()
        if self._is_directory_focus_widget(focus):
            index = self.dir_tree.currentIndex()
            if index.isValid():
                candidate = Path(self.dir_model.filePath(index))
                if candidate.is_dir():
                    return candidate
        return self.current_dir

    def _copy_file_selection(self, *, cut: bool) -> None:
        paths = [path for path in self._file_panel_paths() if path.exists()]
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        # Explorer understands Preferred DropEffect, making Ctrl+X usable
        # outside the application as well as in another RAWww tab.
        mime.setData(_PREFERRED_DROP_EFFECT_MIME, (2 if cut else 1).to_bytes(4, "little"))
        if cut:
            mime.setData("application/x-rawww-cut", b"1")
        QApplication.clipboard().setMimeData(mime)

    def _paste_file_selection(self) -> None:
        mime = QApplication.clipboard().mimeData()
        paths = _local_paths_from_mime(mime)
        if not paths:
            return
        action = Qt.DropAction.MoveAction if _mime_requests_move(mime) else Qt.DropAction.CopyAction
        self._receive_dropped_paths(paths, self._paste_destination(), action)

    @staticmethod
    def _renamed_transfer_target(target: Path) -> Path:
        """Return the first non-existing ``name (N)`` variant of a target."""
        for number in range(2, 10_000):
            candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
            if not candidate.exists():
                return candidate
        raise OSError("Не удалось подобрать свободное имя")

    def _resolve_transfer_conflict(self, source: Path, target: Path, move: bool) -> str:
        verb = "перемещении" if move else "копировании"
        dialog = QMessageBox(self)
        dialog.setObjectName("transferConflictDialog")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Файл уже существует")
        dialog.setText(f"В папке назначения уже есть «{target.name}».")
        dialog.setInformativeText(f"Что сделать при {verb} «{source.name}»?")
        skip = dialog.addButton("Пропустить", QMessageBox.ButtonRole.ActionRole)
        rename = dialog.addButton("Переименовать", QMessageBox.ButtonRole.ActionRole)
        replace = dialog.addButton("Заменить", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(rename)
        dialog.exec()
        chosen = dialog.clickedButton()
        if chosen is skip:
            return "skip"
        if chosen is rename:
            return "rename"
        if chosen is replace:
            return "replace"
        return "cancel"

    def _receive_dropped_paths(self, paths: list[Path], destination: Path | None, action, progress: Callable[[int, int], None] | None = None) -> None:
        """Copy external files or copy/move app files into a folder destination."""
        if destination is None:
            destination = self.current_dir
        if not destination.is_dir():
            return
        sources = list(dict.fromkeys(path for path in paths if path.exists()))
        if not sources:
            return
        move = action == Qt.DropAction.MoveAction
        # Moving the folder currently being viewed must release its SQLite
        # cache before relocating that cache alongside the folder.
        if move and self.current_dir in sources:
            self.load_directory(self.current_dir.parent)
        errors: list[str] = []
        changed = False
        moved_files: list[Path] = []
        moved_folders: list[tuple[Path, Path]] = []
        # Avoid a delayed watcher reload after the grid has been reconciled
        # below, just as delete does.
        if move and any(source.parent == self.current_dir for source in sources):
            self.folder_change_timer.stop()
            self._ignore_folder_changes_until = max(
                self._ignore_folder_changes_until,
                monotonic() + FOLDER_CHANGE_DEBOUNCE_MS / 1_000 + 0.5,
            )
        for completed, source in enumerate(sources, start=1):
            try:
                source_resolved = source.resolve()
                destination_resolved = destination.resolve()
            except OSError:
                source_resolved, destination_resolved = source, destination
            if source.parent == destination:
                if move:
                    continue
                errors.append(f"{source.name}: уже находится в этой папке")
                continue
            if source.is_dir() and destination_resolved.is_relative_to(source_resolved):
                errors.append(f"{source.name}: нельзя поместить папку внутрь самой себя")
                continue
            target = destination / source.name
            if target.exists():
                resolution = self._resolve_transfer_conflict(source, target, move)
                if resolution == "cancel":
                    break
                if resolution == "skip":
                    continue
                if resolution == "rename":
                    try:
                        target = self._renamed_transfer_target(target)
                    except OSError as exc:
                        errors.append(f"{source.name}: {exc}")
                        continue
                elif resolution == "replace":
                    try:
                        if target.is_dir():
                            remove_folder_cache(target)
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                        self.cache_flush_executor.submit(prune_folder_cache, destination)
                    except OSError as exc:
                        errors.append(f"{source.name}: не удалось заменить {exc}")
                        continue
            is_folder = source.is_dir()
            try:
                if move:
                    shutil.move(str(source), str(target))
                    if is_folder:
                        moved_folders.append((source, target))
                    else:
                        moved_files.append(source)
                elif is_folder:
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
                changed = True
            except OSError as exc:
                errors.append(f"{source.name}: {exc}")
            if progress is not None:
                progress(completed, len(sources))
        for source, target in moved_folders:
            try:
                relocate_folder_caches(source, target)
            except OSError as exc:
                errors.append(f"кэш {source.name}: {exc}")
        if changed:
            moved_from_current = [
                source for source in [*moved_files, *(source for source, _target in moved_folders)]
                if source.parent == self.current_dir
            ]
            if moved_from_current:
                # Keep the grid stable instead of rebuilding every thumbnail.
                self._remove_paths_from_grid(moved_from_current)
                self.cache_flush_executor.submit(prune_folder_cache, self.current_dir)
            # The current folder received files: its grid must discover them.
            if self.current_dir == destination:
                self.folder_change_timer.stop()
                self.load_directory(self.current_dir)
        if errors:
            QMessageBox.warning(self, "Копирование файлов", "Не удалось обработать некоторые объекты:\n" + "\n".join(errors))

    def _delete_paths(self, paths: list[Path], *, permanent: bool) -> None:
        """Delete grid/tree entries, preserving selection copies from ShotSync."""
        targets = list(dict.fromkeys(path for path in paths if path.exists()))
        if not targets:
            return
        files = [path for path in targets if path.is_file()]
        try:
            is_selection_copy = self.current_dir.is_relative_to(selection_root())
        except OSError:
            is_selection_copy = False
        if files and is_selection_copy:
            QMessageBox.information(
                self,
                "ShotSync",
                "Фотографии из съёмки, взятой на отбор из ShotSync, нельзя удалить отдельно.",
            )
            targets = [path for path in targets if path.is_dir()]
            if not targets:
                return
        for folder in (path for path in targets if path.is_dir()):
            try:
                resolved = folder.resolve()
            except OSError:
                resolved = folder
            if resolved.parent == resolved:
                QMessageBox.warning(self, "Удаление", "Нельзя удалить корневую папку диска.")
                return

        action = "удалить навсегда" if permanent else "переместить в корзину"
        if not self.settings.value("behavior/delete_without_confirmation", False, bool):
            names = "\n".join(f"• {path.name}" for path in targets[:8])
            if len(targets) > 8:
                names += f"\n• и ещё {len(targets) - 8}"
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Удаление")
            dialog.setText(f"{action.capitalize()} {len(targets)} объект(а)?\n\n{names}")
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Yes).setText("Удалить")
            dialog.button(QMessageBox.StandardButton.Cancel).setText("Отмена")
            if dialog.exec() != QMessageBox.StandardButton.Yes:
                return

        # A current-folder delete must first release the open cache and watcher.
        deleting_current_folder = self.current_dir in targets
        if deleting_current_folder:
            self.load_directory(self.current_dir.parent)

        # The watcher also reports changes made by this operation. The grid is
        # reconciled below without clearing it, so a delayed full reload would
        # only cause a second, distracting redraw.
        self.folder_change_timer.stop()
        self._ignore_folder_changes_until = max(
            self._ignore_folder_changes_until,
            monotonic() + FOLDER_CHANGE_DEBOUNCE_MS / 1_000 + 0.5,
        )

        deleted_files: list[Path] = []
        deleted_folders: list[Path] = []
        errors: list[str] = []
        for path in targets:
            is_folder = path.is_dir()
            try:
                if permanent:
                    if is_folder:
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                else:
                    send2trash(str(path))
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if is_folder:
                deleted_folders.append(path)
            else:
                deleted_files.append(path)

        for folder in deleted_folders:
            try:
                remove_folder_cache(folder)
            except OSError as exc:
                errors.append(f"кэш {folder.name}: {exc}")

        if deleted_files:
            # Compact cache off the UI thread; it also removes rows for any
            # sidecars or concurrent filesystem changes seen by the watcher.
            self.cache_flush_executor.submit(prune_folder_cache, self.current_dir)

        deleted = [*deleted_files, *deleted_folders]
        if deleted and not deleting_current_folder:
            self._remove_paths_from_grid(deleted)
        if errors:
            QMessageBox.warning(self, "Удаление", "Не удалось удалить:\n" + "\n".join(errors))

    def _remove_paths_from_grid(self, deleted: list[Path]) -> None:
        """Reconcile a local delete without clearing/repopulating the grid."""
        removed = set(deleted)
        old_paths = list(self.paths)
        selected_rows = [
            old_paths.index(path)
            for path in removed
            if path in old_paths
        ]
        anchor: Path | None = None
        if selected_rows:
            after = max(selected_rows) + 1
            candidates = old_paths[after:] + list(reversed(old_paths[:min(selected_rows)]))
            anchor = next((path for path in candidates if path not in removed), None)

        self.all_paths = [path for path in self.all_paths if path not in removed]
        self.view_paths = [path for path in self.view_paths if path not in removed]
        self.paths = self._grid_paths_with_series(self.view_paths)
        self.photo_details = {
            name: detail for name, detail in self.photo_details.items()
            if self.current_dir / name not in removed
        }
        self.image_embeddings = {
            name: embedding for name, embedding in self.image_embeddings.items()
            if self.current_dir / name not in removed
        }
        self.view_generation += 1
        self.populate_timer.stop()
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()

        visible = set(self.paths)
        for path, item in list(self.items_by_path.items()):
            if path not in visible:
                self.grid.takeItem(self.grid.row(item))
                self.items_by_path.pop(path, None)

        # Deleting a collapsed-series leader can promote a previously hidden
        # neighbour. Reuse the existing cards wherever possible and only add
        # the promoted card; nothing visible is cleared in either case.
        for row, path in enumerate(self.paths):
            item = self.items_by_path.get(path)
            if item is None:
                item = self._grid_item_for_path(path)
                self.grid.insertItem(row, item)
                self.items_by_path[path] = item
            elif self.grid.row(item) != row:
                self.grid.takeItem(self.grid.row(item))
                self.grid.insertItem(row, item)
            item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
            item.setData(SERIES_ROLE, self.series_cards.get(path, {}))

        self.populate_index = len(self.paths)
        self._update_analysis_controls()
        self._refresh_status_panel()
        self._schedule_visible_thumb_priority()
        if self.cache_ready:
            self.thumb_timer.start()
        if anchor is not None and anchor in self.items_by_path:
            item = self.items_by_path[anchor]
            self.grid.clearSelection()
            item.setSelected(True)
            self.grid.setCurrentItem(item)
            self.grid.scrollToItem(item, QListWidget.ScrollHint.EnsureVisible)
        self.grid.setFocus(Qt.FocusReason.OtherFocusReason)

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
                button.setIconSize(QSize(16, 16))
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
            target = self._last_directory_for_volume(drive_path) or drive_path
            self.load_directory(target if target.is_dir() else drive_path)

    def _last_directory_for_volume(self, volume_root: Path) -> Path | None:
        try:
            stored = json.loads(self.settings.value("last_directories_by_volume", "{}", str))
        except (TypeError, ValueError):
            return None
        if not isinstance(stored, dict):
            return None
        value = stored.get(_drive_key(volume_root))
        return Path(value) if value else None

    def _remember_directory_for_volume(self, directory: Path) -> None:
        root = _volume_root_for_path(directory, _mounted_volume_paths())
        if root is None or self.shotsync_active:
            return
        try:
            stored = json.loads(self.settings.value("last_directories_by_volume", "{}", str))
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[_drive_key(root)] = str(directory)
        self.settings.setValue("last_directories_by_volume", json.dumps(stored))

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
        logo_path = data_path("assets") / "shotsync.png"
        if logo_path.exists():
            px = QPixmap(str(logo_path)).scaled(
                16, 16,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if not px.isNull():
                return QIcon(px)
        return _fomantic_icon("cloud", 16, "#8fb8ff")

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

    def _show_shotsync_login(self) -> bool:
        """Open the shared sign-in form and return whether it completed."""
        if self.shotsync_client.has_key():
            return True
        self.shotsync_login_dialog.reset()
        return self.shotsync_login_dialog.exec() == QDialog.DialogCode.Accepted

    def _shotsync_logout(self) -> None:
        self.shotsync_client.logout()
        self._shotsync_checked = False
        self.settings.remove("shotsync/api_key")
        self.shotsync.set_api_key("")
        self._set_code_replacements(self._local_code_replacement_sets())
        self.shotsync_panel.show_login()
        self._refresh_shotsync_shortcuts()

    def _shotsync_login_succeeded(self, user: dict, key: str) -> None:
        self._shotsync_checked = True
        self.settings.setValue("shotsync/api_key", key)
        self.shotsync.set_api_key(key)
        self.shotsync_login_dialog.login_succeeded()
        self.shotsync_panel.show_logged_in(user)
        avatar_url = user.get("avatar_url")
        if avatar_url:
            self.shotsync_client.fetch_avatar(avatar_url)
        self.shotsync_panel.set_shootings_loading()
        self.shotsync_client.fetch_shootings()
        self._sync_code_replacements()
        self._refresh_shotsync_shortcuts()

    def _shotsync_login_failed(self, error: str) -> None:
        self.shotsync_login_dialog.show_error(error)
        if self.shotsync_active:
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
        self._sync_code_replacements()
        self._refresh_shotsync_shortcuts()

    def _shotsync_session_invalid(self, error: str) -> None:
        self._shotsync_checked = False
        self.settings.remove("shotsync/api_key")
        self.shotsync.set_api_key("")
        self._set_code_replacements(self._local_code_replacement_sets())
        if self.shotsync_active:
            self.shotsync_panel.show_login()
        self._refresh_shotsync_shortcuts()

    def _sync_code_replacements(self) -> None:
        """Pull the current web sets; mutations are posted immediately by the dialog."""
        if not self.shotsync_client.has_key():
            return
        self.shotsync_client.request_json(
            "/api/users/code-replacements/",
            lambda ok, data, _error: self._set_code_replacements(data.get("sets", [])) if ok else None,
        )

    def _local_code_replacement_sets(self) -> list[dict]:
        """Read the offline replacement library kept in the app settings."""
        sets = self.settings.value("code_replacements/local_sets", [], list)
        return [entry for entry in sets if isinstance(entry, dict)]

    def _set_code_replacements(self, sets: list[dict]) -> None:
        self.code_replacement_sets = [entry for entry in sets if isinstance(entry, dict)]
        if self._xmp_auto_enabled():
            self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))
        active_id = self.settings.value("code_replacements/active_set_id", 0, int)
        if self.code_replacement_sets and not any(group.get("id") == active_id for group in self.code_replacement_sets):
            active_id = int(self.code_replacement_sets[0].get("id") or 0)
            self.settings.setValue("code_replacements/active_set_id", active_id)
        for editor in (self.comment_edit, self.full_view.full_comment_edit):
            editor.set_codes(self.code_replacement_sets, active_id)

    def _shotsync_shootings_loaded(self, shootings: list) -> None:
        self._shotsync_shootings = [shooting for shooting in shootings if isinstance(shooting, dict)]
        self._reconcile_shotsync_selection_copies(self._shotsync_shootings)
        self._resume_shotsync_selection_copies(self._shotsync_shootings)
        self.shotsync_panel.set_shootings(shootings)
        self._refresh_shotsync_receiving()
        self._refresh_shotsync_local_folders(shootings)

    def _shotsync_shootings_failed(self, error: str) -> None:
        self.shotsync_panel.set_shootings_error(error)

    def _shotsync_avatar_loaded(self, image) -> None:
        self.shotsync_panel.set_avatar(image)

    # ----- live receive (feature 1) -------------------------------------
    def _refresh_shotsync_receiving(self) -> None:
        """Reflect which shootings are currently being received in the panel."""
        self.shotsync_panel.set_receiving_ids(self.shotsync.receiving_ids())

    def _shotsync_folder_map(self) -> dict[str, str]:
        try:
            value = json.loads(self.settings.value("shotsync/shooting_folders", "{}", str))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _remember_shotsync_folder(self, shooting_id: int, folder: Path) -> None:
        folders = self._shotsync_folder_map()
        folders[str(int(shooting_id))] = str(folder)
        self.settings.setValue("shotsync/shooting_folders", json.dumps(folders))

    def _forget_shotsync_folder(self, shooting_id: int) -> None:
        folders = self._shotsync_folder_map()
        folders.pop(str(int(shooting_id)), None)
        self.settings.setValue("shotsync/shooting_folders", json.dumps(folders))

    def _remember_shotsync_mode(self, shooting_id: int, mode: str) -> None:
        modes = self._stored_shotsync_modes()
        modes[int(shooting_id)] = mode
        self.settings.setValue("shotsync/folder_modes", json.dumps(modes))

    def _forget_shotsync_selection(self, shooting_id: int) -> None:
        modes = self._stored_shotsync_modes()
        modes.pop(int(shooting_id), None)
        self.settings.setValue("shotsync/folder_modes", json.dumps(modes))
        legacy_ids = self._legacy_shotsync_selection_ids()
        legacy_ids.discard(int(shooting_id))
        self.settings.setValue("shotsync/selection_ids", json.dumps(sorted(legacy_ids)))

    def _stored_shotsync_modes(self) -> dict[int, str]:
        try:
            raw = json.loads(self.settings.value("shotsync/folder_modes", "{}", str))
            if isinstance(raw, dict):
                return {int(shooting_id): str(mode) for shooting_id, mode in raw.items()}
        except (TypeError, ValueError):
            pass
        return {}

    def _legacy_shotsync_selection_ids(self) -> set[int]:
        try:
            raw = json.loads(self.settings.value("shotsync/selection_ids", "[]", str))
            return {int(value) for value in raw} if isinstance(raw, list) else set()
        except (TypeError, ValueError):
            return set()

    def _shotsync_folder_modes(self) -> dict[int, str]:
        """Return persisted local-folder modes, ignoring folders gone from disk."""
        folders = self._shotsync_folder_map()
        modes = self._stored_shotsync_modes()
        for shooting_id in self._legacy_shotsync_selection_ids():
            if shooting_id in modes:
                continue
            folder = folders.get(str(shooting_id))
            if folder:
                path = Path(folder)
                modes[shooting_id] = "selection_copy" if path.is_relative_to(selection_root()) else "uploaded"
        result: dict[int, str] = {}
        for shooting_id, mode in modes.items():
            folder = folders.get(str(shooting_id))
            if folder and Path(folder).is_dir():
                result[shooting_id] = mode
        return result

    def _local_shotsync_folder(self, shooting_id: int, title: str = "") -> Path | None:
        folder = self.shotsync.folder_for(shooting_id)
        if folder is not None and folder.is_dir():
            return folder
        cached = selection_folder(shooting_id, title)
        if cached.is_dir():
            return cached
        saved = self._shotsync_folder_map().get(str(int(shooting_id)))
        if saved and Path(saved).is_dir():
            return Path(saved)
        return None

    def _refresh_shotsync_local_folders(self, shootings: list) -> None:
        local_ids = {
            int(shooting.get("id") or 0)
            for shooting in shootings
            if isinstance(shooting, dict)
            and self._local_shotsync_folder(
                int(shooting.get("id") or 0), str(shooting.get("title") or "")
            ) is not None
        }
        self.shotsync_panel.set_local_ids(local_ids)
        self.shotsync_panel.set_shooting_modes(self._shotsync_folder_modes())
        self._refresh_shotsync_current_shooting()

    def _refresh_shotsync_current_shooting(self) -> None:
        """Bold the card whose linked folder is open in this workspace."""
        shooting_id: int | None = None
        if self.folder_cache is not None and self.cache_ready:
            session = self.folder_cache.shotsync_session()
            if session:
                shooting_id = session[0]
        if shooting_id is None:
            for raw_id, raw_folder in self._shotsync_folder_map().items():
                if Path(raw_folder) == self.current_dir:
                    try:
                        shooting_id = int(raw_id)
                    except (TypeError, ValueError):
                        pass
                    break
        self.shotsync_panel.set_current_shooting_id(shooting_id)

    def _shotsync_shooting_activated(self, shooting: dict) -> None:
        """Open a known folder, otherwise show the two cloud-only actions."""
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id:
            return
        title = str(shooting.get("title") or "Съёмка ShotSync")
        folder = self._local_shotsync_folder(shooting_id, title)
        if folder is not None:
            self.load_directory(folder)
            return
        self._displayed_shotsync_shooting = shooting
        self.shotsync_action_title.setText(title)
        self.stack.setCurrentWidget(self.grid_page)
        self.grid_content_stack.setCurrentWidget(self.shotsync_action_page)

    def _take_displayed_shotsync_shooting(self) -> None:
        if self._displayed_shotsync_shooting:
            self._shotsync_select_requested(self._displayed_shotsync_shooting)

    def _watch_displayed_shotsync_shooting(self) -> None:
        if self._displayed_shotsync_shooting:
            self._shotsync_receive_requested(self._displayed_shotsync_shooting)

    def _shotsync_get_marks_for_requested(self, shooting: dict) -> None:
        shooting_id = int(shooting.get("id") or 0)
        folder = self._local_shotsync_folder(shooting_id, str(shooting.get("title") or ""))
        if not shooting_id or folder is None:
            return
        self._pending_shotsync_marks_for = shooting_id
        if folder != self.current_dir:
            self.load_directory(folder)
            return
        self._fetch_pending_shotsync_marks()

    def _fetch_pending_shotsync_marks(self) -> None:
        shooting_id = self._pending_shotsync_marks_for
        if shooting_id is None or self.folder_cache is None or not self.cache_ready:
            return
        session = self.folder_cache.shotsync_session()
        self._pending_shotsync_marks_for = None
        if not session or session[0] != shooting_id:
            return
        self._shotsync_marks_fetching = True
        self._refresh_status_panel()
        self.shotsync.marks_fetcher.fetch(shooting_id, self.folder_cache)

    def _shotsync_remove_local_requested(self, shooting: dict) -> None:
        """Delete a selection copy from disk without touching the server shooting."""
        shooting_id = int(shooting.get("id") or 0)
        title = str(shooting.get("title") or "Съёмка ShotSync")
        folder = self._local_shotsync_folder(shooting_id, title)
        if not shooting_id or folder is None:
            return
        resolved = folder.resolve()
        # Never allow a malformed saved setting to turn this into a root delete.
        if resolved.parent == resolved:
            QMessageBox.warning(self, "ShotSync", "Нельзя удалить корневую папку диска.")
            return
        if not self._confirm_shotsync_action(
            "Удалить локальную копию",
            f"Удалить с компьютера папку «{folder.name}»?\n\n"
            "Съёмка и метки на ShotSync останутся доступны. Удалятся только локальные файлы.",
            "Удалить",
        ):
            return
        if folder == self.current_dir:
            fallback = folder.parent
            self.load_directory(fallback)
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            QMessageBox.warning(self, "ShotSync", f"Не удалось удалить локальную папку:\n{exc}")
            return
        self._forget_shotsync_folder(shooting_id)
        self._forget_shotsync_selection(shooting_id)
        self._refresh_shotsync_local_folders(self._shotsync_shootings)

    def _shotsync_refresh_requested(self) -> None:
        if not self.shotsync_client.has_key():
            return
        self.shotsync_panel.set_shootings_loading()
        self.shotsync_client.fetch_shootings()

    def _resume_shotsync_selection_copies(self, shootings: list[dict]) -> None:
        modes = self._shotsync_folder_modes()
        for shooting in shootings:
            shooting_id = int(shooting.get("id") or 0)
            if modes.get(shooting_id) != "selection_copy":
                continue
            if self.shotsync.downloader.is_running(shooting_id):
                continue
            folder = self._local_shotsync_folder(shooting_id, str(shooting.get("title") or ""))
            if folder is None:
                continue
            self._resuming_shotsync_selections.add(shooting_id)
            self.shotsync.downloader.start(shooting_id, str(shooting.get("title") or ""))

    def _shotsync_delete_server_requested(self, shooting: dict) -> None:
        """Delete only an uploaded server shooting; keep the source folder."""
        shooting_id = int(shooting.get("id") or 0)
        folder = self._local_shotsync_folder(shooting_id, str(shooting.get("title") or ""))
        if not shooting_id or folder is None:
            return
        if not self._confirm_shotsync_action(
            "Удалить съёмку с сервера",
            "Удалить съёмку из ShotSync? Исходная папка с фотографиями останется на компьютере.",
            "Удалить",
        ):
            return
        self._deleting_shotsync_folders[shooting_id] = folder
        self.shotsync.uploader.delete_shooting(shooting_id)

    def _confirm_shotsync_action(self, title: str, text: str, accept_label: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Yes).setText(accept_label)
        dialog.button(QMessageBox.StandardButton.Cancel).setText("Отмена")
        return dialog.exec() == QMessageBox.StandardButton.Yes

    def _on_shotsync_server_deleted(self, shooting_id: int) -> None:
        folder = self._deleting_shotsync_folders.pop(int(shooting_id), None)
        if folder is None:
            return
        try:
            if folder == self.current_dir and self.folder_cache is not None and self.cache_ready:
                self.folder_cache.clear_shotsync_session()
                self._detach_shotsync_syncer()
            else:
                names = {path.name for path in folder.iterdir() if path.is_file()}
                cache = FolderCache(folder, live_names=names, load_from_disk=True)
                cache.clear_shotsync_session()
                cache.close(flush=True)
        except OSError:
            pass
        self._forget_shotsync_folder(shooting_id)
        self._forget_shotsync_selection(shooting_id)
        self._refresh_shotsync_local_folders(self._shotsync_shootings)
        self._refresh_shotsync_tab_indicator()
        if self.shotsync_client.has_key():
            self.shotsync_client.fetch_shootings()

    def _on_shotsync_server_delete_failed(self, message: str) -> None:
        if not self._deleting_shotsync_folders:
            return
        self._deleting_shotsync_folders.clear()
        QMessageBox.warning(self, "ShotSync", message)

    def _shotsync_receive_requested(self, shooting: dict) -> None:
        """Toggle live receiving for a shooting, choosing a target folder."""
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id:
            return
        if self.shotsync.is_receiving(shooting_id):
            self.shotsync.stop_receiving(shooting_id)
            # Stopping live receive deliberately returns the shooting to the
            # server-only state. Existing downloaded files stay on disk, but
            # this folder is no longer treated as a ShotSync target.
            self._forget_shotsync_folder(shooting_id)
            self._refresh_shotsync_local_folders(self._shotsync_shootings)
            self._refresh_shotsync_tab_indicator()
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
        self._remember_shotsync_folder(shooting_id, folder)
        self._refresh_shotsync_local_folders(self._shotsync_shootings)
        self.load_directory(folder)

    def _on_shotsync_photo_downloaded(self, shooting_id: int, folder: str, filename: str) -> None:
        """A new original landed on disk; refresh the tab showing that folder."""
        if Path(folder) == self.current_dir:
            self.load_directory(self.current_dir)

    def _on_shotsync_receive_progress(self, shooting_id: int, done: int, total: int, retrying: int) -> None:
        self._receive_progress = (done, total, retrying) if total else None
        self._refresh_status_panel()

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

    def _on_shotsync_photo_updated(self, shooting_id: int, photo: dict) -> None:
        """Apply live server marks to the open ShotSync selection folder."""
        if self.folder_cache is None or not self.cache_ready:
            return
        session = self.folder_cache.shotsync_session()
        if not session or session[0] != int(shooting_id):
            return
        photo_id = int(photo.get("id") or 0)
        name = (
            self.folder_cache.shotsync_local_name_for_photo_id(photo_id)
            if photo_id else None
        )
        if not name:
            name = _shotsync_photo_filename(photo)
        if not name:
            return
        self._apply_external_selection(
            name,
            rating=photo.get("rating"),
            color_label=photo.get("color_label") or "",
            comment=photo.get("comment") or "",
        )

    def _on_shotsync_shooting_deleted(self, shooting_id: int) -> None:
        """Remove a server-downloaded selection copy when its shooting is deleted."""
        self._remove_shotsync_selection_copy(int(shooting_id))

    def _reconcile_shotsync_selection_copies(self, shootings: list[dict]) -> None:
        """Clean selection copies that were deleted while this app was offline."""
        server_ids = {int(shooting.get("id") or 0) for shooting in shootings}
        for shooting_id, mode in self._shotsync_folder_modes().items():
            if mode == "selection_copy" and shooting_id not in server_ids:
                self._remove_shotsync_selection_copy(shooting_id)

    def _remove_shotsync_selection_copy(self, shooting_id: int) -> None:
        if self._shotsync_folder_modes().get(int(shooting_id)) != "selection_copy":
            return
        folder = self._local_shotsync_folder(shooting_id)
        if folder is None:
            return
        if folder == self.current_dir:
            self.load_directory(folder.parent)
        try:
            shutil.rmtree(folder)
        except OSError:
            return
        self._forget_shotsync_folder(shooting_id)
        self._forget_shotsync_selection(shooting_id)
        self._refresh_shotsync_local_folders(self._shotsync_shootings)

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
        if self._xmp_auto_enabled():
            path = next((candidate for candidate in self.all_paths if candidate.name == name), None)
            if path is not None:
                self._queue_xmp(path)
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
            return
        title = shooting.get("title") or "Съёмка ShotSync"
        self._requested_shotsync_selections.add(shooting_id)
        self._requested_shotsync_folders[shooting_id] = selection_folder(shooting_id, str(title))
        self.stack.setCurrentWidget(self.grid_page)
        self.grid_content_stack.setCurrentWidget(self.shotsync_upload_page)
        self.shotsync_upload_title.setText("Получаем фотографии с сервера…")
        self.shotsync_upload_status.setText("Подготавливаем загрузку…")
        self._selection_progress = (0, 0)
        self._refresh_status_panel()
        self.shotsync.downloader.start(shooting_id, title)

    def _on_shotsync_selection_progress(self, shooting_id: int, done: int, total: int) -> None:
        if shooting_id not in self._requested_shotsync_selections and shooting_id not in self._resuming_shotsync_selections:
            return
        if shooting_id in self._resuming_shotsync_selections:
            return
        self._selection_progress = (done, total) if total else None
        self.shotsync_upload_status.setText(
            f"Получено фотографий: {done} из {total}" if total else "Загружаем фотографии…"
        )
        self._refresh_status_panel()

    def _on_shotsync_selection_ready(self, shooting_id: int, folder: str) -> None:
        is_requested = shooting_id in self._requested_shotsync_selections
        is_resuming = shooting_id in self._resuming_shotsync_selections
        if not is_requested and not is_resuming:
            return
        self._resuming_shotsync_selections.discard(shooting_id)
        if is_resuming:
            self._remember_shotsync_folder(shooting_id, Path(folder))
            self._remember_shotsync_mode(shooting_id, "selection_copy")
            return
        self._requested_shotsync_selections.discard(shooting_id)
        target_folder = self._requested_shotsync_folders.pop(shooting_id, Path(folder))
        emitted_folder = Path(folder)
        if emitted_folder.is_dir() and emitted_folder.name != selection_root().name:
            target_folder = emitted_folder
        elif emitted_folder == selection_root() and emitted_folder.is_dir():
            candidates = sorted(
                (path for path in emitted_folder.iterdir()
                 if path.is_dir() and path.name.split("-", 1)[0] == str(shooting_id)),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if candidates:
                target_folder = candidates[0]
        target_folder.mkdir(parents=True, exist_ok=True)
        self._selection_progress = None
        self.folder_change_timer.stop()
        watched = self.folder_watcher.directories()
        if watched:
            self.folder_watcher.removePaths(watched)
        self._refresh_status_panel()
        self.grid_content_stack.setCurrentWidget(self.grid)
        self._remember_shotsync_folder(shooting_id, target_folder)
        self._remember_shotsync_mode(shooting_id, "selection_copy")
        self._refresh_shotsync_local_folders(self._shotsync_shootings)
        self._refresh_shotsync_tab_indicator()
        self.stack.setCurrentWidget(self.grid_page)
        self.grid_content_stack.setCurrentWidget(self.grid)
        def open_selection_folder() -> None:
            if not target_folder.is_dir():
                return
            self._set_tree_root_for_path(target_folder)
            tree_index = self.dir_model.index(str(target_folder))
            if tree_index.isValid():
                self.dir_tree.setCurrentIndex(tree_index)
            self.load_directory(target_folder)

        QTimer.singleShot(0, open_selection_folder)

    def _on_shotsync_selection_failed(self, shooting_id: int, message: str) -> None:
        if shooting_id in self._resuming_shotsync_selections:
            self._resuming_shotsync_selections.discard(shooting_id)
            return
        if shooting_id not in self._requested_shotsync_selections:
            return
        self._requested_shotsync_selections.discard(shooting_id)
        self._requested_shotsync_folders.pop(shooting_id, None)
        self._selection_progress = None
        self._refresh_status_panel()
        self.grid_content_stack.setCurrentWidget(self.grid)
        QMessageBox.warning(self, "ShotSync", f"Не удалось загрузить съёмку:\n{message}")

    def _attach_shotsync_syncer(self) -> None:
        """If the open folder is a ShotSync selection, start syncing its marks."""
        self._detach_shotsync_syncer()
        self._refresh_shotsync_shortcuts()
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
        self._on_shotsync_pending_changed(self._shotsync_syncer.pending_count())

    def _detach_shotsync_syncer(self) -> None:
        if getattr(self, "_shotsync_syncer", None) is not None:
            self._shotsync_syncer.detach()
            self._shotsync_syncer.deleteLater()
            self._shotsync_syncer = None
        self._on_shotsync_pending_changed(0)

    def _on_shotsync_pending_changed(self, count: int) -> None:
        self._shotsync_pending_marks = max(0, int(count))
        self._refresh_status_panel()

    # ----- send folder to server + get marks (feature 3) -----------------
    def _shotsync_send_current_folder(self) -> None:
        """Choose a source folder and create its ShotSync shooting."""
        if self.current_dir is None:
            return
        if not self.shotsync_client.has_key():
            QMessageBox.information(
                self, "ShotSync", "Сначала войдите в ShotSync на боковой панели."
            )
            return
        if self.shotsync.uploader.busy:
            return
        self._show_shotsync_upload_popup()

    def _show_shotsync_upload_popup(self) -> None:
        dialog = QDialog(self)
        self._shotsync_upload_popup = dialog
        dialog.setObjectName("shotsyncUploadPopup")
        dialog.setWindowTitle("Отправить на ShotSync")
        # A real Qt.Popup closes itself when the native folder chooser gains
        # focus. Use a tool window with the same compact visual treatment so
        # the selected path can return to this form reliably.
        dialog.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title_label = QLabel("Новая съёмка")
        title_label.setObjectName("shotsyncUploadPopupTitle")
        layout.addWidget(title_label)
        hint = QLabel("Укажите название и папку с фотографиями для отправки на отбор.")
        hint.setObjectName("shotsyncUploadPopupHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        name = QLineEdit()
        name.setObjectName("shotsyncUploadPopupField")
        name.setPlaceholderText("Название съёмки")
        layout.addWidget(name)

        path_row = QHBoxLayout()
        path_edit = QLineEdit()
        path_edit.setObjectName("shotsyncUploadPopupField")
        path_edit.setPlaceholderText("Папка не выбрана")
        path_edit.setReadOnly(True)
        browse = QPushButton("Выбрать папку…")
        browse.setObjectName("shotsyncUploadPopupBrowse")
        path_row.addWidget(path_edit, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        ai = QCheckBox("AI: лица и серии")
        ai.setObjectName("shotsyncUploadPopupCheck")
        layout.addWidget(ai)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("shotsyncUploadPopupCancel")
        send = QPushButton("Отправить")
        send.setObjectName("shotsyncUploadPopupSend")
        send.setEnabled(False)
        buttons.addWidget(cancel)
        buttons.addWidget(send)
        layout.addLayout(buttons)

        selected: list[Path | None] = [None]
        def choose_folder() -> None:
            folder = QFileDialog.getExistingDirectory(dialog, "Папка со съёмкой", str(self.current_dir))
            if not folder:
                return
            selected[0] = Path(folder)
            path_edit.setText(str(selected[0]))
            send.setEnabled(selected[0].is_dir())
            if not name.text().strip():
                name.setText(selected[0].name)

        browse.clicked.connect(choose_folder)
        cancel.clicked.connect(dialog.close)
        def submit() -> None:
            folder = selected[0]
            if folder is None or not folder.is_dir():
                return
            shooting_title = name.text().strip()
            if not shooting_title:
                shooting_title = folder.name or "Новая съёмка"
                name.setText(shooting_title)
            dialog.close()
            self._start_shotsync_upload(folder, shooting_title, ai.isChecked())

        send.clicked.connect(submit)
        dialog.finished.connect(lambda: setattr(self, "_shotsync_upload_popup", None))
        dialog.show()

    def _start_shotsync_upload(self, folder: Path, title: str, ai_faces_series: bool) -> None:
        self.stack.setCurrentWidget(self.grid_page)
        self.grid_content_stack.setCurrentWidget(self.shotsync_upload_page)
        self.shotsync_upload_status.setText("Подготавливаем фотографии…")
        self._upload_progress = (0, 0)
        self._refresh_status_panel()
        original_datetimes = {
            path.name: self.photo_details.get(path.name, {}).get("original_datetime")
            for path in self.all_paths
            if path.is_file() and folder == self.current_dir
        }
        self.shotsync.uploader.start(folder, title, original_datetimes, ai_faces_series)

    def _shotsync_upload_dialog(self) -> tuple[Path, str, bool] | None:
        """Collect all creation settings instead of silently using this tab's folder."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Отправить на отбор")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Название съёмки"))
        name = QLineEdit(self.current_dir.name or "Новая съёмка")
        layout.addWidget(name)
        layout.addWidget(QLabel("Папка с фотографиями"))
        folders = QComboBox()
        used = {Path(value) for value in self._shotsync_folder_map().values()}
        candidates = [self.current_dir]
        try:
            candidates.extend(path for path in self.current_dir.iterdir() if path.is_dir() and path not in used)
        except OSError:
            pass
        for path in candidates:
            folders.addItem(str(path), path)
        layout.addWidget(folders)
        browse = QPushButton("Выбрать папку на диске…")
        layout.addWidget(browse)
        selected_external: list[Path | None] = [None]
        def choose_folder() -> None:
            chosen = QFileDialog.getExistingDirectory(dialog, "Папка со съёмкой", str(self.current_dir))
            if chosen:
                selected_external[0] = Path(chosen)
                folders.setCurrentText(chosen)
        browse.clicked.connect(choose_folder)
        ai = QCheckBox("AI: лица и серии")
        ai.setToolTip("Включить обработку лиц и серий на сервере ShotSync")
        layout.addWidget(ai)
        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        create = QPushButton("Отправить")
        cancel.clicked.connect(dialog.reject)
        create.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        folder = selected_external[0] or folders.currentData()
        if not isinstance(folder, Path) or not folder.is_dir():
            QMessageBox.warning(self, "ShotSync", "Выберите существующую папку с фотографиями.")
            return None
        title = name.text().strip() or folder.name or "Новая съёмка"
        return folder, title, ai.isChecked()

    def _on_shotsync_upload_progress(self, done: int, total: int) -> None:
        self._upload_progress = (done, total)
        self.shotsync_upload_status.setText(
            f"Загружено фотографий: {done} из {total}" if total else "Подготавливаем фотографии…"
        )
        self._refresh_status_panel()

    def _on_shotsync_upload_finished(self, shooting_id: int, folder: str) -> None:
        self._upload_progress = None
        self._refresh_status_panel()
        self._remember_shotsync_folder(shooting_id, Path(folder))
        self._remember_shotsync_mode(shooting_id, "uploaded")
        self._refresh_shotsync_local_folders(self._shotsync_shootings)
        self.shotsync_panel.set_current_shooting_id(shooting_id)
        self._refresh_shotsync_tab_indicator()
        self.grid_content_stack.setCurrentWidget(self.grid)
        if self.shotsync_client.has_key():
            self.shotsync_client.fetch_shootings()
        # The folder is now a ShotSync session; re-attach so marks sync live.
        if Path(folder) == self.current_dir:
            self._attach_shotsync_syncer()
            self._refresh_shotsync_shortcuts()

    def _on_shotsync_upload_failed(self, message: str) -> None:
        self._upload_progress = None
        self.grid_content_stack.setCurrentWidget(self.grid)
        self._refresh_status_panel()
        QMessageBox.warning(self, "ShotSync", f"Не удалось отправить съёмку:\n{message}")

    def _shotsync_fetch_marks(self) -> None:
        """Pull marks for the current ShotSync folder (the "Получить" action)."""
        if self.folder_cache is None or not self.cache_ready:
            return
        session = self.folder_cache.shotsync_session()
        if not session:
            QMessageBox.information(
                self, "ShotSync", "Эта папка не связана со съёмкой ShotSync."
            )
            return
        shooting_id, _title = session
        self._shotsync_marks_fetching = True
        self._refresh_status_panel()
        self.shotsync.marks_fetcher.fetch(shooting_id, self.folder_cache)

    def _on_shotsync_marks_fetched(self, applied: int) -> None:
        del applied
        self._shotsync_marks_fetching = False
        self._refresh_status_panel()
        self._xmp_export_after_cache_load = self._xmp_auto_enabled()
        # Repaint the grid/details from the freshly written cache.
        if self.current_dir is not None:
            self.load_directory(self.current_dir)

    def _on_shotsync_marks_failed(self, message: str) -> None:
        self._shotsync_marks_fetching = False
        self._refresh_status_panel()
        QMessageBox.warning(self, "ShotSync", f"Не удалось получить метки:\n{message}")

    def _refresh_shotsync_shortcuts(self) -> None:
        """Enable/disable the ShotSync folder actions for the current folder."""
        is_session = False
        if self.folder_cache is not None and self.cache_ready:
            is_session = self.folder_cache.shotsync_session() is not None
        can_send = (
            self.current_dir is not None and self.shotsync_client.has_key()
        )
        self.shotsync_panel.set_folder_actions(can_send=can_send, is_session=is_session)

    def _refresh_shotsync_tab_indicator(self) -> None:
        """Tell the tab host whether the open folder belongs to ShotSync."""
        linked = any(
            self.shotsync.folder_for(shooting_id) == self.current_dir
            for shooting_id in self.shotsync.receiving_ids()
        )
        linked = linked or any(
            Path(folder) == self.current_dir
            for folder in self._shotsync_folder_map().values()
        )
        if self.folder_cache is not None and self.cache_ready:
            linked = linked or self.folder_cache.shotsync_session() is not None
        self.shotsyncFolderChanged.emit(linked)

    def load_directory(self, directory: Path) -> None:
        switching_directory = directory != self.current_dir
        if hasattr(self, "grid_content_stack"):
            self.grid_content_stack.setCurrentWidget(self.grid)
        if self._folder_context_active:
            self._save_folder_grid_context()
        self._folder_context_active = False
        watched = self.folder_watcher.directories()
        if watched:
            self.folder_watcher.removePaths(watched)
        if directory.is_dir():
            self.folder_watcher.addPath(str(directory))
        self.scheduler.cancel_pending()
        self.full_request_timer.stop()
        self.grid_full_request_timer.stop()
        self.pending_full_request = None
        self.pending_grid_full_request = None
        if switching_directory:
            self._abandon_preview_decode_work()
        self.video_thumbnailer.cancel()
        self.decode_cache.clear()
        self.populate_timer.stop()
        self.thumb_timer.stop()
        self.ai_progress_timer.stop()
        self.ai_progress_total = 0
        self.preview_progress_total = 0
        if hasattr(self, "ai_button"):
            # The toolbar button opens an explanatory menu, so it must remain
            # available while the folder cache is loading as well.
            self.ai_button.setEnabled(True)
            self._refresh_status_panel()
        self.cache_load_generation += 1
        self.directory_generation += 1
        self._flush_folder_cache(wait=False, close=True)
        self._detach_shotsync_syncer()
        self.folder_cache = None
        self.cache_ready = False
        self.current_dir = directory
        self._remember_directory_for_volume(directory)
        self._refresh_shotsync_tab_indicator()
        self._refresh_shotsync_current_shooting()
        self._restore_series_mode(directory)
        # A folder change starts navigation from the beginning.  Do not carry
        # the remembered grid cursor/scroll position into the newly opened
        # folder.
        self._pending_folder_grid_context = None if switching_directory else self._load_folder_grid_context(directory)
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
            self._reset_grid_cursor()
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

    def _reset_grid_cursor(self) -> None:
        """Start grid navigation at the first item in the current folder."""
        if self.grid.count() == 0:
            return
        self.grid.setCurrentRow(0)
        self.grid.scrollToTop()

    def _restore_series_mode(self, directory: Path) -> None:
        enabled = self.settings.value(self._series_mode_setting_key(directory), True, bool)
        self.series_toggle.blockSignals(True)
        self.series_toggle.setChecked(enabled)
        self.series_toggle.blockSignals(False)

    def _series_toggle_changed(self, enabled: bool) -> None:
        self.settings.setValue(self._series_mode_setting_key(self.current_dir), enabled)
        self._apply_view()
        self._show_viewer_toast(
            "Группировка по сериям включена" if enabled else "Группировка по сериям выключена"
        )

    def _show_viewer_toast(self, message: str) -> None:
        """Show a brief confirmation over the viewer page."""
        parent = self.centralWidget() or self
        previous = getattr(self, "_viewer_toast", None)
        if previous is not None:
            previous.deleteLater()

        toast = QLabel(message, parent)
        toast.setObjectName("viewerToast")
        toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        toast.adjustSize()
        toast.move(
            max(8, (parent.width() - toast.width()) // 2),
            max(8, parent.height() // 2 - toast.height() // 2),
        )
        toast.raise_()
        toast.show()
        self._viewer_toast = toast

        timer = QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(toast.deleteLater)
        timer.start(1_800)

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
            item = self._grid_item_for_path(path)
            self.grid.addItem(item)
            self.items_by_path[path] = item
        self.populate_index = end
        self._schedule_visible_thumb_priority()
        self._refresh_status_panel()
        if self.populate_index >= len(self.paths):
            self.populate_timer.stop()
            if self.cache_ready:
                QTimer.singleShot(0, self._restore_folder_grid_context)

    def _grid_item_for_path(self, path: Path) -> QListWidgetItem:
        """Create one card while preserving any thumbnail already in RAM."""
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
        return item

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
        self.ai_analysis_available = False
        self.ai_pipeline.scan(analysis_paths, self.folder_cache, self._background_decode_executor())
        self.ai_progress_timer.start()
        self._refresh_status_panel()

    def _show_ai_menu(self) -> None:
        """Show the entry point for the AI analysis already available in the workspace."""
        menu = QMenu(self.ai_button)
        menu.setObjectName("toolbarPopup")
        content = QWidget(menu)
        content.setObjectName("toolbarPopupContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        title = QLabel("AI: серии и лица")
        title.setObjectName("toolbarPopupTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Находит похожие кадры и лица среди фотографий, "
            "которые ещё не были обработаны."
        )
        hint.setObjectName("toolbarPopupHint")
        hint.setWordWrap(True)
        hint.setFixedWidth(270)
        layout.addWidget(hint)
        if self.ai_analysis_available:
            start = QPushButton("Обработать серии и лица")
            start.setObjectName("toolbarPopupPrimaryButton")
            start.setIcon(_fomantic_icon("magic", 16, "#ffffff"))
            start.setIconSize(QSize(16, 16))
            start.clicked.connect(lambda: (menu.close(), self._start_ai_analysis()))
            layout.addWidget(start)
        elif self.ai_pipeline.pending_count() == 0:
            complete = QLabel("В текущей папке все фото уже обработаны.")
            complete.setObjectName("toolbarPopupHint")
            complete.setWordWrap(True)
            layout.addWidget(complete)

        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        menu.exec(self.ai_button.mapToGlobal(QPoint(0, self.ai_button.height())))

    def _xmp_auto_enabled(self) -> bool:
        return self.settings.value("xmp/auto_export", False, bool)

    def _xmp_replacements(self, detail: dict | None = None) -> dict[str, str]:
        replacements = {
            str(code.get("code") or ""): str(code.get("value") or "")
            for group in self.code_replacement_sets if isinstance(group, dict)
            for code in group.get("codes", []) if isinstance(code, dict) and code.get("code")
        }
        detail = detail or {}
        try:
            captured = datetime.fromisoformat(str(detail.get("original_datetime") or ""))
        except ValueError:
            captured = None
        if captured is not None:
            replacements.update(date=captured.strftime("%d.%m.%Y"), time=captured.strftime("%H:%M"), datetime=captured.strftime("%d.%m.%Y %H:%M"))
        camera = detail.get("camera") or {}
        if isinstance(camera, dict) and camera.get("model"):
            replacements["camera"] = str(camera["model"])
        capture = detail.get("capture_settings") or {}
        if isinstance(capture, dict):
            if capture.get("iso"):
                replacements["iso"] = str(round(float(capture["iso"])))
            if capture.get("aperture"):
                replacements["aperture"] = f"f/{float(capture['aperture']):.1f}"
            if capture.get("exposure_display"):
                replacements["shutter"] = str(capture["exposure_display"])
            if capture.get("focal_length_mm"):
                replacements["focal"] = f"{round(float(capture['focal_length_mm']))}mm"
        return replacements

    def _show_xmp_menu(self) -> None:
        menu = QMenu(self.xmp_button)
        menu.setObjectName("toolbarPopup")
        content = QWidget(menu)
        content.setObjectName("toolbarPopupContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        title = QLabel("XMP")
        title.setObjectName("toolbarPopupTitle")
        layout.addWidget(title)
        auto = SettingsCheckBox("Автоматически создавать XMP")
        auto.setChecked(self._xmp_auto_enabled())
        layout.addWidget(auto)
        create = QPushButton("Создать XMP файлы")
        create.setObjectName("toolbarPopupPrimaryButton")
        create.setIcon(_fomantic_icon("file", 16, "#ffffff"))
        create.setIconSize(QSize(16, 16))
        create.setEnabled(not auto.isChecked() and self.cache_ready)
        layout.addWidget(create)

        def set_auto(enabled: bool) -> None:
            self.settings.setValue("xmp/auto_export", enabled)
            create.setEnabled(not enabled and self.cache_ready)
            if enabled:
                self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))

        auto.toggled.connect(set_auto)
        create.clicked.connect(lambda: (menu.close(), self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))))
        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        menu.exec(self.xmp_button.mapToGlobal(QPoint(0, self.xmp_button.height())))

    def _queue_xmp_paths(self, paths) -> None:
        for path in paths:
            self._queue_xmp(path)

    def _queue_xmp(self, path: Path) -> None:
        if self.closing or not path.is_file() or not is_supported_image(path):
            return
        detail = dict(self.photo_details.get(path.name, {}))
        self._xmp_pending[path] = (detail, list(self.face_sets), self._xmp_replacements(detail))
        if path not in self._xmp_running:
            self._start_xmp_write(path)

    def _start_xmp_write(self, path: Path) -> None:
        payload = self._xmp_pending.pop(path, None)
        if payload is None or self.closing:
            return
        self._xmp_running.add(path)
        # QFileSystemWatcher reports the directory change caused by our own
        # sidecar write. The photo list is unchanged, so reloading it only
        # disrupts selection and scrolling. Leave external changes alone once
        # this small write window has elapsed.
        self._ignore_folder_changes_until = max(self._ignore_folder_changes_until, monotonic() + 2.0)
        future = self.xmp_executor.submit(_write_xmp_task, path, *payload)
        future.add_done_callback(lambda done, target=path: self.bridge.xmpWritten.emit((target, done)))

    def _on_xmp_written(self, result: object) -> None:
        path, future = result
        self._xmp_running.discard(path)
        if self.closing:
            return
        try:
            future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(path), f"XMP: {exc}")
        if path in self._xmp_pending:
            self._start_xmp_write(path)

    def _show_utilities_menu(self) -> None:
        """Show the reserved place for batch tools without implying availability."""
        menu = QMenu(self.utilities_button)
        menu.setObjectName("toolbarPopup")
        content = QWidget(menu)
        content.setObjectName("toolbarPopupContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        title = QLabel("Утилиты")
        title.setObjectName("toolbarPopupTitle")
        layout.addWidget(title)
        hint = QLabel("Инструменты для пакетной работы с файлами.")
        hint.setObjectName("toolbarPopupHint")
        hint.setWordWrap(True)
        hint.setFixedWidth(270)
        layout.addWidget(hint)
        for label, icon, callback in (
            ("Групповое переименование", "edit", self._show_batch_rename_dialog),
            ("Групповой резайс", "expand", self._show_batch_resize_dialog),
            ("Уменьшить JPG", "download", self._show_shrink_jpeg_dialog),
        ):
            button = QPushButton(label)
            button.setObjectName("toolbarPopupUtilityButton")
            button.setIcon(_fomantic_icon(icon, 16, "#ededed"))
            button.setIconSize(QSize(16, 16))
            if callback is None:
                button.setEnabled(False)
                button.setToolTip("Пока не реализовано")
            else:
                # Let QMenu complete its close cycle before a modal dialog is
                # created; otherwise Qt can warn that its transient window is
                # not a top-level window.
                button.clicked.connect(lambda _checked=False, target=callback: (menu.close(), QTimer.singleShot(0, target)))
            layout.addWidget(button)

        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        menu.exec(self.utilities_button.mapToGlobal(QPoint(0, self.utilities_button.height())))

    def _show_batch_rename_dialog(self) -> None:
        if self._is_shotsync_rename_blocked():
            QMessageBox.information(
                self,
                "Групповое переименование",
                "Переименование недоступно для папок, связанных со съёмками ShotSync.",
            )
            return
        if not self.cache_ready:
            QMessageBox.information(self, "Групповое переименование", "Подождите, пока загрузится папка.")
            return
        paths = [path for path in self.view_paths if path.is_file() and is_supported_image(path)]
        if not paths:
            QMessageBox.information(self, "Групповое переименование", "В текущем списке нет фотографий.")
            return
        dialog = BatchRenameDialog(paths, self.photo_details, self.settings, self)
        dialog.renameRequested.connect(lambda names, view=dialog: self._rename_from_dialog(view, names))
        dialog.exec()

    def _rename_from_dialog(self, dialog: BatchRenameDialog, names: dict[str, str]) -> None:
        changes = sum(old != new for old, new in names.items())
        if not changes:
            return
        dialog.set_renaming(changes * 2)
        try:
            self._rename_files_safely(names, dialog.update_rename_progress)
            if self.folder_cache is not None:
                self.folder_cache.rename_photo_names(names)
        except OSError as exc:
            dialog.rename_failed(f"Не удалось переименовать файлы: {exc}")
            return
        except Exception as exc:
            QMessageBox.warning(dialog, "Групповое переименование", f"Файлы переименованы, но кэш не обновлён:\n{exc}")
            self.folder_change_timer.stop()
            self.load_directory(self.current_dir)
            dialog.accept()
            return
        self.folder_change_timer.stop()
        self.load_directory(self.current_dir)
        dialog.accept()

    def _is_shotsync_rename_blocked(self) -> bool:
        if self.folder_cache is not None and self.cache_ready and self.folder_cache.shotsync_session() is not None:
            return True
        if any(Path(folder) == self.current_dir for folder in self._shotsync_folder_map().values()):
            return True
        return any(self.shotsync.folder_for(shooting_id) == self.current_dir for shooting_id in self.shotsync.receiving_ids())

    def _folder_jpeg_paths(self) -> list[Path]:
        """Every JPEG living directly in the current folder, sorted by name."""
        suffixes = {".jpg", ".jpeg"}
        return sorted(
            (
                path
                for path in self.current_dir.iterdir()
                if path.is_file() and path.suffix.lower() in suffixes
            ),
            key=lambda item: item.name.casefold(),
        )

    def _show_shrink_jpeg_dialog(self) -> None:
        try:
            paths = self._folder_jpeg_paths()
        except OSError as exc:
            QMessageBox.information(self, "Уменьшить JPG", f"Не удалось прочитать папку: {exc}")
            return
        if not paths:
            QMessageBox.information(self, "Уменьшить JPG", "В текущей папке нет JPG-файлов.")
            return
        dialog = ShrinkJpegDialog(self.current_dir, len(paths), self.settings, self)
        dialog.startRequested.connect(lambda options, view=dialog, items=paths: self._start_shrink_jpeg(view, items, options))
        dialog.exec()

    def _start_shrink_jpeg(self, dialog: ShrinkJpegDialog, paths: list[Path], options: dict) -> None:
        dialog.set_running(len(paths))
        jobs = [(str(path), int(options["quality"]), bool(options["keep_exif"])) for path in paths]
        errors: list[str] = []
        saved_bytes = 0
        workers = min(8, max(1, os.cpu_count() or 1), len(jobs))
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_recompress_jpeg_worker, job) for job in jobs]
            for completed, future in enumerate(as_completed(futures), 1):
                source, original_size, new_size, error = future.result()
                if error:
                    errors.append(f"{Path(source).name}: {error}")
                else:
                    saved_bytes += max(0, original_size - new_size)
                dialog.update_progress(completed, len(jobs))
        self.folder_change_timer.stop()
        self.load_directory(self.current_dir)
        saved_mb = saved_bytes / (1024 * 1024)
        summary = f"Готово. Сэкономлено {saved_mb:.1f} МБ."
        if errors:
            summary = f"Готово с ошибками ({len(errors)}). Сэкономлено {saved_mb:.1f} МБ. {errors[0]}"
        dialog.status.setText(summary)
        dialog.cancel_button.setEnabled(True)
        dialog.cancel_button.setText("Закрыть")

    def _show_batch_resize_dialog(self) -> None:
        paths = [path for path in self.view_paths if path.is_file() and is_supported_image(path)]
        if not paths:
            QMessageBox.information(self, "Групповой резайс", "В текущем списке нет фотографий.")
            return
        dialog = BatchResizeDialog(self.current_dir, self.settings, self)
        dialog.startRequested.connect(lambda options, view=dialog, items=paths: self._start_batch_resize(view, items, options))
        dialog.exec()

    def _start_batch_resize(self, dialog: BatchResizeDialog, paths: list[Path], options: dict) -> None:
        output_dir = Path(options["output_dir"])
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            dialog.status.setText(f"Не удалось создать папку: {exc}")
            return
        targets = self._resolve_resize_targets(paths, output_dir)
        if targets is None:
            return
        dialog.set_running(len(targets))
        jobs = [
            (
                str(source), str(target), int(options["max_side"]), bool(options["sharpen"]),
                int(options["sharpen_amount"]), bool(options["unsharp"]), float(options["unsharp_radius"]),
                int(options["unsharp_amount"]), int(options["unsharp_threshold"]), bool(options["keep_exif"]),
                int(self.photo_details.get(source.name, {}).get("orientation") or 1),
            )
            for source, target in targets
        ]
        errors: list[str] = []
        workers = min(8, max(1, os.cpu_count() or 1), len(jobs))
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_resize_export_worker, job) for job in jobs]
            for completed, future in enumerate(as_completed(futures), 1):
                source, _output, error = future.result()
                if error:
                    errors.append(f"{Path(source).name}: {error}")
                dialog.update_progress(completed, len(jobs))
        if errors:
            dialog.status.setText(f"Готово с ошибками ({len(errors)}): {errors[0]}")
            dialog.cancel_button.setEnabled(True)
            dialog.cancel_button.setText("Закрыть")
            return
        dialog.accept()

    def _resolve_resize_targets(self, paths: list[Path], output_dir: Path) -> list[tuple[Path, Path]] | None:
        """Pick non-destructive output paths and ask once per existing collision."""
        targets: list[tuple[Path, Path]] = []
        planned: set[str] = set()
        overwrite_all = False
        for source in paths:
            target = output_dir / f"{source.stem}.jpg"
            if target.name.casefold() in planned:
                target = self._next_resize_name(target, planned)
            if target.exists() and not overwrite_all:
                choice = QMessageBox(self)
                choice.setWindowTitle("Файл уже существует")
                choice.setText(f"В папке экспорта уже есть «{target.name}».")
                overwrite = choice.addButton("Перезаписать", QMessageBox.ButtonRole.AcceptRole)
                overwrite_all_button = choice.addButton("Перезаписать все", QMessageBox.ButtonRole.YesRole)
                rename = choice.addButton("Переименовать", QMessageBox.ButtonRole.ActionRole)
                cancel = choice.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
                choice.exec()
                clicked = choice.clickedButton()
                if clicked == cancel or clicked is None:
                    return None
                if clicked == overwrite_all_button:
                    overwrite_all = True
                elif clicked == rename:
                    target = self._next_resize_name(target, planned)
                elif clicked != overwrite:
                    return None
            planned.add(target.name.casefold())
            targets.append((source, target))
        return targets

    @staticmethod
    def _next_resize_name(target: Path, planned: set[str]) -> Path:
        for index in range(2, 100_000):
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            if candidate.name.casefold() not in planned and not candidate.exists():
                return candidate
        raise OSError("Не удалось подобрать свободное имя")

    def _rename_files_safely(
        self, names: dict[str, str], progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Use temporary sibling names so swaps and occupied targets are lossless."""
        changes = {old: new for old, new in names.items() if old != new}
        if not changes:
            return
        directory = self.current_dir
        if len({name.casefold() for name in changes.values()}) != len(changes):
            raise OSError("Шаблон создаёт одинаковые имена")
        for old, new in changes.items():
            source, target = directory / old, directory / new
            if not source.is_file():
                raise OSError(f"Файл «{old}» больше не существует")
            if target.exists() and not any(target.samefile(directory / source_name) for source_name in changes):
                raise OSError(f"Файл «{new}» уже существует")

        token = uuid4().hex
        temporary = {old: directory / f".__rawww_rename_{token}_{index}" for index, old in enumerate(changes)}
        moved: list[str] = []
        completed: list[str] = []
        total_steps = len(changes) * 2
        step = 0
        try:
            for old, temporary_path in temporary.items():
                (directory / old).rename(temporary_path)
                moved.append(old)
                step += 1
                if progress is not None:
                    progress(step, total_steps)
            for old, temporary_path in temporary.items():
                temporary_path.rename(directory / changes[old])
                completed.append(old)
                step += 1
                if progress is not None:
                    progress(step, total_steps)
        except OSError:
            for old in reversed(completed):
                target, temporary_path = directory / changes[old], temporary[old]
                if target.exists():
                    target.rename(temporary_path)
            for old in reversed(moved):
                temporary_path, source = temporary[old], directory / old
                if temporary_path.exists():
                    temporary_path.rename(source)
            raise

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
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(analysis_paths)
            self.ai_analysis_available = remaining > 0
        self._refresh_status_panel()

    def _folder_changed(self, path: str) -> None:
        if self._selection_progress is not None or self._upload_progress is not None:
            return
        if monotonic() < self._ignore_folder_changes_until:
            return
        if not self.closing and Path(path) == self.current_dir:
            self.folder_change_timer.start(FOLDER_CHANGE_DEBOUNCE_MS)

    def _reload_changed_folder(self) -> None:
        if self._selection_progress is not None or self._upload_progress is not None:
            return
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
        self.ai_analysis_available = waiting > 0 and self.ai_pipeline.pending_count() == 0
        self._refresh_status_panel()

    def _reset_ai_status(self) -> None:
        if not hasattr(self, "ai_button"):
            return
        self.ai_analysis_available = False
        self._refresh_status_panel()

    def _refresh_status_panel(self) -> None:
        """Show one active operation and keep folder/selection counts in the toolbar."""
        if not hasattr(self, "status_label"):
            return
        # Directory scanning already established these collections. Avoid
        # thousands of filesystem stat calls whenever progress is repainted.
        visible_files = [
            path for path in self.view_paths
            if is_supported_media(path)
        ]
        total_files = sum(
            1 for path in self.all_paths
            if is_supported_media(path)
        )
        filtered = len(visible_files)
        position = visible_files.index(self.current_path) + 1 if self.current_path in visible_files else "-"
        selected = len(self._selected_paths())
        text = f"{position}/{filtered}"
        if filtered != total_files:
            text += f" (всего {total_files})"
        if hasattr(self, "full_view"):
            self.full_view.set_counter(
                f"{position}/{filtered}",
                self.settings.value("interface/show_full_view_counter", True, bool),
            )
        if selected > 1:
            text += f" (выделено: {selected})"
        self._status_text = text
        self._fit_status_text()
        self.status_label.setToolTip(text)

        if self._upload_progress is not None:
            done, total = self._upload_progress
            self.status_progress.setRange(0, max(1, total))
            self.status_progress.setValue(done)
            self.status_progress.setFormat(f"Отправка: {done}/{total}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(done, total)
            return

        if self._receive_progress is not None:
            done, total, retrying = self._receive_progress
            self.status_progress.setRange(0, max(1, total))
            self.status_progress.setValue(done)
            suffix = f" · ошибок: {retrying}, повторяем" if retrying else ""
            self.status_progress.setFormat(f"Приём ShotSync: {done}/{total}{suffix}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(done, total)
            return

        if self._selection_progress is not None:
            done, total = self._selection_progress
            if not total:
                self.status_progress.setRange(0, 0)
                self.status_progress.setFormat("Отбор ShotSync…")
                self.status_progress.setToolTip(self.status_progress.format())
                self.status_progress.show()
                self._set_taskbar_progress(0, 0)
                return
            self.status_progress.setRange(0, max(1, total))
            self.status_progress.setValue(done)
            self.status_progress.setFormat(f"Отбор ShotSync: {done}/{total}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(done, total)
            return

        if self._shotsync_marks_fetching:
            self.status_progress.setRange(0, 0)
            self.status_progress.setFormat("Получение меток ShotSync…")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(0, 0)
            return

        if self._shotsync_pending_marks:
            self.status_progress.setRange(0, 0)
            self.status_progress.setFormat(
                f"Синхронизация меток: {self._shotsync_pending_marks}"
            )
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(0, 0)
            return

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
            file_type = self.file_type_filter.currentData()
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
                suffix = path.suffix.lower()
                if file_type == "jpg" and suffix not in JPEG_EXTENSIONS:
                    continue
                if file_type == "raw" and suffix not in RAW_EXTENSIONS:
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
        if self._xmp_export_after_cache_load:
            self._xmp_export_after_cache_load = False
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))
        self._refresh_camera_filter()
        # Series membership depends on embeddings, which are only available
        # after the folder cache has loaded. Rebuild the grid now so a restored
        # checked state reflects the actual collapsed-series view.
        self._apply_view()
        self._update_analysis_controls()
        self._refresh_ai_status()
        if (
            self.stack.currentWidget() is self.full_view
            and self.current_path is not None
            and self.current_path.parent == self.current_dir
            and not is_supported_video(self.current_path)
        ):
            # A file launch opens Full View before the folder is scanned, so the
            # requested photo could not be decoded yet (_submit_decode bails
            # while folder_cache is None) and the deferred load_directory
            # cancelled any in-flight decode. Now that the cache is ready,
            # decode the shown photo so it reaches full resolution instead of
            # staying on the thumbnail fallback.
            full_size = self._full_preview_size()
            if self._cache_get((self.current_path, full_size)) is None:
                self._show_best_cached_full(self.current_path, full_size)
                self._promote_current_full_task(self.current_path, full_size)
                self._submit_decode(self.current_path, full_size, full_priority=True)
                self._preload_neighbors(self.current_path)
            # The preview strip was built empty because the folder had not been
            # scanned when Full View opened. Rebuild it now so it shows the
            # neighbours and scrolls to the current photo without needing a
            # manual left/right keypress.
            self._refresh_full_view_navigation(self.current_path)
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
        self._refresh_shotsync_tab_indicator()
        self._refresh_shotsync_current_shooting()
        if self.folder_cache is not None:
            session = self.folder_cache.shotsync_session()
            if session:
                self._remember_shotsync_folder(session[0], self.current_dir)
                mode = "selection_copy" if self.current_dir.is_relative_to(selection_root()) else "uploaded"
                self._remember_shotsync_mode(session[0], mode)
                self._refresh_shotsync_local_folders(self._shotsync_shootings)
        self._fetch_pending_shotsync_marks()

    def _apply_view(self, *_args) -> None:
        if not hasattr(self, "rating_filter"):
            return
        rating = self.rating_filter.currentData()
        color = self.color_filter.currentData()
        media = self.media_filter.currentData()
        file_type = self.file_type_filter.currentData()
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
            suffix = path.suffix.lower()
            if file_type == "jpg" and suffix not in JPEG_EXTENSIONS:
                return False
            if file_type == "raw" and suffix not in RAW_EXTENSIONS:
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
            def capture_time(path: Path) -> float:
                value = self.photo_details.get(path.name, {}).get("original_datetime")
                if value:
                    try:
                        return datetime.fromisoformat(str(value)).timestamp()
                    except ValueError:
                        pass
                return path.stat().st_mtime_ns / 1_000_000_000

            key = capture_time
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
            if self._xmp_auto_enabled():
                self._queue_xmp(path)
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

    def _show_comment_dialog(self) -> None:
        """Edit a comment without letting a hotkey steal focus from the grid."""
        selected = self._selected_paths()
        in_full_view = self.stack.currentWidget() is self.full_view
        if not in_full_view and not selected:
            return
        if in_full_view and self.current_path is not None:
            path = self.current_path
        elif selected:
            path = selected[0]
        else:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Комментарий")
        dialog.setModal(True)
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        edit = RichCodeCommentEdit()
        edit.setFixedHeight(34)
        edit.setText(str(self.photo_details.get(path.name, {}).get("comment") or ""))
        active_id = self.settings.value("code_replacements/active_set_id", 0, int)
        edit.set_codes(self.code_replacement_sets, active_id)
        cursor = edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        edit.setTextCursor(cursor)
        layout.addWidget(edit)
        set_select = QComboBox()
        for group in self.code_replacement_sets:
            set_select.addItem(str(group.get("name") or "Без названия"), int(group.get("id") or 0))
        if set_select.count() > 1:
            set_select.setCurrentIndex(max(0, set_select.findData(active_id)))
            layout.addWidget(set_select)
        else:
            set_select.hide()
        def change_set(index: int) -> None:
            nonlocal active_id
            active_id = int(set_select.itemData(index) or 0)
            self.settings.setValue("code_replacements/active_set_id", active_id)
            edit.set_codes(self.code_replacement_sets, active_id)
            self._set_code_replacements(self.code_replacement_sets)
        set_select.currentIndexChanged.connect(change_set)
        edit.returnPressed.connect(dialog.accept)
        QTimer.singleShot(0, edit.setFocus)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.comment_edit.setText(edit.text())
            self.full_view.set_comment(edit.text())
            self._update_selection(comment=edit.text().strip())

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

    def _toggle_full_view_mark_indicator(self) -> None:
        """Apply M on an unmarked photo, or clear every mark on the next click."""
        if self.current_path is None or self.stack.currentWidget() is not self.full_view:
            return
        detail = self.photo_details.get(self.current_path.name, {})
        if int(detail.get("rating") or 0) > 0 or detail.get("color_label"):
            self._update_selection(rating=None, color_label="")
            return
        self._apply_quick_mark()

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
                (variant, decoded) for (path, variant), decoded in self.decode_cache.memory.items()
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
        already_added = any(
            self._face_similarity(embedding, item.get("embedding", [])) >= 0.42
            for item in self.face_sets
        )
        toast_message = "Это лицо уже есть в наборе."
        if not already_added:
            avatar = self._current_face_avatar(face, 80)
            self.face_sets.append({
                "id": sha1(json.dumps(embedding).encode()).hexdigest()[:12],
                "name": "Без имени",
                "embedding": embedding,
                "avatar": self._pixmap_to_base64(avatar),
            })
            self._save_face_sets()
            self._update_analysis_controls()
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))
            toast_message = "Лицо добавлено в набор."
        self._show_face_sets(toast_message)

    def _face_set_by_id(self, face_id: str) -> dict | None:
        return next((entry for entry in self.face_sets if entry.get("id") == face_id), None)

    def _show_face_sets(self, toast_message: str | None = None) -> None:
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
        if toast_message:
            toast = QLabel(toast_message, dialog)
            toast.setObjectName("faceSetsToast")
            toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
            toast.setWordWrap(True)
            toast.setFixedWidth(300)
            toast.setMinimumHeight(38)

            def place_toast() -> None:
                toast.adjustSize()
                toast.move((dialog.width() - toast.width()) // 2, 48)
                toast.raise_()
                toast.show()

            QTimer.singleShot(0, place_toast)
            dismiss_toast = QTimer(toast)
            dismiss_toast.setSingleShot(True)
            dismiss_toast.timeout.connect(toast.deleteLater)
            dismiss_toast.start(3_000)
        dialog.exec()

    def _rename_face_set(self, face_id: str, name: str) -> None:
        entry = self._face_set_by_id(face_id)
        if entry is not None:
            entry["name"] = name.strip() or "Без имени"
            self._save_face_sets()
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))

    def _delete_face_set(self, face_id: str, rebuild: Callable[[], None]) -> None:
        self.face_sets = [entry for entry in self.face_sets if entry.get("id") != face_id]
        self._save_face_sets()
        if self._xmp_auto_enabled():
            self._queue_xmp_paths(path for path in self.all_paths if path.is_file() and is_supported_image(path))
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
        for label, value in (("Красная", "red"), ("Жёлтая", "yellow"), ("Зелёная", "green"), ("Синяя", "blue"), ("Фиол��товая", "purple")):
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
        self.scheduler.submit_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def _submit_video_thumbnail(self, path: Path, *, visible_priority: bool) -> None:
        self.scheduler.submit_video_thumbnail(path, visible_priority=visible_priority)

    def _on_decoded(self, payload: object) -> None:
        decoded, max_size = payload
        self.visible_thumb_pending.discard((decoded.path, max_size))
        if not self.workspace_active:
            return
        if max_size == ORIGINAL_SIZE:
            if self.stack.currentWidget() is self.full_view and decoded.path == self.current_path:
                self.full_view.show_original(decoded)
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
        if self.closing or not self.workspace_active or preview.isNull() or path.parent != self.current_dir:
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

    def _open_in_editor(self) -> None:
        """Open the active image in the configured external editor."""
        path = self.current_path if self.stack.currentWidget() is self.full_view else None
        if path is None:
            item = self.grid.currentItem()
            path = Path(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        if path is None or not path.is_file():
            return

        executable = self.settings.value("editor/executable", "", str).strip()
        use_custom = self.settings.value("editor/use_custom_executable", bool(executable), bool)
        if use_custom:
            editor = Path(executable)
            if not editor.is_file():
                QMessageBox.warning(
                    self,
                    "Внешний редактор",
                    f"Не найден исполняемый файл редактора:\n{editor}",
                )
                return
            command = [str(editor), str(path)]
        else:
            command = self._photoshop_command(path)
            if command is None:
                QMessageBox.warning(
                    self,
                    "Adobe Photoshop не найден",
                    "Установите Adobe Photoshop или укажите исполняемый файл другого редактора "
                    "в Настройки → Поведение.",
                )
                return
        try:
            subprocess.Popen(command, **no_window_kwargs())
        except OSError as error:
            QMessageBox.warning(self, "Не удалось открыть редактор", str(error))

    @staticmethod
    def _photoshop_command(path: Path) -> list[str] | None:
        """Build a launch command for the default Adobe Photoshop installation."""
        if sys.platform == "darwin":
            return ["open", "-a", "Adobe Photoshop", str(path)]
        if sys.platform != "win32":
            executable = shutil.which("photoshop")
            return [executable, str(path)] if executable else None

        # Photoshop commonly registers this application path; the directory
        # fallback also covers installations that do not add it to PATH.
        try:
            import winreg

            for hive, key_path in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
            ):
                with winreg.OpenKey(hive, key_path) as key:
                    executable, _ = winreg.QueryValueEx(key, None)
                    if Path(executable).is_file():
                        return [executable, str(path)]
        except OSError:
            pass

        adobe_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Adobe"
        candidates = sorted(adobe_dir.glob("Adobe Photoshop*/Photoshop.exe"), reverse=True)
        if candidates:
            return [str(candidates[0]), str(path)]
        executable = shutil.which("Photoshop.exe")
        return [executable, str(path)] if executable else None

    def _open_grid_audio(self, path: Path) -> None:
        """The grid microphone follows the web viewer: open and play at once."""
        self.open_full(path)
        QTimer.singleShot(0, self.full_view._toggle_audio)

    def _set_grid_audio_hover(self, value: object) -> None:
        path = value if isinstance(value, Path) else None
        if path is None:
            self.grid_audio_player.stop()
            return
        detail = self.photo_details.get(path.name, {})
        audio_path = str(detail.get("audio_comment_path") or "")
        if not audio_path or not Path(audio_path).is_file():
            return
        if self.grid_audio_path != audio_path:
            self.grid_audio_path = audio_path
            self.grid_audio_player.setSource(QUrl.fromLocalFile(audio_path))
        self.grid_audio_player.play()

    def _grid_current_item_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.pending_grid_full_request = None
            self.grid_full_request_timer.stop()
            self._refresh_status_panel()
            return
        value = current.data(Qt.ItemDataRole.UserRole)
        if not value:
            return
        if self.stack.currentWidget() is not self.grid_page:
            # Full View owns the current photo. A grid selection that changes
            # in the background (for example while the folder populates after a
            # file launch) must not hijack current_path, otherwise the launched
            # photo's decode is dropped in _on_decoded and Full View stays blank.
            return
        path = Path(value)
        self.current_path = path
        self.workspace_state.current_photo = path
        self._refresh_status_panel()
        if hasattr(self, "meta_bar"):
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
        if self.current_path != path:
            self.full_view.stop_audio()
        self.current_path = path
        self._refresh_status_panel()
        self.full_view.cancel_zoom()
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

    def _request_original_zoom(self, _position: object) -> None:
        """Load the source only on demand; repeated 100% views reuse RAM."""
        if self.current_path is None or is_supported_video(self.current_path):
            return
        # A 100% inspection is explicitly user-driven: do not let queued
        # thumbnails or screen-sized neighbour decodes delay it.
        self._suspend_thumbnail_work()
        original_key = (self.current_path, ORIGINAL_SIZE)
        for key, future in list(self.pending.items()):
            if key != original_key and key[1] != THUMB_SIZE:
                future.cancel()
        self._submit_decode(self.current_path, ORIGINAL_SIZE, full_priority=True)

    def show_grid(self) -> None:
        # Leaving full view must not keep a video playing in the background.
        self.full_view.stop_video()
        self.full_view.stop_audio()
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
        return self.decode_cache.get(key)

    def _thumbnail_cache_get(self, path: Path) -> QImage | None:
        return self.decode_cache.thumbnail_get(path)

    def _thumbnail_cache_put(self, path: Path, image: QImage) -> None:
        self.decode_cache.thumbnail_put(path, image)

    def _cache_put(self, key: tuple[Path, int], decoded: DecodedImage) -> None:
        self.decode_cache.put(key, decoded)

    def _background_decode_executor(self) -> ProcessPoolExecutor:
        return self.scheduler._background_decode_executor()

    def _abandon_preview_decode_work(self) -> None:
        self.scheduler.abandon_preview_decode_work()

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

    def __init__(self, open_target: Path | None = None) -> None:
        super().__init__()
        self.settings = _application_settings()
        self._update_check_running = False
        self._update_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="update-check")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(_application_icon())
        self.resize(1440, 920)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = ChromeTitleBar(self)
        self.title_bar.setObjectName("chromeTitleBar")
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(3, 0, 3, 0)
        title_layout.setSpacing(3)
        app_icon = QLabel()
        app_icon.setObjectName("appIcon")
        app_icon.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        app_icon.setStyleSheet("background: transparent; border: 0;")
        app_icon.setFixedSize(32, 32)
        app_icon.setToolTip(APP_NAME)
        app_icon.setPixmap(_title_bar_icon().pixmap(29, 29))
        title_layout.addWidget(app_icon, 0, Qt.AlignmentFlag.AlignVCenter)
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
        self.tabs.pathsDropped.connect(self._drop_on_workspace_tab)
        title_layout.addWidget(self.tabs)
        add_tab = QToolButton()
        add_tab.setObjectName("titleAction")
        add_tab.setIcon(_chrome_icon("plus"))
        add_tab.setIconSize(QSize(16, 16))
        add_tab.setToolTip("Новая вкладка")
        add_tab.clicked.connect(self._add_workspace)
        title_layout.addWidget(add_tab)
        title_layout.addStretch(1)
        settings_button = QToolButton()
        settings_button.setObjectName("settingsTitleAction")
        settings_button.setIcon(_fomantic_icon("cog", 18, "#c9c9c9"))
        settings_button.setIconSize(QSize(24, 24))
        settings_button.setFixedSize(34, 34)
        settings_button.setToolTip("Настройки")
        settings_button.clicked.connect(self._show_settings)
        title_layout.addWidget(settings_button)
        help_button = QToolButton()
        help_button.setObjectName("settingsTitleAction")
        help_button.setIcon(_fomantic_icon("help", 18, "#c9c9c9"))
        help_button.setIconSize(QSize(24, 24))
        help_button.setFixedSize(34, 34)
        help_button.setToolTip("Помощь")
        help_button.clicked.connect(self._show_help_menu)
        title_layout.addWidget(help_button)
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
        if open_target is not None and open_target.exists():
            # An OS open request is a direct navigation, not a regular
            # application restore.  Building restored tabs first would make a
            # file launch flash the grid before Full View takes over.
            self._open_launch_target(open_target)
        else:
            self._restore_workspaces()
        if self.settings.value("updates/auto_check", True, bool):
            QTimer.singleShot(10_000, lambda: self._check_for_updates(interactive=False))

    def _check_for_updates(self, *, interactive: bool) -> None:
        if self._update_check_running:
            return
        self._update_check_running = True
        future = self._update_executor.submit(
            fetch_release_info, APP_VERSION
        )

        def finish() -> None:
            self._update_check_running = False
            try:
                payload = future.result()
                release = payload["latest"]
                version = str(release.get("version", ""))
                if is_newer(version, APP_VERSION):
                    self._show_update_dialog(release, payload.get("releases", []))
                elif interactive:
                    QMessageBox.information(self, "Обновления", "У вас установлена актуальная версия Контрольки.")
            except Exception:
                # A background check must never interrupt the photo workflow.
                if interactive:
                    QMessageBox.warning(
                        self,
                        "Обновления",
                        "Не удалось проверить обновления. Проверьте подключение к интернету и повторите попытку позже.",
                    )

        def wait_for_finish() -> None:
            if future.done():
                finish()
            else:
                QTimer.singleShot(100, wait_for_finish)

        QTimer.singleShot(0, wait_for_finish)

    def _show_update_dialog(self, release: dict, releases: object) -> None:
        version = str(release.get("version", ""))
        changes: list[str] = []
        if isinstance(releases, list):
            for item in releases:
                if not isinstance(item, dict):
                    continue
                item_version = str(item.get("version", ""))
                if item_version and is_newer(item_version, APP_VERSION):
                    changes.append(f"<h4>Версия {item_version}</h4>{item.get('changelog', '')}")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Доступно обновление")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(f"Доступна Контролька {version}")
        dialog.setInformativeText("".join(changes) or "Откройте страницу загрузки, чтобы узнать об изменениях.")
        dialog.setTextFormat(Qt.TextFormat.RichText)
        download = dialog.addButton("Открыть страницу загрузки", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is download:
            webbrowser.open(str(release.get("landing_url") or "https://shotsync.ru/ctrlka/"))

    def _open_launch_target(self, target: Path) -> None:
        """Open a folder in a workspace tab or a file directly in Full View."""
        if target.is_dir():
            self._open_folder_tab(target)
            return
        if not target.is_file():
            return
        self._open_folder_tab(target.parent, defer_initial_scan=True)
        workspace = self.workspace_stack.currentWidget()
        if isinstance(workspace, Workspace):
            # Do this before the native window is shown.  The first presented
            # frame is therefore Full View rather than a briefly visible grid.
            workspace.open_full(target)

    def _add_workspace(
        self,
        directory: Path | None = None,
        *,
        defer_initial_scan: bool = False,
    ) -> None:
        workspace = Workspace(directory, defer_initial_scan=defer_initial_scan)
        workspace.destination_paths_provider = self._open_workspace_paths
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
        workspace.shotsyncFolderChanged.connect(
            lambda linked, view=workspace: self._set_workspace_shotsync_icon(view, linked)
        )
        self.tabs.setCurrentIndex(index)
        self._select_workspace(self.tabs.currentIndex())
        self._update_tab_geometry()

    def _open_workspace_paths(self) -> list[Path]:
        return [
            workspace.current_dir
            for index in range(self.workspace_stack.count())
            if isinstance(workspace := self.workspace_stack.widget(index), Workspace)
        ]

    def _open_folder_tab(self, folder: Path, *, defer_initial_scan: bool = False) -> None:
        """Focus an existing tab for ``folder`` or open a new one."""
        folder = Path(folder)
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and workspace.current_dir == folder:
                self.tabs.setCurrentIndex(index)
                self._select_workspace(index)
                return
        self._add_workspace(folder, defer_initial_scan=defer_initial_scan)

    def _drop_on_workspace_tab(self, paths: list[Path], index: int, action) -> None:
        workspace = self.workspace_stack.widget(index)
        if not isinstance(workspace, Workspace):
            return
        self.tabs.setCurrentIndex(index)
        workspace._receive_dropped_paths(paths, workspace.current_dir, action)

    def _create_actions(self) -> None:
        next_tab = QAction("Next workspace", self)
        next_tab.setShortcut(QKeySequence("Ctrl+Right"))
        next_tab.triggered.connect(lambda: self._select_relative_workspace(1))
        self.addAction(next_tab)

        previous_tab = QAction("Previous workspace", self)
        previous_tab.setShortcut(QKeySequence("Ctrl+Left"))
        previous_tab.triggered.connect(lambda: self._select_relative_workspace(-1))
        self.addAction(previous_tab)

    def _show_settings(self) -> None:
        workspace = self.workspace_stack.currentWidget()
        if not isinstance(workspace, Workspace):
            return
        dialog = SettingsDialog(
            self.settings,
            workspace.shotsync_client,
            workspace._set_code_replacements,
            workspace._show_shotsync_login,
            lambda: self._check_for_updates(interactive=True),
            cache_size,
            self._clear_all_caches,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for workspace_index in range(self.workspace_stack.count()):
                candidate = self.workspace_stack.widget(workspace_index)
                if isinstance(candidate, Workspace):
                    candidate._reload_hotkeys()
                    candidate._refresh_status_panel()
                    candidate.full_view.refresh_mark_indicator()
        if workspace.shotsync_client.has_key():
            workspace._sync_code_replacements()

    def _clear_all_caches(self) -> None:
        """Close active cache databases before removing their files."""
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if not isinstance(workspace, Workspace):
                continue
            workspace._abandon_preview_decode_work()
            workspace._flush_folder_cache(wait=True, close=True)
            workspace.folder_cache = None
            workspace.cache_ready = False
            workspace.decode_cache.clear()
        clear_cache()
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and workspace.current_dir.is_dir():
                workspace.load_directory(workspace.current_dir)

    def _show_help_menu(self) -> None:
        menu = QMenu(self.sender() if isinstance(self.sender(), QWidget) else self)
        menu.setObjectName("helpPopup")
        content = QWidget(menu)
        content.setObjectName("helpPopupContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        title = QLabel("Помощь")
        title.setObjectName("helpPopupTitle")
        layout.addWidget(title)

        online = QPushButton("Онлайн-справка")
        online.setObjectName("helpPopupPrimaryButton")
        online.clicked.connect(lambda: (menu.close(), webbrowser.open("https://shotsync.ru/help/s/kontrolka/")))
        layout.addWidget(online)

        hotkeys = QPushButton("Горячие клавиши")
        hotkeys.setObjectName("helpPopupButton")
        hotkeys.clicked.connect(lambda: (menu.close(), HelpDialog(self.settings, self).exec()))
        layout.addWidget(hotkeys)

        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        button = self.sender()
        if isinstance(button, QToolButton):
            menu.exec(button.mapToGlobal(QPoint(0, button.height())))

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

    def _set_workspace_shotsync_icon(self, workspace: Workspace, linked: bool) -> None:
        """Show a small cloud marker without altering the folder's tab title."""
        index = self.workspace_stack.indexOf(workspace)
        if index < 0:
            return
        self.tabs.setTabIcon(
            index,
            _fomantic_icon("cloud", 14, "#76a8df") if linked else QIcon(),
        )
        self.tabs.setTabToolTip(
            index,
            "Папка связана с ShotSync" if linked else str(workspace.current_dir),
        )

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
        shotsync_paths = [
            str(workspace.current_dir)
            for index in range(self.workspace_stack.count())
            if (workspace := self.workspace_stack.widget(index)) is not None
            and workspace.shotsync_active
        ]
        self.settings.setValue("shotsync_workspaces", shotsync_paths)
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
        if not self.settings.value("restore_workspaces", True, bool):
            self._add_workspace()
            return
        stored = self.settings.value("open_workspaces", [], list)
        directories = stored if isinstance(stored, list) else [stored]
        for value in directories:
            directory = Path(str(value))
            if directory.is_dir():
                self._add_workspace(directory)
        if self.tabs.count() == 0:
            self._add_workspace()
        stored_shotsync = self.settings.value("shotsync_workspaces", [], list)
        shotsync_paths = {
            str(value) for value in (stored_shotsync if isinstance(stored_shotsync, list) else [stored_shotsync])
        }
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and str(workspace.current_dir) in shotsync_paths:
                workspace._activate_shotsync()

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
        if not self.isVisible():
            # ``main`` will show this native window fullscreen as its first
            # presentation.  Scheduling a transition here would expose the
            # normal window for one event-loop turn.
            return
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


def _drive_key(path: Path) -> str:
    anchor = path.anchor or str(path)
    return anchor.replace("\\", "/")


def _workspace_title(directory: Path) -> str:
    return directory.name or str(directory)


def _set_windows_app_user_model_id() -> None:
    """Give source runs the same Windows taskbar identity as the packaged app."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ru.shotsync.ctrlka")
    except (AttributeError, OSError):
        pass


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
    _set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(_application_icon())
    qt_ru = QTranslator(app)
    if qt_ru.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt_ru)
    apply_theme(app)
    window = MainWindow(target_from_argv())
    if getattr(window, "_fast_full_view", False):
        window.showFullScreen()
        workspace = window.workspace_stack.currentWidget()
        if isinstance(workspace, Workspace):
            QTimer.singleShot(0, workspace.full_view.finish_fast_resize)
    else:
        window.showMaximized()
    sys.exit(app.exec())
