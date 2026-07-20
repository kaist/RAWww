## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Глобальная очередь локального копирования и перемещения файлов."""

from __future__ import annotations

import errno
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

from PySide6.QtCore import QObject, QSettings, QSize, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .theme import _fomantic_icon
from .cache import relocate_folder_caches


# Большой блок снижает число переходов Python ↔ ядро на RAW и видео, но всё ещё
# даёт достаточно частые точки для прогресса, паузы и отмены.
COPY_CHUNK_SIZE = 16 * 1024 * 1024
PARALLEL_TRANSFER_LIMIT = 3


def format_transfer_size(value: float) -> str:
    """Форматирует объём или скорость без лишней ложной точности."""
    value = max(0.0, float(value))
    if value >= 1024**4:
        return f"{value / 1024**4:.1f} ТБ"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} ГБ"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} МБ"
    if value >= 1024:
        return f"{value / 1024:.1f} КБ"
    return f"{value:.0f} Б"


def format_transfer_eta(seconds: float | None) -> str:
    """Возвращает короткое ETA либо тире, пока оценка ненадёжна."""
    if seconds is None:
        return "—"
    if 0 < seconds < 1:
        return "< 1 с"
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {seconds:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


@dataclass(frozen=True)
class TransferEntry:
    """Описывает один корневой источник и уже согласованную цель."""

    source: Path
    target: Path
    replace: bool = False


@dataclass
class TransferTask:
    """Хранит состояние одной пользовательской операции в общей очереди."""

    entries: list[TransferEntry]
    destination: Path
    move: bool
    identifier: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    completed_files: int = 0
    total_files: int = 0
    transferred_bytes: int = 0
    total_bytes: int = 0
    started_at: float | None = None
    transfer_started_at: float | None = None
    last_report_at: float = 0.0
    speed_bytes_per_second: float = 0.0
    speed_sample_at: float | None = None
    speed_sample_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    entry_totals: dict[Path, tuple[int, int]] = field(default_factory=dict, repr=False)
    copy_buffer: bytearray | None = field(default=None, repr=False)
    run_event: Event = field(default_factory=Event, repr=False)
    cancel_event: Event = field(default_factory=Event, repr=False)

    def __post_init__(self) -> None:
        self.run_event.set()

    @property
    def title(self) -> str:
        action = "Перемещение" if self.move else "Копирование"
        return f"{action} → {self.destination}"


class _TransferCancelled(Exception):
    """Внутренний выход, при котором временная цель должна быть убрана."""


class TransferManager(QObject):
    """Последовательно или параллельно выполняет глобальные файловые задачи.

    Объект живёт у ``MainWindow``. Рабочие потоки получают только пути и простое
    состояние, а все сигналы и изменения интерфейса возвращаются в Qt-поток.
    """

    changed = Signal()
    taskFinished = Signal(object)
    # Python ``int`` нужен для байтов: Qt ``int`` 32-битный и ломает прогресс
    # уже после 2 ГБ — для папки с RAW это даже не разминка.
    _progressArrived = Signal(str, int, int, object, object, str)
    _finishedArrived = Signal(str, object)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.serial = settings.value("transfers/use_queue", True, bool)
        self.pending: list[TransferTask] = []
        self.active: dict[str, TransferTask] = {}
        self._tasks: dict[str, TransferTask] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=PARALLEL_TRANSFER_LIMIT,
            thread_name_prefix="file-transfer",
        )
        self._closing = False
        self._progressArrived.connect(self._apply_progress)
        self._finishedArrived.connect(self._finish_task)

    def enqueue(self, entries: list[TransferEntry], destination: Path, *, move: bool) -> str | None:
        """Добавляет согласованную операцию и запускает её, когда доступен слот."""
        if self._closing or not entries:
            return None
        task = TransferTask(list(entries), destination, move)
        self._tasks[task.identifier] = task
        self.pending.append(task)
        self.changed.emit()
        self._pump()
        return task.identifier

    def set_serial(self, serial: bool) -> None:
        """Меняет режим новых запусков; уже работающие задачи не прерываются."""
        self.serial = serial
        self.settings.setValue("transfers/use_queue", serial)
        self._pump()
        self.changed.emit()

    def target_reserved(self, path: Path) -> bool:
        """Не даёт двум ещё не завершённым задачам выбрать одно имя цели."""
        return any(
            entry.target == path
            for task in (*self.pending, *self.active.values())
            for entry in task.entries
        )

    def set_paused(self, paused: bool) -> None:
        """Приостанавливает активные задачи на ближайшей границе блока данных."""
        for task in self.active.values():
            if paused:
                task.run_event.clear()
                task.status = "paused"
            else:
                task.run_event.set()
                task.status = "running"
                task.speed_sample_at = monotonic()
                task.speed_sample_bytes = task.transferred_bytes
        self.changed.emit()

    def cancel(self, identifier: str) -> None:
        """Убирает ожидающую задачу или просит активный поток безопасно остановиться."""
        for index, task in enumerate(self.pending):
            if task.identifier == identifier:
                self.pending.pop(index)
                task.status = "cancelled"
                self._tasks.pop(identifier, None)
                self.changed.emit()
                return
        task = self.active.get(identifier)
        if task is not None:
            task.status = "cancelling"
            task.cancel_event.set()
            task.run_event.set()
            self.changed.emit()

    def cancel_active(self) -> None:
        """Отменяет операцию, подробно показанную в верхней части панели."""
        identifier = next(iter(self.active), None)
        if identifier is not None:
            self.cancel(identifier)

    def shutdown(self) -> None:
        """Останавливает очередь и дожидается очистки временных файлов."""
        if self._closing:
            return
        self._closing = True
        self.pending.clear()
        for task in self.active.values():
            task.cancel_event.set()
            task.run_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _pump(self) -> None:
        if self._closing:
            return
        limit = 1 if self.serial else PARALLEL_TRANSFER_LIMIT
        while self.pending and len(self.active) < limit:
            task = self.pending.pop(0)
            task.status = "preparing"
            task.started_at = monotonic()
            self.active[task.identifier] = task
            self._executor.submit(self._run_task, task)
        self.changed.emit()

    def _run_task(self, task: TransferTask) -> None:
        errors: list[str] = []
        try:
            task.total_files, task.total_bytes = self._measure(task)
            task.transfer_started_at = monotonic()
            self._report(task, "Подготовка завершена")
            for entry in task.entries:
                self._checkpoint(task)
                try:
                    self._transfer_entry(task, entry)
                except _TransferCancelled:
                    raise
                except OSError as exc:
                    errors.append(f"{entry.source.name}: {exc}")
        except _TransferCancelled:
            task.status = "cancelled"
        except OSError as exc:
            errors.append(str(exc))
        self._finishedArrived.emit(task.identifier, errors)

    def _measure(self, task: TransferTask) -> tuple[int, int]:
        files = 0
        total_bytes = 0
        for entry in task.entries:
            self._checkpoint(task)
            source = entry.source
            entry_files = 0
            entry_bytes = 0
            if source.is_file():
                entry_files = 1
                entry_bytes = source.stat().st_size
                task.entry_totals[source] = (entry_files, entry_bytes)
                files += entry_files
                total_bytes += entry_bytes
                continue
            for root, _dirs, names in os.walk(source):
                self._checkpoint(task)
                for name in names:
                    path = Path(root) / name
                    try:
                        if path.is_file():
                            entry_files += 1
                            entry_bytes += path.stat().st_size
                    except OSError:
                        # Ошибка повторится с понятным именем уже при копировании.
                        pass
            task.entry_totals[source] = (entry_files, entry_bytes)
            files += entry_files
            total_bytes += entry_bytes
        return files, total_bytes

    def _transfer_entry(self, task: TransferTask, entry: TransferEntry) -> None:
        source, target = entry.source, entry.target
        if not source.exists():
            raise OSError("источник больше не существует")
        source_is_dir = source.is_dir()
        if task.move and self._same_device(source, target.parent):
            self._checkpoint(task)
            self._publish_path(task, source, target, replace=entry.replace)
            if source_is_dir:
                relocate_folder_caches(source, target)
            entry_files, entry_bytes = task.entry_totals.get(source, (0, 0))
            task.completed_files += entry_files
            task.transferred_bytes += entry_bytes
            self._report(task, source.name)
            return
        temporary = target.with_name(f".{target.name}.rawww-part-{task.identifier[:8]}")
        self._remove_path(temporary)
        try:
            if source.is_dir():
                self._copy_directory(task, source, temporary)
            else:
                temporary.parent.mkdir(parents=True, exist_ok=True)
                self._copy_file(task, source, temporary)
            self._checkpoint(task)
            self._publish_path(task, temporary, target, replace=entry.replace)
            if task.move:
                self._retry_locked_path(task, lambda: self._remove_path(source))
                if source_is_dir:
                    relocate_folder_caches(source, target)
        except BaseException:
            self._remove_path(temporary)
            raise

    def _copy_directory(self, task: TransferTask, source: Path, temporary: Path) -> None:
        temporary.mkdir(parents=True)
        directories: list[tuple[Path, Path]] = [(source, temporary)]
        for root, dir_names, file_names in os.walk(source):
            self._checkpoint(task)
            root_path = Path(root)
            relative = root_path.relative_to(source)
            target_root = temporary / relative
            for name in dir_names:
                source_dir = root_path / name
                target_dir = target_root / name
                target_dir.mkdir()
                directories.append((source_dir, target_dir))
            for name in file_names:
                source_file = root_path / name
                self._copy_file(task, source_file, target_root / name)
        for source_dir, target_dir in reversed(directories):
            shutil.copystat(source_dir, target_dir, follow_symlinks=False)

    def _copy_file(self, task: TransferTask, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._copy_file_buffered(task, source, target)
        shutil.copystat(source, target, follow_symlinks=False)
        task.completed_files += 1
        self._report(task, source.name)

    def _copy_file_buffered(self, task: TransferTask, source: Path, target: Path) -> None:
        """Копирует внутренним циклом с одним большим переиспользуемым буфером."""
        if task.copy_buffer is None:
            task.copy_buffer = bytearray(COPY_CHUNK_SIZE)
        buffer = task.copy_buffer
        view = memoryview(buffer)
        with source.open("rb", buffering=0) as reader, target.open("xb", buffering=0) as writer:
            while True:
                self._checkpoint(task)
                read = reader.readinto(buffer)
                if not read:
                    break
                written = 0
                while written < read:
                    written += writer.write(view[written:read])
                task.transferred_bytes += read
                self._report(task, source.name)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _same_device(source: Path, destination: Path) -> bool:
        """Позволяет перемещению на одном томе остаться мгновенным rename."""
        try:
            return source.stat().st_dev == destination.stat().st_dev
        except OSError:
            return False

    def _publish_path(
        self,
        task: TransferTask,
        prepared: Path,
        target: Path,
        *,
        replace: bool,
    ) -> None:
        """Публикует готовый объект, сохраняя прежнюю цель до последнего шага."""
        self._checkpoint(task)
        if not target.exists():
            self._retry_locked_path(task, lambda: os.replace(prepared, target))
            return
        if not replace:
            raise OSError("цель появилась после постановки в очередь")
        backup = target.with_name(f".{target.name}.rawww-replaced-{task.identifier[:8]}")
        self._remove_path(backup)
        self._retry_locked_path(task, lambda: os.replace(target, backup))
        try:
            # После отвода старой цели отмена уже не должна оставить на её месте
            # пустоту: короткую публикацию обязательно доводим до конца.
            self._retry_locked_path(
                task,
                lambda: os.replace(prepared, target),
                honor_cancel=False,
            )
        except BaseException:
            os.replace(backup, target)
            raise
        self._remove_path(backup)

    def _retry_locked_path(self, task: TransferTask, operation, *, honor_cancel: bool = True) -> None:
        """Ждёт освобождения файла декодером, не блокируя Qt и не крутя CPU."""
        deadline = monotonic() + 30.0
        while True:
            if honor_cancel:
                self._checkpoint(task)
            try:
                operation()
                return
            except OSError as exc:
                sharing_error = (
                    getattr(exc, "winerror", None) in {5, 32, 33}
                    or exc.errno in {errno.EACCES, errno.EBUSY, errno.EPERM}
                )
                if not sharing_error or monotonic() >= deadline:
                    raise
                sleep(0.1)

    @staticmethod
    def _checkpoint(task: TransferTask) -> None:
        while not task.run_event.wait(0.1):
            if task.cancel_event.is_set():
                raise _TransferCancelled
        if task.cancel_event.is_set():
            raise _TransferCancelled

    def _report(self, task: TransferTask, detail: str) -> None:
        now = monotonic()
        if now - task.last_report_at < 0.1 and task.completed_files < task.total_files:
            return
        task.last_report_at = now
        self._progressArrived.emit(
            task.identifier,
            task.completed_files,
            task.total_files,
            task.transferred_bytes,
            task.total_bytes,
            detail,
        )

    def _apply_progress(
        self,
        identifier: str,
        completed: int,
        total: int,
        transferred: int,
        total_bytes: int,
        _detail: str,
    ) -> None:
        task = self.active.get(identifier)
        if task is None:
            return
        task.completed_files = completed
        task.total_files = total
        now = monotonic()
        if task.speed_sample_at is None:
            task.speed_sample_at = now
            task.speed_sample_bytes = transferred
        elif transferred > task.speed_sample_bytes:
            duration = now - task.speed_sample_at
            if duration > 0:
                current_speed = (transferred - task.speed_sample_bytes) / duration
                task.speed_bytes_per_second = (
                    current_speed
                    if not task.speed_bytes_per_second
                    else task.speed_bytes_per_second * 0.7 + current_speed * 0.3
                )
        if transferred != task.speed_sample_bytes:
            task.speed_sample_at = now
            task.speed_sample_bytes = transferred
        task.transferred_bytes = transferred
        task.total_bytes = total_bytes
        if task.status == "preparing":
            task.status = "running"
        self.changed.emit()

    def _finish_task(self, identifier: str, errors: object) -> None:
        task = self.active.pop(identifier, None)
        if task is None:
            return
        if isinstance(errors, list):
            task.errors.extend(str(error) for error in errors)
        if task.status not in {"cancelled", "cancelling"}:
            task.status = "failed" if task.errors else "finished"
        elif task.status == "cancelling":
            task.status = "cancelled"
        task.copy_buffer = None
        self._tasks.pop(identifier, None)
        self.taskFinished.emit(task)
        self._pump()


class TransferQueuePanel(QFrame):
    """Показывает общую очередь во всех вкладках и скрывается без работы."""

    def __init__(self, manager: TransferManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setObjectName("transferQueuePanel")
        self.setMinimumHeight(108)
        self.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)

        header = QHBoxLayout()
        title = QLabel("ФАЙЛОВЫЕ ОПЕРАЦИИ")
        title.setObjectName("transferQueueTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.pause_button = QToolButton()
        self.pause_button.setObjectName("transferQueueAction")
        self.pause_button.setIconSize(QSize(12, 12))
        self.pause_button.setToolTip("Пауза")
        self.pause_button.clicked.connect(self._toggle_pause)
        header.addWidget(self.pause_button)
        self.cancel_button = QToolButton()
        self.cancel_button.setObjectName("transferQueueCancel")
        self.cancel_button.setIcon(_fomantic_icon("close", 12, "#e8e8e8"))
        self.cancel_button.setIconSize(QSize(12, 12))
        self.cancel_button.setToolTip("Отменить активную операцию")
        self.cancel_button.clicked.connect(manager.cancel_active)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)

        self.active_title = QLabel()
        self.active_title.setObjectName("transferQueueActive")
        self.active_title.setWordWrap(True)
        layout.addWidget(self.active_title)
        self.progress = QProgressBar()
        self.progress.setObjectName("transferQueueProgress")
        layout.addWidget(self.progress)
        self.detail = QLabel()
        self.detail.setObjectName("transferQueueDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.queue_container = QWidget()
        self.queue_container.setObjectName("transferQueueContainer")
        self.queue_layout = QVBoxLayout(self.queue_container)
        self.queue_layout.setContentsMargins(0, 3, 0, 0)
        self.queue_layout.setSpacing(3)
        layout.addWidget(self.queue_container)
        manager.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        active = list(self.manager.active.values())
        pending = list(self.manager.pending)
        self.setVisible(bool(active or pending))
        if not active:
            self.active_title.setText("Ожидание запуска…")
            self.progress.setRange(0, 0)
            self.detail.clear()
        else:
            task = active[0]
            extra = f" (+{len(active) - 1})" if len(active) > 1 else ""
            self.active_title.setText(task.title + extra)
            if task.total_bytes:
                self.progress.setRange(0, 1000)
                self.progress.setValue(min(1000, round(task.transferred_bytes * 1000 / task.total_bytes)))
            elif task.status == "preparing":
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, max(1, task.total_files))
                self.progress.setValue(task.completed_files)
            self.progress.setFormat(
                f"{task.completed_files} из {task.total_files} файлов"
                if task.total_files
                else "Подсчёт файлов…"
            )
            if task.status == "preparing":
                self.detail.setText("Подсчитываю файлы и объём…")
            else:
                speed = task.speed_bytes_per_second
                remaining = max(0, task.total_bytes - task.transferred_bytes)
                eta = remaining / speed if speed > 0 and task.transferred_bytes and remaining else None
                volume = (
                    f"{format_transfer_size(task.transferred_bytes)} из "
                    f"{format_transfer_size(task.total_bytes)}"
                )
                speed_text = (
                    f"{format_transfer_size(speed)}/с"
                    if task.transferred_bytes
                    else "измеряю скорость"
                )
                eta_text = (
                    "готово"
                    if remaining == 0
                    else (format_transfer_eta(eta) if eta is not None else "считаю…")
                )
                pause_text = "Пауза · " if task.status == "paused" else ""
                self.detail.setText(
                    f"{pause_text}{volume} · {speed_text} · ~ {eta_text}"
                )
        paused = bool(active) and all(task.status == "paused" for task in active)
        self.pause_button.setIcon(
            _fomantic_icon("play" if paused else "pause", 12, "#e8e8e8")
        )
        self.pause_button.setToolTip("Продолжить" if paused else "Пауза")
        self.pause_button.setEnabled(bool(active))
        self.cancel_button.setEnabled(bool(active))
        self._rebuild_queue(active[1:], pending)

    def _toggle_pause(self) -> None:
        paused = bool(self.manager.active) and all(
            task.status == "paused" for task in self.manager.active.values()
        )
        self.manager.set_paused(not paused)

    def _rebuild_queue(
        self,
        other_active: list[TransferTask],
        pending: list[TransferTask],
    ) -> None:
        while self.queue_layout.count():
            item = self.queue_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not other_active and not pending:
            self.queue_container.hide()
            return
        self.queue_container.show()
        parts = []
        if other_active:
            parts.append(f"ещё выполняется: {len(other_active)}")
        if pending:
            parts.append(f"в очереди: {len(pending)}")
        caption = QLabel(" · ".join(parts).capitalize())
        caption.setObjectName("transferQueueCaption")
        self.queue_layout.addWidget(caption)
        for task in (*other_active, *pending):
            row = QWidget()
            row.setObjectName("transferQueueRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(3, 1, 1, 1)
            row_layout.setSpacing(4)
            label = QLabel(task.title)
            label.setObjectName("transferQueueItem")
            label.setToolTip(task.title)
            row_layout.addWidget(label, 1)
            cancel = QToolButton()
            cancel.setObjectName("transferQueueItemCancel")
            cancel.setIcon(_fomantic_icon("close", 11, "#e8e8e8"))
            cancel.setIconSize(QSize(11, 11))
            cancel.setToolTip("Убрать из очереди")
            cancel.clicked.connect(
                lambda _checked=False, identifier=task.identifier: self.manager.cancel(identifier)
            )
            row_layout.addWidget(cancel)
            self.queue_layout.addWidget(row)
