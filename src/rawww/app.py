## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Главное окно Ctrlka: интерфейс просмотра, каталогов и рабочих вкладок.

Здесь намеренно собрана Qt-обвязка приложения. Тяжёлая работа вынесена в
отдельные модули и фоновые исполнители, чтобы интерфейс не замирал в позе
задумчивого фотографа при открытии большой папки.
"""

from __future__ import annotations

import os
import sys
import signal
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
from threading import Event
from time import monotonic, sleep, time_ns
from typing import Callable

from send2trash import send2trash

from PySide6.QtCore import QBuffer, QDir, QEvent, QFileInfo, QFileSystemWatcher, QLibraryInfo, QPoint, QPointF, QRect, QRectF, QIODevice, QMimeData, QSettings, QSize, QSizeF, Qt, QTimer, QTranslator, Signal, QObject, QStorageInfo, QItemSelectionModel, QStandardPaths, QUrl, QStringListModel
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QDrag, QFont, QFontMetricsF, QGuiApplication, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygon, QTextCharFormat, QTextFormat, QTextObjectInterface, QWindow
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
    QProgressDialog,
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
from .error_log import install_error_logging
from .decode_scheduler import DecodeScheduler
from .shotsync_client import ShotSyncClient
from .face_sets_sync import merge_server_faces, upload_fields_for_entry
from .face_search import FACE_MATCH_THRESHOLD, FaceSearchIndex, indexed_face_matches
from .focus import focus_is_defect
from .shotsync_login import ShotSyncLoginDialog
from .shotsync_hub import shotsync_hub
from .shotsync_panel import ShotSyncPanel
from .shotsync_selection import SelectionMarkSyncer, selection_folder, selection_root
from .imaging import JPEG_EXTENSIONS, RAW_EXTENSIONS, DecodedImage, PixelImage, is_supported_image, is_supported_media, is_supported_video
from .launch import target_from_argv
from .runtime_paths import PORTABLE, data_path, filesystem_name_key, filesystem_path_key, work_path
from .single_instance import SingleInstance
from .process_guard import install_process_tree_guard
from .task_lifecycle import retire_executor, wait_for_retired_executors
from .telemetry import TelemetryClient
from .subprocess_utils import detached_process_kwargs
from .windows_shell_menu import show_file_context_menu
from .windows_activation import activate_foreground_window
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
from .transfer_queue import TransferEntry, TransferManager, TransferQueuePanel, TransferTask
from .card_import import CardImportScan, build_backup_entries, build_import_entries, merge_scans, scan_card
from .dialogs import (
    BatchRenameDialog,
    BatchResizeDialog,
    CardImportDialog,
    HelpDialog,
    QuickTransferDialog,
    SettingsDialog,
    ShrinkJpegDialog,
)
from .workspace import WorkspaceRequest, WorkspaceState
from .xmp import (
    XmpChangedError,
    XmpFields,
    XmpParseError,
    XmpReadResult,
    named_face_regions,
    read_sidecar,
    sidecar_path,
    update_sidecar,
)
from .updater import fetch_release_info, is_newer
from .version import __version__


THUMB_SIZE = 256
ORIGINAL_SIZE = 0
# Ниже этого eye aspect ratio глаз считается закрытым (порог подобран на кадрах:
# у открытых глаз EAR заметно выше, у закрытых — ниже).
EYES_OPEN_THRESHOLD = 0.25
CARD_HARD_MIN_WIDTH = 96
CARD_TARGET_WIDTH = 200
CARD_MAX_WIDTH = 280
CARD_SIZE_TARGETS = (120, 150, CARD_TARGET_WIDTH, 280)
CARD_ASPECT = 3 / 2
RAM_CACHE_LIMIT = 96
THUMBNAIL_RAM_CACHE_LIMIT_BYTES = 700 * 1024 * 1024
FULL_PRELOAD_RADIUS = 10
FULL_RAM_CACHE_LIMIT = FULL_PRELOAD_RADIUS * 2 + 1
FULL_STRIP_PAGE_SIZE = 160
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
DETAIL_ROLE = PREVIEW_ROLE + 1
SERIES_ROLE = DETAIL_ROLE + 1
CURRENT_DECODE_WORKERS = 2
BACKGROUND_DECODE_WORKERS = 3
VISIBLE_THUMB_DECODE_WORKERS = 1
MAX_PENDING_THUMBS = 8
VISIBLE_THUMB_LOOKUP_WORKERS = 2
MAX_VISIBLE_THUMB_PENDING = 1
POPULATE_BATCH = 48
THUMB_SUBMIT_BATCH = 8
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
        settings_path.mkdir(parents=True, exist_ok=True)
        return QSettings(
            str(settings_path / f"{SETTINGS_NAME}.ini"),
            QSettings.Format.IniFormat,
        )
    return QSettings(SETTINGS_NAME, SETTINGS_NAME)


def _resize_export_worker(job: tuple[str, str, int, bool, int, bool, float, int, int, bool, int]) -> tuple[str, str, str | None]:
    """Экспортирует JPEG в отдельном процессе.

    Для RAW сначала берём встроенное превью: для пакетной операции это обычно
    быстрее и бережнее к памяти, чем разворачивать весь исходник на каждом кадре.
    """
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
    """Пересохраняет JPEG с меньшим качеством, сохраняя профиль цвета и EXIF.

    Работа идёт через временный файл: фотография не должна исчезнуть из-за
    неудачного пересохранения — такие фокусы хороши только в плохих бэкапах.
    """
    source_text, quality, keep_exif = job
    source = Path(source_text)
    temporary = source.with_name(f".{source.stem}.{uuid4().hex}.tmp")
    try:
        from PIL import Image

        original_size = source.stat().st_size
        with Image.open(source) as opened:
            opened.load()
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
                options.pop("subsampling", None)
                image.save(temporary, **options)
        new_size = temporary.stat().st_size
        os.replace(temporary, source)
        return source_text, original_size, new_size, None
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return source_text, 0, 0, str(exc)


class DecodeBridge(QObject):
    """Переносит результаты фоновых задач обратно в главный поток Qt."""

    decoded = Signal(object)
    failed = Signal(str, str)
    cacheLoaded = Signal(int, object)
    aiCacheChecked = Signal(int, object)
    directoryScanned = Signal(object, Path, object)
    renameProgress = Signal(object, int, int)
    renameCacheUpdating = Signal(object)
    renameFinished = Signal(object, object, object)
    metadataUpdated = Signal(object)
    xmpWritten = Signal(object)
    xmpScanned = Signal(object)
    folderChecked = Signal(object)
    schedulerFinished = Signal(object)
    faceSearchFinished = Signal(object)


class AiProgressBar(QProgressBar):
    """Полоса AI с рисуемой внутри иконкой отмены, не зависящей от геометрии дочерних кнопок."""

    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancel_visible = False

    def set_cancel_visible(self, visible: bool) -> None:
        """Показывает крестик только пока AI-задача действительно выполняется."""
        if self._cancel_visible != visible:
            self._cancel_visible = visible
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._cancel_visible:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#d6d6d6"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center = QPointF(self.width() - 9, self.height() / 2)
        painter.drawLine(center + QPointF(-3.25, -3.25), center + QPointF(3.25, 3.25))
        painter.drawLine(center + QPointF(3.25, -3.25), center + QPointF(-3.25, 3.25))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            self._cancel_visible
            and event.button() == Qt.MouseButton.LeftButton
            and event.position().x() >= self.width() - 18
        ):
            self.cancelRequested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _write_xmp_task(
    path: Path, fields: dict, regions: list[dict], expected_digest: str | None,
):
    """Атомарно обновляет один общий sidecar вне главного потока."""
    return update_sidecar(
        path, XmpFields.from_dict(fields), regions, expected_digest=expected_digest
    )


def _scan_xmp_task(
    paths: list[Path], known: dict[str, tuple[int, int, str | None]],
    needed_missing: set[str], full_hash: bool,
) -> list[tuple[Path, object]]:
    """По stat отбрасывает прежние sidecar и разбирает только изменившиеся."""
    results = []
    entries: dict[str, os.stat_result] = {}
    if paths:
        try:
            with os.scandir(paths[0].parent) as iterator:
                for entry in iterator:
                    try:
                        if entry.name.casefold().endswith(".xmp") and entry.is_file(follow_symlinks=False):
                            entries[entry.name] = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
        except OSError:
            entries = {}
    for path in paths:
        try:
            previous = known.get(path.name)
            stat = entries.get(path.name)
            if stat is None and previous is None and path.name not in needed_missing:
                continue
            if stat is None:
                # Один scandir уже доказал отсутствие файла. Не создаём тысячи
                # заведомо неудачных open() при полном ручном перечитывании.
                results.append((path, XmpReadResult(
                    path, XmpFields(), None, 0, 0, False,
                )))
                continue
            if not full_hash and previous is not None:
                if stat is not None and (stat.st_size, stat.st_mtime_ns) == previous[:2]:
                    continue
            results.append((path, read_sidecar(path)))
        except (OSError, XmpParseError) as exc:
            results.append((path, exc))
    return results


def _plan_xmp_sidecar_relocation(directory: Path, names: dict[str, str]) -> dict[Path, tuple[Path, ...]]:
    """Строит безопасное соответствие sidecar после переименования фото и пар."""
    photos = []
    try:
        photos = [path for path in directory.iterdir() if path.is_file() and is_supported_image(path)]
    except OSError:
        return {}
    targets: dict[Path, set[Path]] = {}
    for photo in photos:
        old_sidecar = sidecar_path(photo)
        new_photo = photo.with_name(names.get(photo.name, photo.name))
        targets.setdefault(old_sidecar, set()).add(sidecar_path(new_photo))
    plan = {
        source: tuple(sorted(destinations, key=lambda item: item.name.casefold()))
        for source, destinations in targets.items()
        if source.exists() and destinations != {source}
    }
    owners: dict[Path, Path] = {}
    for source, destinations in plan.items():
        for destination in destinations:
            owner = owners.setdefault(destination, source)
            if owner != source:
                raise OSError(f"Несколько XMP должны получить имя «{destination.name}»")
            if destination.exists() and destination != source and destination not in plan:
                raise OSError(f"XMP «{destination.name}» уже существует")
    return plan


def _relocate_xmp_sidecars(plan: dict[Path, tuple[Path, ...]]) -> None:
    """Публикует копии XMP атомарно и удаляет источник только после успеха всех целей."""
    payloads = {source: source.read_bytes() for source in plan}
    for source, destinations in plan.items():
        payload = payloads[source]
        for destination in destinations:
            if destination == source:
                continue
            temporary = destination.with_name(f".{destination.name}.rawww-tmp")
            try:
                temporary.write_bytes(payload)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    for source, destinations in plan.items():
        if source not in destinations and source not in {
            destination for targets in plan.values() for destination in targets
        }:
            source.unlink()


def _load_cache(cache: FolderCache) -> None:
    """Открывает SQLite-кэш в фоне, чтобы сразу начать чтение миниатюр."""
    cache.load_from_disk()


def _store_xmp_cache_batch(cache: FolderCache, selections: list[dict], states: list[dict]) -> None:
    """Фиксирует пакет XMP в SQLite вне Qt-потока."""
    cache.store_xmp_batch(selections, states)


def _check_cached_ai(cache: FolderCache, paths: list[Path]) -> set[Path]:
    """Проверяет полноту AI-кэша после старта загрузки миниатюр."""
    embedding_missing = cache.missing_ai_paths(paths, "image_embeddings")
    face_missing = cache.missing_ai_paths(paths, "face_analysis")
    return set(embedding_missing) | set(face_missing)


class VideoThumbnailer(QObject):
    """Последовательно создаёт миниатюры видео средствами Qt Multimedia.

    ``QMediaPlayer`` и ``QVideoSink`` переиспользуются для всей рабочей вкладки:
    класс берёт следующий путь из очереди, ждёт пригодный кадр, уменьшает его и
    отдаёт через ``previewReady``. Одновременно обрабатывается только один файл.
    Это не просто экономия — асинхронный кадр от прошлого ролика иначе легко
    приезжает позже и притворяется миниатюрой следующего.

    Счётчик поколения отбрасывает запоздавшие события после отмены, а флаг
    активности останавливает работу у скрытых вкладок и во время просмотра
    видео. Сам миниатюрщик ничего не пишет в кэш: это забота ``Workspace``.
    """

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
        """Отменяет текущую и отложенную работу, не выключая миниатюрщик целиком."""
        self._queue.clear()
        self._queued.clear()
        self._reset_current()

    def _reset_current(self) -> None:
        self._generation += 1
        self._player.stop()
        self._current = None
        self._current_source = None
        self._ready_for_frame = False

    def _maybe_start(self) -> None:
        if self._current is not None:
            return
        if not self._active or not self._queue:
            return
        self._begin(self._queue.popleft())

    def _begin(self, path: Path) -> None:
        self._generation += 1
        self._current = path
        self._queued.discard(path)
        self._ready_for_frame = False
        self._current_source = QUrl.fromLocalFile(str(path))
        self._player.setSource(self._current_source)
        self._player.play()

    def _is_current_source(self) -> bool:
        return self._current is not None and self._player.source() == self._current_source

    def _media_status_changed(self, status) -> None:
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
    """Временный редактор имени папки прямо поверх дерева каталогов."""

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
    """Возвращает существующие локальные пути из перетаскивания, сохраняя порядок."""
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
    """Распознаёт отметку «вырезать» нашего приложения и стандартную отметку Проводника."""
    if mime.hasFormat("application/x-rawww-cut"):
        return True
    effect = bytes(mime.data(_PREFERRED_DROP_EFFECT_MIME))
    return bool(effect and int.from_bytes(effect[:4], "little") & 2)


class PhotoGrid(QListWidget):
    """Главная сетка фотографий и папок в рабочей вкладке.

    Класс отвечает не за данные каталога, а за их интерактивное представление:
    рассчитывает адаптивную ширину карточек, сообщает контроллеру о прокрутке,
    открытии и удалении, обрабатывает серии, аудиозаметки и перенос файлов.
    Сами операции с диском выполняет ``Workspace`` — сетка только формулирует
    намерение пользователя через сигналы. Иначе один неудачный drag-and-drop
    мог бы превратить виджет в маленький, но очень деятельный файловый менеджер.
    """

    openRequested = Signal(Path)
    viewportChanged = Signal()
    cardSizeChanged = Signal(int)
    seriesToggleRequested = Signal(Path)
    audioRequested = Signal(Path)
    audioHoverChanged = Signal(object)
    deleteRequested = Signal(bool)   # нажат ли Shift
    pathsDropped = Signal(object, object, object)   # пути, пункт назначения, действие
    orderDropped = Signal(object, object)  # переносимые пути, путь перед которым их вставить
    contextRequested = Signal(object, object)  # путь карточки, глобальная позиция меню

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("photoGrid")
        self._last_icon_size = QSize()
        self._last_grid_size = QSize()
        self._last_spacing = -1
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
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
        self.setUniformItemSizes(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSpacing(0)
        self.setItemDelegate(PhotoCardDelegate(self))
        self.itemActivated.connect(self._emit_open)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._emit_context_request)
        self.verticalScrollBar().rangeChanged.connect(self._queue_card_size_update)
        self._update_card_size()

    def _queue_card_size_update(self, _minimum: int, _maximum: int) -> None:
        """Пересчитывает карточки после того, как Qt окончательно уложит полосу прокрутки."""
        QTimer.singleShot(0, self._update_card_size)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Сообщает, что после изменения размера набор видимых кадров поменялся."""
        super().resizeEvent(event)
        self._update_card_size()
        self.viewportChanged.emit()

    def _emit_open(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.openRequested.emit(Path(path))

    def _emit_context_request(self, position: QPoint) -> None:
        """Выбирает карточку под курсором и передаёт построение меню владельцу."""
        item = self.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
            self.setCurrentItem(item)
        value = item.data(Qt.ItemDataRole.UserRole)
        if value:
            self.contextRequested.emit(
                Path(value),
                self.viewport().mapToGlobal(position),
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Разбирает клики по интерактивным областям карточки до обычного выбора элемента."""
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
        """Упаковывает выбранные пути в стандартный MIME-набор для приложения и Проводника."""
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
        """Определяет цель переноса и передаёт реальную файловую операцию рабочей вкладке.

        Внутренний перенос сохраняет выбранное действие copy/move. Внешний
        источник по умолчанию копируется: угадывать желание пользователя по
        настроению курсора — пока не самая надёжная технология.
        """
        paths = _local_paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        internal_reorder = event.mimeData().hasFormat("application/x-rawww-drag") and item is not None
        if internal_reorder:
            before = Path(item.data(Qt.ItemDataRole.UserRole))
            dragged = set(paths)
            if before not in dragged:
                self.orderDropped.emit(paths, before)
            event.acceptProposedAction()
            return
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
        """Следит только за значком аудиозаметки, не перерисовывая всю карточку без нужды."""
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
        """Распределяет ширину окна между колонками без щелей и скачков.

        Остаточные пиксели получают первые колонки. Поэтому сумма ширин всегда
        совпадает с viewport даже при DPI-масштабировании и нечётных размерах.
        """
        available = max(CARD_HARD_MIN_WIDTH, self.viewport().width())
        target_width = CARD_SIZE_TARGETS[self.card_size]
        min_columns = max(1, math.ceil(available / CARD_MAX_WIDTH))
        max_columns = max(1, available // CARD_HARD_MIN_WIDTH)
        columns = max(min_columns, min(max_columns, round(available / target_width)))
        layout_width = max(1, available - 2)
        width, remainder = divmod(layout_width, columns)
        height = int((available / columns) / CARD_ASPECT)
        icon_size = QSize(width + bool(remainder), height)
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
        """Возвращает фактический размер карточки с учётом остаточного пикселя колонки."""
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
    """Круглая кнопка аудиозаметки с кольцом прогресса, как в веб-просмотрщике."""

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
    """Круглая кнопка быстрой метки в полноэкранном просмотре."""

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
    """Рисует карточки файлов в основной сетке и навигационных лентах.

    Данные лежат в ролях ``QListWidgetItem``: путь, миниатюра, метаданные,
    состояние серии, аудиозаметка и прогресс фоновой работы. Делегат превращает
    их в готовую карточку — фон выделения, превью, имя, рейтинг, цветную метку,
    значки видео и звука. В компактном режиме часть деталей скрывается, чтобы
    боковая и нижняя ленты не превращались в приборную панель самолёта.

    Здесь нет загрузки файлов и изменения метаданных. Делегат только рисует и
    сообщает геометрию интерактивных областей; клики разбирает ``PhotoGrid``.
    Один художник на все режимы не даёт карточкам разъехаться визуально.
    """

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
        colors = {"red": "#c45b5b", "yellow": "#c39b2f", "green": "#459d63", "blue": "#4a7fbc", "purple": "#9261af"}
        tints = {
            "red": QColor(196, 91, 91, 118), "yellow": QColor(195, 155, 47, 118),
            "green": QColor(69, 157, 99, 118), "blue": QColor(74, 127, 188, 118),
            "purple": QColor(146, 97, 175, 118),
        }

        if expanded_series:
            bg = QColor("#dddddd") if selected else QColor("#888888" if hovered else "#747474")
        else:
            bg = QColor("#e0e0e0") if selected else QColor("#b3b3b3" if hovered else "#a7a7a7")
        painter.fillRect(rect, bg)
        if label in tints:
            painter.fillRect(rect, tints[label])
        painter.setPen(QPen(QColor(colors.get(label, "#767676")), 2 if label else 1))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        if selected:
            painter.setPen(QPen(QColor("#ffffff"), 3))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))

        top, side, bottom = (14, 4, 15) if self.compact else (20, 4, 16)
        image_rect = rect.adjusted(side, top, -side, -bottom)
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
            icon_provider = QFileIconProvider()
            folder_icon = icon_provider.icon(QFileInfo(str(path_obj)))
            if not folder_icon.isNull():
                painter.fillRect(folder_rect, Qt.GlobalColor.transparent)
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

            if expanded_series:
                painter.fillRect(image_rect, QColor(0, 0, 0, 76))

        caption_rect = QRect(rect.left() + 5, rect.bottom() - bottom + 2, rect.width() - 10, bottom - 2)
        text_rect = QRect(caption_rect)
        if path_obj and path_obj.is_dir():
            text_rect = QRect(rect.left() + 8, rect.bottom() - (20 if self.compact else 24) - 1, rect.width() - 16, 20 if self.compact else 24)
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
            display_text = path_obj.stem
        painter.drawText(
            text_rect,
            (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            if path_obj and path_obj.is_dir()
            else (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            font_metrics.elidedText(display_text, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
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
    """Навигационная лента кадров в полноэкранном просмотре.

    Один и тот же класс работает горизонтально для соседних фотографий и
    вертикально для раскрытой серии. Он хранит соответствие ``Path -> item``,
    обновляет только изменившиеся превью и метаданные, держит текущий кадр в
    видимой области и сообщает планировщику, какие миниатюры сейчас важнее.
    Файлы лента не открывает сама — она посылает ``pathActivated``, а решение
    принимает ``Workspace``. Виджет знает своё место и не рвётся руководить.
    """

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
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.itemClicked.connect(self._activate)
        scroll_bar = self.verticalScrollBar() if vertical else self.horizontalScrollBar()
        scroll_bar.valueChanged.connect(self.viewportChanged)

    def _activate(self, item: QListWidgetItem) -> None:
        """Синхронизирует визуальный выбор и просит рабочую вкладку открыть путь."""
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
        """Синхронизирует содержимое ленты без лишней полной перестройки.

        Если список путей прежний, обновляются только готовые превью и серии.
        Так сохраняется прокрутка, а Qt не пересоздаёт сотни элементов из-за
        одной приехавшей миниатюры — он и без того сегодня хорошо поработал.
        """
        if paths != self._paths:
            self.clear()
            self._items_by_path.clear()
            self._paths = list(paths)
            for path in paths:
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setData(DETAIL_ROLE, details.get(path.name, {}))
                item.setData(SERIES_ROLE, (series_cards or {}).get(path, {}))
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
            if series_cards is not None:
                for path, series in series_cards.items():
                    item = self._items_by_path.get(path)
                    if item is not None:
                        item.setData(SERIES_ROLE, series)
        self.set_current(current)

    def set_current(self, current: Path | None) -> None:
        """Выделяет открытый кадр и возвращает его в видимую часть ленты."""
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
        """Преобразует колесо мыши в горизонтальную прокрутку нижней ленты."""
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
        """Возвращает элементы, пересекающие область просмотра, в порядке отображения."""
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
    """Ползунок видео, перескакивающий прямо в место щелчка по дорожке."""

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
    """Образец цвета с контуром выделения, нарисованным внутри его границ."""

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
    """Рисует код замены как цельную плашку внутри текстового редактора."""

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
    """Однострочный редактор комментария с визуальными кодами замены.

    Последовательности вроде ``{code}`` хранятся как обычный исходный текст, но
    на экране превращаются в неделимые плашки через ``CodeTokenObject``. Поэтому
    пользователь не может случайно стереть половину маркера, а ShotSync и XMP
    всё равно получают исходную запись, пригодную для последующей подстановки.

    Класс следит за курсором, предлагает варианты из активного набора, умеет
    вставлять и удалять токены целиком и сохраняет поведение однострочного поля
    поверх многострочного ``QTextEdit``. Флаг ``_rendering`` защищает от
    рекурсивной перерисовки документа — Qt любит прислать ещё один сигнал ровно
    тогда, когда предыдущий ещё не успел снять пальто.
    """

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
        self._suggestion_popup: QListWidget | None = None
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
        """Перестраивает документ из исходного текста и сохраняет позицию курсора."""
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
        """Показывает подходящие коды замены рядом с текущим маркером."""
        before = self.text()[:self._raw_cursor()]
        start, opener = max((before.rfind(mark), mark) for mark in ("{", "\\", "=", "@"))
        if start < 0:
            self._hide_suggestions()
            return
        fragment = before[start + 1:]
        if (opener == "@" and fragment and not fragment.replace("_", "a").isalnum()) or (opener != "@" and ("}" if opener == "{" else opener) in fragment):
            self._hide_suggestions()
            return
        if opener == "@" and fragment in self._lookup:
            self._hide_suggestions()
            return
        self._start, self._opener = start, opener
        self._labels = {f"{code} — {value}": code for code, value in self._lookup.items()}
        labels = [label for label in self._labels if fragment.casefold() in label.casefold()]
        if not labels:
            self._hide_suggestions()
            return
        popup = self._ensure_suggestion_popup()
        popup.clear()
        popup.addItems(labels)
        popup.setCurrentRow(0)
        popup.setFixedWidth(max(240, self.width()))
        popup.setFixedHeight(min(180, popup.sizeHintForRow(0) * len(labels) + 4))
        popup.move(self.mapToGlobal(self.cursorRect().bottomLeft()))
        popup.show()

    def _ensure_suggestion_popup(self) -> QListWidget:
        if self._suggestion_popup is None:
            popup = QListWidget(self)
            popup.setWindowFlags(Qt.WindowType.ToolTip)
            popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            popup.setObjectName("codeSuggestionPopup")
            popup.itemClicked.connect(lambda item: self._insert_code(item.text()))
            self._suggestion_popup = popup
        return self._suggestion_popup

    def _hide_suggestions(self) -> None:
        if self._suggestion_popup is not None:
            self._suggestion_popup.hide()

    def _insert_code(self, label: str) -> None:
        code = self._labels.get(label)
        if not code: return
        raw = self.text(); end = self._raw_cursor()
        close = "}" if self._opener == "{" else ("" if self._opener == "@" else self._opener)
        insertion = f"{self._opener}{code}{close}"
        self._render(raw[:self._start] + insertion + raw[end:], self._start + len(insertion))
        self._hide_suggestions()
        self._completer.popup().hide()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda: self.setFocus(Qt.FocusReason.OtherFocusReason))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        popup = self._suggestion_popup
        if popup is not None and popup.isVisible():
            if event.key() == Qt.Key.Key_Down:
                popup.setCurrentRow(min(popup.count() - 1, popup.currentRow() + 1)); event.accept(); return
            if event.key() == Qt.Key.Key_Up:
                popup.setCurrentRow(max(0, popup.currentRow() - 1)); event.accept(); return
            if event.key() == Qt.Key.Key_Tab:
                item = popup.currentItem()
                if item: self._insert_code(item.text())
                event.accept(); return
            if event.key() == Qt.Key.Key_Escape:
                popup.hide(); event.accept(); return
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                popup.hide()
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
        self._hide_suggestions()
        self.editingFinished.emit()


class ViewerMetaBar(QWidget):
    """Показывает и редактирует метаданные активной фотографии.

    Эту панель используют и сетка, и полноэкранный просмотр, поэтому вся логика
    одинаковых элементов управления собрана в одном месте: рейтинг от нуля до
    пяти, цветовая метка, комментарий, быстрая метка и автопереход к следующему
    кадру. Справа выводится короткая выжимка из EXIF — камера, выдержка,
    диафрагма, ISO и фокусное расстояние.

    Класс намеренно не записывает данные в кэш и не меняет файлы. Действия
    пользователя он сообщает сигналами, а ``Workspace`` уже решает, куда
    сохранить результат. Методы ``set_*`` выполняют обратную синхронизацию и
    блокируют сигналы на время обновления, иначе один клик мог бы устроить
    маленький вечный двигатель из сигналов Qt.
    """

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
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(5)

        self.quick_mark_button = QToolButton()
        self.quick_mark_button.setObjectName("fullQuickMark")
        self.quick_mark_button.setIcon(_fomantic_icon("bookmark", 13))
        self.quick_mark_button.setText("быстр. метка")
        self.quick_mark_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.quick_mark_button.setToolTip("Настроить быструю метку; M — применить")
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
        """Строит меню выбора действия, которое будет висеть на клавише M."""
        menu = QMenu(self.window())
        menu.setToolTipsVisible(True)
        title = menu.addAction("Настроить быструю метку")
        title.setEnabled(False)
        menu.addSeparator()

        def add_visual_action(*, selected: bool, visual: str | QIcon, tooltip: str, callback) -> None:
            """Добавляет в меню строку с отметкой выбора и визуальным значением."""
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
            ("red", "Красная метка", "#c45b5b"),
            ("yellow", "Жёлтая метка", "#c39b2f"),
            ("green", "Зелёная метка", "#459d63"),
            ("blue", "Синяя метка", "#4a7fbc"),
            ("purple", "Фиолетовая метка", "#9261af"),
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
        """Запоминает выбранную быструю метку и сообщает о новой настройке."""
        self._quick_mark = (kind, value)
        self.quickMarkConfigured.emit(kind, value)

    def set_quick_mark(self, kind: str, value: object) -> None:
        self._quick_mark = (kind, value)

    def set_auto_advance(self, enabled: bool) -> None:
        """Обновляет автопереход без обратного сигнала в ``Workspace``."""
        self.auto_advance_button.blockSignals(True)
        self.auto_advance_button.setChecked(enabled)
        self.auto_advance_button.blockSignals(False)

    def set_comment(self, comment: str) -> None:
        """Подставляет комментарий активного кадра без имитации ручного ввода."""
        self.comment_edit.blockSignals(True)
        self.comment_edit.setText(comment)
        self.comment_edit.blockSignals(False)

    def set_metadata(self, detail: dict) -> None:
        """Синхронизирует кнопки и EXIF-строку с метаданными активного кадра."""
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
    """Собирает полноэкранный просмотр фотографии или видео.

    Компонент координирует изображение, плеер, нижнюю и боковую ленты,
    метаданные, лица, масштаб 100 % и плавающие элементы управления. Он не
    загружает файлы сам: ``Workspace`` поставляет готовые кадры и получает
    сигналы навигации. Благодаря этому просмотрщик не знает, откуда приехала
    фотография — с диска, из кэша или после долгой прогулки по RAW.
    """

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.video_widget: QVideoWidget | None = None
        self.media_stack = QStackedWidget()
        self.media_stack.addWidget(self.image_view)
        self.video_player = QMediaPlayer(self)
        self.video_audio = QAudioOutput(self)
        self.video_audio.setVolume(1.0)
        self.video_player.setAudioOutput(self.video_audio)
        self.video_player.positionChanged.connect(self._video_position_changed)
        self.video_player.durationChanged.connect(self._video_duration_changed)
        self.video_player.playbackStateChanged.connect(self._video_state_changed)
        self._is_video = False
        self.video_controls = QFrame(self)
        self.video_controls.setObjectName("videoControls")
        self.video_controls.setFixedWidth(360)
        self.video_controls_layout = QHBoxLayout(self.video_controls)
        self.video_controls_layout.setContentsMargins(8, 5, 8, 5)
        self.video_controls_layout.setSpacing(7)
        self.video_controls.hide()

        self.info_label = QLabel(self)
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
        self.media_panel.installEventFilter(self)

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
        self.face_filter_clear.setIcon(_fomantic_icon("close", 16))
        self.face_filter_clear.setFixedSize(26, 26)
        self.face_filter_clear.setIconSize(QSize(16, 16))
        self.face_filter_clear.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.face_filter_clear.setAutoRaise(True)
        self.face_filter_clear.setToolTip("Сбросить фильтр по лицу")
        self.face_filter_clear.clicked.connect(self.faceFilterClearRequested)
        face_chip_layout.addWidget(self.face_filter_clear)
        self.face_filter_chip.hide()

        self.face_search_loader = QFrame(self.media_panel)
        self.face_search_loader.setObjectName("faceSearchLoader")
        face_loader_layout = QVBoxLayout(self.face_search_loader)
        face_loader_layout.setContentsMargins(20, 12, 20, 12)
        face_loader_layout.setSpacing(7)
        self.face_search_loader_label = QLabel("Ищу похожие лица")
        self.face_search_loader_label.setObjectName("faceSearchLoaderText")
        self.face_search_loader_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        face_loader_layout.addWidget(self.face_search_loader_label)
        face_loader_progress = QProgressBar()
        face_loader_progress.setObjectName("faceSearchLoaderProgress")
        face_loader_progress.setRange(0, 0)
        face_loader_progress.setTextVisible(False)
        face_loader_layout.addWidget(face_loader_progress)
        self.face_search_loader.setFixedSize(210, 62)
        self.face_search_loader.hide()

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
        self.strip_toggle.setToolTip("Свернуть ленту превью")
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

        self.strip_panel = QFrame(self)
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
        self.zoom_action = QAction(self)
        self.zoom_action.setShortcut(QKeySequence("Z"))
        self.zoom_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.zoom_action.triggered.connect(self._toggle_zoom)
        self.addAction(self.zoom_action)

    STRIP_FULL = 0
    STRIP_COLLAPSED = 1
    STRIP_HIDDEN = 2

    def _load_strip_level(self) -> int:
        settings = _application_settings()
        if settings.contains("viewer_strip_level"):
            return max(self.STRIP_FULL, min(self.STRIP_HIDDEN, settings.value("viewer_strip_level", self.STRIP_FULL, int)))
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
        """Полностью останавливает видео при выходе из полноэкранного режима."""
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
        """Обновляет соседние кадры, текущую серию и навигационные ленты."""
        series_current = current if series_current is None else series_current
        if generation != self._photo_generation or paths != self.photo_strip._paths:
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
        """Показывает действия над найденным лицом в точке щелчка."""
        if not isinstance(face, dict) or not isinstance(position, QPoint):
            return
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
        # Запоздалое экранное превью не должно заменить уже открытый оригинал.
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
            video_widget = self._ensure_video_widget()
            self.video_controls.setParent(video_widget)
            self.video_controls.setAttribute(
                Qt.WidgetAttribute.WA_DontCreateNativeAncestors,
                True,
            )
            self.video_controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            self.media_stack.setCurrentWidget(video_widget)
            self._position_video_controls()
            self.video_controls.show()
            self.video_player.play()

    def _ensure_video_widget(self) -> QVideoWidget:
        if self.video_widget is None:
            video_widget = QVideoWidget(self.media_stack)
            video_widget.setObjectName("fullVideoView")
            video_widget.installEventFilter(self)
            self.media_stack.addWidget(video_widget)
            self.video_player.setVideoOutput(video_widget)
            self.video_widget = video_widget
        return self.video_widget

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
        """Включает масштаб 100 %, когда исходный кадр уже декодирован."""
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
        """Обновляет индикатор метки без повторного открытия просмотра."""
        self._update_mark_indicator()

    def _update_mark_indicator(self) -> None:
        detail = self._mark_detail
        rating = int(detail.get("rating") or 0)
        color_label = str(detail.get("color_label") or "")
        has_mark = rating > 0 or bool(color_label)
        visible = (
            not self._is_video
            and not self.image_view.zoom_requested
            and has_mark
            and _application_settings().value("interface/show_full_view_mark_indicator", True, bool)
        )
        if not visible:
            self.mark_indicator.hide()
            return
        colors = {
            "red": "#c45b5b", "yellow": "#c39b2f", "green": "#459d63",
            "blue": "#4a7fbc", "purple": "#9261af",
        }
        self.mark_indicator.setText(f"★ {rating}" if rating > 0 else "")
        self.mark_indicator.setToolTip(
            "Снять все метки" if has_mark else "Применить быструю метку (M)"
        )
        self.mark_indicator.set_mark_color(colors.get(color_label, "#4d535b"))
        self.mark_indicator.show()
        self._position_mark_indicator()

    def cancel_zoom(self) -> None:
        """Отменяет незавершённое увеличение перед сменой фотографии."""
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
            self._position_video_controls()
            self._position_counter()
            self._position_face_filter_chip()
            self._position_mark_indicator()
            self._position_face_search_loader()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.image_view.update()
        QTimer.singleShot(0, self._position_video_controls)
        QTimer.singleShot(0, self._position_face_filter_chip)
        QTimer.singleShot(0, self._position_counter)
        QTimer.singleShot(0, self._position_mark_indicator)
        QTimer.singleShot(0, self._position_face_search_loader)

    def _position_face_filter_chip(self) -> None:
        if not self.face_filter_chip.isVisible():
            return
        self.face_filter_chip.adjustSize()
        self.face_filter_chip.move(
            max(8, self.media_panel.width() - self.face_filter_chip.width() - self.mark_indicator.width() - 20), 12
        )
        self.face_filter_chip.raise_()

    def set_face_search_loading(self, loading: bool) -> None:
        """Показывает состояние поиска, пока Workspace фильтрует большую папку."""
        self.set_busy_loading("Ищу похожие лица" if loading else None)

    def set_busy_loading(self, text: str | None) -> None:
        """Показывает общий индикатор долгой операции поверх FullView."""
        if not text:
            self.face_search_loader.hide()
            return
        self.face_search_loader_label.setText(text)
        self._position_face_search_loader()
        self.face_search_loader.show()
        self.face_search_loader.raise_()

    def _position_face_search_loader(self) -> None:
        self.face_search_loader.move(
            max(0, (self.media_panel.width() - self.face_search_loader.width()) // 2),
            max(0, (self.media_panel.height() - self.face_search_loader.height()) // 2),
        )

    def _position_mark_indicator(self) -> None:
        if not self.mark_indicator.isVisible():
            return
        self.mark_indicator.move(
            max(8, self.media_panel.width() - self.mark_indicator.width() - 12),
            12,
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
    """Рисует открытый кадр и управляет интерактивным просмотром 100 %.

    Здесь живут геометрия вписывания, удержание точки под курсором, панорамирование,
    индикатор загрузки и клики по найденным лицам. Виджет работает только с уже
    декодированным ``QPixmap``; запрос оригинала уходит наружу сигналом, чтобы
    тяжёлая работа не поселилась в обработчике мыши.
    """

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
            if self._face_at(event.position()) >= 0:
                event.accept()
                return
            if self._zoomed:
                self._drag_position = event.position()
                self._drag_center = QPointF(self._view_center)
            else:
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
        """Возвращает круглый аватар найденного лица для плашек и наборов."""
        if self._pixmap is None or self._pixmap.isNull():
            return QPixmap()
        return self.face_avatar_from_pixmap(self._pixmap, face, size)

    @staticmethod
    def face_avatar_from_pixmap(pixmap: QPixmap, face: dict, size: int) -> QPixmap:
        """Вырезает лицо из декодированного кадра, а не из мелкой миниатюры."""
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
    """Перетаскиваемый заголовок окна с вкладками в стиле браузера."""

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


class DirectoryTree(QTreeView):
    """Дерево папок, которое принимает URL-адреса локальных файлов, не позволяя своей модели перемещать их.
    """

    pathsDropped = Signal(object, object, object)   # пути, пункт назначения, действие

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
    """Переупорядочиваемый список, в котором при перетаскивании отображается путь к папке."""

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
    """Видимая ручка для изменения размера панели избранного."""

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
    """Разделитель дерева и избранного с собственным заметным захватом."""

    def createHandle(self):  # noqa: N802
        return FavoritesSplitterHandle(self.orientation(), self)


class FilterComboBox(QComboBox):
    """Комбобокс фильтра, который не отдаёт колесу мыши случайно менять выбор."""

    def showPopup(self) -> None:  # noqa: N802
        view = self.view()
        # Верхняя панель не должна превращать короткие списки фильтров в
        # прокручиваемое окошко: Qt по умолчанию ограничивает его десятью строками.
        self.setMaxVisibleItems(self.count())
        visible_items = self.count()
        content_height = sum(
            max(0, view.sizeHintForRow(index))
            for index in range(visible_items)
        )
        view.setMinimumHeight(content_height + 2 * view.frameWidth())
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        super().showPopup()


class CenteredSearchEdit(QLineEdit):
    """Поле поиска с аккуратно отцентрированными значками по краям."""

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
    """Компактные элементы управления наложением для изменения размера миниатюр."""

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
    """Цель перетаскивания, используемая для удаления папки из списка избранного."""

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
    """Панель рабочих вкладок с браузерной шириной и переносом файлов.

    Qt по умолчанию растягивает вкладки на всю строку; здесь ширина ограничена,
    кнопка закрытия рисуется отдельно, а пустое место остаётся перетаскиваемой
    частью окна. При сбросе файлов класс определяет вкладку под курсором и
    передаёт пути наружу, но сам ничего на диске не перемещает.
    """
    closeRequested = Signal(int)
    pathsDropped = Signal(object, int, object)   # пути, индекс табуляции, действие

    def __init__(self) -> None:
        super().__init__()
        self._tab_width = 220
        self._close_press_index: int | None = None
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

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.count() > 1 and event.button() == Qt.MouseButton.LeftButton:
            for index in range(self.count()):
                if self._close_rect(index).contains(event.position().toPoint()):
                    # Иначе базовый QTabBar активирует вкладку ещё до её закрытия.
                    self._close_press_index = index
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        close_index = self._close_press_index
        self._close_press_index = None
        if close_index is not None and event.button() == Qt.MouseButton.LeftButton:
            if self._close_rect(close_index).contains(event.position().toPoint()):
                self.closeRequested.emit(close_index)
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
    """Показывает прогресс долгих операций на кнопке приложения в Windows.

    Тонкая обёртка над COM-интерфейсом ``ITaskbarList3`` создаётся только на
    Windows и молча отключается, если API недоступен. Интерфейс не должен падать
    лишь потому, что панель задач сегодня не в настроении.
    """

    _TBPF_NORMAL = 0x2
    _TBPF_NOPROGRESS = 0x0

    def __init__(self) -> None:
        self._taskbar = None
        self._com_initialized = False
        if sys.platform != "win32":
            return
        try:
            class GUID(ctypes.Structure):
                """Хранит GUID в том виде, в каком его ожидает WinAPI."""

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
    """Показывает прогресс на значке Dock, если установлен мост PyObjC."""

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
    """Превращает название съёмки в безопасное имя папки."""
    cleaned = "".join(c if c not in '<>:"/\\|?*' else "_" for c in str(title or "").strip())
    cleaned = cleaned.rstrip(". ").strip()
    return cleaned or "shotsync"


def _shotsync_photo_filename(photo: dict) -> str:
    """Достаёт из данных ShotSync имя файла и отбрасывает компоненты пути."""
    raw = str(photo.get("name") or "").replace("\\", "/")
    return Path(raw).name.strip()


def _humanize_shotsync_network_error(error: str) -> str:
    """Переводит техническую сетевую ошибку Qt на человеческий язык."""
    low = (error or "").lower()
    if any(part in low for part in ("host", "network", "unreachable", "timeout", "connection", "refused")):
        return "Нет подключения к интернету."
    return "Не удалось подключиться к ShotSync."


def _format_remaining_time(seconds: float) -> str:
    """Возвращает короткую оценку оставшегося времени для строки прогресса."""
    total_seconds = max(1, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"≈ {hours} ч {minutes} мин"
    if minutes:
        return f"≈ {minutes} мин {seconds} с"
    return f"≈ {seconds} с"


class Workspace(QMainWindow):
    """Одна независимая рабочая вкладка с папкой фотографий.

    ``Workspace`` связывает файловую модель, сетку, полноэкранный просмотр,
    кэши, очереди декодирования, EXIF, AI и ShotSync. Он владеет состоянием
    текущей папки и проверяет поколение каждого фонового результата: старый
    поток не должен внезапно дорисовать кадр уже в новой папке.

    Класс большой, потому что это координатор пользовательского сценария, а не
    склад алгоритмов: декодирование, кэш, сеть и формирование XMP вынесены в
    отдельные модули. Здесь остаётся дирижёр. Иногда с очень большим пультом.
    """

    fullViewRequested = Signal(object)
    fullscreenRequested = Signal(object)
    gridRequested = Signal()
    singlePhotoExitRequested = Signal(object)
    singlePhotoFolderRequested = Signal(object)
    openFolderRequested = Signal(object)    # Путь: открыть (или выделить) вкладку папки.
    shotsyncFolderChanged = Signal(bool)    # текущая папка связана с ShotSync
    seriesModeChanged = Signal(bool)
    cardImportRequested = Signal(object)
    _cache_maintenance_started = False

    def __init__(
        self,
        initial_directory: Path | None = None,
        *,
        defer_initial_scan: bool = False,
        transfer_manager: TransferManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 920)
        self.closing = False
        # Временное рабочее пространство используется только для открытия одного
        # файла из проводника: оно не становится вкладкой и не попадает в сессию.
        self.single_photo_mode = False
        self._taskbar_progress = WindowsTaskbarProgress()
        self._dock_progress = MacDockProgress()

        self.directory_scan_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_load_executor = ThreadPoolExecutor(max_workers=1)
        self.cache_flush_executor = ThreadPoolExecutor(max_workers=1)
        self.rename_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-rename")
        self.face_search_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="face-search")
        self._preview_cache_write_buffer: dict[
            tuple[int, Path, int], tuple[FolderCache, PixelImage, int]
        ] = {}
        self.preview_cache_write_timer = QTimer(self)
        self.preview_cache_write_timer.setSingleShot(True)
        self.preview_cache_write_timer.timeout.connect(self._drain_preview_cache_writes)
        self.xmp_cache_write_timer = QTimer(self)
        self.xmp_cache_write_timer.setSingleShot(True)
        self.xmp_cache_write_timer.timeout.connect(self._drain_xmp_cache_writes)
        self.xmp_bulk_timer = QTimer(self)
        self.xmp_bulk_timer.setSingleShot(True)
        self.xmp_bulk_timer.timeout.connect(self._drain_xmp_bulk_queue)
        self._xmp_bulk_queue: deque[Path] = deque()
        self._xmp_bulk_queued: set[Path] = set()
        self._xmp_cache_selection_buffer: dict[tuple[int, str], tuple[FolderCache, dict]] = {}
        self._xmp_cache_state_buffer: dict[tuple[int, str], tuple[FolderCache, dict]] = {}
        self.cache_maintenance_executor = ThreadPoolExecutor(max_workers=1)
        self.xmp_executor = ThreadPoolExecutor(max_workers=1)
        self.bridge = DecodeBridge()
        self.bridge.decoded.connect(self._on_decoded)
        self.bridge.failed.connect(self._on_decode_failed)
        self.bridge.cacheLoaded.connect(self._on_cache_loaded)
        self.bridge.aiCacheChecked.connect(self._on_ai_cache_checked)
        self.bridge.directoryScanned.connect(self._on_directory_scanned)
        self.bridge.renameProgress.connect(self._on_rename_progress)
        self.bridge.renameCacheUpdating.connect(self._on_rename_cache_updating)
        self.bridge.renameFinished.connect(self._on_rename_finished)
        self.bridge.metadataUpdated.connect(self._on_metadata_updated)
        self.bridge.xmpWritten.connect(self._on_xmp_written)
        self.bridge.xmpScanned.connect(self._on_xmp_scanned)
        self.bridge.folderChecked.connect(self._on_folder_checked)
        self.video_thumbnailer = VideoThumbnailer(self)
        self.video_thumbnailer.previewReady.connect(self._on_video_preview)
        self._xmp_pending: dict[Path, tuple[dict, list[dict], str | None]] = {}
        self._xmp_running: set[Path] = set()
        self._xmp_retry_after_change: set[Path] = set()
        self._xmp_futures: dict[Path, Future] = {}
        self._xmp_states: dict[str, dict] = {}
        self._xmp_pair_members: dict[Path, list[Path]] = {}
        self._xmp_scan_future: Future | None = None
        self._xmp_scan_generation = -1
        self._xmp_rescan_requested = False
        self._xmp_full_hash_requested = False
        self._xmp_rescan_priority: str | None = None
        self._xmp_queue_all_after_scan = False
        self._xmp_export_after_cache_load = False
        self._ignore_folder_changes_until = 0.0
        self.last_navigation_at = 0.0
        self.pending_full_request: Path | None = None
        self.pending_grid_full_request: Path | None = None
        self.populate_index = 0
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
        )
        self.bridge.schedulerFinished.connect(self._on_scheduler_finished)
        self.bridge.faceSearchFinished.connect(self._on_face_search_finished)
        self.items_by_path: dict[Path, QListWidgetItem] = {}
        self.all_paths: list[Path] = []
        self._custom_order: list[str] = []
        self.preview_paths: set[Path] = set()
        self.preview_finished_paths: set[Path] = set()
        self.view_paths: list[Path] = []
        self.view_generation = 0
        self._full_navigation_generation = -1
        self._full_navigation_paths: list[Path] = []
        self._full_navigation_indices: dict[Path, int] = {}
        self._full_navigation_series: dict[Path, tuple[Path, ...]] = {}
        self._full_navigation_cards: dict[Path, dict] = {}
        self.paths: list[Path] = []
        self.series_cards: dict[Path, dict] = {}
        self.expanded_series: set[Path] = set()
        self.photo_details: dict[str, dict] = {}
        self.image_embeddings: dict[str, bytes] = {}
        self._status_visible_count = 0
        self._status_total_count = 0
        self._status_positions: dict[Path, int] = {}
        self._file_time_cache: dict[Path, float] = {}
        self._metadata_view_refresh_needed = False
        self.settings = _application_settings()
        self.transfer_manager = transfer_manager
        self.destination_paths_provider: Callable[[], list[Path]] | None = None

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
        self.shotsync_login_dialog: ShotSyncLoginDialog | None = None
        self.shotsync_client.set_api_key(self.settings.value("shotsync/api_key", "", str))
        self.shotsync_client.loginSucceeded.connect(self._shotsync_login_succeeded)
        self.shotsync_client.loginFailed.connect(self._shotsync_login_failed)
        self.shotsync_client.sessionVerified.connect(self._shotsync_session_verified)
        self.shotsync_client.sessionInvalid.connect(self._shotsync_session_invalid)
        self.shotsync_client.sessionCheckFailed.connect(self._shotsync_session_check_failed)
        self.shotsync_client.shootingsLoaded.connect(self._shotsync_shootings_loaded)
        self.shotsync_client.shootingsFailed.connect(self._shotsync_shootings_failed)
        self.shotsync_client.avatarLoaded.connect(self._shotsync_avatar_loaded)

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
        self.shotsync.uploader.finishedWithErrors.connect(self._on_shotsync_upload_finished_with_errors)
        self.shotsync.uploader.failed.connect(self._on_shotsync_upload_failed)
        self.shotsync.uploader.deleteFinished.connect(self._on_shotsync_server_deleted)
        self.shotsync.uploader.deleteFailed.connect(self._on_shotsync_server_delete_failed)
        self.shotsync.marks_fetcher.finished.connect(self._on_shotsync_marks_fetched)
        self.shotsync.marks_fetcher.failed.connect(self._on_shotsync_marks_failed)

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
        self._face_match_names: set[str] | None = None
        self._face_search_generation = 0
        self._face_search_index: FaceSearchIndex | None = None
        self._face_search_future: Future | None = None
        self._face_search_cancel: Event | None = None
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
        self._pending_folder_grid_restore = False
        self._pending_view_cursor_path: Path | None = None
        self._pending_view_selection: set[Path] = set()
        self._pending_view_scroll: int | None = None
        self._restoring_folder_grid_context = False
        self._restoring_view_context = False
        self.folder_cache: FolderCache | None = None
        self._ai_pipeline = None
        self._metadata_pipeline = None
        self._resume_ai_when_active = False
        self.ai_progress_total = 0
        self._ai_progress_started_at: float | None = None
        self.preview_progress_total = 0
        self._auto_ai_generation = -1
        self._ai_requested_generation = -1
        self._cache_ai_waiting = False
        self._cache_ai_paths: set[Path] = set()
        self._upload_progress: tuple[int, int] | None = None
        self._receive_progress: tuple[int, int, int] | None = None
        self._selection_progress: tuple[int, int] | None = None
        self._file_mutation_waiting = False
        self._shotsync_pending_marks = 0
        self._shotsync_marks_fetching = False
        self.statusBar().hide()
        self.fast_fullscreen = False
        self.normal_geometry = None
        self.normal_window_flags = self.windowFlags()
        self.normal_window_state = self.windowState()

        self.stack = QStackedWidget(self)
        self.grid_page = self._build_grid_page()
        self.full_view = FullView(self.stack)
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
        self.status_refresh_timer = QTimer(self)
        self.status_refresh_timer.setSingleShot(True)
        self.status_refresh_timer.setInterval(80)
        self.status_refresh_timer.timeout.connect(self._refresh_status_panel)
        self.metadata_ui_timer = QTimer(self)
        self.metadata_ui_timer.setSingleShot(True)
        self.metadata_ui_timer.setInterval(250)
        self.metadata_ui_timer.timeout.connect(self._flush_metadata_ui_updates)

        self._create_actions()
        initial_scan_delay = 350 if defer_initial_scan else 0
        QTimer.singleShot(initial_scan_delay, lambda: self.load_directory(self.current_dir))
        QTimer.singleShot(0, self._focus_grid_panel)
        QTimer.singleShot(0, self._restore_face_filter_chip)
        QTimer.singleShot(5_000, self._start_cache_maintenance)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.begin_shutdown()
        super().closeEvent(event)

    def begin_shutdown(self) -> None:
        """Запрещает новую работу вкладки, не блокируя закрытие интерфейса.

        Все очереди регистрируются в общей финальной фазе. Поэтому закрытая
        вкладка исчезает сразу, а при выходе из приложения уже начатые короткие
        операции всё равно будут штатно завершены.
        """
        if self.closing:
            return
        self.closing = True
        self._cancel_face_search()
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
        self._ai_progress_started_at = None
        self.status_refresh_timer.stop()
        self.metadata_ui_timer.stop()
        self.folder_change_timer.stop()
        self.volume_refresh_timer.stop()
        self.preview_cache_write_timer.stop()
        self.xmp_cache_write_timer.stop()
        self.xmp_bulk_timer.stop()
        self._xmp_bulk_queue.clear()
        self._xmp_bulk_queued.clear()
        self.video_thumbnailer.cancel()
        self.full_view.stop_video()
        self.full_view.stop_audio()
        self.grid_audio_player.stop()
        self._flush_folder_cache(wait=False, close=True)
        self._detach_shotsync_syncer()
        self.shotsync_client.shutdown()
        self.folder_cache = None
        self.cache_ready = False
        self.scheduler.shutdown()
        retire_executor(self.directory_scan_executor)
        retire_executor(self.cache_load_executor)
        retire_executor(self.cache_flush_executor, cancel_futures=False)
        retire_executor(self.rename_executor, cancel_futures=False)
        retire_executor(self.face_search_executor)
        retire_executor(self.cache_maintenance_executor)
        # XMP отражает явные пользовательские изменения и не является
        # производным кэшем, поэтому очередь дописывается полностью.
        retire_executor(self.xmp_executor, cancel_futures=False)
        if self._metadata_pipeline is not None:
            self._metadata_pipeline.shutdown()
        if self._ai_pipeline is not None:
            self._ai_pipeline.shutdown()

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

    def _paths_touch_current_workspace(self, paths: list[Path]) -> bool:
        """Проверяет, может ли активная вкладка держать один из изменяемых путей."""
        for path in paths:
            try:
                if (
                    path == self.current_dir
                    or path.parent == self.current_dir
                    or self.current_dir.is_relative_to(path)
                ):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _run_after_file_consumers_release(
        self,
        paths: list[Path],
        operation: Callable[[], None],
        *,
        restart_consumers: bool = True,
        loading_text: str = "Выполняется файловая операция",
    ) -> None:
        """Откладывает файловую операцию до освобождения исходников воркерами.

        Отмена Future не останавливает уже начатый RAW/AI/ExifTool. Мы запрещаем
        новые задания, но ждём работающие асинхронно через event loop, чтобы Qt
        не зависал и Windows получил закрытые файловые дескрипторы.
        """
        if self.closing:
            return
        if getattr(self, "_file_mutation_waiting", False):
            QTimer.singleShot(
                50,
                lambda targets=list(paths), callback=operation, restart=restart_consumers, text=loading_text:
                self._run_after_file_consumers_release(
                    targets,
                    callback,
                    restart_consumers=restart,
                    loading_text=text,
                ),
            )
            return

        self._file_mutation_waiting = True
        original_dir = self.current_dir
        original_generation = self.directory_generation
        touches_workspace = self._paths_touch_current_workspace(paths)
        futures = set(self.scheduler.pending_futures()) if touches_workspace else set()
        if touches_workspace:
            self.populate_timer.stop()
            self.thumb_timer.stop()
            self.visible_thumb_timer.stop()
            self.full_request_timer.stop()
            self.grid_full_request_timer.stop()
            self.pending_full_request = None
            self.pending_grid_full_request = None
            self.scheduler.cancel_pending()
            self.scheduler.abandon_preview_decode_work()
            self.video_thumbnailer.cancel()
            self.full_view.stop_video()
            self._cancel_face_search()
            self._set_face_search_loading(False)

        had_ai = False
        if touches_workspace and self._ai_pipeline is not None:
            ai_futures = self._ai_pipeline.pending_futures()
            if ai_futures:
                had_ai = True
                futures.update(ai_futures)
                self._ai_pipeline.shutdown()
                self._ai_pipeline = None
                self.ai_progress_timer.stop()

        had_metadata = False
        if touches_workspace and self._metadata_pipeline is not None:
            metadata_futures = self._metadata_pipeline.pending_futures()
            if metadata_futures:
                had_metadata = True
                futures.update(metadata_futures)
                self._metadata_pipeline.shutdown()
                self._metadata_pipeline = None

        if touches_workspace:
            xmp_futures = tuple(self._xmp_futures.values())
            futures.update(xmp_futures)
            for future in xmp_futures:
                future.cancel()
            self._xmp_pending.clear()

        self.grid_restore_loader_label.setText(loading_text)
        self._set_grid_restore_loader_visible(True)
        self.full_view.set_busy_loading(loading_text)

        def finish_when_released() -> None:
            if self.closing:
                self._file_mutation_waiting = False
                return
            if any(not future.done() for future in futures):
                QTimer.singleShot(25, finish_when_released)
                return
            self._file_mutation_waiting = False
            try:
                operation()
            finally:
                self.full_view.set_busy_loading(None)
                if self._restoring_folder_grid_context:
                    self.grid_restore_loader_label.setText("Папка открывается")
                    self._schedule_grid_restore_loader()
                elif self._restoring_view_context:
                    self.grid_restore_loader_label.setText("Обновляю список")
                    self._schedule_grid_restore_loader()
                else:
                    self._hide_grid_restore_loader()
            if (
                restart_consumers
                and self.current_dir == original_dir
                and self.directory_generation == original_generation
                and self.current_dir.is_dir()
                and self.workspace_active
            ):
                self._schedule_visible_thumb_priority()
                self.thumb_timer.start()
                if had_metadata and self.folder_cache is not None and self.cache_ready:
                    self.metadata_pipeline.scan(
                        [path for path in self.view_paths if is_supported_image(path)],
                        self.folder_cache,
                        self.bridge.metadataUpdated.emit,
                    )
                if had_ai:
                    self._start_ai_analysis()

        QTimer.singleShot(0, finish_when_released)

    def _start_cache_maintenance(self) -> None:
        """Запускает обслуживание кэша после срочных стартовых задач."""
        if self.closing or type(self)._cache_maintenance_started:
            return
        type(self)._cache_maintenance_started = True
        self.cache_maintenance_executor.submit(maintain_folder_caches)

    def set_workspace_active(self, active: bool) -> None:
        """Разрешает строить превью только у видимой рабочей вкладки."""
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
            self.scheduler.abandon_preview_decode_work()
            if self._ai_pipeline is not None and self._ai_pipeline.pending_count() > 0:
                self._resume_ai_when_active = True
                self._ai_pipeline.shutdown()
                self._ai_pipeline = None
                self.ai_progress_timer.stop()
            self.pending_grid_full_request = None
            self.scheduler.cancel_pending()
            self.full_view.video_player.pause()
            return
        if self.populate_index < len(self.paths):
            self.populate_timer.start()
        self._schedule_visible_thumb_priority()
        if self.thumb_priority or self.thumb_index < len(self.paths):
            self.thumb_timer.start()
        if self._resume_ai_when_active:
            self._resume_ai_when_active = False
            self._start_ai_analysis()

    def _video_playback_changed(self, playing: bool) -> None:
        """Приостанавливает декодирование сетки, пока воспроизводится видео."""
        self.video_thumbnailer.set_active(self.workspace_active and not playing)
        if not playing and self.workspace_active:
            self._schedule_visible_thumb_priority()

    def _build_grid_page(self) -> QWidget:
        """Собирает основную страницу: дерево папок, сетку и служебные панели.

        Метод большой, потому что здесь один раз связываются виджеты и сигналы.
        Рабочая логика загрузки и обработки файлов живёт в отдельных методах.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.dir_model = QFileSystemModel(self)
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath(QDir.rootPath())

        class CleanDirModel(QFileSystemModel):
            """Не рисует стрелку раскрытия у папки, в которой нет подпапок."""

            def __init__(self, parent=None):
                super().__init__(parent)
                self._new_folder_path: Path | None = None

            def hasChildren(self, parent=None):
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
                if self._new_folder_path and self.filePath(index) == str(self._new_folder_path):
                    return default_flags | Qt.ItemFlag.ItemIsEditable
                return default_flags & ~Qt.ItemFlag.ItemIsEditable

            def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
                if role == Qt.ItemDataRole.EditRole:
                    old_path_str = self.filePath(index)
                    old_path = Path(old_path_str)
                    new_name = str(value).strip()

                    if not new_name or new_name == old_path.name:
                        self._new_folder_path = None  # редактор больше не ждёт новое имя
                        return False

                    new_path = old_path.parent / new_name
                    if new_path.exists():
                        QMessageBox.warning(None, "Ошибка", "Папка с таким именем уже существует.")
                        self._new_folder_path = None
                        return False

                    if QDir().rename(old_path_str, str(new_path)):
                        self._new_folder_path = None
                        return True

                    self._new_folder_path = None
                    return False
                return super().setData(index, value, role)
        
        self.dir_model = CleanDirModel(self)
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath(QDir.rootPath())
        
        self.dir_tree = DirectoryTree()
        self.dir_tree.setModel(self.dir_model)
        self.dir_tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.dir_tree.setSortingEnabled(True)
        self.dir_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
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
        self.grid.contextRequested.connect(self._show_grid_context_menu)
        self.grid.audioRequested.connect(self._open_grid_audio)
        self.grid.audioHoverChanged.connect(self._set_grid_audio_hover)
        self.grid.orderDropped.connect(self._save_custom_grid_order)
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
        self.card_import_button = QPushButton("Импорт с карты памяти")
        self.card_import_button.setObjectName("cardImportButton")
        self.card_import_button.setIcon(self.removable_volume_icon)
        self.card_import_button.setIconSize(QSize(22, 22))
        self.card_import_button.clicked.connect(self._request_card_import)
        self.card_import_button.hide()
        sidebar_layout.addWidget(self.card_import_button)
        self.drive_button_layout = QHBoxLayout()
        self.drive_button_layout.setContentsMargins(0, 0, 0, 0)
        self.drive_button_layout.setSpacing(3)
        self.drive_button_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        sidebar_layout.addLayout(self.drive_button_layout)

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

        self.up_button = QToolButton()
        self.up_button.setObjectName("directoryAction")
        self.up_button.setIcon(_fomantic_icon("arrow-up", 20, "#e6e6e6"))
        self.up_button.setIconSize(QSize(20, 20))
        self.up_button.setToolTip("На уровень вверх")
        self.up_button.clicked.connect(self._go_up_directory)
        directory_header.addWidget(self.up_button)

        self.new_folder_button = QToolButton()
        self.new_folder_button.setObjectName("directoryAction")
        self.new_folder_button.setIcon(_fomantic_icon("folder-plus", 20, "#e6e6e6"))
        self.new_folder_button.setIconSize(QSize(20, 20))
        self.new_folder_button.setToolTip("Создать папку")
        self.new_folder_button.clicked.connect(self._create_new_folder)
        directory_header.addWidget(self.new_folder_button)
        directory_layout.addLayout(directory_header)
        directory_layout.addWidget(self.dir_tree, 1)

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
        if self.transfer_manager is not None:
            self.transfer_queue_panel = TransferQueuePanel(self.transfer_manager)
            self.favorites_splitter.addWidget(self.transfer_queue_panel)
        self.favorites_splitter.setStretchFactor(0, 1)
        self.favorites_splitter.setStretchFactor(1, 0)
        if self.favorites_splitter.count() == 3:
            self.favorites_splitter.setStretchFactor(2, 0)
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

        
        self.rating_filter = FilterComboBox()
        self.rating_filter.addItem("Все рейтинги", None)
        self.rating_filter.setItemIcon(0, _fomantic_icon("star", 10, "#a8b0bd"))
        for rating in range(5, 0, -1):
            self.rating_filter.addItem("★" * rating, rating)
            self.rating_filter.setItemIcon(self.rating_filter.count() - 1, _fomantic_icon("star", 10, "#a8b0bd"))
        self.rating_filter.setFixedWidth(118)
        self.color_filter = FilterComboBox()
        for label, value in (("Все цвета", None), ("Без цвета", ""), ("Красный", "red"), ("Жёлтый", "yellow"), ("Зелёный", "green"), ("Синий", "blue"), ("Фиолетовый", "purple")):
            self.color_filter.addItem(label, value)
            if value is not None:
                self.color_filter.setItemIcon(self.color_filter.count() - 1, _color_swatch_icon(value or None))
        self.color_filter.setItemIcon(0, _fomantic_icon("brush", 10, "#a8b0bd"))
        self.color_filter.setFixedWidth(118)
        self.media_filter = FilterComboBox()
        for label, value in (("Фото и видео", None), ("Фото", "image"), ("Видео", "video")):
            self.media_filter.addItem(label, value)
        self.media_filter.setItemIcon(0, _fomantic_icon("media", 10, "#a8b0bd"))
        self.media_filter.setItemIcon(1, _fomantic_icon("images", 10, "#a8b0bd"))
        self.media_filter.setItemIcon(2, _fomantic_icon("film", 10, "#a8b0bd"))
        self.media_filter.setFixedWidth(118)
        self.file_type_filter = FilterComboBox()
        for label, value in (("JPG и RAW", None), ("Только JPG", "jpg"), ("Только RAW", "raw")):
            self.file_type_filter.addItem(label, value)
        self.file_type_filter.setItemIcon(0, _fomantic_icon("images", 10, "#a8b0bd"))
        self.file_type_filter.setItemIcon(1, _fomantic_icon("file", 10, "#a8b0bd"))
        self.file_type_filter.setItemIcon(2, _fomantic_icon("camera", 10, "#a8b0bd"))
        self.file_type_filter.setFixedWidth(106)
        self.camera_filter = FilterComboBox()
        self.camera_filter.addItem("Все камеры", None)
        self.camera_filter.setItemIcon(0, _fomantic_icon("images", 10, "#a8b0bd"))
        self.camera_filter.setFixedWidth(132)
        self.shot_filter = FilterComboBox()
        for label, value in (("Все планы", None), ("Крупный", "closeup"), ("Средний", "medium"), ("Общий", "wide"), ("Без лиц", "no_face")):
            self.shot_filter.addItem(label, value)
        self.shot_filter.hide()
        self.eyes_filter = FilterComboBox()
        for label, value in (("Все глаза", None), ("Закрытые глаза", "closed")):
            self.eyes_filter.addItem(label, value)
        self.eyes_filter.hide()
        self.focus_filter = FilterComboBox()
        for label, value in (("Весь фокус", None), ("Не в фокусе / смаз", "defect")):
            self.focus_filter.addItem(label, value)
        self.focus_filter.hide()
        self.sort_combo = FilterComboBox()
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
        self.search_edit.setFixedHeight(self.media_filter.sizeHint().height())
        for control in (self.rating_filter, self.color_filter, self.media_filter, self.file_type_filter, self.camera_filter, self.shot_filter, self.eyes_filter, self.focus_filter, self.sort_combo):
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
        self.face_clear_button.setIcon(_fomantic_icon("close", 14))
        self.face_clear_button.setFixedSize(23, 23)
        self.face_clear_button.setIconSize(QSize(14, 14))
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
        self.xmp_button.setToolTip("Синхронизация метаданных XMP")
        self.xmp_button.clicked.connect(self._handle_xmp_button)
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
        self.status_progress = AiProgressBar()
        self.status_progress.setObjectName("viewerStatusProgress")
        self.status_progress.setFixedHeight(14)
        self.status_progress.setTextVisible(True)
        progress_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        progress_policy.setRetainSizeWhenHidden(True)
        self.status_progress.setSizePolicy(progress_policy)
        self.status_progress.setToolTip("Нажмите на крестик справа, чтобы остановить AI-анализ")
        self.status_progress.cancelRequested.connect(self._cancel_ai_analysis)
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

        self.eyes_group = QWidget()
        self.eyes_group.setObjectName("aiPanelGroup")
        eyes_layout = QHBoxLayout(self.eyes_group)
        eyes_layout.setContentsMargins(0, 0, 0, 0)
        eyes_layout.setSpacing(3)
        self.eyes_panel_title = QLabel("ГЛАЗА")
        self.eyes_panel_title.setObjectName("aiPanelTitle")
        eyes_layout.addWidget(self.eyes_panel_title)
        # Один переключатель: нажатие включает фильтр брака, повторное — снимает.
        self.eyes_toggle = QToolButton()
        self.eyes_toggle.setObjectName("shotFilter")
        self.eyes_toggle.setText("Закрытые глаза")
        self.eyes_toggle.setCheckable(True)
        self.eyes_toggle.clicked.connect(self._toggle_eyes_filter)
        eyes_layout.addWidget(self.eyes_toggle)

        self.focus_group = QWidget()
        self.focus_group.setObjectName("aiPanelGroup")
        focus_layout = QHBoxLayout(self.focus_group)
        focus_layout.setContentsMargins(0, 0, 0, 0)
        focus_layout.setSpacing(3)
        self.focus_panel_title = QLabel("ФОКУС")
        self.focus_panel_title.setObjectName("aiPanelTitle")
        focus_layout.addWidget(self.focus_panel_title)
        # Переключатель брака по фокусу/смазу по аналогии с «Закрытые глаза».
        self.focus_toggle = QToolButton()
        self.focus_toggle.setObjectName("shotFilter")
        self.focus_toggle.setText("Не в фокусе / смаз")
        self.focus_toggle.setCheckable(True)
        self.focus_toggle.clicked.connect(self._toggle_focus_filter)
        focus_layout.addWidget(self.focus_toggle)

        ai_layout.addStretch(1)
        ai_layout.addWidget(self.eyes_group)
        ai_layout.addWidget(self.focus_group)
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
        self.grid_restore_loader = QFrame(self.grid_content_stack)
        self.grid_restore_loader.setObjectName("gridRestoreLoader")
        loader_layout = QVBoxLayout(self.grid_restore_loader)
        loader_layout.setContentsMargins(22, 16, 22, 16)
        loader_layout.setSpacing(8)
        self.grid_restore_loader_label = QLabel("Папка открывается")
        self.grid_restore_loader_label.setObjectName("gridRestoreLoaderText")
        self.grid_restore_loader_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        loader_layout.addWidget(self.grid_restore_loader_label)
        loader_progress = QProgressBar()
        loader_progress.setObjectName("gridRestoreLoaderProgress")
        loader_progress.setRange(0, 0)
        loader_progress.setTextVisible(False)
        loader_layout.addWidget(loader_progress)
        self.grid_restore_loader.setFixedSize(220, 76)
        self.grid_restore_loader.hide()
        self.grid_restore_loader_timer = QTimer(self)
        self.grid_restore_loader_timer.setSingleShot(True)
        self.grid_restore_loader_timer.setInterval(100)
        self.grid_restore_loader_timer.timeout.connect(self._show_grid_restore_loader_if_needed)
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
        """Строит заглушку для облачной съёмки, ещё не загруженной на диск."""
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
        """Создаёт действия рабочей вкладки и привязывает горячие клавиши."""
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
        add_hotkey("grid", self._show_grid_or_open_single_photo_folder)
        add_hotkey("strip_collapse", lambda: self.full_view.cycle_strip(1), target=self.full_view, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        add_hotkey("strip_expand", lambda: self.full_view.cycle_strip(-1), target=self.full_view, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

        escape = QAction("Back", self)
        escape.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        escape.triggered.connect(self._handle_escape)
        self.addAction(escape)

        add_hotkey("refresh", lambda: self.load_directory(self.current_dir))
        add_hotkey("fullscreen", self.toggle_fullscreen)
        add_hotkey("quick_mark", self._apply_quick_mark)
        add_hotkey("comment", self._show_comment_dialog)
        add_hotkey("create_folder", self._create_new_folder)
        add_hotkey("quick_copy", lambda: self._show_quick_transfer(move=False))
        add_hotkey("quick_move", lambda: self._show_quick_transfer(move=True))
        add_hotkey("card_import", self._request_card_import)

        for rating in range(0, 6):
            add_hotkey(f"rating_{rating}", lambda _checked=False, value=rating: self._set_selected_rating(value or None))

        for index, color in enumerate(("", "red", "yellow", "green", "blue", "purple")):
            add_hotkey(f"color_{index}", lambda _checked=False, value=color: self._set_selected_color(value))

        self._reload_hotkeys()

    def _reload_hotkeys(self) -> None:
        for identifier, action in self._hotkey_actions.items():
            action.setShortcut(_hotkey_sequence(self.settings, identifier))

    def _quick_transfer_destinations(self) -> list[Path]:
        """Собирает цели переноса: последнюю, открытые вкладки и историю."""
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
        """Запрашивает цель быстрого копирования или перемещения выделения."""
        sources = [path for path in self._file_panel_paths() if path.exists()]
        if not sources:
            return
        identifier = "quick_move" if move else "quick_copy"
        operation = "переместить" if move else "скопировать"
        dialog = QuickTransferDialog(
            operation,
            self._quick_transfer_destinations(),
            _hotkey_sequence(self.settings, identifier),
            lambda destination, update_recent: self._quick_transfer_to(
                sources, destination, move, update_recent
            ),
            self,
        )
        # Иначе QAction с оконным контекстом перехватит повторное сочетание
        # раньше диалога. Пока он открыт, оба сочетания быстрого переноса
        # принадлежат именно ему.
        quick_actions = [self._hotkey_actions[name] for name in ("quick_copy", "quick_move")]
        enabled = [action.isEnabled() for action in quick_actions]
        for action in quick_actions:
            action.setEnabled(False)
        try:
            dialog.exec()
        finally:
            for action, was_enabled in zip(quick_actions, enabled):
                action.setEnabled(was_enabled)

    def _quick_transfer_to(self, sources: list[Path], destination: Path, move: bool, update_recent: bool) -> None:
        if update_recent:
            self._remember_quick_transfer_destination(destination)
        self._receive_dropped_paths(
            sources,
            destination,
            Qt.DropAction.MoveAction if move else Qt.DropAction.CopyAction,
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

    def _set_grid_restore_loader_visible(self, visible: bool) -> None:
        """Показывает оверлей, пока сетка собирается вне видимой области пользователя."""
        if not hasattr(self, "grid_restore_loader"):
            return
        if visible:
            loader = self.grid_restore_loader
            loader.move(
                max(0, (self.grid_content_stack.width() - loader.width()) // 2),
                max(0, (self.grid_content_stack.height() - loader.height()) // 2),
            )
            loader.show()
            loader.raise_()
            return
        self.grid_restore_loader.hide()

    def _schedule_grid_restore_loader(self) -> None:
        """Откладывает оверлей, чтобы быстрый переход между папками не мигал."""
        self.grid_restore_loader_timer.start()

    def _show_grid_restore_loader_if_needed(self) -> None:
        """Показывает оверлей лишь для ещё не завершённого восстановления позиции."""
        if self._restoring_folder_grid_context:
            self.grid_restore_loader_label.setText("Папка открывается")
            self._set_grid_restore_loader_visible(True)
        elif self._restoring_view_context:
            self.grid_restore_loader_label.setText("Обновляю список")
            self._set_grid_restore_loader_visible(True)

    def _hide_grid_restore_loader(self) -> None:
        """Отменяет отложенный оверлей и сразу убирает уже показанный."""
        self.grid_restore_loader_timer.stop()
        self._set_grid_restore_loader_visible(False)

    def _set_face_search_loading(self, loading: bool) -> None:
        """Показывает поиск лица там, где пользователь увидит его до перестройки списка."""
        if getattr(self, "_file_mutation_waiting", False):
            # Поздний callback отменённого поиска не должен переименовать
            # индикатор уже начатой файловой операции.
            self.full_view.set_face_search_loading(False)
            return
        # Пользователь может сменить grid на FullView, пока запрос работает.
        # Оба оверлея должны разделять состояние, иначе один останется поверх результата.
        self.full_view.set_face_search_loading(loading)
        if loading:
            self.grid_restore_loader_label.setText("Ищу похожие лица")
            self._set_grid_restore_loader_visible(True)
            return
        self.grid_restore_loader_label.setText("Папка открывается")
        self._set_grid_restore_loader_visible(self._restoring_folder_grid_context)

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "grid_content_stack", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._position_grid_zoom_controls)
            QTimer.singleShot(0, lambda: self._set_grid_restore_loader_visible(self.grid_restore_loader.isVisible()))
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
                        self._delete_paths(
                            [Path(self.dir_model.filePath(index))],
                            permanent=self._delete_permanently_for_shortcut(
                                bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                            ),
                        )
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

    def _show_grid_or_open_single_photo_folder(self) -> None:
        """По G превращает временный просмотр файла в обычную вкладку папки."""
        if self.single_photo_mode:
            self.singlePhotoFolderRequested.emit(self)
            return
        self.show_grid()

    def _go_up_directory(self) -> None:
        """Переходит в родительскую папку текущего каталога."""
        if self.current_dir and self.current_dir.parent != self.current_dir:
            self.load_directory(self.current_dir.parent)

    def _expand_tree_path(self, index) -> None:
        """Раскрывает родителей каталога, чтобы нужная строка стала видимой."""
        parents = []
        current = index.parent()
        while current.isValid():
            parents.append(current)
            current = current.parent()
        for parent in reversed(parents):
            self.dir_tree.expand(parent)

    def _begin_directory_inline_rename(self, path: Path, index=None, attempts_left: int = 20) -> None:
        """Запускает встроенное переименование, дождавшись готовности модели."""
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
        """Проверяет новое имя папки и передаёт переименование файловой модели."""
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
        """Создаёт новую папку и сразу предлагает дать ей нормальное имя."""
        parent_dir = parent_dir or self.current_dir
        if not parent_dir:
            return

        i = 1
        while True:
            temp_name = f"Новая папка {i}"
            temp_path = parent_dir / temp_name
            if not temp_path.exists():
                break
            i += 1

        try:
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
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать папку: {e}")
            self.dir_model._new_folder_path = None  # повторный редактор уже не появится

    def _directory_editor_closed(self, _editor, _hint) -> None:
        """Сбрасывает состояние редактора даже после отмены переименования."""
        if self.dir_model._new_folder_path is None:
            return
        self.dir_model._new_folder_path = None

    def _directory_selected(self, index) -> None:
        path = Path(self.dir_model.filePath(index))
        self.load_directory(path)

    @staticmethod
    def _favorite_path_key(path: Path) -> str:
        return filesystem_path_key(path)

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
        visible_widgets = sum(
            not self.favorites_splitter.widget(index).isHidden()
            for index in range(self.favorites_splitter.count())
        )
        handles = max(0, visible_widgets - 1)
        available = max(
            48,
            self.favorites_splitter.height() - self.favorites_splitter.handleWidth() * handles,
        )
        height = max(48, min(int(requested), max(48, available - 48)))
        if self.favorites_splitter.count() == 3:
            transfer_height = (
                max(108, self.transfer_queue_panel.sizeHint().height())
                if not self.transfer_queue_panel.isHidden()
                else 0
            )
            directory_height = max(48, available - height - transfer_height)
            self.favorites_splitter.setSizes([directory_height, height, transfer_height])
        else:
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
        """Раскрывает дерево и выбирает избранную папку после загрузки модели."""
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
        """Строит контекстное меню папки с учётом корня и текущего выбора."""
        index = self.dir_tree.indexAt(position)
        if not index.isValid():
            return
        path = Path(self.dir_model.filePath(index))
        if not path.is_dir():
            return
        self.dir_tree.setCurrentIndex(index)
        menu = QMenu(self.dir_tree)
        self._populate_folder_context_menu(menu, path)
        menu.exec(self.dir_tree.viewport().mapToGlobal(position))

    def _populate_folder_context_menu(self, menu: QMenu, path: Path) -> None:
        """Добавляет одинаковый набор действий папки для дерева и сетки."""
        open_tab = menu.addAction("Открыть в новой вкладке")
        open_tab.setIcon(_fomantic_icon("folder", 13))
        open_tab.triggered.connect(
            lambda _checked=False, target=path: self.openFolderRequested.emit(target)
        )
        menu.addSeparator()
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

    def _show_grid_context_menu(self, path: Path, global_position: QPoint) -> None:
        """Показывает меню папки или нативное меню файла для карточки сетки."""
        path = Path(path)
        if path.is_dir():
            menu = QMenu(self.grid)
            self._populate_folder_context_menu(menu, path)
            menu.exec(global_position)
            return
        if not path.is_file():
            return

        if sys.platform == "win32":
            try:
                show_file_context_menu(
                    path,
                    int(self.window().winId()),
                    global_position.x(),
                    global_position.y(),
                )
                return
            except Exception:
                # Shell-расширения и исчезнувшие файлы не должны ломать сетку.
                pass

        menu = QMenu(self.grid)
        open_file = menu.addAction("Открыть")
        open_file.triggered.connect(
            lambda _checked=False, target=path: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(target))
            )
        )
        reveal = menu.addAction("Показать в папке")
        reveal.triggered.connect(
            lambda _checked=False, target=path: self._reveal_file_in_system(target)
        )
        menu.exec(global_position)

    @staticmethod
    def _reveal_file_in_system(path: Path) -> None:
        """Выделяет файл в Проводнике либо открывает его родительскую папку."""
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["explorer.exe", f"/select,{path}"],
                    **detached_process_kwargs(),
                )
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        except OSError:
            return

    def _delete_permanently_for_shortcut(self, shift_pressed: bool) -> bool:
        """Определяет действие Delete, сохраняя Shift обратным переключателем."""
        default_permanent = self.settings.value(
            "behavior/delete_permanently_on_del", False, bool
        )
        return default_permanent != shift_pressed

    def _delete_grid_selection(self, shift_pressed: bool) -> None:
        self._delete_paths(
            self._selected_paths(),
            permanent=self._delete_permanently_for_shortcut(shift_pressed),
        )

    def _file_panel_paths(self) -> list[Path]:
        """Возвращает пути, выбранные в активной файловой панели."""
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
        """Подбирает свободное имя назначения в виде ``имя (N)``."""
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

    def _receive_dropped_paths(
        self,
        paths: list[Path],
        destination: Path | None,
        action,
        progress: Callable[..., None] | None = None,
        *,
        _consumers_released: bool = False,
    ) -> None:
        """Копирует внешние файлы или переносит внутреннее перетаскивание в цель."""
        if destination is None:
            destination = self.current_dir
        if not destination.is_dir():
            return
        sources = list(dict.fromkeys(path for path in paths if path.exists()))
        if not sources:
            return
        if self.transfer_manager is not None:
            self._enqueue_transfer(sources, destination, action)
            return
        # Для индикатора считаем байты самих файлов. Папки остаются в общем
        # счётчике объектов: обходить весь их состав до начала операции означало
        # бы получить второй, заметный проход по медленному диску.
        total_bytes = sum(
            path.stat().st_size for path in sources if path.is_file()
        )
        transferred_bytes = 0
        if progress is not None:
            progress(0, len(sources), 0, total_bytes)
        move = action == Qt.DropAction.MoveAction
        if (
            self._selection_progress is not None
            or self._upload_progress is not None
            or self._receive_progress is not None
        ):
            QMessageBox.information(
                self,
                "Файловая операция",
                "Дождитесь завершения текущего приёма, отбора или отправки ShotSync.",
            )
            return
        if not _consumers_released and self._paths_touch_current_workspace([*sources, destination]):
            self._run_after_file_consumers_release(
                [*sources, destination],
                lambda selected=list(sources), target=destination, drop_action=action, callback=progress:
                self._receive_dropped_paths(
                    selected,
                    target,
                    drop_action,
                    callback,
                    _consumers_released=True,
                ),
                loading_text=(
                    "Выполняется перемещение"
                    if move
                    else "Выполняется копирование"
                ),
            )
            return
        if move and self.current_dir in sources:
            self.load_directory(self.current_dir.parent)
        errors: list[str] = []
        changed = False
        moved_files: list[Path] = []
        moved_folders: list[tuple[Path, Path]] = []
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
                if not is_folder:
                    transferred_bytes += source.stat().st_size if source.exists() else target.stat().st_size
                changed = True
            except OSError as exc:
                errors.append(f"{source.name}: {exc}")
            if progress is not None:
                progress(completed, len(sources), transferred_bytes, total_bytes)
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
                self._remove_paths_from_grid(moved_from_current)
                self.cache_flush_executor.submit(prune_folder_cache, self.current_dir)
            if self.current_dir == destination:
                self.folder_change_timer.stop()
                self.load_directory(self.current_dir)
        if errors:
            QMessageBox.warning(self, "Копирование файлов", "Не удалось обработать некоторые объекты:\n" + "\n".join(errors))

    def _enqueue_transfer(self, sources: list[Path], destination: Path, action) -> None:
        """Согласует корневые конфликты и передаёт операцию глобальному менеджеру."""
        if self.transfer_manager is None:
            return
        move = action == Qt.DropAction.MoveAction
        auto_rename = self.settings.value("transfers/auto_rename_conflicts", True, bool)
        entries: list[TransferEntry] = []
        errors: list[str] = []
        for source in sources:
            try:
                source_resolved = source.resolve()
                destination_resolved = destination.resolve()
            except OSError:
                source_resolved, destination_resolved = source, destination
            if source.parent == destination:
                if not move:
                    errors.append(f"{source.name}: уже находится в этой папке")
                continue
            if source.is_dir() and destination_resolved.is_relative_to(source_resolved):
                errors.append(f"{source.name}: нельзя поместить папку внутрь самой себя")
                continue
            target = destination / source.name
            replace = False
            if target.exists() or self.transfer_manager.target_reserved(target):
                resolution = "rename" if auto_rename else self._resolve_transfer_conflict(source, target, move)
                if resolution == "cancel":
                    break
                if resolution == "skip":
                    continue
                if resolution == "rename":
                    try:
                        target = self._queued_renamed_transfer_target(target)
                    except OSError as exc:
                        errors.append(f"{source.name}: {exc}")
                        continue
                else:
                    replace = True
            entries.append(TransferEntry(source, target, replace))
        if entries:
            self.transfer_manager.enqueue(entries, destination, move=move)
        if errors:
            QMessageBox.warning(
                self,
                "Файловая операция",
                "Не удалось поставить некоторые объекты в очередь:\n" + "\n".join(errors),
            )

    def _queued_renamed_transfer_target(self, target: Path) -> Path:
        """Подбирает имя с учётом диска и ещё не запущенных заданий очереди."""
        for number in range(2, 10_000):
            candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
            if not candidate.exists() and not (
                self.transfer_manager is not None
                and self.transfer_manager.target_reserved(candidate)
            ):
                return candidate
        raise OSError("Не удалось подобрать свободное имя")

    def _delete_paths(self, paths: list[Path], *, permanent: bool) -> None:
        """Удаляет выбранные файлы или папки, учитывая локальные копии ShotSync."""
        targets = list(dict.fromkeys(path for path in paths if path.exists()))
        if not targets:
            return
        if (
            self._selection_progress is not None
            or self._upload_progress is not None
            or self._receive_progress is not None
        ):
            QMessageBox.information(
                self,
                "Удаление",
                "Дождитесь завершения текущего приёма, отбора или отправки ShotSync.",
            )
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

        self._run_after_file_consumers_release(
            targets,
            lambda selected=list(targets), remove_permanently=permanent:
            self._delete_paths_now(selected, permanent=remove_permanently),
            loading_text="Выполняется удаление",
        )

    def _delete_paths_now(self, targets: list[Path], *, permanent: bool) -> None:
        """Удаляет подтверждённые пути после освобождения фоновых читателей."""

        deleting_current_folder = self.current_dir in targets
        if deleting_current_folder:
            self.load_directory(self.current_dir.parent)

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
            self.cache_flush_executor.submit(prune_folder_cache, self.current_dir)

        deleted = [*deleted_files, *deleted_folders]
        if deleted and not deleting_current_folder:
            self._remove_paths_from_grid(deleted)
        if errors:
            QMessageBox.warning(self, "Удаление", "Не удалось удалить:\n" + "\n".join(errors))

    def _remove_paths_from_grid(self, deleted: list[Path]) -> None:
        """Убирает удалённые пути из сетки без её полной пересборки."""
        removed = set(deleted)
        old_paths = list(self.paths)
        path_rows = {path: index for index, path in enumerate(old_paths)}
        selected_rows = [path_rows[path] for path in removed if path in path_rows]
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
        self._rebuild_status_index()
        self.populate_timer.stop()
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()

        if len(removed) >= 128:
            self._apply_view()
            return

        visible = set(self.paths)
        for path, item in list(self.items_by_path.items()):
            if path not in visible:
                self.grid.takeItem(self.grid.row(item))
                self.items_by_path.pop(path, None)

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
        """Синхронизирует боковую панель со смонтированными дисками.

        Пустые кардридеры файловой системы не имеют, поэтому ``QStorageInfo`` их
        не возвращает. Заодно в интерфейс не попадают недоступные носители.
        """
        volumes = _mounted_volume_paths()
        removable_volumes = [path for path in volumes if _is_removable_volume(path)]
        self.card_import_button.setVisible(bool(removable_volumes))
        volume_keys = {_drive_key(path) for path in volumes}
        existing = {
            button.property("volumeKey"): button
            for button in self.drive_buttons.buttons()
        }

        for key, button in existing.items():
            if key == SHOTSYNC_VOLUME_KEY:
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

        if not self.closing and not self.current_dir.is_dir():
            fallback = Path.home()
            self._set_tree_root_for_path(fallback)
            self.load_directory(fallback)

    def _request_card_import(self) -> None:
        """Передаёт главному окну доступные съёмные тома для единого импорта."""
        volumes = [path for path in _mounted_volume_paths() if _is_removable_volume(path)]
        if volumes:
            self.cardImportRequested.emit([(path, _volume_label(path)) for path in volumes])

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

    def _shotsync_button_icon(self) -> QIcon:
        """Загружает логотип ShotSync из ресурсов приложения."""
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
        """Переключает боковую панель с папок на ShotSync."""
        self.shotsync_active = True
        self.shotsync_button.setChecked(True)
        for button in self.drive_buttons.buttons():
            if button is not self.shotsync_button:
                button.setChecked(False)
        self.sidebar_stack.setCurrentWidget(self.shotsync_panel)

        if self.shotsync_client.has_key():
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
        """Открывает форму входа ShotSync и проверяет введённые данные."""
        if self.shotsync_client.has_key():
            return True
        dialog = self._ensure_shotsync_login_dialog()
        dialog.reset()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _ensure_shotsync_login_dialog(self) -> ShotSyncLoginDialog:
        if self.shotsync_login_dialog is None:
            dialog = ShotSyncLoginDialog(self)
            dialog.loginSubmitted.connect(self._shotsync_login)
            self.shotsync_login_dialog = dialog
        return self.shotsync_login_dialog

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
        if self.shotsync_login_dialog is not None:
            self.shotsync_login_dialog.login_succeeded()
        self.shotsync_panel.show_logged_in(user)
        avatar_url = user.get("avatar_url")
        if avatar_url:
            self.shotsync_client.fetch_avatar(avatar_url)
        self.shotsync_panel.set_shootings_loading()
        self.shotsync_client.fetch_shootings()
        self._sync_code_replacements()
        self._sync_face_sets()
        self._refresh_shotsync_shortcuts()

    def _shotsync_login_failed(self, error: str) -> None:
        if self.shotsync_login_dialog is not None:
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
        self._sync_face_sets()
        self._refresh_shotsync_shortcuts()

    def _shotsync_session_invalid(self, error: str) -> None:
        self._shotsync_checked = False
        self.settings.remove("shotsync/api_key")
        self.shotsync.set_api_key("")
        self._set_code_replacements(self._local_code_replacement_sets())
        if self.shotsync_active:
            self.shotsync_panel.show_login()
        self._refresh_shotsync_shortcuts()

    def _shotsync_session_check_failed(self, error: str) -> None:
        """Оставляет доступ к локальным отборам, когда ShotSync недоступен."""
        self._shotsync_checked = True
        self.shotsync_panel.show_logged_in({})
        self._show_local_shotsync_shootings(error)
        self._refresh_shotsync_shortcuts()

    def _sync_code_replacements(self) -> None:
        """Получает с сервера наборы кодов; изменения диалог отправляет сразу."""
        if not self.shotsync_client.has_key():
            return
        self.shotsync_client.request_json(
            "/api/users/code-replacements/",
            lambda ok, data, _error: self._set_code_replacements(data.get("sets", [])) if ok else None,
        )

    def _local_code_replacement_sets(self) -> list[dict]:
        """Читает локальную библиотеку замен из настроек приложения."""
        sets = self.settings.value("code_replacements/local_sets", [], list)
        return [entry for entry in sets if isinstance(entry, dict)]

    def _sync_face_sets(self) -> None:
        """Сверяет локальные наборы лиц с библиотекой пользователя в ShotSync.

        После входа сервер считается источником истины. Локальные аватары при
        объединении сохраняются, отсутствующие наборы отправляются на сервер, а
        недостающие превью загружаются обратно.
        """
        if not self.shotsync_client.has_key():
            return
        self.shotsync_client.request_json(
            "/api/users/faces/",
            lambda ok, data, _error: self._apply_server_faces(data.get("faces", [])) if ok else None,
        )

    def _apply_server_faces(self, server_faces: list) -> None:
        faces = server_faces if isinstance(server_faces, list) else []
        merged, to_push, previews = merge_server_faces(self.face_sets, faces)
        self.face_sets = merged
        self._save_face_sets()
        self._update_analysis_controls()
        for entry in to_push:
            self._push_face_set(entry)
        for local_id, photo_url in previews:
            self._fetch_face_preview(local_id, photo_url)

    def _push_face_set(self, entry: dict) -> None:
        """Отправляет локальный набор лиц в ShotSync и запоминает его серверный ID."""
        if entry.get("server_id") or not self.shotsync_client.has_key():
            return
        if not entry.get("embedding"):
            return
        photo = None
        avatar = str(entry.get("avatar") or "")
        if avatar:
            try:
                photo = ("face.png", base64.b64decode(avatar), "image/png")
            except (ValueError, TypeError):
                photo = None
        local_id = entry.get("id")

        def done(ok: bool, data: dict, _error: str) -> None:
            face = data.get("face") if ok and isinstance(data, dict) else None
            if not isinstance(face, dict) or face.get("id") is None:
                return
            target = self._face_set_by_id(str(local_id))
            if target is not None:
                target["server_id"] = int(face["id"])
                self._save_face_sets()

        self.shotsync_client.post_multipart(
            "/api/users/faces/upload/", upload_fields_for_entry(entry), photo, done
        )

    def _fetch_face_preview(self, local_id: str, photo_url: str) -> None:
        """Загружает превью лица и сохраняет его локально как аватар Base64."""
        def done(ok: bool, data: bytes) -> None:
            if not ok or not data:
                return
            target = self._face_set_by_id(str(local_id))
            if target is None:
                return
            target["avatar"] = base64.b64encode(data).decode("ascii")
            self._save_face_sets()
            self._update_analysis_controls()

        self.shotsync_client.fetch_bytes(photo_url, done)

    def _set_code_replacements(self, sets: list[dict]) -> None:
        self.code_replacement_sets = [entry for entry in sets if isinstance(entry, dict)]
        if self._xmp_auto_enabled():
            self._queue_xmp_paths(path for path in self.all_paths if is_supported_image(path))
        active_id = self.settings.value("code_replacements/active_set_id", 0, int)
        if self.code_replacement_sets and not any(group.get("id") == active_id for group in self.code_replacement_sets):
            active_id = int(self.code_replacement_sets[0].get("id") or 0)
            self.settings.setValue("code_replacements/active_set_id", active_id)
        for editor in (self.comment_edit, self.full_view.full_comment_edit):
            editor.set_codes(self.code_replacement_sets, active_id)

    def _shotsync_shootings_loaded(self, shootings: list) -> None:
        self._shotsync_shootings = [shooting for shooting in shootings if isinstance(shooting, dict)]
        self.shotsync_panel.set_offline_ids(set())
        self._reconcile_shotsync_selection_copies(self._shotsync_shootings)
        self._resume_shotsync_selection_copies(self._shotsync_shootings)
        self.shotsync_panel.set_shootings(shootings)
        self._refresh_shotsync_receiving()
        self._refresh_shotsync_local_folders(shootings)

    def _shotsync_shootings_failed(self, error: str) -> None:
        self._show_local_shotsync_shootings(error)

    def _shotsync_avatar_loaded(self, image) -> None:
        self.shotsync_panel.set_avatar(image)

    def _refresh_shotsync_receiving(self) -> None:
        """Обновляет в панели признаки съёмок, принимаемых прямо сейчас."""
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
        """Возвращает режимы локальных папок, отбрасывая исчезнувшие с диска."""
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

    def _local_shotsync_shootings(self) -> list[dict]:
        """Строит карточки ShotSync по локальным папкам, доступным без сервера."""
        shootings: list[dict] = []
        for shooting_id, mode in self._shotsync_folder_modes().items():
            folder = self._local_shotsync_folder(shooting_id)
            if folder is None:
                continue
            title = f"Съёмка {shooting_id}"
            photo_count = 0
            try:
                names = {path.name for path in folder.iterdir() if path.is_file()}
                cache = FolderCache(folder, live_names=names, load_from_disk=True)
                session = cache.shotsync_session()
                if session is not None and session[1]:
                    title = session[1]
                photo_count = len(cache.shotsync_photo_names())
                cache.close(flush=False)
            except OSError:
                continue
            shootings.append(
                {
                    "id": shooting_id,
                    "title": title,
                    "status": "",
                    "photo_count": photo_count,
                    "viewer_url": "",
                    "local_mode": mode,
                }
            )
        return shootings

    def _show_local_shotsync_shootings(self, error: str) -> None:
        """Показывает сохранённые съёмки, когда связи с ShotSync нет."""
        shootings = self._local_shotsync_shootings()
        self._shotsync_shootings = shootings
        self.shotsync_panel.set_offline_ids({int(shooting["id"]) for shooting in shootings})
        self.shotsync_panel.set_shootings(shootings)
        self._refresh_shotsync_local_folders(shootings)
        suffix = " Открыты сохранённые съёмки; изменения синхронизируются при подключении."
        self.shotsync_panel.set_shootings_error(_humanize_shotsync_network_error(error) + suffix)

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
        """Выделяет карточку съёмки, открытой в этой рабочей вкладке."""
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
        """Открывает локальную папку съёмки или предлагает облачные действия."""
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
        """Удаляет локальную копию отбора, не затрагивая съёмку на сервере."""
        shooting_id = int(shooting.get("id") or 0)
        title = str(shooting.get("title") or "Съёмка ShotSync")
        folder = self._local_shotsync_folder(shooting_id, title)
        if not shooting_id or folder is None:
            return
        resolved = folder.resolve()
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
        """Удаляет загруженную съёмку с сервера, сохраняя исходную папку."""
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
        """Включает или выключает приём съёмки в выбранную локальную папку."""
        shooting_id = int(shooting.get("id") or 0)
        if not shooting_id:
            return
        if self.shotsync.is_receiving(shooting_id):
            self.shotsync.stop_receiving(shooting_id)
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
        """Обновляет открытую папку после появления нового оригинала на диске."""
        if Path(folder) == self.current_dir:
            self.load_directory(self.current_dir)

    def _on_shotsync_receive_progress(self, shooting_id: int, done: int, total: int, retrying: int) -> None:
        self._receive_progress = (done, total, retrying) if total else None
        self._refresh_status_panel()

    def _on_shotsync_mark_updated(self, shooting_id: int, folder: str, photo: dict) -> None:
        """Переносит пришедшую через сокет метку владельца в локальную папку."""
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
        """Применяет серверные метки к открытому локальному отбору ShotSync."""
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
        """Удаляет загруженную копию отбора после удаления съёмки на сервере."""
        self._remove_shotsync_selection_copy(int(shooting_id))

    def _reconcile_shotsync_selection_copies(self, shootings: list[dict]) -> None:
        """Убирает локальные копии съёмок, удалённых за время работы без сети."""
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
        """Обновляет рейтинг, цвет и комментарий файла и перерисовывает карточку."""
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

    def _shotsync_select_requested(self, shooting: dict) -> None:
        """Загружает превью съёмки в локальную папку для отбора."""
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
        """Открывает готовый отбор ShotSync и восстанавливает его связь с сервером."""
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
        """Подключает синхронизацию меток, если открыта папка отбора ShotSync."""
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

    def _shotsync_send_current_folder(self) -> None:
        """Выбирает исходную папку и создаёт для неё съёмку в ShotSync."""
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
        """Показывает немодальный прогресс отправки папки в ShotSync."""
        dialog = QDialog(self)
        self._shotsync_upload_popup = dialog
        dialog.setObjectName("shotsyncUploadPopup")
        dialog.setWindowTitle("Отправить на ShotSync")
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
        """Запрашивает все параметры новой съёмки, включая исходную папку."""
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
        if Path(folder) == self.current_dir:
            self._attach_shotsync_syncer()
            self._refresh_shotsync_shortcuts()

    def _on_shotsync_upload_finished_with_errors(self, shooting_id: int, folder: str, failed: int) -> None:
        """Съёмка отправлена частично: связываем папку и предлагаем догрузку."""
        self._on_shotsync_upload_finished(shooting_id, folder)
        QMessageBox.warning(
            self,
            "ShotSync",
            f"Съёмка отправлена, но {failed} фото не удалось загрузить.\n"
            "Повторите отправку той же папки — недостающие кадры догрузятся.",
        )

    def _on_shotsync_upload_failed(self, message: str) -> None:
        self._upload_progress = None
        self.grid_content_stack.setCurrentWidget(self.grid)
        self._refresh_status_panel()
        QMessageBox.warning(self, "ShotSync", f"Не удалось отправить съёмку:\n{message}")

    def _shotsync_fetch_marks(self) -> None:
        """Получает с сервера метки для открытой папки ShotSync."""
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
        if self.current_dir is not None:
            self.load_directory(self.current_dir)

    def _on_shotsync_marks_failed(self, message: str) -> None:
        self._shotsync_marks_fetching = False
        self._refresh_status_panel()
        QMessageBox.warning(self, "ShotSync", f"Не удалось получить метки:\n{message}")

    def _refresh_shotsync_shortcuts(self) -> None:
        """Включает только те действия ShotSync, которые подходят текущей папке."""
        is_session = False
        if self.folder_cache is not None and self.cache_ready:
            is_session = self.folder_cache.shotsync_session() is not None
        can_send = (
            self.current_dir is not None and self.shotsync_client.has_key()
        )
        self.shotsync_panel.set_folder_actions(can_send=can_send, is_session=is_session)

    def _refresh_shotsync_tab_indicator(self) -> None:
        """Сообщает панели вкладок, связана ли открытая папка с ShotSync."""
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
        """Начинает асинхронную загрузку папки и сбрасывает состояние прошлого вида.

        Результаты старых задач защищены поколением запроса: если пользователь
        уже ушёл в другую папку, запоздавший ответ тихо выбрасывается.
        """
        if self.closing:
            return
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
        if self._ai_pipeline is not None and self._ai_pipeline.pending_count() > 0:
            self._ai_pipeline.shutdown()
            self._ai_pipeline = None
            self._resume_ai_when_active = False
        if self._metadata_pipeline is not None:
            self._metadata_pipeline.shutdown()
            self._metadata_pipeline = None
        self.video_thumbnailer.cancel()
        self.decode_cache.clear()
        self.populate_timer.stop()
        self.thumb_timer.stop()
        if self._ai_pipeline is None or self._ai_pipeline.pending_count() == 0:
            self.ai_progress_timer.stop()
        self.ai_progress_total = 0
        self._ai_progress_started_at = None
        self.preview_progress_total = 0
        self.preview_paths.clear()
        self.preview_finished_paths.clear()
        self._ai_requested_generation = -1
        self._cache_ai_waiting = False
        self._cache_ai_paths.clear()
        if hasattr(self, "ai_button"):
            self.ai_button.setEnabled(True)
            self._refresh_status_panel()
        self.cache_load_generation += 1
        self.directory_generation += 1
        self._rotate_folder_request_executors()
        self._flush_folder_cache(wait=False, close=True)
        self._detach_shotsync_syncer()
        self.folder_cache = None
        self.cache_ready = False
        self.current_dir = directory
        self._cancel_face_search()
        self._face_search_index = None
        self._face_match_names = None
        self._directory_scan_pending = True
        self._custom_order = self._load_custom_order(directory)
        if self._custom_order and self.sort_combo.findData("custom") < 0:
            self.sort_combo.addItem("Пользовательский", "custom")
            self.sort_combo.setItemIcon(self.sort_combo.count() - 1, _fomantic_icon("sort", 10, "#a8b0bd"))
        self._remember_directory_for_volume(directory)
        self._refresh_shotsync_tab_indicator()
        self._refresh_shotsync_current_shooting()
        self._restore_series_mode(directory)
        self._pending_folder_grid_context = self._load_folder_grid_context(directory)
        self._pending_folder_grid_restore = True
        self.settings.setValue("last_directory", str(directory))
        self.setWindowTitle(_workspace_title(directory))
        self.all_paths = []
        self.view_paths = []
        self.paths = []
        self._pending_view_cursor_path = None
        self._pending_view_selection.clear()
        self._pending_view_scroll = None
        self.photo_details = {}
        self.image_embeddings = {}
        self._xmp_states = {}
        self._xmp_pair_members = {}
        self._xmp_retry_after_change.clear()
        self._xmp_scan_future = None
        self._xmp_scan_generation = -1
        self._xmp_rescan_requested = False
        self._xmp_full_hash_requested = False
        self._xmp_rescan_priority = None
        self._xmp_queue_all_after_scan = False
        self.xmp_bulk_timer.stop()
        self._xmp_bulk_queue.clear()
        self._xmp_bulk_queued.clear()
        self._file_time_cache.clear()
        self.ai_progress_total = 0
        self.items_by_path.clear()
        self._restoring_view_context = False
        self.grid.setUpdatesEnabled(True)
        self.grid.clear()
        self._restoring_folder_grid_context = self._pending_folder_grid_context is not None
        if self._restoring_folder_grid_context:
            self.grid_restore_loader_label.setText("Папка открывается")
            self.grid.setUpdatesEnabled(False)
            self._schedule_grid_restore_loader()
        else:
            self._hide_grid_restore_loader()
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        self.visible_thumb_pending.clear()
        
        index = self.dir_model.index(str(directory))
        if index.isValid():
            self.dir_tree.expand(index.parent())
            self.dir_tree.setCurrentIndex(index)
            self.dir_tree.scrollTo(index)
            
        request = self.workspace_state.begin_directory(directory)
        self._refresh_status_panel()
        future = self.directory_scan_executor.submit(_scan_directory, directory)
        future.add_done_callback(lambda done, r=request, d=directory: self._directory_scanned(r, d, done))

    def _rotate_folder_request_executors(self) -> None:
        """Даёт новому запросу папки свежие потоки без ожидания старого диска."""
        retire_executor(self.directory_scan_executor)
        retire_executor(self.cache_load_executor)
        self.directory_scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="folder-scan")
        self.cache_load_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="folder-cache")

    @staticmethod
    def _folder_settings_prefix(directory: Path) -> str:
        normalized = filesystem_path_key(directory)
        return f"folder_settings/{sha1(normalized.encode()).hexdigest()}"

    def _load_custom_order(self, directory: Path) -> list[str]:
        """Возвращает сохранённую последовательность имён для одной папки."""
        value = self.settings.value(f"{self._folder_settings_prefix(directory)}/custom_order", [], list)
        return [str(name) for name in value] if isinstance(value, (list, tuple)) else []

    def _save_custom_grid_order(self, dragged: object, before: object) -> None:
        """Сохраняет ручную перестановку карточек и сразу включает её сортировку."""
        paths = [Path(path) for path in dragged if isinstance(path, Path)]
        if not paths or not isinstance(before, Path):
            return
        ordered = [path for path in self.view_paths if path not in paths]
        try:
            index = ordered.index(before)
        except ValueError:
            return
        ordered[index:index] = paths
        # Храним полный порядок папки, чтобы фильтры лишь временно скрывали кадры.
        names = [path.name for path in ordered]
        names.extend(path.name for path in self.all_paths if path.name not in names)
        self._custom_order = names
        self.settings.setValue(f"{self._folder_settings_prefix(self.current_dir)}/custom_order", names)
        if self.sort_combo.findData("custom") < 0:
            self.sort_combo.addItem("Пользовательский", "custom")
            self.sort_combo.setItemIcon(self.sort_combo.count() - 1, _fomantic_icon("sort", 10, "#a8b0bd"))
        self.sort_combo.setCurrentIndex(self.sort_combo.findData("custom"))
        self._apply_view()

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
        if not self._pending_folder_grid_restore:
            return
        self._pending_folder_grid_restore = False
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
            self.grid.doItemsLayout()
            self.grid.scrollToItem(selected_items[0], QListWidget.ScrollHint.PositionAtCenter)
        else:
            scroll_bar = self.grid.verticalScrollBar()
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
        if self._restoring_folder_grid_context:
            self._restoring_folder_grid_context = False
            self.grid.setUpdatesEnabled(True)
            self.grid.viewport().update()
            self._hide_grid_restore_loader()

    def _remember_view_context(self) -> None:
        """Запоминает курсор и выделение перед перестройкой фильтрованного грида."""
        if self._pending_folder_grid_restore or self.grid.count() == 0:
            return
        # Несколько сигналов фильтров могут прийти раньше, чем закончат добавляться
        # карточки после первого. Контекст должен оставаться одним снимком, иначе
        # временное выделение нового грида смешается с исходным.
        if (
            self._pending_view_cursor_path is not None
            or self._pending_view_selection
            or self._pending_view_scroll is not None
        ):
            return
        if self.stack.currentWidget() is self.full_view:
            # В FullView именно current_path является курсором. Скрытый grid может
            # всё ещё указывать на кадр, с которого просмотр был когда-то открыт.
            self._pending_view_cursor_path = self.current_path
            self._pending_view_selection = (
                {self.current_path} if self.current_path is not None else set()
            )
        else:
            current = self.grid.currentItem()
            if current is not None:
                value = current.data(Qt.ItemDataRole.UserRole)
                if value:
                    self._pending_view_cursor_path = Path(value)
            self._pending_view_selection = set(self._selected_paths())
        self._pending_view_scroll = self.grid.verticalScrollBar().value()

    def _begin_view_context_restore(self) -> None:
        """Замораживает отрисовку грида до возврата курсора после фильтра."""
        if (
            self._restoring_view_context
            or not self.workspace_active
            or (
                self._pending_view_cursor_path is None
                and not self._pending_view_selection
                and self._pending_view_scroll is None
            )
        ):
            return
        # QListWidget автоматически назначает первую добавленную карточку текущей.
        # Не показываем этот промежуточный кадр: пользователь должен увидеть уже
        # окончательный список с восстановленным курсором.
        self._restoring_view_context = True
        self.grid.setUpdatesEnabled(False)
        self.grid_restore_loader_label.setText("Обновляю список")
        self._schedule_grid_restore_loader()

    def _restore_pending_view_cursor(self) -> None:
        """Возвращает сохранившиеся курсор, выделение и позицию после фильтра."""
        path = self._pending_view_cursor_path
        selected_paths = self._pending_view_selection
        scroll_value = self._pending_view_scroll
        self._pending_view_cursor_path = None
        self._pending_view_selection = set()
        self._pending_view_scroll = None
        selected_items = [
            self.items_by_path[selected]
            for selected in selected_paths
            if selected in self.items_by_path
        ]
        current_item = self.items_by_path.get(path) if path is not None else None
        if current_item is None and selected_items:
            current_item = selected_items[0]
        self.grid.clearSelection()
        if current_item is not None:
            self.grid.setCurrentItem(
                current_item,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
            for item in selected_items:
                item.setSelected(True)
            self.grid.doItemsLayout()
            self.grid.scrollToItem(current_item, QListWidget.ScrollHint.PositionAtCenter)
        elif scroll_value is not None:
            scroll_bar = self.grid.verticalScrollBar()
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
        if self._restoring_view_context:
            self._restoring_view_context = False
            self.grid.setUpdatesEnabled(True)
            self.grid.viewport().update()
            self._hide_grid_restore_loader()

    def _reset_grid_cursor(self) -> None:
        """Ставит курсор навигации на первый элемент текущей папки."""
        if self.grid.count() == 0:
            return
        self.grid.setCurrentRow(0)
        self.grid.scrollToTop()

    def _restore_series_mode(self, directory: Path) -> None:
        setting_key = "view/series_enabled"
        if self.settings.contains(setting_key):
            enabled = self.settings.value(setting_key, True, bool)
        else:
            legacy_key = f"{self._folder_settings_prefix(directory)}/series_enabled"
            enabled = self.settings.value(legacy_key, True, bool)
            self.settings.setValue(setting_key, enabled)
        self.set_series_mode(enabled, apply_view=False)

    def set_series_mode(self, enabled: bool, *, apply_view: bool = True) -> None:
        self.series_toggle.blockSignals(True)
        self.series_toggle.setChecked(enabled)
        self.series_toggle.blockSignals(False)
        if apply_view:
            self._apply_view()

    def _series_toggle_changed(self, enabled: bool) -> None:
        self.settings.setValue("view/series_enabled", enabled)
        self._apply_view()
        self.seriesModeChanged.emit(enabled)
        self._show_viewer_toast(
            "Группировка по сериям включена" if enabled else "Группировка по сериям выключена"
        )

    def _show_viewer_toast(self, message: str) -> None:
        """Ненадолго показывает поверх просмотрщика подтверждение действия."""
        parent = self.centralWidget() or self
        previous = getattr(self, "_viewer_toast", None)
        if previous is not None:
            self._viewer_toast = None
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
        toast.destroyed.connect(
            lambda _object=None, current=toast: self._clear_viewer_toast(current)
        )

        timer = QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(toast.deleteLater)
        timer.start(1_800)

    def _clear_viewer_toast(self, toast: QLabel) -> None:
        if getattr(self, "_viewer_toast", None) is toast:
            self._viewer_toast = None

    def _directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        if self.closing:
            return
        self.bridge.directoryScanned.emit(request, directory, future)

    def _on_directory_scanned(self, request: WorkspaceRequest, directory: Path, future: Future) -> None:
        """Принимает результат сканирования, если запрос всё ещё относится к этой папке."""
        if self.closing or not self.workspace_state.accepts(request):
            return
        self._directory_scan_pending = False
        self._folder_context_active = True
        try:
            self.all_paths = future.result()
            subfolders = [p for p in self.all_paths if p.is_dir()]
            images = [p for p in self.all_paths if p.is_file()]
            sorted_subfolders = sorted(subfolders, key=lambda p: p.name.lower())
            sorted_images = sorted(images, key=lambda p: p.name.lower())
            self.paths = sorted_subfolders + sorted_images
            self.view_paths = list(self.paths)
            self.preview_progress_total = len(images)
            self.preview_paths = set(images)
            self.preview_finished_paths.clear()
            self.view_generation += 1
            self._rebuild_status_index()
        except Exception as exc:
            self.bridge.failed.emit(str(directory), str(exc))
            self.all_paths = []
            self.view_paths = []
            self.paths = []
            self.preview_progress_total = 0
            self.preview_paths.clear()
            self.preview_finished_paths.clear()
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
            analysis_paths = [path for path in scannable_paths if is_supported_image(path)]
            future = self.cache_load_executor.submit(
                _load_cache, cache,
            )
            future.add_done_callback(
                lambda done, g=generation, target=cache: self._cache_loaded(g, target, done)
            )
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
            if (
                self._pending_view_cursor_path is not None
                or self._pending_view_selection
                or self._pending_view_scroll is not None
            ):
                QTimer.singleShot(0, self._restore_pending_view_cursor)
            if self.cache_ready and self._pending_folder_grid_restore:
                QTimer.singleShot(0, self._restore_folder_grid_context)
            self._refresh_status_panel()

    def _grid_item_for_path(self, path: Path) -> QListWidgetItem:
        """Создаёт карточку пути и повторно использует уже загруженную миниатюру."""
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
        """Дозированно добавляет следующую порцию миниатюр в фоновую очередь."""
        if not self.workspace_active:
            self.thumb_timer.stop()
            return
        if self.folder_cache is None or not self.cache_ready:
            return
        if self.pending_full_request is not None or self.foreground_full_futures:
            return
        pending_thumbs = sum(1 for _, size in self.pending if size == THUMB_SIZE)
        if self.thumb_priority:
            if len(self.visible_thumb_pending) >= MAX_VISIBLE_THUMB_PENDING:
                return
        elif self.visible_thumb_pending or pending_thumbs >= MAX_PENDING_THUMBS:
            return
        image_batch: list[Path] = []
        submitted = 0
        submit_limit = 1 if self.thumb_priority else THUMB_SUBMIT_BATCH
        while submitted < submit_limit:
            next_path = self._next_thumb_path()
            if next_path is None:
                break
            path, visible_priority = next_path
            if is_supported_video(path):
                self._submit_video_thumbnail(path, visible_priority=visible_priority)
            else:
                if visible_priority:
                    self._submit_decode(path, THUMB_SIZE, full_priority=False, visible_priority=True)
                else:
                    image_batch.append(path)
            submitted += 1
        if image_batch:
            self.scheduler.submit_thumbnail_batch(image_batch)
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
        if not self._previews_ready_for_manual_ai():
            self._ai_requested_generation = self.view_generation
            self.ai_analysis_available = False
            self._show_viewer_toast("AI-анализ поставлен в очередь")
            self._refresh_status_panel()
            return
        self._launch_ai_analysis()

    def _launch_ai_analysis(self) -> bool:
        analysis_paths = [path for path in self.view_paths if is_supported_image(path)]
        self._ai_requested_generation = -1
        if not analysis_paths or not self.ai_pipeline.scan(analysis_paths):
            return False
        self.ai_progress_total = 0
        self.ai_analysis_available = False
        self._ai_progress_started_at = monotonic()
        self.ai_progress_timer.start()
        self._refresh_status_panel()
        return True

    def _cancel_ai_analysis(self) -> None:
        """Отменяет текущий AI-конвейер и не даёт авторежиму запустить его повторно."""
        pipeline = self._ai_pipeline
        if pipeline is None or pipeline.pending_count(self.current_dir) == 0:
            return
        self.ai_progress_timer.stop()
        pipeline.shutdown()
        self._ai_pipeline = None
        self._ai_progress_started_at = None
        self._ai_requested_generation = -1
        self._cache_ai_waiting = False
        self._cache_ai_paths.clear()
        self._auto_ai_generation = self.view_generation
        self.ai_analysis_available = False
        self._refresh_status_panel()

    def _previews_ready_for_ai(self) -> bool:
        analysis_paths = self._cache_ai_paths.intersection(self.view_paths)
        return bool(
            self.workspace_active
            and self.cache_ready
            and self.folder_cache is not None
            and analysis_paths
            and self.populate_index >= len(self.paths)
            and self.preview_paths
            and self.preview_paths.issubset(self.preview_finished_paths)
        )

    def _previews_ready_for_manual_ai(self) -> bool:
        """Разрешает ручной повтор AI после готовности превью, даже если кэш уже частично заполнен."""
        return bool(
            self.workspace_active
            and self.cache_ready
            and self.folder_cache is not None
            and any(is_supported_image(path) for path in self.view_paths)
            and self.populate_index >= len(self.paths)
            and self.preview_paths
            and self.preview_paths.issubset(self.preview_finished_paths)
        )

    def _maybe_auto_start_ai_after_previews(self) -> bool:
        """Запускает AI-анализ после готовности превью, если включён авторежим.

        Для одной версии содержимого папки запуск происходит не больше одного
        раза. После повторного сканирования с новыми файлами анализ можно начать
        снова, но уже выполняющееся задание не дублируется. Возвращает ``True``,
        если новый анализ действительно стартовал.
        """
        if self.closing or self._auto_ai_generation == self.view_generation:
            return False
        if not self.settings.value("ai/auto_after_previews", False, bool):
            return False
        if not self._previews_ready_for_ai():
            return False
        if self._ai_pipeline is not None and self._ai_pipeline.pending_count(self.current_dir) != 0:
            return False
        started = self._launch_ai_analysis()
        if started:
            self._auto_ai_generation = self.view_generation
        return started

    def _show_ai_menu(self) -> None:
        """Показывает меню запуска AI-анализа текущей папки."""
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
        ai_running = (
            self._ai_pipeline is not None
            and self._ai_pipeline.pending_count(self.current_dir) > 0
        )
        if self.cache_ready and self.folder_cache is not None and not ai_running:
            start = QPushButton("Обработать серии и лица")
            start.setObjectName("toolbarPopupPrimaryButton")
            start.setIcon(_fomantic_icon("magic", 16, "#ffffff"))
            start.setIconSize(QSize(16, 16))
            start.clicked.connect(lambda: (menu.close(), self._start_ai_analysis()))
            layout.addWidget(start)
        elif not ai_running:
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
        """Собирает активный словарь подстановок для экспорта комментария в XMP."""
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
        """Показывает управление двусторонней синхронизацией XMP."""
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
        auto = SettingsCheckBox("Автоматически синхронизировать XMP")
        auto.setChecked(self._xmp_auto_enabled())
        layout.addWidget(auto)
        write_xmp = QPushButton("Записать XMP")
        read_xmp = QPushButton("Прочитать XMP")
        write_xmp.setIcon(_fomantic_icon("upload", 15, "#d6d6d6"))
        read_xmp.setIcon(_fomantic_icon("download", 15, "#d6d6d6"))
        write_xmp.setIconSize(QSize(15, 15))
        read_xmp.setIconSize(QSize(15, 15))
        for button in (write_xmp, read_xmp):
            button.setObjectName("toolbarPopupUtilityButton")
            layout.addWidget(button)
        manual_enabled = not auto.isChecked() and self.cache_ready
        write_xmp.setEnabled(manual_enabled)
        read_xmp.setEnabled(manual_enabled)

        def set_auto(enabled: bool) -> None:
            self.settings.setValue("xmp/auto_export", enabled)
            write_xmp.setEnabled(not enabled and self.cache_ready)
            read_xmp.setEnabled(not enabled and self.cache_ready)
            if enabled:
                self._xmp_queue_all_after_scan = True
                self._scan_xmp_changes(force=True, priority="local")

        auto.toggled.connect(set_auto)
        read_xmp.clicked.connect(lambda: (
            menu.close(), self._read_xmp_manually()
        ))
        write_xmp.clicked.connect(lambda: (
            menu.close(), self._queue_all_xmp_after_refresh()
        ))
        action = QWidgetAction(menu)
        action.setDefaultWidget(content)
        menu.addAction(action)
        menu.exec(self.xmp_button.mapToGlobal(QPoint(0, self.xmp_button.height())))

    def _handle_xmp_button(self) -> None:
        """Открывает две явные ручные операции и настройку автоматики."""
        self._show_xmp_menu()

    def _read_xmp_manually(self) -> None:
        """Перечитывает sidecar: для управляемых полей приоритет имеет XMP."""
        self._scan_xmp_changes(force=True, full_hash=True, priority="external")

    def _queue_all_xmp_after_refresh(self) -> None:
        """Сначала фиксирует внешнюю версию, затем ставит всю папку на запись."""
        self._xmp_queue_all_after_scan = True
        self._scan_xmp_changes(force=True, priority="local")

    def _queue_xmp_paths(self, paths) -> None:
        self._rebuild_xmp_pairs()
        for path in paths:
            target = sidecar_path(path)
            if target in self._xmp_bulk_queued:
                continue
            members = self._xmp_pair_members.get(target, [path])
            self._xmp_bulk_queue.append(members[0])
            self._xmp_bulk_queued.add(target)
        if self._xmp_bulk_queue and not self.xmp_bulk_timer.isActive():
            self.xmp_bulk_timer.start(0)

    def _drain_xmp_bulk_queue(self) -> None:
        """Готовит большую XMP-очередь порциями, оставляя время просмотру и вводу."""
        for _index in range(min(8, len(self._xmp_bulk_queue))):
            path = self._xmp_bulk_queue.popleft()
            self._xmp_bulk_queued.discard(sidecar_path(path))
            self._queue_xmp(path)
        if self._xmp_bulk_queue and not self.closing:
            self.xmp_bulk_timer.start(0)

    def _queue_xmp(self, path: Path) -> None:
        if self.closing or not is_supported_image(path):
            return
        target = sidecar_path(path)
        members = self._xmp_pair_members.get(target, [path])
        fields = self._xmp_local_fields(target)
        regions = []
        for member in members:
            detail = self.photo_details.get(member.name, {})
            found = named_face_regions(detail, self.face_sets)
            regions.extend(found)
        state = self._xmp_states.get(target.name, {})
        self._xmp_pending[target] = (fields.to_dict(), regions, state.get("digest"))
        if target not in self._xmp_running:
            self._start_xmp_write(target)

    def _start_xmp_write(self, path: Path) -> None:
        payload = self._xmp_pending.pop(path, None)
        if payload is None or self.closing:
            return
        self._xmp_running.add(path)
        future = self.xmp_executor.submit(_write_xmp_task, path, *payload)
        self._xmp_futures[path] = future
        future.add_done_callback(lambda done, target=path: self.bridge.xmpWritten.emit((target, done)))

    def _on_xmp_written(self, result: object) -> None:
        path, future = result
        self._xmp_running.discard(path)
        if self._xmp_futures.get(path) is future:
            self._xmp_futures.pop(path, None)
        if self.closing:
            return
        if future.cancelled():
            return
        try:
            written = future.result()
        except XmpChangedError:
            # Для начатой записи приоритет остаётся локальным. Перечитываем
            # отпечаток и повторяем только этот sidecar, а не всю папку.
            self._xmp_retry_after_change.add(path)
            self._scan_xmp_changes(force=True, full_hash=True, priority="local")
        except Exception as exc:
            self.bridge.failed.emit(str(path), f"XMP: {exc}")
            if path.parent == self.current_dir:
                self._store_xmp_status(path, status="error", error=str(exc))
        else:
            if path.parent != self.current_dir or not self.cache_ready:
                if path in self._xmp_pending:
                    self._start_xmp_write(path)
                return
            state = self._xmp_states.get(path.name, {})
            fields = written.fields.to_dict()
            state.update(
                size=written.size, mtime_ns=written.mtime_ns, digest=written.digest,
                base_fields=fields, status="synchronized", conflicts=[], error="",
            )
            self._xmp_states[path.name] = state
            self._persist_xmp_state(path, state)
            self._update_xmp_button()
        if path in self._xmp_pending:
            self._start_xmp_write(path)

    def _rebuild_xmp_pairs(self) -> None:
        groups: dict[Path, list[Path]] = {}
        for path in self.all_paths:
            if is_supported_image(path):
                groups.setdefault(sidecar_path(path), []).append(path)
        self._xmp_pair_members = groups

    def _xmp_local_fields(self, target: Path) -> XmpFields:
        members = self._xmp_pair_members.get(target, [])
        explicit = [
            member for member in members
            if self.photo_details.get(member.name, {}).get("_selection_updated_ns")
        ]
        candidates = explicit or members
        if not candidates:
            return XmpFields()
        selected = max(
            candidates,
            key=lambda member: int(self.photo_details.get(member.name, {}).get("_selection_updated_ns") or 0),
        )
        if not explicit:
            return XmpFields()
        detail = self.photo_details.get(selected.name, {})
        return XmpFields.from_detail(detail, self._xmp_replacements(detail))

    def _apply_xmp_fields_to_pair(self, target: Path, fields: XmpFields) -> set[str]:
        """Применяет общие XMP-поля ко всем участникам RAW+JPEG-пары."""
        changed_fields: set[str] = set()
        for path in self._xmp_pair_members.get(target, []):
            detail = self.photo_details.setdefault(path.name, {})
            values = fields.to_dict()
            member_changed = {
                name for name, value in values.items() if detail.get(name) != value
            }
            changed_fields.update(member_changed)
            if not member_changed and detail.get("_selection_updated_ns"):
                continue
            detail.update(values)
            detail["_selection_updated_ns"] = max(
                int(detail.get("_selection_updated_ns") or 0), 1
            )
            item = self.items_by_path.get(path)
            if item is not None:
                item.setData(DETAIL_ROLE, dict(detail))
                rect = self.grid.visualItemRect(item)
                if rect.isValid():
                    self.grid.viewport().update(rect)
            if self.folder_cache is not None and self.cache_ready:
                self._queue_xmp_cache_selection(path.name, fields)
        if self.current_path is not None and sidecar_path(self.current_path) == target:
            self.full_view.set_metadata(self.photo_details.get(self.current_path.name, {}))
        selected = self._selected_paths()
        if len(selected) == 1 and sidecar_path(selected[0]) == target:
            self.meta_bar.set_metadata(self.photo_details.get(selected[0].name, {}))
        return changed_fields

    def _scan_xmp_changes(
        self, *, force: bool = False, full_hash: bool = False,
        priority: str | None = None,
    ) -> None:
        """Читает XMP в фоне; priority задаёт победителя явной ручной операции."""
        if self.closing or not self.cache_ready:
            return
        if self._xmp_scan_future is not None:
            self._xmp_rescan_requested = True
            self._xmp_full_hash_requested |= full_hash
            if priority is not None:
                self._xmp_rescan_priority = priority
            return
        self._rebuild_xmp_pairs()
        paths = list(self._xmp_pair_members)
        if not paths:
            return
        generation = self.cache_load_generation
        self._xmp_scan_generation = generation
        known = {
            name: (
                int(state.get("size") or 0), int(state.get("mtime_ns") or 0), state.get("digest")
            )
            for name, state in self._xmp_states.items()
        }
        needed_missing = {
            target.name
            for target, members in self._xmp_pair_members.items()
            if any(
                self.photo_details.get(member.name, {}).get("_selection_updated_ns")
                for member in members
            )
        }
        future = self.xmp_executor.submit(
            _scan_xmp_task, paths, known, needed_missing, full_hash
        )
        self._xmp_scan_future = future
        future.add_done_callback(
            lambda done, g=generation, requested=force, winner=priority: (
                self.bridge.xmpScanned.emit((g, requested, winner, done))
            )
        )

    def _on_xmp_scanned(self, payload: object) -> None:
        generation, force, priority, future = payload
        if self._xmp_scan_future is future:
            self._xmp_scan_future = None
        if self.closing or generation != self.cache_load_generation:
            return
        try:
            results = future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(self.current_dir), f"XMP: {exc}")
            results = []
        changed_fields: set[str] = set()
        # Если во время обычного обхода нажали «Записать», его локальный снимок
        # не должен быть предварительно заменён результатом уже летящего чтения.
        superseded = self._xmp_rescan_requested and self._xmp_rescan_priority == "local"
        if not superseded:
            for target, result in results:
                if isinstance(result, Exception):
                    self._store_xmp_status(target, status="error", error=str(result))
                    continue
                changed_fields.update(
                    self._merge_xmp_snapshot(target, result, priority=priority)
                )
        self._update_xmp_button()
        if self._xmp_change_requires_view_rebuild(changed_fields):
            self._apply_view()
        elif changed_fields and self.stack.currentWidget() is self.full_view and self.current_path is not None:
            self._refresh_full_view_navigation(self.current_path)
        if self._xmp_rescan_requested:
            full_hash = self._xmp_full_hash_requested
            priority = self._xmp_rescan_priority
            self._xmp_rescan_requested = False
            self._xmp_full_hash_requested = False
            self._xmp_rescan_priority = None
            self._scan_xmp_changes(force=True, full_hash=full_hash, priority=priority)
            return
        if self._xmp_queue_all_after_scan:
            self._xmp_queue_all_after_scan = False
            self._queue_xmp_paths(
                path for path in self.all_paths if is_supported_image(path)
            )
        if self._xmp_retry_after_change:
            retry_targets = set(self._xmp_retry_after_change)
            self._xmp_retry_after_change.clear()
            self._queue_xmp_paths(
                members[0]
                for target, members in self._xmp_pair_members.items()
                if target in retry_targets and members
            )

    def _merge_xmp_snapshot(
        self, target: Path, snapshot, *, priority: str | None,
    ) -> set[str]:
        """Применяет снимок с однозначным приоритетом файла либо программы."""
        changed_fields: set[str] = set()
        state = dict(self._xmp_states.get(target.name, {}))
        if state and state.get("digest") == snapshot.digest and priority is None:
            return changed_fields
        external = snapshot.fields
        explicit = any(
            self.photo_details.get(path.name, {}).get("_selection_updated_ns")
            for path in self._xmp_pair_members.get(target, [])
        )
        if priority == "local":
            # Sidecar уже прочитан для сохранения его неизвестных XML-полей, но
            # рейтинг, метки и текст берём из текущего состояния Контрольки.
            state.update(base_fields=external.to_dict(), status="local_changes")
        elif snapshot.exists:
            # Ручное чтение и внешнее файловое событие означают одно и то же:
            # управляемые поля sidecar становятся актуальной версией.
            changed_fields.update(self._apply_xmp_fields_to_pair(target, external))
            state.update(base_fields=external.to_dict(), status="synchronized")
        elif explicit:
            # Отсутствующий sidecar — не команда стереть локальный отбор.
            state.update(base_fields=XmpFields().to_dict(), status="local_changes")
        else:
            state.update(base_fields=XmpFields().to_dict(), status="synchronized")
        state.update(conflicts=[])
        state.update(
            size=snapshot.size, mtime_ns=snapshot.mtime_ns, digest=snapshot.digest,
            error="",
        )
        self._xmp_states[target.name] = state
        self._persist_xmp_state(target, state)
        if state.get("status") == "local_changes" and self._xmp_auto_enabled():
            member = next(iter(self._xmp_pair_members.get(target, [])), None)
            if member is not None:
                self._queue_xmp(member)
        return changed_fields

    def _xmp_change_requires_view_rebuild(self, changed_fields: set[str]) -> bool:
        """Перестраивает список только если XMP влияет на текущую выборку или порядок."""
        if not changed_fields:
            return False
        if "rating" in changed_fields and (
            self.rating_filter.currentData() is not None or self.sort_combo.currentData() == "rating"
        ):
            return True
        if "color_label" in changed_fields and self.color_filter.currentIndex() > 0:
            return True
        return "comment" in changed_fields and bool(self.search_edit.text().strip())

    def _persist_xmp_state(self, target: Path, state: dict) -> None:
        if self.folder_cache is None or not self.cache_ready:
            return
        cache = self.folder_cache
        payload = {
            "sidecar_name": target.name, "size": int(state.get("size") or 0),
            "mtime_ns": int(state.get("mtime_ns") or 0), "digest": state.get("digest"),
            "base_fields": dict(state.get("base_fields") or {}),
            "status": str(state.get("status") or "synchronized"),
            "conflicts": list(state.get("conflicts") or []), "error": str(state.get("error") or ""),
        }
        self._xmp_cache_state_buffer[(id(cache), target.name)] = (cache, payload)
        if not self.xmp_cache_write_timer.isActive():
            self.xmp_cache_write_timer.start(120)

    def _queue_xmp_cache_selection(self, name: str, fields: XmpFields) -> None:
        cache = self.folder_cache
        if cache is None:
            return
        payload = {"name": name, **fields.to_dict()}
        self._xmp_cache_selection_buffer[(id(cache), name)] = (cache, payload)
        if not self.xmp_cache_write_timer.isActive():
            self.xmp_cache_write_timer.start(120)

    def _store_xmp_status(self, target: Path, *, status: str, error: str = "") -> None:
        state = dict(self._xmp_states.get(target.name, {}))
        state.update(status=status, error=error)
        self._xmp_states[target.name] = state
        self._persist_xmp_state(target, state)
        self._update_xmp_button()

    def _update_xmp_button(self) -> None:
        if not hasattr(self, "xmp_button"):
            return
        errors = sum(state.get("status") == "error" for state in self._xmp_states.values())
        pending = sum(state.get("status") == "local_changes" for state in self._xmp_states.values())
        self.xmp_button.setText(f"XMP · {errors}" if errors else "XMP")
        if errors:
            self.xmp_button.setToolTip(f"XMP: ошибок {errors}")
        elif pending:
            self.xmp_button.setToolTip(f"XMP: локальных изменений {pending}")
        else:
            self.xmp_button.setToolTip("XMP синхронизирован")

    def _show_utilities_menu(self) -> None:
        """Показывает место будущих пакетных инструментов без неработающих команд."""
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
        dialog.set_renaming(changes)
        paths = [
            self.current_dir / old
            for old, new in names.items()
            if old != new
        ]
        self._run_after_file_consumers_release(
            paths,
            lambda view=dialog, plan=dict(names): self._submit_file_rename(view, plan),
            restart_consumers=False,
            loading_text="Выполняется переименование",
        )

    def _submit_file_rename(self, dialog: BatchRenameDialog, names: dict[str, str]) -> None:
        """Запускает переименование после освобождения исходников воркерами."""
        cache = self.folder_cache
        future = self.rename_executor.submit(self._rename_files_with_cache, names, cache, dialog)
        future.add_done_callback(
            lambda done, view=dialog, plan=dict(names): self.bridge.renameFinished.emit(view, plan, done)
        )

    def _rename_files_with_cache(
        self, names: dict[str, str], cache: FolderCache | None, dialog: BatchRenameDialog
    ) -> str | None:
        """Переименовывает файлы и кэш вне Qt-потока, сохраняя отзывчивость диалога."""
        changes = sum(old != new for old, new in names.items())
        xmp_plan = _plan_xmp_sidecar_relocation(self.current_dir, names)
        self._rename_files_safely(
            names,
            lambda completed, total: (
                self.bridge.renameProgress.emit(
                    dialog,
                    min(changes, completed * changes // total),
                    changes,
                )
                if completed % 16 == 0 or completed == total
                else None
            ),
        )
        _relocate_xmp_sidecars(xmp_plan)
        if cache is None:
            return None
        self.bridge.renameCacheUpdating.emit(dialog)
        try:
            cache.rename_photo_names(names)
            cache.relocate_xmp_states({
                source.name: tuple(target.name for target in targets)
                for source, targets in xmp_plan.items()
            })
        except Exception as exc:
            return str(exc)
        return None

    def _on_rename_progress(self, dialog: BatchRenameDialog, completed: int, total: int) -> None:
        """Обновляет Qt-виджет только в главном потоке по сигналу фоновой операции."""
        if not self.closing:
            dialog.update_rename_progress(completed, total)

    def _on_rename_cache_updating(self, dialog: BatchRenameDialog) -> None:
        """Показывает отдельную фазу после перемещения файлов, не маскируя её зависанием."""
        if not self.closing:
            dialog.set_cache_updating()

    def _on_rename_finished(
        self, dialog: BatchRenameDialog, _names: dict[str, str], future: Future
    ) -> None:
        """Завершает диалог после файловой операции и обновления кэша."""
        if self.closing:
            return
        try:
            cache_error = future.result()
        except OSError as exc:
            dialog.rename_failed(f"Не удалось переименовать файлы: {exc}")
            return
        except Exception as exc:
            dialog.rename_failed(f"Не удалось переименовать файлы: {exc}")
            return
        if cache_error:
            QMessageBox.warning(dialog, "Групповое переименование", f"Файлы переименованы, но кэш не обновлён:\n{cache_error}")
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
        """Возвращает JPEG из текущей папки, без подпапок и по порядку имён."""
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
        """Запускает пакетный экспорт после подтверждения параметров диалога."""
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
        """Подбирает безопасные выходные пути и один раз спрашивает о конфликтах."""
        targets: list[tuple[Path, Path]] = []
        planned: set[str] = set()
        overwrite_all = False
        for source in paths:
            target = output_dir / f"{source.stem}.jpg"
            if filesystem_name_key(target.name) in planned:
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
            planned.add(filesystem_name_key(target.name))
            targets.append((source, target))
        return targets

    @staticmethod
    def _next_resize_name(target: Path, planned: set[str]) -> Path:
        for index in range(2, 100_000):
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            if filesystem_name_key(candidate.name) not in planned and not candidate.exists():
                return candidate
        raise OSError("Не удалось подобрать свободное имя")

    def _rename_files_safely(
        self, names: dict[str, str], progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Переименовывает через временные соседние файлы без потерь при обмене имён."""
        changes = {old: new for old, new in names.items() if old != new}
        if not changes:
            return
        directory = self.current_dir
        if len({filesystem_name_key(name) for name in changes.values()}) != len(changes):
            raise OSError("Шаблон создаёт одинаковые имена")
        try:
            existing_keys = {filesystem_name_key(path.name) for path in directory.iterdir()}
        except OSError as exc:
            raise OSError(f"Не удалось прочитать папку: {exc}") from exc
        source_keys = {filesystem_name_key(old) for old in changes}
        for old, new in changes.items():
            source, target = directory / old, directory / new
            if not source.is_file():
                raise OSError(f"Файл «{old}» больше не существует")
            if filesystem_name_key(target.name) in existing_keys and filesystem_name_key(target.name) not in source_keys:
                raise OSError(f"Файл «{new}» уже существует")

        total_steps = self._rename_step_count(changes)
        target_keys = {filesystem_name_key(new) for new in changes.values()}
        if source_keys.isdisjoint(target_keys):
            completed: list[str] = []
            try:
                for step, (old, new) in enumerate(changes.items(), start=1):
                    (directory / old).rename(directory / new)
                    completed.append(old)
                    if progress is not None:
                        progress(step, total_steps)
            except OSError:
                for old in reversed(completed):
                    target, source = directory / changes[old], directory / old
                    if target.exists():
                        target.rename(source)
                raise
            return

        token = uuid4().hex
        temporary = {old: directory / f".__rawww_rename_{token}_{index}" for index, old in enumerate(changes)}
        moved: list[str] = []
        completed: list[str] = []
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

    @staticmethod
    def _rename_step_count(names: dict[str, str]) -> int:
        """Возвращает число перемещений с учётом необходимости временных имён."""
        changes = {old: new for old, new in names.items() if old != new}
        source_keys = {filesystem_name_key(old) for old in changes}
        target_keys = {filesystem_name_key(new) for new in changes.values()}
        return len(changes) * (2 if source_keys & target_keys else 1)

    def _update_ai_progress(self) -> None:
        if self._ai_pipeline is None:
            self.ai_progress_timer.stop()
            return
        completed_folders = self.ai_pipeline.take_completed_folders()
        if (
            self.current_dir in completed_folders
            and self.folder_cache is not None
            and self.cache_ready
        ):
            completed, total, _running = self.ai_pipeline.progress(self.current_dir)
            self._reload_photo_details()
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(
                    path for path in self.view_paths if is_supported_image(path)
                )
            self.ai_analysis_available = completed < total
            if completed >= total:
                self._cache_ai_paths.clear()
                self._cache_ai_waiting = False
        if self.ai_pipeline.pending_count() == 0:
            self.ai_progress_timer.stop()
            self.ai_pipeline.release_analysis_workers()
            self._ai_progress_started_at = None
        self._refresh_status_panel()

    def _folder_changed(self, path: str) -> None:
        if self._selection_progress is not None or self._upload_progress is not None:
            return
        if not self.closing and Path(path) == self.current_dir:
            self.folder_change_timer.start(FOLDER_CHANGE_DEBOUNCE_MS)

    def _reload_changed_folder(self) -> None:
        if self._selection_progress is not None or self._upload_progress is not None:
            return
        if not self.closing and self.current_dir.is_dir():
            self._scan_xmp_changes(force=True)
            generation = self.cache_load_generation
            directory = self.current_dir
            future = self.directory_scan_executor.submit(_scan_directory, directory)
            future.add_done_callback(
                lambda done, g=generation, d=directory: self.bridge.folderChecked.emit((g, d, done))
            )

    def _on_folder_checked(self, payload: object) -> None:
        """Перезагружает папку лишь при изменении фото, а не после записи XMP."""
        generation, directory, future = payload
        if self.closing or generation != self.cache_load_generation or directory != self.current_dir:
            return
        try:
            paths = future.result()
        except Exception:
            self.load_directory(self.current_dir)
            return
        if set(paths) != set(self.all_paths):
            self.load_directory(self.current_dir)

    def _refresh_ai_status(self) -> None:
        if self.folder_cache is None or not self.cache_ready:
            self._reset_ai_status()
            return
        running = (
            self._ai_pipeline is not None
            and self._ai_pipeline.pending_count(self.current_dir) > 0
        )
        self.ai_analysis_available = self._cache_ai_waiting and not running
        self._refresh_status_panel()

    def _reset_ai_status(self) -> None:
        if not hasattr(self, "ai_button"):
            return
        self.ai_analysis_available = False
        self._refresh_status_panel()

    def _rebuild_status_index(self) -> None:
        """Сохраняет счётчики списка, чтобы прогресс превью не обходил папку заново."""
        visible_files = [path for path in self.view_paths if is_supported_media(path)]
        self._status_visible_count = len(visible_files)
        self._status_total_count = sum(is_supported_media(path) for path in self.all_paths)
        self._status_positions = {
            path: index for index, path in enumerate(visible_files, start=1)
        }

    def _schedule_status_refresh(self) -> None:
        """Склеивает частые завершения миниатюр в одно обновление строки состояния."""
        if not self.closing and not self.status_refresh_timer.isActive():
            self.status_refresh_timer.start()

    def _refresh_status_panel(self) -> None:
        """Показывает активную операцию и счётчики папки или выделения."""
        if not hasattr(self, "status_label"):
            return
        self.status_progress.set_cancel_visible(False)
        filtered = self._status_visible_count
        total_files = self._status_total_count
        position = self._status_positions.get(self.current_path, "-")
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

        if getattr(self, "_directory_scan_pending", False):
            self.status_progress.setRange(0, 0)
            self.status_progress.setFormat("Сканирую папку…")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(0, 0)
            return

        ai_completed, ai_total, ai_running = (
            self._ai_pipeline.progress(self.current_dir)
            if self._ai_pipeline is not None
            else (0, 0, False)
        )
        if ai_running:
            self.status_progress.set_cancel_visible(True)
            if ai_total <= 0:
                self.status_progress.setRange(0, 0)
                self.status_progress.setFormat("Подготовка AI…")
                self.status_progress.setToolTip(self.status_progress.format())
                self.status_progress.show()
                self._set_taskbar_progress(0, 0)
                return
            self.status_progress.setRange(0, ai_total)
            self.status_progress.setValue(ai_completed)
            eta = ""
            if ai_completed > 0 and self._ai_progress_started_at is not None:
                elapsed = monotonic() - self._ai_progress_started_at
                remaining = (ai_total - ai_completed) * elapsed / ai_completed
                eta = f" ({_format_remaining_time(remaining)})"
            self.status_progress.setFormat(f"Анализ: {ai_completed}/{ai_total}{eta}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(ai_completed, ai_total)
            return

        loaded = len(self.preview_finished_paths & self.preview_paths)
        if self.preview_progress_total and loaded < self.preview_progress_total:
            self.status_progress.setRange(0, self.preview_progress_total)
            self.status_progress.setValue(loaded)
            self.status_progress.setFormat(f"Превью: {loaded}/{self.preview_progress_total}")
            self.status_progress.setToolTip(self.status_progress.format())
            self.status_progress.show()
            self._set_taskbar_progress(loaded, self.preview_progress_total)
            return

        if (
            self._ai_requested_generation == self.view_generation
            and self._previews_ready_for_ai()
            and self._launch_ai_analysis()
        ):
            return
        if self._maybe_auto_start_ai_after_previews():
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
        self._face_search_index = None
        self._refresh_camera_filter()
        for path, item in self.items_by_path.items():
            item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        self.grid.viewport().update()
        self._update_analysis_controls()
        if self.face_reference is not None:
            self._cancel_face_search()
            self._set_face_search_loading(True)
            self._apply_face_search_view()
        else:
            self._apply_view()

    def _on_metadata_updated(self, results: object) -> None:
        """Добавляет полученный в фоне EXIF, не перечитывая весь кэш папки."""
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
            selection = {
                key: detail.get(key)
                for key in ("rating", "color_label", "comment", "keywords")
                if detail.get("_selection_updated_ns")
            }
            detail.update(metadata)
            detail.update(selection)
            camera_key = self._camera_filter_key(detail)
            if camera_key is not None:
                changed_camera_keys.add(camera_key)
            item = self.items_by_path.get(path)
            if item is not None:
                item.setData(DETAIL_ROLE, detail)
            changed_current |= path == self.current_path
        if self.camera_filter.currentData() in changed_camera_keys:
            self._metadata_view_refresh_needed = True
        if changed_camera_keys and not self.metadata_ui_timer.isActive():
            self.metadata_ui_timer.start()
        if changed_current and self.current_path is not None:
            detail = self.photo_details.get(self.current_path.name, {})
            self.full_view.set_metadata(detail)
            if self.stack.currentWidget() is self.grid_page and hasattr(self, "meta_bar"):
                self.meta_bar.set_metadata(detail)

    def _flush_metadata_ui_updates(self) -> None:
        """Обновляет фильтры один раз за несколько пакетов EXIF, а не на каждые 32 файла."""
        if self.closing:
            return
        self._refresh_camera_filter()
        if self._metadata_view_refresh_needed:
            self._metadata_view_refresh_needed = False
            self._apply_view()

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
            cameras.setdefault(key, {"model": str(camera.get("model") or "")})
        self.camera_filter.blockSignals(True)
        self.camera_filter.clear()
        self.camera_filter.addItem("Все камеры", None)
        self.camera_filter.setItemIcon(0, _fomantic_icon("images", 12, "#a8b0bd"))
        for key, entry in sorted(cameras.items(), key=lambda item: (str(item[1]["model"]).casefold(), item[0])):
            self.camera_filter.addItem(str(entry["model"]), key)
        index = self.camera_filter.findData(selected)
        self.camera_filter.setCurrentIndex(index if index >= 0 else 0)
        self.camera_filter.blockSignals(False)
        self.camera_filter.setVisible(len(cameras) > 1)

    def _update_analysis_controls(self) -> None:
        """Синхронизирует кнопки AI с готовностью моделей, кэша и текущего задания."""
        if not hasattr(self, "faces_panel_button"):
            return
        has_faces = any(detail.get("faces") for detail in self.photo_details.values())
        has_focus = any(detail.get("focus") for detail in self.photo_details.values())
        has_series = self._has_available_series(self.view_paths or self.all_paths)
        if hasattr(self, "ai_panel"):
            self.ai_panel.setVisible(has_faces or has_focus or has_series)
            self.series_faces_group.setVisible(has_faces or has_series)
            self.series_toggle.setVisible(True)
            self.faces_panel_button.setVisible(has_faces)
            self.shot_group.setVisible(has_faces)
            self.eyes_group.setVisible(has_faces)
            self.focus_group.setVisible(has_focus)
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
                if (
                    self.face_reference is not None
                    and self._face_match_names is not None
                    and path.name not in self._face_match_names
                ):
                    continue
                if needle and needle not in path.name.casefold() and needle not in str(detail.get("comment", "")).casefold():
                    continue
                matching_paths.append(path)
            closed_eyes_count = 0
            focus_defect_count = 0
            for path in matching_paths:
                detail = self.photo_details.get(path.name, {})
                counts[self._shot_size(detail)] = counts.get(self._shot_size(detail), 0) + 1
                if self._eyes_closed(detail):
                    closed_eyes_count += 1
                if focus_is_defect(detail):
                    focus_defect_count += 1
            for value, button in self.shot_buttons.items():
                button.setChecked(self.shot_filter.currentData() == value)
                count = len(matching_paths) if value is None else counts.get(value, 0)
                label = button.property("shotLabel") or button.text().split("  ")[0]
                button.setProperty("shotLabel", label)
                button.setText(f"{label}  {count}")
            self.eyes_toggle.setChecked(self.eyes_filter.currentData() == "closed")
            self.eyes_toggle.setText(f"Закрытые глаза  {closed_eyes_count}")
            self.focus_toggle.setChecked(self.focus_filter.currentData() == "defect")
            self.focus_toggle.setText(f"Не в фокусе / смаз  {focus_defect_count}")

    def _set_shot_filter(self, value: str | None) -> None:
        index = self.shot_filter.findData(value)
        if index >= 0:
            self.shot_filter.setCurrentIndex(index)

    def _toggle_eyes_filter(self) -> None:
        # Кнопка-переключатель: включена → показываем брак по глазам, иначе все.
        value = "closed" if self.eyes_toggle.isChecked() else None
        index = self.eyes_filter.findData(value)
        if index >= 0:
            self.eyes_filter.setCurrentIndex(index)

    def _toggle_focus_filter(self) -> None:
        # Кнопка-переключатель: включена → показываем брак по фокусу/смазу.
        value = "defect" if self.focus_toggle.isChecked() else None
        index = self.focus_filter.findData(value)
        if index >= 0:
            self.focus_filter.setCurrentIndex(index)

    @staticmethod
    def _eyes_closed(detail: dict) -> bool:
        """Признак брака по глазам: закрыты глаза у ключевого лица кадра.

        Брак, если закрыто крупнейшее лицо; либо когда лиц с известным
        состоянием глаз не больше трёх и закрыто хотя бы одно из них. В больших
        группах учитываем только крупнейшее лицо, чтобы моргнувший на фоне не
        браковал кадр. Лица без состояния глаз игнорируются.
        """
        faces = [face for face in (detail.get("faces") or []) if isinstance(face, dict)]
        known = 0
        any_closed = False
        largest_closed = False
        largest_size = -1.0
        for face in faces:
            state = face.get("eyes_open")
            if state is None:
                continue
            try:
                open_score = float(state)
            except (TypeError, ValueError):
                continue
            known += 1
            closed = open_score < EYES_OPEN_THRESHOLD
            any_closed = any_closed or closed
            bbox = face.get("bbox") or {}
            try:
                size = max(float(bbox.get("width", 0.0)), float(bbox.get("height", 0.0)))
            except (TypeError, ValueError):
                size = 0.0
            if size > largest_size:
                largest_size = size
                largest_closed = closed
        if not known:
            return False
        if largest_closed:
            return True
        return known <= 3 and any_closed

    def _has_available_series(self, paths: list[Path]) -> bool:
        photos = [path for path in paths if path.is_file()]
        return any(
            self._embedding_similarity(left, right) >= 0.92
            for left, right in zip(photos, photos[1:])
        )

    def _prioritize_visible_thumbs(self) -> None:
        """Продвигает видимые карточки и ближайший экран вперёд фоновой очереди."""
        if not self.workspace_active:
            return
        if self.folder_cache is None or not self.cache_ready or self.grid.count() == 0:
            return
        cell = self.grid.card_size_hint(0)
        if cell.width() <= 0 or cell.height() <= 0:
            return

        viewport = self.grid.viewport()
        visible_rows: set[int] = set()
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
            if not path.is_file() or not is_supported_media(path):
                continue
            if (path, THUMB_SIZE) not in self.pending:
                return path, True
        while self.thumb_index < len(self.paths):
            path = self.paths[self.thumb_index]
            self.thumb_index += 1
            if not path.is_file() or not is_supported_media(path):
                continue
            item = self.items_by_path.get(path)
            if item is not None and item.data(PREVIEW_ROLE) is not None:
                continue
            if (path, THUMB_SIZE) in self.pending:
                continue
            return path, False
        return None

    def _cache_loaded(self, generation: int, cache: FolderCache, future: Future) -> None:
        if self.closing or generation != self.cache_load_generation:
            cache.close(flush=False)
            return
        self.bridge.cacheLoaded.emit(generation, future)

    def _ai_cache_checked(self, generation: int, future: Future) -> None:
        """Передаёт результат поздней проверки AI-кэша в поток интерфейса."""
        if self.closing or generation != self.cache_load_generation:
            return
        self.bridge.aiCacheChecked.emit(generation, future)

    def _on_cache_loaded(self, generation: int, future: Future) -> None:
        """Подключает загруженный кэш к текущему поколению содержимого папки."""
        if self.closing or generation != self.cache_load_generation:
            return
        try:
            future.result()
        except Exception as exc:
            self.bridge.failed.emit(str(self.current_dir), str(exc))
            return
        self._cache_ai_paths.clear()
        self._cache_ai_waiting = False
        self.cache_ready = True
        if self.folder_cache is not None:
            self.photo_details = self.folder_cache.load_photo_details(
                include_metadata=ENABLE_EXIF_METADATA
            )
            self._xmp_states = self.folder_cache.load_xmp_states()
            for state in self._xmp_states.values():
                if state.get("status") == "conflict":
                    # Старое состояние мигрирует в новую модель приоритетов при
                    # ближайшем чтении, без отдельного окна разрешения.
                    state.update(size=0, mtime_ns=0, status="synchronized", conflicts=[])
            self._rebuild_xmp_pairs()
            self.image_embeddings = self.folder_cache.load_image_embeddings()
            # Данные лиц заменены целиком, поэтому прежний RAM-индекс им больше
            # не соответствует (при старте он также мог быть ещё пустым).
            self._face_search_index = None
            for path, item in self.items_by_path.items():
                item.setData(DETAIL_ROLE, self.photo_details.get(path.name, {}))
        export_xmp_after_load = self._xmp_export_after_cache_load
        self._xmp_export_after_cache_load = False
        if export_xmp_after_load and self._xmp_auto_enabled():
            self._xmp_queue_all_after_scan = True
        self._scan_xmp_changes()
        self._update_xmp_button()
        self._refresh_camera_filter()
        self._apply_view()
        if self.face_reference is not None:
            self._cancel_face_search()
            self._set_face_search_loading(True)
            self._apply_face_search_view()
        self._refresh_ai_status()
        if (
            self.stack.currentWidget() is self.full_view
            and self.current_path is not None
            and self.current_path.parent == self.current_dir
            and not is_supported_video(self.current_path)
        ):
            full_size = self._full_preview_size()
            if self._cache_get((self.current_path, full_size)) is None:
                self._show_best_cached_full(self.current_path, full_size)
                self._promote_current_full_task(self.current_path, full_size)
                self._submit_decode(self.current_path, full_size, full_priority=True)
                self._preload_neighbors(self.current_path)
            self._refresh_full_view_navigation(self.current_path)
        if ENABLE_EXIF_METADATA and self.folder_cache is not None:
            self.metadata_pipeline.scan(
                [path for path in self.view_paths if is_supported_image(path)],
                self.folder_cache,
                self.bridge.metadataUpdated.emit,
            )
        self.thumb_index = 0
        self._schedule_visible_thumb_priority()

        if self.folder_cache is not None:
            analysis_paths = [
                path for path in self.all_paths
                if path.is_file() and is_supported_image(path)
            ]
            ai_future = self.cache_load_executor.submit(
                _check_cached_ai, self.folder_cache, analysis_paths
            )
            ai_future.add_done_callback(
                lambda done, g=generation: self._ai_cache_checked(g, done)
            )

    def _on_ai_cache_checked(self, generation: int, future: Future) -> None:
        """Включает автозапуск AI только после фоновой проверки полноты кэша."""
        if self.closing or generation != self.cache_load_generation:
            return
        try:
            self._cache_ai_paths = set(future.result())
        except Exception as exc:
            self.bridge.failed.emit(str(self.current_dir), str(exc))
            self._cache_ai_paths.clear()
        self._cache_ai_waiting = bool(self._cache_ai_paths)
        self._refresh_ai_status()
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

    def _reset_unavailable_ai_filters(self) -> None:
        """Сбрасывает AI-фильтры, для которых в текущей папке нет данных.

        Панель AI и её группы скрываются, когда в папке нет лиц или анализа
        фокуса. Скрытый активный фильтр (например, «Не в фокусе / смаз») иначе
        продолжал бы отбирать кадры и при переходе в такую папку оставил бы
        пустой список без видимого способа это исправить. Возвращаем такие
        фильтры в нейтральное состояние. Сигналы гасим: вызов идёт из
        перестройки вида, которая тут же применит уже актуальные значения.
        """
        if not hasattr(self, "focus_filter"):
            return
        has_faces = any(detail.get("faces") for detail in self.photo_details.values())
        has_focus = any(detail.get("focus") for detail in self.photo_details.values())
        stale = []
        if not has_focus and self.focus_filter.currentData() is not None:
            stale.append(self.focus_filter)
        if not has_faces and self.eyes_filter.currentData() is not None:
            stale.append(self.eyes_filter)
        if not has_faces and self.shot_filter.currentData() is not None:
            stale.append(self.shot_filter)
        for control in stale:
            control.blockSignals(True)
            control.setCurrentIndex(0)
            control.blockSignals(False)

    def _apply_view(self, *_args) -> None:
        """Перестраивает видимый список по фильтрам, поиску и режиму серий."""
        if not hasattr(self, "rating_filter"):
            return
        self._reset_unavailable_ai_filters()
        self._remember_view_context()
        self._begin_view_context_restore()
        rating = self.rating_filter.currentData()
        color = self.color_filter.currentData()
        media = self.media_filter.currentData()
        file_type = self.file_type_filter.currentData()
        camera_key = self.camera_filter.currentData()
        shot = self.shot_filter.currentData()
        eyes = self.eyes_filter.currentData()
        focus = self.focus_filter.currentData()
        needle = self.search_edit.text().strip().casefold()

        def visible(path: Path) -> bool:
            """Проверяет один путь по всем активным фильтрам сетки."""
            if path.is_dir():
                return True
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
            if eyes == "closed" and not self._eyes_closed(detail):
                return False
            if focus == "defect" and not focus_is_defect(detail):
                return False
            if (
                self.face_reference is not None
                and self._face_match_names is not None
                and path.name not in self._face_match_names
            ):
                return False
            return not needle or needle in path.name.casefold() or needle in str(detail.get("comment", "")).casefold()

        order = self.sort_combo.currentData()
        if order == "custom":
            positions = {name: index for index, name in enumerate(self._custom_order)}
            key = lambda path: (positions.get(path.name, len(positions)), path.name.casefold())
            reverse = False
        elif order == "rating":
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
                cached = self._file_time_cache.get(path)
                if cached is not None:
                    return cached
                try:
                    captured = path.stat().st_mtime_ns / 1_000_000_000
                except OSError:
                    captured = 0.0
                self._file_time_cache[path] = captured
                return captured

            key = capture_time
            reverse = order.endswith("desc")
        else:
            key = lambda path: path.name.casefold()
            reverse = order == "name_desc"
        self.view_paths = _build_photo_view(self.all_paths, predicate=visible, sort_key=key, reverse=reverse)
        self.paths = self._grid_paths_with_series(self.view_paths)
        self.preview_paths = {path for path in self.paths if path.is_file()}
        self.preview_progress_total = len(self.preview_paths)
        self.view_generation += 1
        self._rebuild_status_index()
        self.populate_timer.stop()
        self.grid.clear()
        self.items_by_path.clear()
        self.populate_index = 0
        self.thumb_index = 0
        self.thumb_priority.clear()
        self.thumb_priority_set.clear()
        # Первый пакет появляется в том же событии, что и готовый фильтр. Остальные
        # карточки добавляются таймером, но пользователь уже не видит пустой grid.
        if self.workspace_active:
            self._populate_next_items()
        self._update_analysis_controls()
        self._refresh_status_panel()
        if self.workspace_active and self.populate_index < len(self.paths):
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
        """Разворачивает выбранные серии, сохраняя экранный порядок сетки."""
        self.series_cards = {}
        if not self.series_toggle.isChecked():
            return list(paths)
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
        """Применяет метаданные к выделению, кэшу, XMP и открытому просмотрщику."""
        selected_paths = self._selected_paths()
        paths = list(selected_paths)
        if self.current_path is not None and self.stack.currentWidget() is self.full_view:
            paths = [self.current_path]
        elif {"rating", "color_label"}.intersection(changes):
            targets: list[Path] = []
            for path in paths:
                series = self.series_cards.get(path) or {}
                if int(series.get("count", 0) or 0) > 1 and not series.get("expanded"):
                    targets.extend(self._series_for_path(path))
                else:
                    targets.append(path)
            paths = list(dict.fromkeys(targets))
        if {"rating", "color_label", "comment", "keywords"}.intersection(changes):
            self._rebuild_xmp_pairs()
            paths = list(dict.fromkeys(
                member
                for path in paths
                for member in self._xmp_pair_members.get(sidecar_path(path), [path])
            ))
        auto_xmp_targets: dict[Path, Path] = {}
        for path in paths:
            detail = self.photo_details.setdefault(path.name, {})
            detail.update(changes)
            detail["_selection_updated_ns"] = time_ns()
            item = self.items_by_path.get(path)
            if item is not None:
                item.setData(DETAIL_ROLE, dict(detail))
            if self.folder_cache is not None and self.cache_ready:
                self.folder_cache.store_photo_selection(
                    path.name,
                    rating=detail.get("rating"),
                    color_label=detail.get("color_label", ""),
                    comment=detail.get("comment", ""),
                    keywords=detail.get("keywords") or [],
                )
            if self._shotsync_syncer is not None:
                self._shotsync_syncer.queue_mark(path.name, detail=dict(detail), changes=changes)
            if self._xmp_auto_enabled():
                auto_xmp_targets.setdefault(sidecar_path(path), path)
        for path in auto_xmp_targets.values():
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
        """Редактирует комментарий так, чтобы горячие клавиши не забрали ввод."""
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
        """Ставит быструю метку клавишей M, а повторным нажатием снимает её."""
        if self.current_path is None or self.stack.currentWidget() is not self.full_view:
            return
        detail = self.photo_details.get(self.current_path.name, {})
        if int(detail.get("rating") or 0) > 0 or detail.get("color_label"):
            self._update_selection(rating=None, color_label="")
            return
        self._apply_quick_mark()

    def _load_face_sets(self) -> list[dict]:
        """Загружает общую библиотеку лиц независимо от кэша открытой папки."""
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
        """Берёт аватар из самого крупного доступного декодированного кадра."""
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
        """Добавляет найденное лицо в новый или существующий набор человека."""
        if not isinstance(face, dict) or face.get("embedding") is None:
            return
        embedding = [float(value) for value in face["embedding"]]
        already_added = any(
            self._face_similarity(embedding, item.get("embedding", [])) >= 0.42
            for item in self.face_sets
        )
        toast_message = "Это лицо уже есть в наборе."
        if not already_added:
            avatar = self._current_face_avatar(face, 80)
            entry = {
                "id": sha1(json.dumps(embedding).encode()).hexdigest()[:12],
                "name": "Без имени",
                "embedding": embedding,
                "avatar": self._pixmap_to_base64(avatar),
            }
            bbox = face.get("bbox")
            if isinstance(bbox, dict) and bbox:
                entry["bbox"] = bbox
            self.face_sets.append(entry)
            self._save_face_sets()
            self._update_analysis_controls()
            if self._xmp_auto_enabled():
                self._queue_xmp_paths(path for path in self.all_paths if is_supported_image(path))
            self._push_face_set(entry)
            toast_message = "Лицо добавлено в набор."
        self._show_face_sets(toast_message)

    def _face_set_by_id(self, face_id: str) -> dict | None:
        return next((entry for entry in self.face_sets if entry.get("id") == face_id), None)

    def _show_face_sets(self, toast_message: str | None = None) -> None:
        """Открывает библиотеку людей и позволяет пересобрать или удалить наборы."""
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
            """Пересобирает строки людей после изменения библиотеки лиц."""
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
                self._queue_xmp_paths(path for path in self.all_paths if is_supported_image(path))
            server_id = entry.get("server_id")
            if server_id and self.shotsync_client.has_key():
                self.shotsync_client.request_json(
                    f"/api/users/faces/{int(server_id)}/",
                    lambda _ok, _data, _error: None,
                    method="POST",
                    payload={"name": entry["name"]},
                )

    def _delete_face_set(self, face_id: str, rebuild: Callable[[], None]) -> None:
        removed = self._face_set_by_id(face_id)
        self.face_sets = [entry for entry in self.face_sets if entry.get("id") != face_id]
        self._save_face_sets()
        if self._xmp_auto_enabled():
            self._queue_xmp_paths(path for path in self.all_paths if is_supported_image(path))
        server_id = (removed or {}).get("server_id")
        if server_id and self.shotsync_client.has_key():
            self.shotsync_client.request_json(
                f"/api/users/faces/{int(server_id)}/delete/",
                lambda _ok, _data, _error: None,
                method="POST",
            )
        rebuild()

    def _show_face_set(self, face_id: str, dialog: QDialog) -> None:
        entry = self._face_set_by_id(face_id)
        if entry is None:
            return
        self._set_face_reference(entry["embedding"], self._face_avatar_from_entry(entry), show_loading=True)
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
        remove_rating = menu.addAction("Убрать рейтинг")
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
    def _face_similarity(left: object, right: object) -> float:
        if left is None or right is None:
            return -1.0
        try:
            if len(left) == 0 or len(left) != len(right):
                return -1.0
        except TypeError:
            return -1.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        norm = math.sqrt(
            sum(float(a) * float(a) for a in left)
            * sum(float(b) * float(b) for b in right)
        )
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
        if embedding is not None and len(embedding):
            reference = [float(value) for value in embedding]
            canonical = max(
                self.face_sets,
                key=lambda entry: self._face_similarity(
                    reference, entry.get("embedding", [])
                ),
                default=None,
            )
            if (
                canonical is not None
                and self._face_similarity(reference, canonical.get("embedding", []))
                >= FACE_MATCH_THRESHOLD
            ):
                self._set_face_reference(
                    canonical["embedding"],
                    self._face_avatar_from_entry(canonical),
                    show_loading=True,
                )
                return
            self._set_face_reference(
                reference,
                self._current_face_avatar(face),
                show_loading=True,
            )

    def _set_face_reference(
        self,
        embedding: list[float],
        avatar: QPixmap | None = None,
        *,
        show_loading: bool = False,
    ) -> None:
        """Запоминает лицо и запускает устойчивое сопоставление вне UI-потока."""
        self._cancel_face_search()
        self.face_reference = embedding
        self._face_match_names = None
        self._face_search_generation += 1
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
        if show_loading:
            self._set_face_search_loading(True)
            QTimer.singleShot(0, self._apply_face_search_view)
            return
        self._apply_face_search_view()

    def _apply_face_search_view(self) -> None:
        """Считает совпадения в фоне, чтобы бесконечный индикатор продолжал двигаться."""
        if self.face_reference is None or self.closing:
            self._set_face_search_loading(False)
            return
        # При старте сохранённый фильтр восстанавливается раньше SQLite-кэша.
        # Пустой индекс из этого окна нельзя кэшировать: иначе он останется
        # пустым и после загрузки десятков тысяч лиц.
        if not self.cache_ready:
            self._set_face_search_loading(True)
            return
        generation = self._face_search_generation
        reference = list(self.face_reference)
        # photo_details заменяется целиком после обновления кэша, а старый запрос
        # тогда отменяется. Поэтому фон может читать текущий снимок напрямую, без
        # синхронной копии тысяч записей перед первым кадром лоадера.
        details = self.photo_details if self._face_search_index is None else None
        cancelled = Event()
        self._face_search_cancel = cancelled
        future = self.face_search_executor.submit(
            indexed_face_matches,
            details,
            reference,
            self._face_search_index,
            cancelled,
        )
        self._face_search_future = future
        future.add_done_callback(
            lambda done, token=generation: self.bridge.faceSearchFinished.emit((token, done))
        )

    def _cancel_face_search(self) -> None:
        """Останавливает текущий обход и удаляет ещё не начатый запрос из очереди."""
        if self._face_search_cancel is not None:
            self._face_search_cancel.set()
            self._face_search_cancel = None
        if self._face_search_future is not None:
            self._face_search_future.cancel()
            self._face_search_future = None

    def _on_face_search_finished(self, payload: object) -> None:
        """Принимает только последний запрос лица и перестраивает вид после расчёта."""
        generation, future = payload
        if (
            self.closing
            or generation != self._face_search_generation
            or future is not self._face_search_future
            or future.cancelled()
        ):
            return
        self._face_search_future = None
        self._face_search_cancel = None
        try:
            self._face_search_index, matches = future.result()
            self._face_match_names = set(matches)
        except Exception as exc:
            self.bridge.failed.emit(str(self.current_dir), str(exc))
            self._face_match_names = set()
        self._apply_view()
        if self.stack.currentWidget() is self.full_view and self.current_path is not None:
            self._refresh_full_view_navigation(self.current_path)
        self._set_face_search_loading(False)

    def _clear_face_search(self) -> None:
        self._cancel_face_search()
        self.face_reference = None
        self._face_match_names = None
        self._face_search_generation += 1
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
        if self.closing or not path.is_file() or not is_supported_image(path):
            return
        self.scheduler.submit_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def _submit_video_thumbnail(self, path: Path, *, visible_priority: bool) -> None:
        self.scheduler.submit_video_thumbnail(path, visible_priority=visible_priority)

    def _on_decoded(self, payload: object) -> None:
        """Маршрутизирует готовый кадр в карточку, просмотрщик или RAM-кэш."""
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
            self.preview_finished_paths.add(decoded.path)
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
        if max_size == THUMB_SIZE:
            self._schedule_status_refresh()

    def _on_video_preview(self, path: Path, preview: QImage) -> None:
        if self.closing or not self.workspace_active or preview.isNull() or path.parent != self.current_dir:
            return
        preview = preview.scaled(THUMB_SIZE, THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._thumbnail_cache_put(path, preview)
        item = self.items_by_path.get(path)
        if item is not None:
            item.setData(PREVIEW_ROLE, preview)
            self.grid.update(self.grid.visualItemRect(item))
        self.full_view.update_preview(path, preview)
        self.full_view.set_video_preview(path, preview)
        if self.folder_cache is not None and self.cache_ready:
            rgba = preview.convertToFormat(QImage.Format.Format_RGBA8888)
            self.queue_preview_cache_write(
                self.folder_cache,
                PixelImage(path=path, pixels=bytes(rgba.bits()), width=rgba.width(), height=rgba.height()),
                THUMB_SIZE,
            )
        self.preview_finished_paths.add(path)
        self._schedule_status_refresh()

    def _on_decode_failed(self, path: str, message: str) -> None:
        failed_path = Path(path)
        self.visible_thumb_pending.discard((failed_path, THUMB_SIZE))
        self.preview_finished_paths.add(failed_path)
        item = self.items_by_path.get(failed_path)
        if item is not None:
            item.setText(f"{failed_path.name}\n{message}")
        if failed_path == self.current_path:
            self.thumb_timer.start()
        self._schedule_status_refresh()

    def _on_scheduler_finished(self, payload: object) -> None:
        """Принимает bookkeeping декодера в главном потоке Qt."""
        self.scheduler.handle_completion(payload)

    def _open_selected(self) -> None:
        item = self.grid.currentItem()
        if item:
            self.open_full(Path(item.data(Qt.ItemDataRole.UserRole)))

    def _open_in_editor(self) -> None:
        """Открывает активное изображение в выбранном внешнем редакторе."""
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
            is_macos_bundle = sys.platform == "darwin" and editor.suffix.casefold() == ".app" and editor.is_dir()
            if not editor.is_file() and not is_macos_bundle:
                QMessageBox.warning(
                    self,
                    "Внешний редактор",
                    f"Не найдено приложение или исполняемый файл редактора:\n{editor}",
                )
                return
            command = ["open", "-a", str(editor), str(path)] if is_macos_bundle else [str(editor), str(path)]
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
            subprocess.Popen(command, **detached_process_kwargs())
        except OSError as error:
            QMessageBox.warning(self, "Не удалось открыть редактор", str(error))

    @staticmethod
    def _photoshop_command(path: Path) -> list[str] | None:
        """Ищет стандартную установку Photoshop и собирает команду запуска."""
        if sys.platform == "darwin":
            return ["open", "-a", "Adobe Photoshop", str(path)]
        if sys.platform != "win32":
            executable = shutil.which("photoshop")
            return [executable, str(path)] if executable else None

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
        """Открывает аудиозаметку из сетки и сразу начинает воспроизведение."""
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
            return
        path = Path(value)
        self.current_path = path
        self.workspace_state.current_photo = path
        self._refresh_status_panel()
        if hasattr(self, "meta_bar"):
            self.meta_bar.set_metadata(self.photo_details.get(path.name, {}))
        if not path.is_file() or not is_supported_image(path):
            self.pending_grid_full_request = None
            self.grid_full_request_timer.stop()
            return
        self.pending_grid_full_request = path
        self.grid_full_request_timer.start(70)

    def open_full(self, path: Path) -> None:
        """Переключает рабочую вкладку в полный просмотр выбранного файла."""
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
        self.fullViewRequested.emit(self)
        self.full_view.setFocus(Qt.FocusReason.OtherFocusReason)
        if is_supported_video(path):
            item = self.items_by_path.get(path)
            preview = item.data(PREVIEW_ROLE) if item is not None else self._thumbnail_cache_get(path)
            self.full_view.set_video(path, preview if isinstance(preview, QImage) else None)
            if not isinstance(preview, QImage) or preview.isNull():
                self.video_thumbnailer.request(path)
            QTimer.singleShot(0, lambda current=path: self._finish_open_full_video(current))
            return
        full_size = self._full_preview_size()
        self._show_best_cached_full(path, full_size)
        # Навигация и ленты большой папки не должны задерживать первый кадр.
        QTimer.singleShot(
            16,
            lambda current=path, rapid=rapid_navigation: self._finish_open_full(current, rapid),
        )

    def _finish_open_full_video(self, path: Path) -> None:
        """Достраивает ленты видео после первой отрисовки его превью."""
        if not self.closing and path == self.current_path and self.stack.currentWidget() is self.full_view:
            self._refresh_full_view_navigation(path)

    def _finish_open_full(self, path: Path, rapid_navigation: bool) -> None:
        """Достраивает ленты после того, как Qt уже показал выбранную фотографию."""
        if self.closing or path != self.current_path or self.stack.currentWidget() is not self.full_view:
            return
        self._suspend_thumbnail_work()
        self._cancel_outdated_full_tasks(path, self._full_preview_size())
        self._refresh_full_view_navigation(path)
        full_size = self._full_preview_size()
        in_series = len(self._series_for_path(path)) > 1
        if in_series:
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
        """Загружает оригинал только для масштаба 100 % и повторно использует RAM-кэш."""
        if self.current_path is None or is_supported_video(self.current_path):
            return
        self._suspend_thumbnail_work()
        original_key = (self.current_path, ORIGINAL_SIZE)
        for key, future in list(self.pending.items()):
            if key != original_key and key[1] != THUMB_SIZE:
                future.cancel()
        self._submit_decode(self.current_path, ORIGINAL_SIZE, full_priority=True)

    def show_grid(self) -> None:
        self.full_view.stop_video()
        self.full_view.stop_audio()
        if self.single_photo_mode:
            self.singlePhotoExitRequested.emit(self)
            return
        self.stack.setCurrentWidget(self.grid_page)
        self._restore_grid_context()
        self._refresh_status_panel()
        self.gridRequested.emit()

    def _remember_thumbnail_size(self, size: int) -> None:
        self.workspace_state.thumbnail_size = size
        self.settings.setValue("thumbnail_size", size)
        QTimer.singleShot(0, self._keep_current_grid_item_visible)

    def _keep_current_grid_item_visible(self) -> None:
        """Возвращает текущую карточку в центр после изменения геометрии сетки."""
        item = self.grid.currentItem()
        if item is None and self.current_path is not None:
            item = self.items_by_path.get(self.current_path)
        if item is None:
            return
        self.grid.doItemsLayout()
        self.grid.scrollToItem(item, QListWidget.ScrollHint.PositionAtCenter)

    def _restore_grid_context(self) -> None:
        """Возвращает сетку к карточке, на которой закончился полный просмотр."""
        path = self.workspace_state.current_photo or self.current_path
        if path is None:
            return
        item = self.items_by_path.get(path)
        if item is None:
            return
        # FullView представляет ровно один текущий файл. Старое выделение грида
        # не должно переживать навигацию в просмотрщике вторым активным кадром.
        self.grid.clearSelection()
        self.grid.setCurrentItem(
            item,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        item.setSelected(True)
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
        if not self.current_path:
            return
        targets, indices, _series, _cards, _changed = self._full_navigation_snapshot()
        current = self.current_path
        if current not in indices:
            current = self._full_series_for_path(current)[0]
        if current not in indices:
            return
        index = indices.get(current, 0) + direction
        if 0 <= index < len(targets):
            self.open_full(targets[index])

    def _refresh_full_view_navigation(self, current: Path) -> None:
        """Обновляет соседей, ленты и предзагрузку вокруг текущего кадра."""
        strip_paths, indices, _series, strip_cards, navigation_changed = self._full_navigation_snapshot()
        series = self._full_series_for_path(current)
        strip_current = current if current in indices else series[0]
        strip_index = indices.get(strip_current, 0)
        # Нижняя лента — визуальный навигатор, а не второе хранилище всех
        # файлов папки. Стрелки работают по полному снимку, но Qt создаёт лишь
        # одну стабильную страницу элементов и не платит за 4000 карточек при
        # первом входе в Full View.
        page_start = (strip_index // FULL_STRIP_PAGE_SIZE) * FULL_STRIP_PAGE_SIZE
        visible_strip_paths = strip_paths[page_start : page_start + FULL_STRIP_PAGE_SIZE]
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
            visible_strip_paths,
            strip_current,
            self.photo_details,
            previews,
            series,
            self.view_generation,
            series_current=current,
            strip_series_cards=(
                {path: strip_cards.get(path, {}) for path in visible_strip_paths}
                if (
                    navigation_changed
                    or self.full_view._photo_generation != self.view_generation
                    or visible_strip_paths != self.full_view.photo_strip._paths
                )
                else None
            ),
            show_series_strip=not (len(series) > 1 and series[0] in self.expanded_series),
        )
        self._prioritize_full_strip_thumbs(strip_current, strip_paths, series)

    def _prioritize_full_strip_thumbs(self, current: Path, strip_paths: list[Path], series: list[Path]) -> None:
        """Продвигает миниатюры актуальных лент в общей очереди сетки."""
        index = self._full_navigation_indices.get(current, 0)
        nearby = [*series, *strip_paths[max(0, index - 4) : index + 5]]
        self._prioritize_strip_thumbs(nearby)

    def _prioritize_visible_full_strip_thumbs(self) -> None:
        """Сразу ставит видимые элементы прокрученной ленты в очередь превью."""
        if self.stack.currentWidget() is not self.full_view:
            return
        self._prioritize_strip_thumbs(
            [*self.full_view.photo_strip.visible_paths(), *self.full_view.series_strip.visible_paths()]
        )

    def _prioritize_strip_thumbs(self, paths: list[Path]) -> None:
        """Ставит элементы ленты перед фоновым сканированием в экранном порядке."""
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
        """Даёт декодированию текущего полного кадра исключительный приоритет."""
        self.thumb_timer.stop()
        self.visible_thumb_timer.stop()
        for key, future in list(self.pending.items()):
            if key[1] == THUMB_SIZE:
                future.cancel()
                self.visible_thumb_pending.discard(key)

    def _photo_mode_paths(self) -> list[Path]:
        """Возвращает сохранённый порядок ленты без обхода всей папки."""
        return self._full_navigation_snapshot()[0]

    def _full_navigation_snapshot(
        self,
    ) -> tuple[list[Path], dict[Path, int], dict[Path, tuple[Path, ...]], dict[Path, dict], bool]:
        """Кэширует навигацию Full View до смены фильтра, сортировки или серий.

        ``view_generation`` меняется при перестройке списка. Между такими
        изменениями Right/Left не должны снова обходить тысячи карточек.
        """
        if self._full_navigation_generation == self.view_generation:
            return (
                self._full_navigation_paths,
                self._full_navigation_indices,
                self._full_navigation_series,
                self._full_navigation_cards,
                False,
            )

        image_only_paths = [path for path in self.view_paths if path.is_file()]
        result: list[Path] = []
        series_by_path: dict[Path, tuple[Path, ...]] = {}
        if not self.series_toggle.isChecked():
            result = image_only_paths
        else:
            group: list[Path] = []

            def flush() -> None:
                if not group:
                    return
                members = tuple(group)
                for member in members:
                    series_by_path[member] = members
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

        self._full_navigation_paths = result
        self._full_navigation_indices = {path: index for index, path in enumerate(result)}
        self._full_navigation_series = series_by_path
        self._full_navigation_cards = {path: self.series_cards.get(path, {}) for path in result}
        self._full_navigation_generation = self.view_generation
        return result, self._full_navigation_indices, series_by_path, self._full_navigation_cards, True

    def _full_series_for_path(self, path: Path) -> list[Path]:
        """Возвращает серию из навигационного снимка без линейного поиска пути."""
        _paths, _indices, series_by_path, _cards, _changed = self._full_navigation_snapshot()
        return list(series_by_path.get(path, (path,)))

    def _series_for_path(self, path: Path) -> list[Path]:
        return self._full_series_for_path(path)

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
        navigation_paths, index = self._full_navigation_context(path)
        if index < 0:
            return
        full_size = self._full_preview_size()
        before = list(reversed(navigation_paths[max(0, index - FULL_PRELOAD_RADIUS) : index]))
        after = navigation_paths[index + 1 : index + FULL_PRELOAD_RADIUS + 1]
        if self.last_move_direction >= 0:
            primary, secondary = after, before
        else:
            primary, secondary = before, after
        neighbors = [
            neighbor
            for distance in range(FULL_PRELOAD_RADIUS)
            for group in (primary, secondary)
            for neighbor in group[distance : distance + 1]
        ]
        for neighbor in neighbors:
            self._submit_decode(neighbor, full_size, full_priority=True)

    def _full_preload_paths(self, path: Path) -> list[Path]:
        """Выбирает соседние кадры для упреждающей загрузки внутри серии."""
        return self._full_navigation_context(path)[0]

    def _full_navigation_context(self, path: Path) -> tuple[list[Path], int]:
        """Возвращает список предзагрузки и позицию кадра без поиска по папке."""
        series = self._full_series_for_path(path)
        if len(series) > 1:
            return series, series.index(path)
        navigation_paths, indices, _series, _cards, _changed = self._full_navigation_snapshot()
        return navigation_paths, indices.get(path, -1)

    def _cancel_outdated_full_tasks(self, path: Path, full_size: int) -> None:
        keep = {path}
        navigation_paths, index = self._full_navigation_context(path)
        if index >= 0:
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
        """Повышает приоритет кадра, который из соседнего стал текущим."""
        key = (path, full_size)
        future = self.pending.get(key)
        if future is not None:
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
        item = self.items_by_path.get(path)
        preview = item.data(PREVIEW_ROLE) if item is not None else self._thumbnail_cache_get(path)
        if isinstance(preview, QImage) and not preview.isNull():
            self.full_view.set_image(
                DecodedImage(path=path, image=preview, width=preview.width(), height=preview.height()),
                fallback=True,
            )

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
        self._drain_preview_cache_writes(cache)
        self._drain_xmp_cache_writes(cache)
        future = self.cache_flush_executor.submit(_flush_and_close, cache, close)
        if wait:
            future.result()

    def queue_preview_cache_write(
        self, cache: FolderCache, pixel: PixelImage, max_size: int
    ) -> None:
        """Записывает превью после показа, сохраняя порядок с закрытием кэша."""
        if self.closing:
            return
        key = (id(cache), pixel.path, max_size)
        self._preview_cache_write_buffer[key] = (cache, pixel, max_size)
        if not self.preview_cache_write_timer.isActive():
            self.preview_cache_write_timer.start(120)

    def _drain_preview_cache_writes(self, only_cache: FolderCache | None = None) -> None:
        """Передаёт накопленные превью пакетами в последовательную очередь SQLite."""
        grouped: dict[FolderCache, list[tuple[PixelImage, int]]] = {}
        for key, (cache, pixel, max_size) in list(self._preview_cache_write_buffer.items()):
            if only_cache is not None and cache is not only_cache:
                continue
            self._preview_cache_write_buffer.pop(key, None)
            grouped.setdefault(cache, []).append((pixel, max_size))
        if not self._preview_cache_write_buffer:
            self.preview_cache_write_timer.stop()
        for cache, previews in grouped.items():
            try:
                self.cache_flush_executor.submit(_store_cache_pixels, cache, previews)
            except RuntimeError:
                if not self.closing:
                    raise

    def _drain_xmp_cache_writes(self, only_cache: FolderCache | None = None) -> None:
        """Объединяет импорт XMP в одну транзакцию на папку вне UI-потока."""
        grouped: dict[FolderCache, tuple[list[dict], list[dict]]] = {}
        for key, (cache, payload) in list(self._xmp_cache_selection_buffer.items()):
            if only_cache is not None and cache is not only_cache:
                continue
            self._xmp_cache_selection_buffer.pop(key, None)
            grouped.setdefault(cache, ([], []))[0].append(payload)
        for key, (cache, payload) in list(self._xmp_cache_state_buffer.items()):
            if only_cache is not None and cache is not only_cache:
                continue
            self._xmp_cache_state_buffer.pop(key, None)
            grouped.setdefault(cache, ([], []))[1].append(payload)
        if not self._xmp_cache_selection_buffer and not self._xmp_cache_state_buffer:
            self.xmp_cache_write_timer.stop()
        for cache, (selections, states) in grouped.items():
            try:
                self.cache_flush_executor.submit(
                    _store_xmp_cache_batch, cache, selections, states
                )
            except RuntimeError:
                if not self.closing:
                    raise

    def _initial_directory(self) -> Path:
        saved = self.settings.value("last_directory", "", str)
        if saved:
            path = Path(saved)
            if path.exists() and path.is_dir():
                return path
        return Path.home()


class MainWindow(QMainWindow):
    """Верхняя оболочка приложения, которая управляет рабочими вкладками.

    Окно восстанавливает сессию, принимает повторные запуски из Проводника,
    переключает полноэкранный режим и хранит общие действия. Содержимое папок
    ему не принадлежит — каждая вкладка держит собственный ``Workspace``.
    """

    """Оболочка приложения, которая владеет независимыми рабочими пространствами папок с отслеживанием состояния.
    """

    def __init__(self, open_target: Path | None = None) -> None:
        super().__init__()
        self.settings = _application_settings()
        self.transfer_manager = TransferManager(self.settings, self)
        self.transfer_manager.taskFinished.connect(self._transfer_task_finished)
        self._card_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="card-import")
        self._card_import_backups: dict[str, tuple[list[TransferEntry], Path, Path, bool]] = {}
        self._single_photo_workspace: Workspace | None = None
        self._single_photo_origin_index: int | None = None
        self._single_photo_was_minimized = False
        self._single_photo_was_active = True
        self._single_photo_closes_window = False
        self._closing = False
        self._background_shutdown_done = False
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
            self._open_launch_target(open_target)
        else:
            self._restore_workspaces()
        if self.settings.value("updates/auto_check", True, bool):
            QTimer.singleShot(10_000, lambda: self._check_for_updates(interactive=False))

    def _check_for_updates(self, *, interactive: bool) -> None:
        """Проверяет обновление в фоне и показывает результат по правилам режима."""
        if self._update_check_running:
            return
        self._update_check_running = True
        future = self._update_executor.submit(
            fetch_release_info, APP_VERSION
        )

        def finish() -> None:
            self._update_check_running = False
            if self._closing:
                return
            try:
                payload = future.result()
                release = payload["latest"]
                version = str(release.get("version", ""))
                if is_newer(version, APP_VERSION):
                    self._show_update_dialog(release, payload.get("releases", []))
                elif interactive:
                    QMessageBox.information(self, "Обновления", "У вас установлена актуальная версия Контрольки.")
            except Exception:
                if interactive:
                    QMessageBox.warning(
                        self,
                        "Обновления",
                        "Не удалось проверить обновления. Проверьте подключение к интернету и повторите попытку позже.",
                    )

        def wait_for_finish() -> None:
            if self._closing:
                return
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
        """Открывает папку во вкладке, а файл — сразу в полном просмотре."""
        if target.is_dir():
            self._open_folder_tab(target)
            return
        if not target.is_file():
            return
        self._present_single_photo(target, close_window_on_exit=True)

    def open_external_target(self, target: Path | None) -> None:
        """Обрабатывает путь, переданный повторным запуском из файлового менеджера."""
        if target is not None and target.exists() and target.is_dir():
            self._open_folder_tab(target)
        elif target is not None and target.exists() and target.is_file():
            self._present_single_photo(target, preserve_window_state_on_exit=False)
        self._restore_and_activate()

    def _restore_and_activate(self) -> None:
        """Восстанавливает и выводит на передний план окно по внешнему запросу.

        Вызывается после подготовки цели, чтобы пользователь сразу увидел
        нужную папку или кадр, а не предыдущее содержимое окна.
        """
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            if state & Qt.WindowState.WindowFullScreen:
                self.showFullScreen()
            elif state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        native_handle = getattr(self, "winId", None)
        if native_handle is not None:
            activate_foreground_window(int(native_handle()))

    def _present_single_photo(
        self,
        target: Path,
        *,
        close_window_on_exit: bool = False,
        preserve_window_state_on_exit: bool = True,
    ) -> None:
        """Открывает файл во временном просмотре, не меняя набор вкладок пользователя.

        Внешний запрос не восстанавливает прежнее свёрнутое состояние, потому
        что пользователь явно попросил показать этот файл через Проводник.
        """
        if self._single_photo_workspace is not None:
            self._single_photo_workspace.open_full(target)
            return

        self._single_photo_origin_index = self.workspace_stack.currentIndex()
        if preserve_window_state_on_exit:
            self._single_photo_was_minimized = self.isMinimized()
            self._single_photo_was_active = self.isActiveWindow()
        else:
            self._single_photo_was_minimized = False
            self._single_photo_was_active = True
        self._single_photo_closes_window = close_window_on_exit
        workspace = self._add_workspace(target.parent, defer_initial_scan=True, single_photo=True)
        workspace.open_full(target)
        self._single_photo_workspace = workspace

    def _discard_single_photo_workspace(self, workspace: Workspace) -> None:
        """Удаляет временное пространство и возвращает активную пользовательскую вкладку."""
        if workspace is not self._single_photo_workspace:
            return
        self._leave_full_view()
        self.workspace_stack.removeWidget(workspace)
        workspace.close()
        workspace.deleteLater()
        origin_index = self._single_photo_origin_index
        self._single_photo_workspace = None
        self._single_photo_origin_index = None
        if origin_index is not None and 0 <= origin_index < self.workspace_stack.count():
            self.tabs.setCurrentIndex(origin_index)
            self._select_workspace(origin_index)

    def _exit_single_photo(self, workspace: Workspace) -> None:
        """Завершает временный просмотр: новый запуск закрывает окно, старый — возвращает фон."""
        closes_window = self._single_photo_closes_window
        was_minimized = self._single_photo_was_minimized
        was_active = self._single_photo_was_active
        self._discard_single_photo_workspace(workspace)
        if closes_window:
            self.close()
        elif was_minimized:
            self.showMinimized()
        elif not was_active:
            self.lower()

    def _open_single_photo_folder(self, workspace: Workspace) -> None:
        """По G открывает папку временного файла в обычной рабочей вкладке."""
        if workspace is not self._single_photo_workspace:
            return
        folder = workspace.current_dir
        self._discard_single_photo_workspace(workspace)
        self._open_folder_tab(folder)

    def _add_workspace(
        self,
        directory: Path | None = None,
        *,
        defer_initial_scan: bool = False,
        single_photo: bool = False,
    ) -> Workspace:
        """Создаёт рабочую вкладку и подключает её к общим действиям окна."""
        workspace = Workspace(
            directory,
            defer_initial_scan=defer_initial_scan,
            transfer_manager=self.transfer_manager,
            parent=self.workspace_stack,
        )
        workspace.single_photo_mode = single_photo
        workspace.destination_paths_provider = self._open_workspace_paths
        index = self.workspace_stack.addWidget(workspace)
        if not single_photo:
            self.tabs.addTab(_workspace_title(workspace.current_dir))
        workspace.windowTitleChanged.connect(
            lambda title, view=workspace: self._update_workspace_title(view, title)
        )
        workspace.fullViewRequested.connect(self._show_full_view)
        workspace.fullscreenRequested.connect(self._toggle_workspace_fullscreen)
        workspace.gridRequested.connect(self._leave_full_view)
        workspace.singlePhotoExitRequested.connect(self._exit_single_photo)
        workspace.singlePhotoFolderRequested.connect(self._open_single_photo_folder)
        workspace.openFolderRequested.connect(self._open_folder_tab)
        workspace.cardImportRequested.connect(
            lambda sources, view=workspace: self._show_card_import(sources, view)
        )
        workspace.shotsyncFolderChanged.connect(
            lambda linked, view=workspace: self._set_workspace_shotsync_icon(view, linked)
        )
        workspace.seriesModeChanged.connect(self._set_global_series_mode)
        if single_photo:
            self.workspace_stack.setCurrentWidget(workspace)
            for workspace_index in range(self.workspace_stack.count()):
                candidate = self.workspace_stack.widget(workspace_index)
                if isinstance(candidate, Workspace):
                    candidate.set_workspace_active(candidate is workspace)
        else:
            self.tabs.setCurrentIndex(index)
            self._select_workspace(self.tabs.currentIndex())
            self._update_tab_geometry()
        return workspace

    def _set_global_series_mode(self, enabled: bool) -> None:
        self.settings.setValue("view/series_enabled", enabled)
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and workspace.series_toggle.isChecked() != enabled:
                workspace.set_series_mode(enabled)

    def _open_workspace_paths(self) -> list[Path]:
        return [
            workspace.current_dir
            for index in range(self.workspace_stack.count())
            if isinstance(workspace := self.workspace_stack.widget(index), Workspace)
            and not workspace.single_photo_mode
        ]

    def _open_folder_tab(self, folder: Path, *, defer_initial_scan: bool = False) -> None:
        """Активирует уже открытую вкладку папки или создаёт новую."""
        folder = Path(folder)
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if not isinstance(workspace, Workspace) or workspace.single_photo_mode:
                continue
            try:
                is_open_folder = workspace.current_dir.samefile(folder)
            except OSError:
                is_open_folder = workspace.current_dir == folder
            if is_open_folder:
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
        """Создаёт общие действия окна: вкладки, кэши, справку и обновления."""
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
            self.transfer_manager.set_serial(
                self.settings.value("transfers/use_queue", True, bool)
            )
            for workspace_index in range(self.workspace_stack.count()):
                candidate = self.workspace_stack.widget(workspace_index)
                if isinstance(candidate, Workspace):
                    candidate._reload_hotkeys()
                    candidate._refresh_status_panel()
                    candidate.full_view.refresh_mark_indicator()
        if workspace.shotsync_client.has_key():
            workspace._sync_code_replacements()

    def _transfer_task_finished(self, task: TransferTask) -> None:
        """Обновляет открытые папки и сообщает ошибки завершённой операции."""
        self._continue_card_import_backup(task)
        touched = {task.destination}
        if task.move:
            touched.update(entry.source.parent for entry in task.entries)
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace) and workspace.current_dir in touched:
                workspace.load_directory(workspace.current_dir)
        if task.errors and task.status != "cancelled":
            QMessageBox.warning(
                self,
                "Файловая операция",
                "Не удалось обработать некоторые объекты:\n" + "\n".join(task.errors),
            )

    def _show_card_import(self, sources: list[tuple[Path, str]], parent: QWidget) -> None:
        """Открывает единый диалог импорта для карт, выбранных во вкладке."""
        dialog = CardImportDialog(sources, self.settings, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.options is None:
            return
        self._prepare_card_import(dialog.options, parent)

    def _prepare_card_import(self, options: dict, parent: QWidget) -> None:
        """Собирает карту в фоне, чтобы большая вложенная структура не заморозила Qt."""
        sources = [Path(source) for source in options.get("sources", [])]
        if not sources or not all(source.is_dir() for source in sources):
            QMessageBox.warning(parent, "Импорт с карты памяти", "Одна из выбранных карт больше недоступна.")
            return
        reserved_targets = self.transfer_manager.reserved_targets()
        future = self._card_import_executor.submit(
            self._build_card_import_plan, sources, options, reserved_targets
        )
        progress = QProgressDialog("Подготавливаю список файлов с карты…", None, 0, 0, parent)
        progress.setWindowTitle("Импорт с карты памяти")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()

        def finish() -> None:
            if not future.done():
                QTimer.singleShot(80, finish)
                return
            progress.close()
            try:
                scan, entries, destination, backup_destination = future.result()
            except Exception as exc:  # noqa: BLE001 — сообщение пользователю важнее детали потока
                QMessageBox.warning(parent, "Импорт с карты памяти", f"Не удалось прочитать карту:\n{exc}")
                return
            self._enqueue_card_import(scan, entries, destination, backup_destination, options, parent)

        QTimer.singleShot(0, finish)

    @staticmethod
    def _build_card_import_plan(
        sources: list[Path], options: dict, reserved_targets: set[Path]
    ) -> tuple[CardImportScan, list[TransferEntry], Path, Path]:
        """Собирает и сравнивает конфликтующие файлы вне UI-потока."""
        scan = merge_scans([scan_card(source) for source in sources])
        folder_name = str(options["shoot_name"]).strip() if options["folder_mode"] == "name" else scan.capture_date.isoformat()
        destination = Path(options["destination"]) / folder_name
        backup_destination = (
            Path(options["backup_destination"]) / folder_name
            if options["backup_enabled"]
            else destination
        )
        source_roots = scan.source_roots or (scan.root,)
        if any(destination.resolve().is_relative_to(root.resolve()) for root in source_roots):
            raise ValueError("Основная папка не может находиться внутри импортируемой карты.")
        if options["backup_enabled"] and any(backup_destination.resolve().is_relative_to(root.resolve()) for root in source_roots):
            raise ValueError("Папка резервной копии не может находиться внутри импортируемой карты.")
        entries = build_import_entries(
            scan, destination, flatten=bool(options["flatten"]), reserved=reserved_targets.__contains__,
        )
        return scan, entries, destination, backup_destination

    def _enqueue_card_import(
        self,
        scan: CardImportScan,
        entries: list[TransferEntry],
        destination: Path,
        backup_destination: Path,
        options: dict,
        parent: QWidget,
    ) -> None:
        """Ставит каждую карту отдельной задачей, чтобы несколько носителей читались параллельно."""
        if not scan.files:
            QMessageBox.information(parent, "Импорт с карты памяти", "На выбранных картах нет файлов для импорта.")
            return
        if not entries:
            QMessageBox.information(parent, "Импорт с карты памяти", "Все файлы уже есть в папке назначения.")
            return
        remaining = list(entries)
        batches: list[list[TransferEntry]] = []
        for source_root in scan.source_roots or (scan.root,):
            card_entries: list[TransferEntry] = []
            for entry in remaining[:]:
                try:
                    entry.source.relative_to(source_root)
                except ValueError:
                    continue
                card_entries.append(entry)
                remaining.remove(entry)
            if card_entries:
                batches.append(card_entries)
        if remaining:
            batches.append(remaining)

        identifiers: list[tuple[str, list[TransferEntry]]] = []
        for card_entries in batches:
            identifier = self.transfer_manager.enqueue(
                card_entries,
                destination,
                move=bool(options["delete_sources"]),
                parallel=True,
            )
            if identifier is not None:
                identifiers.append((identifier, card_entries))
        if not identifiers:
            QMessageBox.warning(parent, "Импорт с карты памяти", "Не удалось поставить импорт в очередь.")
            return
        if options["backup_enabled"]:
            for identifier, card_entries in identifiers:
                self._card_import_backups[identifier] = (
                    card_entries,
                    destination,
                    backup_destination,
                    bool(options["flatten"]),
                )

    def _continue_card_import_backup(self, task: TransferTask) -> None:
        """Ставит резервную копию только после полностью успешного основного импорта."""
        pending = self._card_import_backups.pop(task.identifier, None)
        if pending is None:
            return
        entries, import_root, backup_root, flatten = pending
        if task.status != "finished":
            QMessageBox.warning(
                self,
                "Резервная копия",
                "Основной импорт завершился с ошибкой или был отменён; резервная копия не запускалась.",
            )
            return
        try:
            backup_entries = build_backup_entries(
                entries, import_root, backup_root, flatten=flatten, reserved=self.transfer_manager.target_reserved,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Резервная копия", f"Не удалось подготовить резервную копию:\n{exc}")
            return
        self.transfer_manager.enqueue(backup_entries, backup_root, move=False)

    def _clear_all_caches(self) -> None:
        """Закрывает активные базы кэша перед удалением их файлов."""
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
        """Показывает меню справки, обновлений и сведений о приложении."""
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
        if index >= 0 and not workspace.single_photo_mode:
            self.tabs.setTabText(index, title)

    def _set_workspace_shotsync_icon(self, workspace: Workspace, linked: bool) -> None:
        """Показывает облачный значок, не меняя название папки на вкладке."""
        index = self.workspace_stack.indexOf(workspace)
        if index < 0 or workspace.single_photo_mode:
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
        if self._closing:
            super().closeEvent(event)
            return
        if self.transfer_manager.active or self.transfer_manager.pending:
            answer = QMessageBox.question(
                self,
                "Файловые операции не завершены",
                "Копирование или перемещение ещё выполняется. Выйти и отменить все операции?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        directories = [
            str(workspace.current_dir)
            for index in range(self.workspace_stack.count())
            if (workspace := self.workspace_stack.widget(index)) is not None
            and not workspace.single_photo_mode
            and workspace.current_dir.is_dir()
        ]
        self.settings.setValue("open_workspaces", directories)
        shotsync_paths = [
            str(workspace.current_dir)
            for index in range(self.workspace_stack.count())
            if (workspace := self.workspace_stack.widget(index)) is not None
            and not workspace.single_photo_mode
            and workspace.shotsync_active
        ]
        self.settings.setValue("shotsync_workspaces", shotsync_paths)
        self.shutdown_background_work()
        super().closeEvent(event)

    def shutdown_background_work(self) -> None:
        """Финализирует фон как при закрытии окна, так и при ``QApplication.quit``."""
        if self._background_shutdown_done:
            return
        self._background_shutdown_done = True
        self._closing = True
        workspaces = []
        for index in range(self.workspace_stack.count()):
            workspace = self.workspace_stack.widget(index)
            if isinstance(workspace, Workspace):
                workspaces.append(workspace)
                workspace.begin_shutdown()
        self.transfer_manager.shutdown()
        # После общего запрета новой работы можно безопасно закрыть дочерние
        # окна и дождаться всех очередей, включая оставшиеся от прежних папок.
        for workspace in workspaces:
            workspace.close()
        if workspaces:
            workspaces[0].shotsync.shutdown()
        retire_executor(self._update_executor)
        retire_executor(self._card_import_executor)
        wait_for_retired_executors()

    def _toggle_maximized(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_tab_geometry()

    def _update_tab_geometry(self) -> None:
        if not hasattr(self, "tabs") or self.tabs.count() == 0:
            return
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
        if index >= 0 and not workspace.single_photo_mode:
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
            return
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
    """Даёт запуску из исходников тот же ID панели задач, что и сборке."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ru.shotsync.ctrlka")
    except (AttributeError, OSError):
        pass


def _mounted_volume_paths() -> list[Path]:
    """Возвращает доступные смонтированные корни файловой системы по данным Qt."""
    paths: dict[str, Path] = {}
    for volume in QStorageInfo.mountedVolumes():
        if not volume.isValid() or not volume.isReady():
            continue
        root = Path(volume.rootPath())
        if root.is_dir():
            paths[_drive_key(root)] = root
    return sorted(paths.values(), key=lambda path: _drive_key(path).lower())


def _volume_root_for_path(path: Path, volumes: list[Path]) -> Path | None:
    """Находит смонтированный том для ``path``, включая вложенные точки монтирования."""
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
    """Возвращает метку тома или, если её нет, понятное имя корня."""
    storage = QStorageInfo(str(path))
    name = storage.displayName().strip()
    return f"{name} ({path})" if name else str(path)


def _volume_button_text(path: Path) -> str:
    """Готовит короткую подпись диска для компактной кнопки."""
    drive = path.drive.rstrip("\\/")
    return drive or path.name or str(path)


def _removable_volume_icon() -> QIcon:
    """Рисует компактный значок SD-карты для кнопки съёмного носителя."""
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
    """Определяет съёмный носитель средствами текущей операционной системы."""
    if sys.platform == "win32":
        return ctypes.windll.kernel32.GetDriveTypeW(str(path)) == 2
    if sys.platform.startswith("linux"):
        return _linux_volume_is_removable(path)
    if sys.platform == "darwin":
        return _macos_volume_is_removable(path)
    return False


def _linux_volume_is_removable(path: Path) -> bool:
    """Находит для точки монтирования устройство sysfs и читает флаг ``removable``."""
    try:
        mount_path = str(path.resolve())
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            source = after.split()[1] if separator and len(after.split()) > 1 else ""
            if len(fields) < 5 or fields[4] != mount_path or not source.startswith("/dev/"):
                continue
            block = Path("/sys/class/block", Path(source).name)
            for device in (block, block.resolve().parent):
                removable = device / "removable"
                if removable.is_file():
                    return removable.read_text(encoding="utf-8").strip() == "1"
            return False
    except OSError:
        pass
    return False


def _macos_volume_is_removable(path: Path) -> bool:
    """Один раз запрашивает у ``diskutil``, является ли том съёмным."""
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
    """Возвращает доступные папки и поддерживаемые файлы.

    Права на отдельный объект могут измениться прямо во время обхода. Такой
    объект не должен отменять показ всей папки или доходить до Pillow. Файл
    открывается только для проверки прав: само чтение и декодирование остаются
    в соответствующих фоновых очередях.
    """
    try:
        entries = []
        for entry in directory.iterdir():
            try:
                if entry.is_dir():
                    entries.append(entry)
                elif entry.is_file() and is_supported_media(entry):
                    entries.append(entry)
            except OSError:
                continue
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
    """Строит отсортированный и отфильтрованный список для сетки и навигации.

    Исходная коллекция остаётся нетронутой: фильтры можно переключать без нового
    сканирования папки и без смешивания правил интерфейса с очередью миниатюр.
    """
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


def _store_cache_pixels(
    cache: FolderCache, previews: list[tuple[PixelImage, int]]
) -> None:
    """Кодирует и сохраняет превью; повреждение кэша не отменяет показ файла."""
    try:
        cache.store_pixels_batch(previews)
    except Exception:
        return


class _StartupWindowTrace(QObject):
    """Диагностирует лишние нативные окна в первые секунды запуска.

    В обычном режиме почти ничего не делает; при включённой трассировке помогает
    поймать короткую вспышку окна, которая обычно исчезает ровно перед тем, как
    разработчик успевает сказать «ну вот же она».
    """

    _EVENTS = {
        QEvent.Type.Show,
        QEvent.Type.Hide,
        QEvent.Type.PlatformSurface,
        QEvent.Type.WinIdChange,
        QEvent.Type.WindowStateChange,
        QEvent.Type.Expose,
    }

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self.started = monotonic()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() not in self._EVENTS:
            return False
        if isinstance(watched, QWindow):
            self._write(
                event.type().name,
                watched.metaObject().className(),
                watched.objectName(),
                watched.title(),
                watched.isVisible(),
                int(watched.flags()),
                watched.geometry().getRect(),
            )
        elif isinstance(watched, QWidget) and watched.isWindow():
            self._write(
                event.type().name,
                watched.metaObject().className(),
                watched.objectName(),
                watched.windowTitle(),
                watched.isVisible(),
                int(watched.windowFlags()),
                watched.geometry().getRect(),
            )
        return False

    def snapshot(self, label: str) -> None:
        windows = [
            (
                window.metaObject().className(),
                window.objectName(),
                window.title(),
                window.isVisible(),
                int(window.flags()),
                window.geometry().getRect(),
            )
            for window in QGuiApplication.allWindows()
        ]
        self._write(label, windows)

    def finish(self) -> None:
        self.snapshot("trace-finished")
        self.app.removeEventFilter(self)

    def _write(self, *parts: object) -> None:
        elapsed = monotonic() - self.started
        print(f"[startup-window {elapsed:0.3f}s]", *parts, file=sys.stderr, flush=True)


def _install_interrupt_shutdown(app: QApplication, window: MainWindow) -> None:
    """Проводит Ctrl+C через закрытие окна и штатную финализацию очередей."""
    if not hasattr(signal, "SIGINT"):
        return

    def request_shutdown(_signum, _frame) -> None:
        # Python-сигнал способен прервать произвольный Qt callback. Закрытие
        # откладывается до следующего оборота event loop, чтобы не войти в него
        # повторно посреди обновления грида или дерева дисков.
        QTimer.singleShot(0, window.close)

    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_shutdown)
    # Qt может надолго уйти в нативный event loop без исполнения Python-кода.
    # Короткий пустой timer даёт интерпретатору регулярно доставлять SIGINT.
    heartbeat = QTimer(app)
    heartbeat.setInterval(200)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()
    app._interrupt_heartbeat = heartbeat  # type: ignore[attr-defined]


def main() -> None:
    """Настраивает окружение Qt, единственный экземпляр и запускает приложение."""
    install_error_logging()
    import multiprocessing

    multiprocessing.freeze_support()
    install_process_tree_guard()
    _set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    single_instance = SingleInstance(app)
    target = target_from_argv()
    if single_instance.start(target):
        return
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(_application_icon())
    startup_trace = _StartupWindowTrace(app) if os.environ.get("RAWWW_TRACE_STARTUP") else None
    if startup_trace is not None:
        app.installEventFilter(startup_trace)
        startup_trace.snapshot("application-created")
    qt_ru = QTranslator(app)
    if qt_ru.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt_ru)
    apply_theme(app)
    window = MainWindow(target)
    single_instance.target_received.connect(window.open_external_target)
    telemetry = TelemetryClient(window.settings, app)
    telemetry.start()
    _install_interrupt_shutdown(app, window)
    app.aboutToQuit.connect(window.shutdown_background_work)
    app.aboutToQuit.connect(telemetry.stop)
    app.aboutToQuit.connect(wait_for_retired_executors)
    if startup_trace is not None:
        startup_trace.snapshot("main-window-constructed")
    screenshot_path = os.environ.get("RAWWW_CAPTURE_SCREENSHOT")
    if screenshot_path:
        # Offscreen-платформа Qt часто сообщает маленький виртуальный экран.
        # Для CI фиксируем кадр, чтобы артефакты разных runner были сопоставимы.
        window.setGeometry(0, 0, 1920, 1080)
        window.show()
    elif getattr(window, "_fast_full_view", False):
        window.showFullScreen()
        workspace = window.workspace_stack.currentWidget()
        if isinstance(workspace, Workspace):
            QTimer.singleShot(0, workspace.full_view.finish_fast_resize)
    else:
        screen = window.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            window.setGeometry(screen.availableGeometry())
        window.showMaximized()
    if screenshot_path:
        def capture_screenshot() -> None:
            """Сохраняет виджет после первой отрисовки для проверки собранного приложения."""
            target_path = Path(screenshot_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap = window.grab()
            if pixmap.isNull() or not pixmap.save(str(target_path), "PNG"):
                print(f"Could not save startup screenshot: {target_path}", file=sys.stderr)
                app.exit(1)
                return
            print(f"Startup screenshot saved: {target_path}")
            app.quit()

        QTimer.singleShot(1_500, capture_screenshot)
    if startup_trace is not None:
        startup_trace.snapshot("show-requested")
        QTimer.singleShot(3_000, startup_trace.finish)
    try:
        exit_code = app.exec()
    except KeyboardInterrupt:
        # Страховка для платформ, где SIGINT успел прийти до установки
        # обработчика: фон всё равно завершается до выхода интерпретатора.
        exit_code = 130
    finally:
        window.shutdown_background_work()
        telemetry.stop()
        wait_for_retired_executors()
    sys.exit(exit_code)
