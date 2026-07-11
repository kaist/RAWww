from __future__ import annotations

import os
import sys
import math
import ctypes
import plistlib
import subprocess
from collections import OrderedDict, deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from PySide6.QtCore import QDir, QFileInfo, QFileSystemWatcher, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal, QObject, QStorageInfo
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QInputDialog,
    QMessageBox,
)

from .cache import FolderCache
from .ai import AiPipeline
from .imaging import DecodedImage, PixelImage, decode_pixels, decode_thumbnail_pixels, is_supported_image, pixel_to_decoded
from .workspace import WorkspaceRequest, WorkspaceState


THUMB_SIZE = 256
CARD_MIN_WIDTH = 150
CARD_TARGET_WIDTH = 200
CARD_MAX_WIDTH = 280
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

FOMANTIC_ICON_CODES = {
    "images": "\uf302", "user": "\uf007", "brush": "\uf1fc", "media": "\uf87c",
    "sort": "\uf160", "search": "\uf002", "star": "\uf005", "ban": "\uf05e",
    "chevron-down": "\uf078", "chevron-up": "\uf077", "bookmark": "\uf02e",
    "step-forward": "\uf051", "keyboard": "\uf11c", "folder": "\uf07c",
    "filter": "\uf0b0", "lightbulb": "\uf0eb", "volume": "\uf028", "close": "\uf00d",
    "expand": "\uf065", "zoom": "\uf00e", "zoom-out": "\uf010",
}
FOMANTIC_ICON_FAMILY = ""


class DecodeBridge(QObject):
    decoded = Signal(object)
    failed = Signal(str, str)
    cacheLoaded = Signal(int, object)
    directoryScanned = Signal(object, Path, object)


