## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет управление долгими операциями утилит и общий учёт их прогресса."""

from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rawww.app import MainWindow, Workspace
from rawww.batch_jobs import (
    BatchJobControl,
    CallbackJobControl,
    PoolBatchJob,
    UtilityProgressHub,
    utility_progress_hub,
)
from rawww.widgets import BatchProgressBar, format_remaining_time


def _double(value: int) -> int:
    """Задача пула обязана быть picklable, поэтому живёт на уровне модуля."""
    return value * 2


class BatchJobControlTests(unittest.TestCase):
    def test_pause_blocks_until_resume(self) -> None:
        control = BatchJobControl()
        control.pause()
        self.assertTrue(control.paused)
        threading.Timer(0.05, control.resume).start()

        self.assertTrue(control.wait_while_paused())
        self.assertFalse(control.paused)

    def test_stop_releases_pause_and_denies_next_task(self) -> None:
        """Иначе исполнитель ждал бы снятия паузы и не увидел бы остановку."""
        control = BatchJobControl()
        control.pause()
        control.stop()

        self.assertFalse(control.wait_while_paused())
        self.assertTrue(control.stopped)

    def test_pause_after_stop_is_ignored(self) -> None:
        control = BatchJobControl()
        control.stop()
        control.pause()

        self.assertFalse(control.paused)

    def test_callback_control_sends_each_command_once(self) -> None:
        calls: list[str] = []
        control = CallbackJobControl(
            lambda: calls.append("pause"),
            lambda: calls.append("resume"),
            lambda: calls.append("stop"),
        )
        control.pause()
        control.pause()
        control.resume()
        control.stop()
        control.stop()

        self.assertEqual(calls, ["pause", "resume", "stop"])
        self.assertTrue(control.stopped)
        self.assertFalse(control.paused)


class UtilityProgressHubTests(unittest.TestCase):
    def test_progress_sums_jobs_and_reports_common_pause(self) -> None:
        hub = UtilityProgressHub()
        first = hub.register("Резайс", BatchJobControl())
        second = hub.register("Уменьшить JPG", BatchJobControl())
        hub.update(first, 3, 10)
        hub.update(second, 1, 5)

        self.assertEqual(hub.progress(), (4, 15, False))
        self.assertEqual(hub.busy_labels(), ["Резайс", "Уменьшить JPG"])

        first.control.pause()
        self.assertEqual(hub.progress()[2], False)
        second.control.pause()
        self.assertEqual(hub.progress()[2], True)

    def test_finished_job_leaves_no_progress_behind(self) -> None:
        hub = UtilityProgressHub()
        job = hub.register("Ретушь", BatchJobControl())
        hub.update(job, 2, 4)
        hub.finish(job)

        self.assertFalse(hub.is_busy())
        self.assertEqual(hub.progress(), (0, 0, False))

    def test_stop_all_stops_every_registered_operation(self) -> None:
        hub = UtilityProgressHub()
        controls = [BatchJobControl(), BatchJobControl()]
        for index, control in enumerate(controls):
            hub.register(f"job {index}", control)

        hub.stop_all()

        self.assertTrue(all(control.stopped for control in controls))

    def test_update_after_finish_is_ignored(self) -> None:
        """Поздний отчёт остановленной операции не должен возвращать её в реестр."""
        hub = UtilityProgressHub()
        job = hub.register("Резайс", BatchJobControl())
        hub.finish(job)
        hub.update(job, 5, 5)

        self.assertFalse(hub.is_busy())


class PoolBatchJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _drain(self, job: PoolBatchJob, timeout: float = 60.0) -> bool:
        outcome: list[bool] = []
        job.finished.connect(outcome.append)
        job.start()
        deadline = threading.Event()
        threading.Timer(timeout, deadline.set).start()
        while not outcome and not deadline.is_set():
            self.app.processEvents()
        self.assertTrue(outcome, "операция не завершилась в отведённое время")
        return outcome[0]

    def test_all_tasks_are_processed_and_reported(self) -> None:
        job = PoolBatchJob(_double, [1, 2, 3], label="Тест", max_workers=2)
        results: list[object] = []
        job.itemFinished.connect(results.append)

        whole = self._drain(job)

        self.assertTrue(whole)
        self.assertEqual(sorted(results), [2, 4, 6])
        self.assertFalse(utility_progress_hub().is_busy())

    def test_stopped_job_reports_incomplete_run(self) -> None:
        job = PoolBatchJob(_double, list(range(200)), label="Тест", max_workers=1)
        job.progress.connect(lambda done, _total: job.stop() if done else None)

        whole = self._drain(job)

        self.assertFalse(whole)
        self.assertFalse(utility_progress_hub().is_busy())


class TaskbarPriorityTests(unittest.TestCase):
    """Индикатор приложения один, поэтому утилита обязана быть важнее фона."""

    def test_utility_progress_wins_over_transfers_and_carries_pause(self) -> None:
        hub = utility_progress_hub()
        job = hub.register("Резайс", BatchJobControl())
        hub.update(job, 4, 8)
        job.control.pause()
        taskbar, dock = Mock(), Mock()
        workspace = SimpleNamespace(
            transfer_manager=SimpleNamespace(progress=lambda: (7, 9)),
            _taskbar_progress=taskbar,
            _dock_progress=dock,
            window=lambda: SimpleNamespace(winId=lambda: 42),
        )
        try:
            Workspace._set_taskbar_progress(workspace, 1, 3)
        finally:
            hub.finish(job)

        taskbar.set_progress.assert_called_once_with(42, 4, 8, True)
        dock.set_progress.assert_called_once_with(4, 8)

    def test_exit_question_lists_unfinished_utilities(self) -> None:
        window = SimpleNamespace(
            transfer_manager=SimpleNamespace(active={}, pending=[]),
        )
        question = MainWindow._unfinished_operations_question(window, ["Резайс", "Ретушь"])

        self.assertIn("Резайс, Ретушь", question)


class BatchProgressBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_pause_click_reports_state_and_marks_format(self) -> None:
        widget = BatchProgressBar()
        states: list[bool] = []
        widget.pauseToggled.connect(states.append)
        widget.start(4, "Экспорт")
        widget.set_progress(1, 4, "Экспорт: 1/4")

        widget.pause_button.click()
        self.assertEqual(states, [True])
        self.assertTrue(widget.paused)
        self.assertEqual(widget.bar.format(), "Пауза · Экспорт: 1/4")

        widget.pause_button.click()
        self.assertEqual(states, [True, False])
        self.assertEqual(widget.bar.format(), "Экспорт: 1/4")
        widget.deleteLater()

    def test_stop_click_only_asks_owner(self) -> None:
        """Кнопки гаснут после подтверждения владельцем, а не сразу по клику."""
        widget = BatchProgressBar()
        asked: list[bool] = []
        widget.stopRequested.connect(lambda: asked.append(True))
        widget.start(2, "Сжатие")

        widget.stop_button.click()

        self.assertEqual(asked, [True])
        self.assertTrue(widget.stop_button.isEnabled())

        widget.set_stopping()
        self.assertFalse(widget.stop_button.isEnabled())
        self.assertFalse(widget.pause_button.isEnabled())
        widget.deleteLater()


class RemainingTimeTests(unittest.TestCase):
    def test_units_grow_with_the_wait(self) -> None:
        self.assertEqual(format_remaining_time(0.2), "≈ 1 с")
        self.assertEqual(format_remaining_time(59), "≈ 59 с")
        self.assertEqual(format_remaining_time(61), "≈ 1 мин 1 с")
        self.assertEqual(format_remaining_time(3_599), "≈ 59 мин 59 с")
        self.assertEqual(format_remaining_time(3_601), "≈ 1 ч 0 мин 1 с")


if __name__ == "__main__":
    unittest.main()
