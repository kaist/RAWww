## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Пауза, остановка и общий учёт долгих операций утилит.

Утилит четыре, они живут в разных окнах, но панель задач и Dock у приложения
одни. Поэтому прогресс каждой операции регистрируется в общем реестре, а окно
приложения только читает суммарные числа. Управление вынесено в отдельные
объекты: пул процессов слушает флаги в памяти, а воркер ретуши — команды в своём
потоке ввода, и вызывающему коду это различие видеть незачем.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait

from PySide6.QtCore import QObject, Signal


class BatchJobControl:
    """Кооперативная пауза и остановка операции, которая живёт в этом процессе.

    Флаги проверяет исполнитель на границе задачи, поэтому пауза не оставляет
    файл записанным наполовину, а остановка не отменяет уже начатый кадр.
    """

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._stopped = threading.Event()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def pause(self) -> None:
        if not self._stopped.is_set():
            self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        # Остановка снимает и паузу: иначе исполнитель остался бы ждать в
        # ``wait_while_paused`` и не увидел бы просьбу закончить работу.
        self._stopped.set()
        self._running.set()

    def wait_while_paused(self) -> bool:
        """Ждёт снятия паузы и отвечает, стоит ли брать следующую задачу."""
        self._running.wait()
        return not self._stopped.is_set()


class CallbackJobControl:
    """Управление операцией, исполнитель которой живёт в чужом процессе.

    Флаги в памяти дочернему процессу не видны, поэтому пауза и остановка
    превращаются в вызовы владельца — обычно отправку команды в поток ввода
    воркера. Состояние паузы хранится здесь, чтобы индикаторы и реестр читали
    его так же, как у ``BatchJobControl``.
    """

    def __init__(
        self,
        pause: Callable[[], None],
        resume: Callable[[], None],
        stop: Callable[[], None],
    ) -> None:
        self._pause = pause
        self._resume = resume
        self._stop = stop
        self._paused = False
        self._stopped = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped

    def pause(self) -> None:
        if self._stopped or self._paused:
            return
        self._paused = True
        self._pause()

    def resume(self) -> None:
        if self._stopped or not self._paused:
            return
        self._paused = False
        self._resume()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._paused = False
        self._stop()


class UtilityJob:
    """Запись реестра об одной операции утилиты: название, прогресс, управление."""

    def __init__(self, label: str, control: BatchJobControl | CallbackJobControl) -> None:
        self.label = label
        self.control = control
        self.done = 0
        self.total = 0

    @property
    def paused(self) -> bool:
        return bool(self.control.paused)


class UtilityProgressHub(QObject):
    """Собирает прогресс всех запущенных утилит для индикатора приложения.

    Реестр не знает ни одного виджета: он хранит числа и ссылки на управление,
    чтобы окно приложения могло показать общий прогресс в панели задач, а при
    закрытии — спросить пользователя и остановить всё разом. Отчёты приходят и из
    рабочих потоков, поэтому список операций защищён замком, а виджеты получают
    ``changed`` уже в главном потоке.
    """

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._jobs: list[UtilityJob] = []

    def register(self, label: str, control: BatchJobControl | CallbackJobControl) -> UtilityJob:
        job = UtilityJob(label, control)
        with self._lock:
            self._jobs.append(job)
        self.changed.emit()
        return job

    def update(self, job: UtilityJob, done: int, total: int) -> None:
        with self._lock:
            if job not in self._jobs:
                # Остановленную операцию поздний отчёт не должен вернуть в реестр.
                return
            job.done, job.total = max(0, done), max(0, total)
        self.changed.emit()

    def finish(self, job: UtilityJob) -> None:
        with self._lock:
            if job not in self._jobs:
                return
            self._jobs.remove(job)
        self.changed.emit()

    def notify_changed(self) -> None:
        """Обновляет индикаторы после смены паузы, о которой реестр не узнал сам."""
        self.changed.emit()

    def active_jobs(self) -> tuple[UtilityJob, ...]:
        with self._lock:
            return tuple(self._jobs)

    def is_busy(self) -> bool:
        return bool(self.active_jobs())

    def busy_labels(self) -> list[str]:
        labels: list[str] = []
        for job in self.active_jobs():
            if job.label not in labels:
                labels.append(job.label)
        return labels

    def progress(self) -> tuple[int, int, bool]:
        """Возвращает суммарный прогресс утилит и признак общей паузы.

        Индикатор один, а операций может быть несколько, поэтому числа
        складываются. Операции без известного объёма в сумму не попадают: пустой
        итог честнее выдуманного.
        """
        jobs = self.active_jobs()
        if not jobs:
            return 0, 0, False
        total = sum(job.total for job in jobs)
        done = sum(min(job.done, job.total) for job in jobs)
        return done, total, all(job.paused for job in jobs)

    def stop_all(self) -> None:
        for job in self.active_jobs():
            job.control.stop()


