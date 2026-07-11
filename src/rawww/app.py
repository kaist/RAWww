from __future__ import annotations

import os
import sys
from collections import OrderedDict, deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from typing import Callable

from PySide6.QtCore import QDir, QFileSystemWatcher, QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileSystemModel,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QStyle,
    QSplitter,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .cache import FolderCache
from .ai import AiPipeline
from .imaging import DecodedImage, PixelImage, decode_pixels, is_supported_image, pixel_to_decoded


THUMB_SIZE = 256
CARD_MIN_WIDTH = 150
CARD_TARGET_WIDTH = 200
CARD_MAX_WIDTH = 280
CARD_ASPECT = 3 / 2
RAM_CACHE_LIMIT = 96
FULL_PRELOAD_RADIUS = 10
FULL_RAM_CACHE_LIMIT = FULL_PRELOAD_RADIUS * 2 + 1
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
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


class DecodeBridge(QObject):
    decoded = Signal(object)
    failed = Signal(str, str)
    cacheLoaded = Signal(int, object)
    directoryScanned = Signal(int, Path, object)


class PhotoGrid(QListWidget):
    openRequested = Signal(Path)
    viewportChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._last_icon_size = QSize()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSpacing(10)
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

    def _update_card_size(self) -> None:
        available = max(CARD_MIN_WIDTH, self.viewport().width() - 28)
        columns = max(1, round(available / CARD_TARGET_WIDTH))
        width = (available - ((columns - 1) * self.spacing())) // columns
        width = max(CARD_MIN_WIDTH, min(CARD_MAX_WIDTH, width))
        height = int(width / CARD_ASPECT)
        icon_size = QSize(width, height)
        if icon_size == self._last_icon_size:
            return
        self._last_icon_size = icon_size
        self.setIconSize(icon_size)
        self.setGridSize(QSize(width + 22, height + 48))


class PhotoCardDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        rect = option.rect.adjusted(5, 5, -5, -5)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        bg = QColor("#3f6db5") if selected else QColor("#303030" if hovered else "#292929")
        painter.fillRect(rect, bg)

        image_rect = QRect(rect.left() + 8, rect.top() + 8, rect.width() - 16, int((rect.width() - 16) / CARD_ASPECT))
        painter.fillRect(image_rect, QColor("#1d1d1d"))

        pixmap = index.data(PREVIEW_ROLE)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            scaled = pixmap.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target = QRect(
                image_rect.left() + (image_rect.width() - scaled.width()) // 2,
                image_rect.top() + (image_rect.height() - scaled.height()) // 2,
                scaled.width(),
                scaled.height(),
            )
            painter.drawPixmap(target, scaled)

        text_rect = QRect(rect.left() + 8, image_rect.bottom() + 7, rect.width() - 16, 20)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = QColor("#ffffff") if selected else QColor("#d8d8d8")
        painter.setPen(color)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return option.widget.gridSize() if isinstance(option.widget, QListWidget) else super().sizeHint(option, index)


class FullView(QFrame):
    exitRequested = Signal()
    nextRequested = Signal()
    previousRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._path: Path | None = None
        self._is_fallback = False
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setSingleShot(True)
        self._smooth_timer.timeout.connect(self._smooth_fit)

        self.image_view = FullImageView()

        self.info_label = QLabel()
        self.info_label.setObjectName("overlayLabel")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.info_label)
        layout.addWidget(self.image_view, 1)

    def set_image(self, decoded: DecodedImage, *, fallback: bool = False) -> None:
        self._path = decoded.path
        self._is_fallback = fallback
        self._pixmap = QPixmap.fromImage(decoded.image)
        suffix = "  -  preview" if fallback else ""
        self.info_label.setText(f"  {decoded.path.name}  -  {decoded.width} x {decoded.height}{suffix}")
        self.image_view.set_pixmap(self._pixmap, smooth=False)
        self._schedule_smooth_fit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.image_view.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in {Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.exitRequested.emit()
        elif key in {Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space}:
            self.nextRequested.emit()
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Backspace}:
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


class FullImageView(QOpenGLWidget):
    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._smooth = False
        self.setMinimumSize(1, 1)
        self.setAutoFillBackground(False)

    def set_pixmap(self, pixmap: QPixmap, *, smooth: bool) -> None:
        self._pixmap = pixmap
        self._smooth = smooth
        self.update()

    def set_smooth(self, smooth: bool) -> None:
        if self._smooth == smooth:
            return
        self._smooth = smooth
        self.update()

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
        painter.end()