class PhotoGrid(QListWidget):
    openRequested = Signal(Path)
    viewportChanged = Signal()
    cardSizeChanged = Signal(int)
    seriesToggleRequested = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("photoGrid")
        self._last_icon_size = QSize()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.card_size = 1
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSpacing(5)
        self.setItemDelegate(PhotoCardDelegate(self))
        self.itemActivated.connect(self._emit_open)
        self._update_card_size()

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
                badge = QRect(rect.right() - 52, rect.top() + 5, 46, 22)
                if series.get("count", 0) > 1 and badge.contains(event.position().toPoint()):
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value:
                        self.seriesToggleRequested.emit(Path(value))
                        event.accept()
                        return
        super().mouseReleaseEvent(event)

    def _update_card_size(self) -> None:
        available = max(CARD_MIN_WIDTH, self.viewport().width() - 28)
        targets = (150, CARD_TARGET_WIDTH, 280)
        target_width = targets[self.card_size]
        columns = max(1, round(available / target_width))
        width = (available - ((columns - 1) * self.spacing())) // columns
        width = max(CARD_MIN_WIDTH, min(CARD_MAX_WIDTH, width))
        height = int(width / CARD_ASPECT)
        icon_size = QSize(width, height)
        if icon_size == self._last_icon_size:
            return
        self._last_icon_size = icon_size
        self.setIconSize(icon_size)
        self.setGridSize(QSize(width + 10, height + 42))

    def change_card_size(self, delta: int) -> None:
        new_size = max(0, min(2, self.card_size + delta))
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
        inset = 3 if self.compact else 5
        rect = option.rect.adjusted(inset, inset, -inset, -inset)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        detail = index.data(DETAIL_ROLE) or {}
        label = str(detail.get("color_label") or "")
        colors = {"red": "#9a5d5d", "yellow": "#a8924f", "green": "#4f8b5a", "blue": "#5b82ba", "purple": "#8e63a8"}
        tints = {
            "red": QColor(140, 78, 78, 46), "yellow": QColor(148, 131, 71, 46),
            "green": QColor(73, 130, 84, 42), "blue": QColor(77, 112, 160, 48),
            "purple": QColor(125, 88, 148, 44),
        }

        bg = QColor("#c4c4c4") if selected else QColor("#b3b3b3" if hovered else "#a7a7a7")
        painter.fillRect(rect, bg)
        if label in tints:
            painter.fillRect(rect, tints[label])
        painter.setPen(QPen(QColor(colors.get(label, "#454545")), 2 if not self.compact else 1))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        if selected:
            painter.setPen(QPen(QColor("#f1f1f1"), 3))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))

        top, side, bottom = (14, 4, 15) if self.compact else (20, 4, 16)
        image_rect = rect.adjusted(side, top, -side, -bottom)
        # Check if this item is a directory
        path = index.data(Qt.ItemDataRole.UserRole)
        path_obj = Path(path) if path else None
        if path_obj and path_obj.is_dir():
             # Use system folder icon from Qt file icon provider
             icon_provider = QFileIconProvider()
             folder_icon = icon_provider.icon(QFileInfo(str(path_obj)))
             if not folder_icon.isNull():
                 # Clear any background first (remove old gray background)
                 painter.fillRect(image_rect, Qt.GlobalColor.transparent)
                 # Scale icon to fill the entire image area like we do for photos
                 scaled = folder_icon.pixmap(image_rect.size()).size().scaled(
                     image_rect.size(), 
                     Qt.AspectRatioMode.KeepAspectRatio
                 )
                 target = QRect(
                     image_rect.left() + (image_rect.width() - scaled.width()) // 2,
                     image_rect.top() + (image_rect.height() - scaled.height()) // 2,
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

        # For folders: full width text, no ratings/badges
        if path_obj and path_obj.is_dir():
            text_rect = QRect(rect.left() + 5, rect.bottom() - bottom + 2, rect.width() - 10, bottom - 2)
        else:
            # For photos: normal layout with space for rating
            text_rect = QRect(rect.left() + 5, rect.bottom() - bottom + 2, rect.width() * (3 if self.compact else 2) // 5, bottom - 2)
        # For folders always use just the folder name, never full path
        if path_obj and path_obj.is_dir():
            text = path_obj.name
        else:
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = QColor("#242424")
        painter.setPen(color)
        font = painter.font()
        font.setPointSizeF(6.5 if self.compact else 7.5)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        # Only render ratings and series badges for photos, not folders
        if not (path_obj and path_obj.is_dir()):
            rating = detail.get("rating")
            if rating:
                badge = QRect(rect.right() - (43 if self.compact else 50), rect.bottom() - bottom + 2, 39 if self.compact else 45, bottom - 2)
                painter.setPen(QColor("#3a3123"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "★" * int(rating))
            series = index.data(SERIES_ROLE) or {}
            count = int(series.get("count", 0) or 0)
            if count > 1:
                badge_rect = QRect(rect.right() - 48, rect.top() + 3, 44, 15)
                painter.fillRect(badge_rect, QColor("#d5d5d5"))
                painter.setPen(QColor("#262626"))
                icon_font = QFont(FOMANTIC_ICON_FAMILY or option.font.family())
                icon_font.setPixelSize(9)
                painter.setFont(icon_font)
                painter.drawText(QRect(badge_rect.left() + 3, badge_rect.top(), 12, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, FOMANTIC_ICON_CODES["images"] if FOMANTIC_ICON_FAMILY else "▣")
                font = painter.font()
                font.setPixelSize(9)
                painter.setFont(font)
                marker = "−" if series.get("expanded") else "+"
                painter.drawText(QRect(badge_rect.left() + 16, badge_rect.top(), 26, badge_rect.height()), Qt.AlignmentFlag.AlignCenter, f"{count} {marker}")
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return option.widget.gridSize() if isinstance(option.widget, QListWidget) else super().sizeHint(option, index)


class ViewerStrip(QListWidget):
    pathActivated = Signal(Path)

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
            self.setGridSize(QSize(82, 76))
            self.setIconSize(QSize(76, 70))
            self.setFixedWidth(90)
        else:
            self.setFlow(QListWidget.Flow.LeftToRight)
            self.setWrapping(False)
            self.setGridSize(QSize(118, 104))
            self.setIconSize(QSize(112, 98))
            self.setFixedHeight(108)
            self.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.itemClicked.connect(self._activate)

    def _activate(self, item: QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self.pathActivated.emit(Path(value))

    def set_paths(self, paths: list[Path], current: Path | None, details: dict[str, dict], previews: dict[Path, QImage]) -> None:
        if paths != self._paths:
            self.clear()
            self._items_by_path.clear()
            self._paths = list(paths)
            for path in paths:
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setData(DETAIL_ROLE, details.get(path.name, {}))
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


class FullView(QFrame):
    exitRequested = Signal()
    nextRequested = Signal()
    previousRequested = Signal()
    pathRequested = Signal(Path)
    ratingRequested = Signal(object)
    colorRequested = Signal(str)
    faceRequested = Signal(object)
    quickMarkRequested = Signal()
    commentSubmitted = Signal(str)

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
        self.image_view.faceRequested.connect(self.faceRequested)

        self.info_label = QLabel()
        self.info_label.setObjectName("overlayLabel")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.photo_strip = ViewerStrip()
        self.photo_strip.pathActivated.connect(self.pathRequested)
        self.series_strip = ViewerStrip(vertical=True)
        self.series_strip.pathActivated.connect(self.pathRequested)

        self.series_panel = QFrame()
        self.series_panel.setObjectName("seriesPanel")
        series_layout = QVBoxLayout(self.series_panel)
        series_layout.setContentsMargins(5, 5, 5, 5)
        series_layout.setSpacing(4)
        self.series_up = QToolButton()
        self.series_up.setObjectName("seriesNav")
        self.series_up.setIcon(_fomantic_icon("chevron-up", 13))
        self.series_up.clicked.connect(lambda: self._move_series(-1))
        self.series_down = QToolButton()
        self.series_down.setObjectName("seriesNav")
        self.series_down.setIcon(_fomantic_icon("chevron-down", 13))
        self.series_down.clicked.connect(lambda: self._move_series(1))
        series_layout.addWidget(self.series_up)
        series_layout.addWidget(self.series_strip, 1)
        series_layout.addWidget(self.series_down)
        self.series_panel.hide()

        stage = QWidget()
        stage.setObjectName("photoStage")
        stage_layout = QHBoxLayout(stage)
        stage_layout.setContentsMargins(12, 12, 12, 12)
        stage_layout.setSpacing(12)
        stage_layout.addWidget(self.series_panel)
        stage_layout.addWidget(self.image_view, 1)

        strip_header = QWidget()
        strip_header.setObjectName("stripHeader")
        strip_header_layout = QHBoxLayout(strip_header)
        strip_header_layout.setContentsMargins(9, 4, 9, 4)
        strip_header_layout.setSpacing(5)
        self.strip_toggle = QToolButton()
        self.strip_toggle.setObjectName("stripToggle")
        self.strip_toggle.setIcon(_fomantic_icon("chevron-down", 12))
        self.strip_toggle.setToolTip("Свернуть ленту превью")
        self.strip_toggle.clicked.connect(self.toggle_strip)
        strip_header_layout.addWidget(self.strip_toggle)
        self.quick_mark_button = QToolButton()
        self.quick_mark_button.setObjectName("fullQuickMark")
        self.quick_mark_button.setIcon(_fomantic_icon("bookmark", 13))
        self.quick_mark_button.setToolTip("Быстрая метка (M)")
        self.quick_mark_button.clicked.connect(self.quickMarkRequested)
        strip_header_layout.addWidget(self.quick_mark_button)
        self.color_buttons: dict[str, QToolButton] = {}
        for color in ("", "red", "yellow", "green", "blue", "purple"):
            button = QToolButton()
            button.setObjectName("viewerColor")
            button.setProperty("colorLabel", color or "none")
            button.setCheckable(True)
            if not color:
                button.setIcon(_fomantic_icon("ban", 11, "#959595"))
            button.clicked.connect(lambda _checked=False, value=color: self.colorRequested.emit(value))
            strip_header_layout.addWidget(button)
            self.color_buttons[color] = button
        self.rating_buttons: dict[int, QToolButton] = {}
        for rating in range(0, 6):
            button = QToolButton()
            button.setObjectName("viewerRating")
            button.setIcon(_fomantic_icon("ban" if rating == 0 else "star", 10, "#95866b"))
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=rating: self.ratingRequested.emit(value or None))
            strip_header_layout.addWidget(button)
            self.rating_buttons[rating] = button
        self.full_comment_edit = QLineEdit()
        self.full_comment_edit.setObjectName("fullComment")
        self.full_comment_edit.setPlaceholderText("Комментарий для выбранных фото")
        self.full_comment_edit.editingFinished.connect(lambda: self.commentSubmitted.emit(self.full_comment_edit.text().strip()))
        strip_header_layout.addWidget(self.full_comment_edit, 1)
        strip_header_layout.addWidget(self.info_label, 1)

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

    def set_navigation(self, paths: list[Path], current: Path | None, details: dict[str, dict], previews: dict[Path, QImage], series: list[Path], generation: int) -> None:
        if generation != self._photo_generation:
            self.photo_strip.set_paths(paths, current, details, previews)
            self._photo_generation = generation
        else:
            self.photo_strip.set_current(current)
            for path, preview in previews.items():
                self.photo_strip.update_preview(path, preview)
        self.series_strip.set_paths(series, current, details, previews)
        self.series_panel.setVisible(len(series) > 1)
        current_row = self.series_strip.currentRow()
        self.series_up.setEnabled(current_row > 0)
        self.series_down.setEnabled(0 <= current_row < self.series_strip.count() - 1)

    def update_preview(self, path: Path, preview: QImage) -> None:
        self.photo_strip.update_preview(path, preview)
        self.series_strip.update_preview(path, preview)

    def set_faces(self, faces: list[dict] | None) -> None:
        self.image_view.set_faces(faces)

    def set_comment(self, comment: str) -> None:
        self.full_comment_edit.blockSignals(True)
        self.full_comment_edit.setText(comment)
        self.full_comment_edit.blockSignals(False)

    def set_metadata(self, detail: dict, paths: tuple[Path, ...] = ()) -> None:
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
        self.set_comment(str(detail.get("comment") or ""))
        for path in paths or ((self._path,) if self._path is not None else ()):
            self.photo_strip.update_details(path, detail)
            self.series_strip.update_details(path, detail)

    def _move_series(self, delta: int) -> None:
        row = self.series_strip.currentRow() + delta
        if 0 <= row < self.series_strip.count():
            item = self.series_strip.item(row)
            self.series_strip.setCurrentItem(item)
            self.pathRequested.emit(Path(item.data(Qt.ItemDataRole.UserRole)))

    def set_image(self, decoded: DecodedImage, *, fallback: bool = False) -> None:
        self._path = decoded.path
        self._is_fallback = fallback
        self._pixmap = QPixmap.fromImage(decoded.image)
        suffix = "  -  preview" if fallback else ""
        self.info_label.setText(f"{decoded.path.name}  ·  {decoded.width} × {decoded.height}{suffix}")
        self.image_view.set_pixmap(self._pixmap, smooth=False)
        self._schedule_smooth_fit()

    @property
    def is_fallback(self) -> bool:
        return self._is_fallback

    @property
    def has_image(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.image_view.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in {Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.exitRequested.emit()
        elif key == Qt.Key.Key_Down and self.series_panel.isVisible():
            self._move_series(1)
        elif key == Qt.Key.Key_Up and self.series_panel.isVisible():
            self._move_series(-1)
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
    faceRequested = Signal(object)

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
                self.faceRequested.emit(self._faces[hit])
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


class Workspace(QMainWindow):
    fullViewRequested = Signal(object)
    fullscreenRequested = Signal(object)
    gridRequested = Signal()

    def __init__(self, initial_directory: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("RAWww")
        self.resize(1440, 920)
        self.closing = False

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
        self.quick_mark: tuple[str, object] = ("rating", 5)
        self.auto_advance = False
        self.face_reference: list[float] | None = None
        self.last_move_direction = 1
        self.settings = QSettings("RAWww", "RAWww")
        self.current_dir = initial_directory or self._initial_directory()
        thumbnail_size = max(0, min(2, self.settings.value("thumbnail_size", 1, int)))
        self.workspace_state = WorkspaceState(self.current_dir, thumbnail_size=thumbnail_size)
        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_watcher.directoryChanged.connect(self._folder_changed)
        self.folder_change_timer = QTimer(self)
        self.folder_change_timer.setSingleShot(True)
        self.folder_change_timer.timeout.connect(self._reload_changed_folder)
        self.current_path: Path | None = None
        self.folder_cache: FolderCache | None = None
        self.ai_pipeline = AiPipeline()
        self.ai_progress_total = 0
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
        self.full_view.ratingRequested.connect(self._set_selected_rating)
        self.full_view.colorRequested.connect(self._set_selected_color)
        self.full_view.faceRequested.connect(self._filter_face_from_full_view)
        self.full_view.quickMarkRequested.connect(self._apply_quick_mark)
        self.full_view.commentSubmitted.connect(self._save_full_comment)
        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.full_view)
        self.setCentralWidget(self.stack)

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

    def closeEvent(self, event) -> None:  # noqa: N802
        # Future callbacks run on worker threads. Mark shutdown before stopping
        # executors so a completed cache lookup cannot enqueue a decode into an
        # executor that has already been shut down.
        self.closing = True
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
        self.ai_pipeline.shutdown()
        super().closeEvent(event)

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
        self.dir_tree.setEditTriggers(QTreeView.EditTrigger.DoubleClicked | QTreeView.EditTrigger.EditKeyPressed)
        self._set_tree_root_for_path(self.current_dir.anchor or QDir.rootPath())
        for column in range(1, self.dir_model.columnCount()):
            self.dir_tree.hideColumn(column)
        self.dir_tree.clicked.connect(self._directory_selected)
        # Hide expand arrows on folders that don't have any subfolders
        def update_node_flags():
            root = self.dir_tree.rootIndex()
            rows = self.dir_model.rowCount(root)
            # BFS through all items to check if they have subdirectories
            from collections import deque
            queue = deque()
            for i in range(rows):
                queue.append(self.dir_model.index(i, 0, root))
            while queue:
                index = queue.popleft()
                path = self.dir_model.filePath(index)
                qdir = QDir(path)
                subdirs = qdir.entryList(QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot)
                if len(subdirs) == 0:
                    # No subfolders, set flag to never have children (hides expand icon)
                    item_flags = self.dir_model.flags(index)
                    self.dir_model.setData(index, item_flags | Qt.ItemNeverHasChildren, Qt.ItemDataRole.EditRole)
                else:
                    # Add children to queue to check them
                    child_rows = self.dir_model.rowCount(index)
                    for i in range(child_rows):
                        queue.append(self.dir_model.index(i, 0, index))
        # Refresh flags when the tree expands
        self.dir_tree.expanded.connect(update_node_flags)
        # Update once at startup
        QTimer.singleShot(100, update_node_flags)
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
        self._refresh_volume_buttons()

        # Создаем тулбар с кнопками навигации над деревом папок
        folder_toolbar = QWidget()
        folder_toolbar_layout = QHBoxLayout(folder_toolbar)
        folder_toolbar_layout.setContentsMargins(0, 0, 0, 8)
        folder_toolbar_layout.setSpacing(4)
        
        # Кнопка "На уровень вверх"
        self.up_button = QToolButton()
        self.up_button.setIcon(qApp.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent))
        self.up_button.setToolTip("На уровень вверх")
        self.up_button.clicked.connect(self._go_up_directory)
        folder_toolbar_layout.addWidget(self.up_button)
        
        # Кнопка "Создать папку"
        self.new_folder_button = QToolButton()
        self.new_folder_button.setIcon(qApp.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        self.new_folder_button.setToolTip("Создать папку")
        self.new_folder_button.clicked.connect(self._create_new_folder)
        folder_toolbar_layout.addWidget(self.new_folder_button)
        
        # Выравниваем кнопки по левому краю
        folder_toolbar_layout.addStretch()
        
        sidebar_layout.addWidget(folder_toolbar)
        sidebar_layout.addWidget(self.dir_tree, 1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("viewerToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(7)

        
        self.rating_filter = QComboBox()
        self.rating_filter.addItem("Все рейтинги", None)
        for rating in range(5, 0, -1):
            self.rating_filter.addItem(f"{rating} ★", rating)
        self.rating_filter.setItemIcon(0, _fomantic_icon("star", 12, "#a8b0bd"))
        self.color_filter = QComboBox()
        for label, value in (("Все цвета", None), ("Без цвета", ""), ("Красный", "red"), ("Жёлтый", "yellow"), ("Зелёный", "green"), ("Синий", "blue"), ("Фиолетовый", "purple")):
            self.color_filter.addItem(label, value)
        self.color_filter.setItemIcon(0, _fomantic_icon("brush", 12, "#a8b0bd"))
        self.shot_filter = QComboBox()
        for label, value in (("Все планы", None), ("Крупный", "closeup"), ("Средний", "medium"), ("Общий", "wide"), ("Без лиц", "no_face")):
            self.shot_filter.addItem(label, value)
        self.shot_filter.hide()
        self.sort_combo = QComboBox()
        for label, value in (("По имени ↑", "name"), ("По имени ↓", "name_desc"), ("По времени ↑", "time"), ("По времени ↓", "time_desc"), ("По рейтингу", "rating")):
            self.sort_combo.addItem(label, value)
        self.sort_combo.setItemIcon(0, _fomantic_icon("sort", 12, "#a8b0bd"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по имени или комментарию")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.addAction(_fomantic_icon("search", 13, "#a8b0bd"), QLineEdit.ActionPosition.LeadingPosition)
        for control in (self.rating_filter, self.color_filter, self.shot_filter, self.sort_combo):
            control.currentIndexChanged.connect(self._apply_view)
            toolbar_layout.addWidget(control)
        self.search_edit.textChanged.connect(self._apply_view)
        toolbar_layout.addWidget(self.search_edit, 1)
        self.ai_button = QPushButton("Обработать новые фото")
        self.ai_button.clicked.connect(self._start_ai_analysis)
        toolbar_layout.addWidget(self.ai_button)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 1)
        self.ai_progress.setValue(0)
        self.ai_progress.setFormat("AI не запускался")
        self.ai_progress.setFixedWidth(150)
        toolbar_layout.addWidget(self.ai_progress)
        self.face_search_button = QPushButton("Найти лицо")
        self.face_search_button.setToolTip("Найти фото с лицом из выбранной карточки")
        self.face_search_button.clicked.connect(self._search_selected_face)
        self.face_search_button.hide()
        toolbar_layout.addWidget(self.face_search_button)
        self.face_clear_button = QToolButton()
        self.face_clear_button.setText("×")
        self.face_clear_button.setToolTip("Сбросить поиск по лицу")
        self.face_clear_button.setEnabled(False)
        self.face_clear_button.clicked.connect(self._clear_face_search)
        self.face_clear_button.hide()
        self.face_clear_button.setIcon(_fomantic_icon("close", 12))
        self.face_clear_button.setText("")
        toolbar_layout.addWidget(self.face_clear_button)
        for icon, delta in (("zoom-out", -1), ("zoom", 1)):
            button = QToolButton()
            button.setIcon(_fomantic_icon(icon, 13))
            button.setToolTip("Размер превью")
            button.clicked.connect(lambda _checked=False, d=delta: self.grid.change_card_size(d))
            toolbar_layout.addWidget(button)

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
        self.series_toggle.toggled.connect(self._apply_view)
        series_faces_layout.addWidget(self.series_toggle)
        self.faces_panel_button = QToolButton()
        self.faces_panel_button.setObjectName("aiFilter")
        self.faces_panel_button.setIcon(_fomantic_icon("user", 13))
        self.faces_panel_button.setText("Лица")
        self.faces_panel_button.clicked.connect(self._search_selected_face)
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

        meta = QWidget()
        meta.setObjectName("viewerMeta")
        meta_layout = QHBoxLayout(meta)
        meta_layout.setContentsMargins(10, 7, 10, 7)
        meta_layout.setSpacing(6)
        self.selection_label = QLabel("Не выбрано")
        meta_layout.addWidget(self.selection_label)
        self.quick_button = QPushButton("M  Быстрая: ★ 5")
        self.quick_button.setIcon(_fomantic_icon("bookmark", 13))
        self.quick_button.clicked.connect(self._apply_quick_mark)
        meta_layout.addWidget(self.quick_button)
        self.auto_button = QPushButton("Автопереход")
        self.auto_button.setIcon(_fomantic_icon("step-forward", 13))
        self.auto_button.setCheckable(True)
        self.auto_button.toggled.connect(lambda value: setattr(self, "auto_advance", value))
        meta_layout.addWidget(self.auto_button)
        for color, title in (("", ""), ("red", ""), ("yellow", ""), ("green", ""), ("blue", ""), ("purple", "")):
            button = QToolButton()
            button.setText(title)
            if not color:
                button.setIcon(_fomantic_icon("ban", 11, "#959595"))
            button.setProperty("colorLabel", color or "none")
            button.setToolTip("Сбросить цвет" if not color else color)
            button.clicked.connect(lambda _checked=False, value=color: self._set_selected_color(value))
            meta_layout.addWidget(button)
        for rating in range(0, 6):
            button = QToolButton()
            button.setText("")
            button.setIcon(_fomantic_icon("ban" if rating == 0 else "star", 11, "#95866b"))
            button.setToolTip("Сбросить рейтинг" if rating == 0 else f"Рейтинг {rating}")
            button.clicked.connect(lambda _checked=False, value=rating: self._set_selected_rating(value or None))
            meta_layout.addWidget(button)
        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("Комментарий")
        self.comment_edit.editingFinished.connect(self._save_comment)
        meta_layout.addWidget(self.comment_edit, 1)

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
        escape.triggered.connect(self.show_grid)
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


    def _go_up_directory(self) -> None:
        """Перейти на уровень вверх от текущей директории"""
        if self.current_dir and self.current_dir.parent != self.current_dir:
            self.load_directory(self.current_dir.parent)
    
    def _create_new_folder(self) -> None:
        """Создать новую папку в текущей директории с inline-редактированием."""
        if not self.current_dir:
            return

        parent_path = str(self.current_dir)

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

            def begin_inline_rename(index) -> None:
                if not index.isValid():
                    return
                # The new folder may be inserted under a collapsed parent, so
                # make it visible before starting the editor.
                self.dir_tree.expand(index.parent())
                self.dir_tree.setCurrentIndex(index)
                self.dir_tree.scrollTo(index, QTreeView.ScrollHint.EnsureVisible)
                self.dir_tree.setFocus(Qt.FocusReason.OtherFocusReason)
                self.dir_tree.edit(index)

            # Слот для обработки добавления строк в модель
            def on_rows_inserted(parent, first, last):
                # Проверяем, что папка добавлена в нужную родительскую директорию
                if self.dir_model.filePath(parent) != parent_path:
                    return
                for row in range(first, last + 1):
                    new_index = self.dir_model.index(row, 0, parent)
                    # Убедимся, что это именно та папка, которую мы создали
                    if self.dir_model.filePath(new_index) == str(temp_path):
                        QTimer.singleShot(0, lambda idx=new_index: begin_inline_rename(idx))
                        # Отключаем сигнал после использования
                        self.dir_model.rowsInserted.disconnect(on_rows_inserted)
                        break

            # Подключаем сигнал
            self.dir_model.rowsInserted.connect(on_rows_inserted)

            # Создаем папку на диске, что вызовет обновление модели и сигнал rowsInserted
            temp_path.mkdir()

        except OSError as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать папку: {e}")
            self.dir_model._new_folder_path = None # Очищаем в случае ошибки
            # Если сигнал был подключен, лучше его отключить
            if 'on_rows_inserted' in locals() and self.dir_model.receivers(self.dir_model.rowsInserted) > 0:
                    self.dir_model.rowsInserted.disconnect(on_rows_inserted)
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
            button.setChecked(button.property("volumeKey") == _drive_key(current_root) if current_root else False)

        # If removable media containing the open folder was unplugged, return
        # to a valid local location instead of retaining a dead tree root.
        if not self.closing and not self.current_dir.is_dir():
            fallback = Path.home()
            self._set_tree_root_for_path(fallback)
            self.load_directory(fallback)

    def _drive_selected(self, drive_path: Path) -> None:
        if drive_path.is_dir():
            self._set_tree_root_for_path(drive_path)
            self.load_directory(drive_path)

    def _set_tree_root_for_path(self, path: str | Path) -> None:
        path_text = str(path)
        model_index = self.dir_model.index(path_text)
        if model_index.isValid():
            self.dir_tree.setRootIndex(model_index)
            root = _volume_root_for_path(Path(path_text), _mounted_volume_paths())
            if root is not None and hasattr(self, "drive_buttons"):
                root_key = _drive_key(root)
                for button in self.drive_buttons.buttons():
                    button.setChecked(button.property("volumeKey") == root_key)

    def load_directory(self, directory: Path) -> None:
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
        if hasattr(self, "ai_button"):
            self.ai_button.setEnabled(False)
            self.ai_progress.setRange(0, 1)
            self.ai_progress.setValue(0)
            self.ai_progress.setFormat("Открытие папки…")
        self.cache_load_generation += 1
        self.directory_generation += 1
        self._flush_folder_cache(wait=False, close=True)
        self.folder_cache = None
        self.cache_ready = False
        self.current_dir = directory
        self.settings.setValue("last_directory", str(directory))
        self.setWindowTitle(_workspace_title(directory))
        self.all_paths = []
        self.view_paths = []
        self.paths = []
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

    def _directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        if self.closing:
            return
        self.bridge.directoryScanned.emit(request, directory, future)

    def _on_directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        if self.closing or not self.workspace_state.accepts(request):
            return
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
            self.view_generation += 1
        except Exception as exc:
            self.bridge.failed.emit(str(directory), str(exc))
            self.all_paths = []
            self.view_paths = []
            self.paths = []
        self.folder_cache = FolderCache(
            directory,
            {path.name for path in self.paths},
            eager_variants={THUMB_SIZE},
            load_from_disk=False,
        )
        self.cache_ready = False
        generation = self.cache_load_generation
        cache = self.folder_cache
        future = self.cache_load_executor.submit(cache.load_from_disk)
        future.add_done_callback(lambda done, g=generation: self._cache_loaded(g, done))
        self.items_by_path.clear()
        self.grid.clear()
        self.populate_index = 0
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self.visible_thumb_pending.clear()
        self.populate_timer.start()
        # Do not decode the first files before the in-memory cache has been
        # deserialized.  Otherwise the sequential queue races cached previews.

    def _populate_next_items(self) -> None:
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
        if self.populate_index >= len(self.paths):
            self.populate_timer.stop()

    def _submit_next_thumbs(self) -> None:
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
            self._submit_decode(path, THUMB_SIZE, full_priority=False, visible_priority=visible_priority)
            submitted += 1
        if self.thumb_index >= len(self.paths) and not self.thumb_priority:
            self.thumb_timer.stop()

    def _schedule_visible_thumb_priority(self) -> None:
        if not self.visible_thumb_timer.isActive():
            self.visible_thumb_timer.start(0)

    def _start_ai_analysis(self) -> None:
        if self.closing or not self.cache_ready or self.folder_cache is None:
            return
        embedding_missing = self.folder_cache.missing_ai_paths(self.view_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.view_paths, "face_analysis")
        self.ai_progress_total = len(set(embedding_missing) | set(face_missing))
        if not self.ai_progress_total:
            self.ai_progress.setRange(0, 1)
            self.ai_progress.setValue(1)
            self.ai_progress.setFormat("Анализ уже готов")
            return
        self.ai_button.setEnabled(False)
        self.ai_progress.setRange(0, self.ai_progress_total)
        self.ai_progress.setValue(0)
        self.ai_progress.setFormat("Анализ: %v / %m")
        self.ai_pipeline.scan(self.view_paths, self.folder_cache, self._background_decode_executor())
        self.ai_progress_timer.start()

    def _update_ai_progress(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            self.ai_progress_timer.stop()
            return
        embedding_missing = self.folder_cache.missing_ai_paths(self.view_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.view_paths, "face_analysis")
        remaining = len(set(embedding_missing) | set(face_missing))
        completed = max(0, self.ai_progress_total - remaining)
        self.ai_progress.setValue(completed)
        if self.ai_pipeline.pending_count() == 0:
            self.ai_progress_timer.stop()
            self.ai_pipeline.release_analysis_workers()
            self._reload_photo_details()
            self.ai_button.setEnabled(remaining > 0)
            if remaining:
                self.ai_progress.setFormat(f"Готово {completed}/{self.ai_progress_total}, ошибок: {remaining}")
            else:
                self.ai_progress.setFormat("Анализ завершён")

    def _folder_changed(self, path: str) -> None:
        if not self.closing and Path(path) == self.current_dir:
            self.folder_change_timer.start(FOLDER_CHANGE_DEBOUNCE_MS)

    def _reload_changed_folder(self) -> None:
        if not self.closing and self.current_dir.is_dir():
            self.load_directory(self.current_dir)

    def _refresh_ai_status(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            return
        embedding_missing = self.folder_cache.missing_ai_paths(self.view_paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.view_paths, "face_analysis")
        waiting = len(set(embedding_missing) | set(face_missing))
        self.ai_button.setEnabled(waiting > 0 and self.ai_pipeline.pending_count() == 0)
        self.ai_progress.setRange(0, 1)
        self.ai_progress.setValue(0 if waiting else 1)
        self.ai_progress.setFormat(f"Ожидают анализа: {waiting}" if waiting else "Все фото обработаны")

    def _reload_photo_details(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            return
        self.photo_details = self.folder_cache.load_photo_details()
        self.image_embeddings = self.folder_cache.load_image_embeddings()
        for path, item in self.items_by_path.items():
            item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        self.grid.viewport().update()
        self._update_analysis_controls()
        self._apply_view()

    def _update_analysis_controls(self) -> None:
        if not hasattr(self, "face_search_button"):
            return
        has_faces = any(detail.get("faces") for detail in self.photo_details.values())
        has_series = self._has_available_series(self.view_paths or self.all_paths)
        self.face_search_button.setVisible(has_faces)
        self.face_clear_button.setVisible(has_faces and self.face_reference is not None)
        if hasattr(self, "ai_panel"):
            self.ai_panel.setVisible(has_faces or has_series)
            self.series_faces_group.setVisible(has_faces or has_series)
            self.series_toggle.setVisible(has_series)
            self.faces_panel_button.setVisible(has_faces)
            self.shot_group.setVisible(has_faces)
            counts = {value: 0 for value in self.shot_buttons}
            for detail in self.photo_details.values():
                counts[self._shot_size(detail)] = counts.get(self._shot_size(detail), 0) + 1
            for value, button in self.shot_buttons.items():
                button.setChecked(self.shot_filter.currentData() == value)
                count = len(self.all_paths) if value is None else counts.get(value, 0)
                label = button.property("shotLabel") or button.text().split("  ")[0]
                button.setProperty("shotLabel", label)
                button.setText(f"{label}  {count}")

    def _set_shot_filter(self, value: str | None) -> None:
        index = self.shot_filter.findData(value)
        if index >= 0:
            self.shot_filter.setCurrentIndex(index)

    def _has_available_series(self, paths: list[Path]) -> bool:
        return any(
            self._embedding_similarity(left, right) >= 0.92
            for left, right in zip(paths, paths[1:])
        )

    def _prioritize_visible_thumbs(self) -> None:
        if self.folder_cache is None or not self.cache_ready or self.grid.count() == 0:
            return
        cell = self.grid.gridSize()
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
            self.photo_details = self.folder_cache.load_photo_details()
            self.image_embeddings = self.folder_cache.load_image_embeddings()
            for path, item in self.items_by_path.items():
                item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        self._update_analysis_controls()
        self._refresh_ai_status()
        if self.folder_cache is not None:
            self.ai_pipeline.scan_exif(self.view_paths, self.folder_cache, self._background_decode_executor())
        self.thumb_index = 0
        self._schedule_visible_thumb_priority()
        self.thumb_timer.start()

    def _apply_view(self, *_args) -> None:
        if not hasattr(self, "rating_filter"):
            return
        rating = self.rating_filter.currentData()
        color = self.color_filter.currentData()
        shot = self.shot_filter.currentData()
        needle = self.search_edit.text().strip().casefold()

        def visible(path: Path) -> bool:
            # Always keep directories - never filter out folders, no matter what
            if path.is_dir():
                return True
            # All filters only apply to actual image files
            detail = self.photo_details.get(path.name, {})
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
        result: list[Path] = []
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

    def _selected_paths(self) -> list[Path]:
        return [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.grid.selectedItems()]

    def _selection_changed(self) -> None:
        selected = self._selected_paths()
        self.selection_label.setText(f"Выбрано: {len(selected)}" if selected else f"Фото: {len(self.paths)}")
        if len(selected) == 1:
            self.comment_edit.setText(str(self.photo_details.get(selected[0].name, {}).get("comment", "")))
        elif not selected:
            self.comment_edit.clear()

    def _update_selection(self, **changes) -> None:
        paths = self._selected_paths()
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            # Grid selection may belong to a previously opened card. In photo
            # mode a mark must never silently land on that stale selection.
            if self.current_path not in paths:
                paths = [self.current_path]
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
        self.grid.viewport().update()
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            self.full_view.set_metadata(
                self.photo_details.get(self.current_path.name, {}),
                (self.current_path, *self._series_for_path(self.current_path)[:1]),
            )
            self._refresh_full_view_navigation(self.current_path)
        if self.auto_advance and len(paths) == 1:
            if self.stack.currentWidget() is self.full_view:
                self._move(1)
            else:
                item = self.items_by_path.get(paths[0])
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

    def _apply_quick_mark(self) -> None:
        kind, value = self.quick_mark
        paths = self._selected_paths()
        if self.current_path is not None and self.stack.currentWidget() is self.full_view and self.current_path not in paths:
            paths = [self.current_path]
        if not paths:
            return
        current = self.photo_details.get(paths[0].name, {}).get(kind)
        self._update_selection(**{kind: None if current == value else value})

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

    def _search_selected_face(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        faces = self.photo_details.get(paths[0].name, {}).get("faces") or []
        if not faces:
            self.selection_label.setText("На выбранном фото лица не найдены")
            return
        best = max(faces, key=lambda face: float(face.get("confidence", 0)))
        embedding = best.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            return
        self._set_face_reference(embedding)

    def _filter_face_from_full_view(self, face: object) -> None:
        embedding = face.get("embedding") if isinstance(face, dict) else None
        if isinstance(embedding, list) and embedding:
            self._set_face_reference(embedding)

    def _set_face_reference(self, embedding: list[float]) -> None:
        self.face_reference = embedding
        self.face_clear_button.setEnabled(True)
        self.face_clear_button.setVisible(True)
        self._apply_view()

    def _clear_face_search(self) -> None:
        self.face_reference = None
        self.face_clear_button.setEnabled(False)
        self._update_analysis_controls()
        self._apply_view()

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
        item = self.items_by_path.get(decoded.path)
        if max_size == THUMB_SIZE:
            self._thumbnail_cache_put(decoded.path, decoded.image)
            if item is not None:
                item.setData(PREVIEW_ROLE, decoded.image)
                self.grid.update(self.grid.visualItemRect(item))
            self.full_view.update_preview(decoded.path, decoded.image)
        if self.stack.currentWidget() is self.full_view and decoded.path == self.current_path:
            if max_size > THUMB_SIZE or not self.full_view.has_image or self.full_view.is_fallback:
                self.full_view.set_image(decoded, fallback=max_size == THUMB_SIZE)
        if max_size > THUMB_SIZE and decoded.path == self.current_path:
            self.thumb_timer.start()

    def _on_decode_failed(self, path: str, message: str) -> None:
        self.visible_thumb_pending.discard((Path(path), THUMB_SIZE))
        item = self.items_by_path.get(Path(path))
        if item is not None:
            item.setText(f"{Path(path).name}\n{message}")
        if Path(path) == self.current_path:
            self.thumb_timer.start()

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
            (path, *self._series_for_path(path)[:1]),
        )
        self.stack.setCurrentWidget(self.full_view)
        self._refresh_full_view_navigation(path)
        self.fullViewRequested.emit(self)
        self.full_view.setFocus(Qt.FocusReason.OtherFocusReason)
        full_size = self._full_preview_size()
        self._suspend_thumbnail_work()
        self._cancel_outdated_full_tasks(path, full_size)
        self._show_best_cached_full(path, full_size)
        if rapid_navigation:
            self.pending_full_request = path
            self.full_request_timer.start(55)
        else:
            self.pending_full_request = None
            self._promote_current_full_task(path, full_size)
            self._submit_decode(path, full_size, full_priority=True)
            self._preload_neighbors(path)

    def show_grid(self) -> None:
        self.stack.setCurrentWidget(self.grid_page)
        self._restore_grid_context()
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
        self.full_view.set_navigation(strip_paths, strip_current, self.photo_details, previews, series, self.view_generation)
        self._prioritize_full_strip_thumbs(current, strip_paths, series)

    def _prioritize_full_strip_thumbs(self, current: Path, strip_paths: list[Path], series: list[Path]) -> None:
        """Use the existing grid thumbnail queue for the currently useful strips."""
        if not self.cache_ready:
            return
        try:
            index = strip_paths.index(current)
        except ValueError:
            index = 0
        nearby = [*series, *strip_paths[max(0, index - 4) : index + 5]]
        for path in reversed(list(dict.fromkeys(nearby))):
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
        """Collapse each adjacent CLIP series to its leading photograph. Exclude folders from strip."""
        # Filter out directories - only show actual image files in the viewer strip
        image_only_paths = [p for p in self.view_paths if p.is_file()]
        if not self.series_toggle.isChecked():
            return list(image_only_paths)
        result: list[Path] = []
        previous: Path | None = None
        for path in image_only_paths:
            if previous is None or self._embedding_similarity(previous, path) < 0.92:
                result.append(path)
            previous = path
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
        navigation_paths = self._photo_mode_paths()
        if path not in navigation_paths:
            path = self._series_for_path(path)[0]
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

    def _cancel_outdated_full_tasks(self, path: Path, full_size: int) -> None:
        keep = {path}
        navigation_paths = self._photo_mode_paths()
        if path not in navigation_paths:
            path = self._series_for_path(path)[0]
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
        self.tabs.setCurrentIndex(index)
        self._update_tab_geometry()

    def _select_workspace(self, index: int) -> None:
        if index >= 0:
            self.workspace_stack.setCurrentIndex(index)

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
            font-family: "Lato", "Segoe UI";
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
            width: 12px;
            height: 12px;
        }
        QScrollBar::handle {
            background: #555555;
            border-radius: 3px;
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
        QWidget#viewerToolbar QComboBox:hover, QWidget#viewerToolbar QPushButton:hover,
        QWidget#viewerToolbar QToolButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #606060, stop:1 #4b4b4b);
            border-color: #707070;
        }
        QWidget#viewerAiPanel {
            min-height: 36px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #292929, stop:1 #222222);
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
            min-height: 42px;
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #303030, stop:1 #222222);
            border-top: 1px solid #111111;
        }
        QWidget#viewerMeta QPushButton, QWidget#viewerMeta QToolButton {
            min-height: 25px;
            color: #c5c5c5;
            background: #3c3c3c;
            border: 1px solid #171717;
            border-radius: 2px;
            padding: 2px 7px;
        }
        QWidget#viewerMeta QPushButton:hover, QWidget#viewerMeta QToolButton:hover {
            background: #505050;
            color: #f4f4f4;
        }
        QWidget#viewerMeta QLineEdit {
            min-height: 25px;
            background: #202020;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 2px;
            padding: 2px 8px;
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
            min-height: 29px;
            background: transparent;
        }
        QToolButton#stripToggle {
            min-width: 46px;
            max-width: 46px;
            min-height: 20px;
            max-height: 20px;
            border: 1px solid #333333;
            border-radius: 8px;
            background: #181818;
            color: #f4f4f5;
            font-size: 13px;
        }
        QToolButton#stripToggle:hover { background: #242424; }
        QToolButton#fullQuickMark {
            min-width: 28px;
            max-width: 28px;
            min-height: 22px;
            max-height: 22px;
            border: 1px solid #1a1a1a;
            border-radius: 3px;
            background: #3c3c3c;
        }
        QToolButton#fullQuickMark:hover { background: #505050; }
        QLineEdit#fullComment {
            min-width: 180px;
            max-width: 360px;
            min-height: 24px;
            background: #202020;
            color: #e1e1e1;
            border: 1px solid #111111;
            border-radius: 2px;
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
            min-width: 90px;
            max-width: 90px;
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
        QToolButton#viewerColor, QToolButton#viewerRating {
            min-width: 18px;
            max-width: 18px;
            min-height: 18px;
            max-height: 18px;
            padding: 0;
            border: 1px solid #1a1a1a;
            border-radius: 2px;
            background: #4e4e4e;
            color: #b8b8b8;
            font-size: 10px;
        }
        QToolButton#viewerColor[colorLabel="red"] { background: #7a5555; }
        QToolButton#viewerColor[colorLabel="yellow"] { background: #7f7556; }
        QToolButton#viewerColor[colorLabel="green"] { background: #5d7560; }
        QToolButton#viewerColor[colorLabel="blue"] { background: #596b82; }
        QToolButton#viewerColor[colorLabel="purple"] { background: #71607d; }
        QToolButton#viewerRating { color: #95866b; background: #363636; }
        QToolButton#viewerColor:hover, QToolButton#viewerRating:hover {
            border-color: #d6d6d6;
        }
        QToolButton#viewerColor:checked {
            border-color: #f1f1f1;
            box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.52);
        }
        QToolButton#viewerRating:checked {
            color: #f1c453;
            background: #4a4538;
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
            if entry.is_dir() or (entry.is_file() and is_supported_image(entry)):
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
    visible = paths if predicate is None else [path for path in paths if predicate(path)]
    key = sort_key or (lambda path: path.name.lower())
    return sorted(visible, key=key, reverse=reverse)


def _flush_and_close(cache: FolderCache, close: bool) -> None:
    try:
        cache.flush()
    finally:
        if close:
            cache.close(flush=False)


def main() -> None:
    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())