_hub: UtilityProgressHub | None = None


def utility_progress_hub() -> UtilityProgressHub:
    """Отдаёт единственный реестр операций утилит на всё приложение."""
    global _hub
    if _hub is None:
        _hub = UtilityProgressHub()
    return _hub


class PoolBatchJob(QObject):
    """Выполняет однотипные задачи в пуле процессов, слушая паузу и остановку.

    Поток-водитель держит небольшое окно поданных задач: пауза перестаёт
    подавать новые, а остановка отменяет ещё не начатые и ждёт только те, что
    уже ушли в процессы. Главный поток при этом свободен, поэтому окно утилиты
    остаётся отзывчивым и на сотнях кадров. Итог задачи отдаётся сигналом как
    есть: агрегировать ошибки и сэкономленные байты — дело утилиты.
    """

    progress = Signal(int, int)
    itemFinished = Signal(object)
    finished = Signal(bool)

    def __init__(
        self,
        worker: Callable[..., object],
        jobs: Sequence[object],
        *,
        label: str,
        max_workers: int = 1,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._jobs = list(jobs)
        self._label = label
        self._max_workers = max(1, min(max_workers, max(1, len(self._jobs))))
        self.control = BatchJobControl()
        self._entry: UtilityJob | None = None
        self._thread: threading.Thread | None = None

    @property
    def total(self) -> int:
        return len(self._jobs)

    @property
    def paused(self) -> bool:
        return self.control.paused

    def start(self) -> None:
        if self._thread is not None:
            return
        hub = utility_progress_hub()
        self._entry = hub.register(self._label, self.control)
        hub.update(self._entry, 0, len(self._jobs))
        self._thread = threading.Thread(target=self._drive, name="rawww-batch-job", daemon=True)
        self._thread.start()

    def set_paused(self, paused: bool) -> None:
        self.control.pause() if paused else self.control.resume()
        utility_progress_hub().notify_changed()

    def stop(self) -> None:
        self.control.stop()
        utility_progress_hub().notify_changed()

    def _drive(self) -> None:
        """Подаёт задачи в пул и собирает результаты вне главного потока."""
        completed = 0
        total = len(self._jobs)
        window = max(1, self._max_workers * 2)
        index = 0
        pending: set[Future] = set()
        executor = ProcessPoolExecutor(max_workers=self._max_workers)
        try:
            while True:
                if not self.control.stopped:
                    while len(pending) < window and index < total and not self.control.paused:
                        pending.add(executor.submit(self._worker, self._jobs[index]))
                        index += 1
                if not pending:
                    if self.control.stopped or index >= total:
                        break
                    # Пауза без начатых задач: ждём события вместо холостого цикла.
                    if not self.control.wait_while_paused():
                        break
                    continue
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    completed += 1
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 — задача сама решает, как показать сбой
                        result = exc
                    self.itemFinished.emit(result)
                if done:
                    self._report(completed, total)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            self._release()
        self.finished.emit(not self.control.stopped and completed >= total)

    def _report(self, completed: int, total: int) -> None:
        if self._entry is not None:
            utility_progress_hub().update(self._entry, completed, total)
        self.progress.emit(completed, total)

    def _release(self) -> None:
        entry, self._entry = self._entry, None
        if entry is not None:
            utility_progress_hub().finish(entry)