def _fit_rect(source: QSize, bounds: QSize) -> QRect:
    if source.width() <= 0 or source.height() <= 0 or bounds.width() <= 0 or bounds.height() <= 0:
        return QRect()
    scale = min(bounds.width() / source.width(), bounds.height() / source.height())
    width = max(1, round(source.width() * scale))
    height = max(1, round(source.height() * scale))
    return QRect((bounds.width() - width) // 2, (bounds.height() - height) // 2, width, height)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
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
        self.items_by_path: dict[Path, QListWidgetItem] = {}
        self.all_paths: list[Path] = []
        self.paths: list[Path] = []
        self.last_move_direction = 1
        self.settings = QSettings("RAWww", "RAWww")
        self.current_dir = self._initial_directory()
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
        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.full_view)
        self.setCentralWidget(self.stack)

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
        self.flush_timer.stop()
        self.full_request_timer.stop()
        self.grid_full_request_timer.stop()
        self.populate_timer.stop()
        self.thumb_timer.stop()
        self.ai_progress_timer.stop()
        self.folder_change_timer.stop()
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
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.dir_model = QFileSystemModel(self)
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath(QDir.rootPath())

        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumHeight(30)
        for root in QDir.drives():
            drive_path = root.absolutePath()
            self.drive_combo.addItem(drive_path, drive_path)
        self.drive_combo.currentIndexChanged.connect(self._drive_selected)

        self.dir_tree = QTreeView()
        self.dir_tree.setModel(self.dir_model)
        self._set_tree_root_for_path(self.current_dir.anchor or QDir.rootPath())
        for column in range(1, self.dir_model.columnCount()):
            self.dir_tree.hideColumn(column)
        self.dir_tree.clicked.connect(self._directory_selected)
        self.dir_tree.setHeaderHidden(True)
        self.dir_tree.setMinimumWidth(260)

        self.grid = PhotoGrid()
        self.grid.openRequested.connect(self.open_full)
        self.grid.currentItemChanged.connect(self._grid_current_item_changed)
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_visible_thumb_priority)
        self.grid.viewportChanged.connect(self._schedule_visible_thumb_priority)

        splitter = QSplitter()
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(8)
        sidebar_layout.addWidget(self.drive_combo)
        self.ai_button = QPushButton("Обработать новые фото")
        self.ai_button.clicked.connect(self._start_ai_analysis)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 1)
        self.ai_progress.setValue(0)
        self.ai_progress.setFormat("AI не запускался")
        sidebar_layout.addWidget(self.ai_button)
        sidebar_layout.addWidget(self.ai_progress)
        sidebar_layout.addWidget(self.dir_tree, 1)

        splitter.addWidget(sidebar)
        splitter.addWidget(self.grid)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1120])
        layout.addWidget(splitter)
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

    def _directory_selected(self, index) -> None:
        path = Path(self.dir_model.filePath(index))
        self.load_directory(path)

    def _drive_selected(self, index: int) -> None:
        drive_path = self.drive_combo.itemData(index)
        if drive_path:
            self._set_tree_root_for_path(drive_path)
            self.load_directory(Path(drive_path))

    def _set_tree_root_for_path(self, path: str | Path) -> None:
        path_text = str(path)
        model_index = self.dir_model.index(path_text)
        if model_index.isValid():
            self.dir_tree.setRootIndex(model_index)
            drive_index = self.drive_combo.findData(_drive_key(Path(path_text)))
            if drive_index >= 0 and self.drive_combo.currentIndex() != drive_index:
                self.drive_combo.blockSignals(True)
                self.drive_combo.setCurrentIndex(drive_index)
                self.drive_combo.blockSignals(False)

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
        self.all_paths = []
        self.paths = []
        self.items_by_path.clear()
        self.grid.clear()
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self.visible_thumb_pending.clear()
        generation = self.directory_generation
        future = self.directory_scan_executor.submit(_scan_directory, directory)
        future.add_done_callback(lambda done, g=generation, d=directory: self._directory_scanned(g, d, done))

    def _directory_scanned(self, generation: int, directory: Path, future: Future) -> None:
        if self.closing:
            return
        self.bridge.directoryScanned.emit(generation, directory, future)

    def _on_directory_scanned(self, generation: int, directory: Path, future: Future) -> None:
        if self.closing or generation != self.directory_generation:
            return
        try:
            self.all_paths = future.result()
            self.paths = _build_photo_view(self.all_paths)
        except Exception as exc:
            self.bridge.failed.emit(str(directory), str(exc))
            self.all_paths = []
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
            self.grid.addItem(item)
            self.items_by_path[path] = item
        self.populate_index = end
        self._schedule_visible_thumb_priority()
        if self.populate_index >= len(self.paths):
            self.populate_timer.stop()

    def _submit_next_thumbs(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
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
        embedding_missing = self.folder_cache.missing_ai_paths(self.paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.paths, "face_analysis")
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
        self.ai_pipeline.scan(self.paths, self.folder_cache, self._background_decode_executor())
        self.ai_progress_timer.start()

    def _update_ai_progress(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            self.ai_progress_timer.stop()
            return
        embedding_missing = self.folder_cache.missing_ai_paths(self.paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.paths, "face_analysis")
        remaining = len(set(embedding_missing) | set(face_missing))
        completed = max(0, self.ai_progress_total - remaining)
        self.ai_progress.setValue(completed)
        if self.ai_pipeline.pending_count() == 0:
            self.ai_progress_timer.stop()
            self.ai_pipeline.release_analysis_workers()
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
        embedding_missing = self.folder_cache.missing_ai_paths(self.paths, "image_embeddings")
        face_missing = self.folder_cache.missing_ai_paths(self.paths, "face_analysis")
        waiting = len(set(embedding_missing) | set(face_missing))
        self.ai_button.setEnabled(waiting > 0 and self.ai_pipeline.pending_count() == 0)
        self.ai_progress.setRange(0, 1)
        self.ai_progress.setValue(0 if waiting else 1)
        self.ai_progress.setFormat(f"Ожидают анализа: {waiting}" if waiting else "Все фото обработаны")

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
        self._refresh_ai_status()
        if self.folder_cache is not None:
            self.ai_pipeline.scan_exif(self.paths, self.folder_cache, self._background_decode_executor())
        self.thumb_index = 0
        self._schedule_visible_thumb_priority()
        self.thumb_timer.start()

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
            future = executor.submit(decode_pixels, path, max_size)
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
        if item is not None and max_size == THUMB_SIZE:
            item.setData(PREVIEW_ROLE, QPixmap.fromImage(decoded.image))
            self.grid.update(self.grid.visualItemRect(item))
        if self.stack.currentWidget() is self.full_view and decoded.path == self.current_path:
            self.full_view.set_image(decoded)

    def _on_decode_failed(self, path: str, message: str) -> None:
        self.visible_thumb_pending.discard((Path(path), THUMB_SIZE))
        item = self.items_by_path.get(Path(path))
        if item is not None:
            item.setText(f"{Path(path).name}\n{message}")

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
        self.pending_grid_full_request = path
        self.grid_full_request_timer.start(70)

    def open_full(self, path: Path) -> None:
        now = monotonic()
        rapid_navigation = now - self.last_navigation_at < 0.14
        self.last_navigation_at = now
        self.current_path = path
        self.stack.setCurrentWidget(self.full_view)
        self.full_view.setFocus(Qt.FocusReason.OtherFocusReason)
        full_size = self._full_preview_size()
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

    def toggle_fullscreen(self) -> None:
        if self.fast_fullscreen:
            self._leave_fast_fullscreen()
        else:
            self._enter_fast_fullscreen()

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
        if not self.current_path or self.current_path not in self.paths:
            return
        index = self.paths.index(self.current_path) + direction
        if 0 <= index < len(self.paths):
            self.open_full(self.paths[index])

    def _preload_neighbors(self, path: Path) -> None:
        if path not in self.paths:
            return
        index = self.paths.index(path)
        full_size = self._full_preview_size()
        before = list(reversed(self.paths[max(0, index - FULL_PRELOAD_RADIUS) : index]))
        after = self.paths[index + 1 : index + FULL_PRELOAD_RADIUS + 1]
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
        if path in self.paths:
            index = self.paths.index(path)
            keep.update(
                self.paths[
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


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QMainWindow, QWidget {
            background: #1f1f1f;
            color: #d6d6d6;
            font-family: "Segoe UI";
            font-size: 12px;
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
        """
    )


def _drive_key(path: Path) -> str:
    anchor = path.anchor or str(path)
    return anchor.replace("\\", "/")


def _scan_directory(directory: Path) -> list[Path]:
    try:
        return [entry for entry in directory.iterdir() if entry.is_file() and is_supported_image(entry)]
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
    window.show()
    sys.exit(app.exec())
