## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки глобальной очереди файловых операций и её клавиатурного входа."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from rawww.app import MainWindow
from rawww.dialogs import QuickTransferDialog
from rawww.transfer_queue import (
    TransferEntry,
    TransferManager,
    TransferQueuePanel,
    TransferTask,
    format_transfer_eta,
    format_transfer_size,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class TransferQueueTests(unittest.TestCase):
    """Проверяет выполнение, резервирование имён и управление панелью."""

    def setUp(self) -> None:
        _app()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
        self.manager = TransferManager(self.settings)

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.temp.cleanup()

    def test_copy_uses_atomic_target_and_reports_completion(self) -> None:
        root = Path(self.temp.name)
        source = root / "source.raw"
        destination = root / "destination"
        destination.mkdir()
        payload = b"raw-data" * 256_000
        source.write_bytes(payload)
        target = destination / source.name
        finished = []
        self.manager.taskFinished.connect(finished.append)

        task = TransferTask([TransferEntry(source, target)], destination, False)
        self.manager.active[task.identifier] = task
        self.manager._run_task(task)

        self.assertTrue(finished)
        self.assertEqual(target.read_bytes(), payload)
        self.assertTrue(source.exists())
        self.assertEqual(finished[0].completed_files, 1)
        self.assertEqual(finished[0].transferred_bytes, len(payload))
        self.assertEqual(list(destination.glob(".*.rawww-part-*")), [])

    def test_same_volume_move_keeps_system_rename_fast_path(self) -> None:
        root = Path(self.temp.name)
        source = root / "source.raw"
        source.write_bytes(b"raw-data")
        destination = root / "destination"
        destination.mkdir()
        target = destination / source.name
        finished = []
        self.manager.taskFinished.connect(finished.append)

        with patch("rawww.transfer_queue.TransferManager._copy_file") as copy_file:
            task = TransferTask([TransferEntry(source, target)], destination, True)
            self.manager.active[task.identifier] = task
            self.manager._run_task(task)
            self.assertTrue(finished)

        copy_file.assert_not_called()
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"raw-data")

    def test_sizes_and_short_eta_are_human_readable(self) -> None:
        self.assertEqual(format_transfer_size(5 * 1024**2), "5.0 МБ")
        self.assertEqual(format_transfer_size(3 * 1024**3), "3.0 ГБ")
        self.assertEqual(format_transfer_eta(0.25), "< 1 с")

    def test_panel_accepts_progress_larger_than_signed_32_bit(self) -> None:
        destination = Path(self.temp.name)
        task = TransferTask([], destination, False)
        task.status = "running"
        task.completed_files = 3
        task.total_files = 5
        task.transferred_bytes = 3 * 1024**3
        task.total_bytes = 5 * 1024**3
        task.started_at = task.transfer_started_at = 1.0
        self.manager.active[task.identifier] = task
        panel = TransferQueuePanel(self.manager)

        self.manager._report(task, "large.raw")
        _app().processEvents()
        panel.refresh()

        self.assertEqual(panel.progress.format(), "3 из 5 файлов")
        self.assertIn("3.0 ГБ из 5.0 ГБ", panel.detail.text())
        self.assertIn(" · ~ ", panel.detail.text())
        self.assertNotIn("ETA", panel.detail.text())
        self.manager.active.clear()
        panel.deleteLater()

    def test_pending_task_can_be_removed_before_start(self) -> None:
        root = Path(self.temp.name)
        source = root / "source.raw"
        source.write_bytes(b"data")
        destination = root / "destination"
        destination.mkdir()

        with patch.object(self.manager, "_pump"):
            first = self.manager.enqueue(
                [TransferEntry(source, destination / "first.raw")], destination, move=False
            )
            second = self.manager.enqueue(
                [TransferEntry(source, destination / "second.raw")], destination, move=False
            )
        self.assertTrue(self.manager.target_reserved(destination / "second.raw"))

        self.manager.cancel(second)

        self.assertEqual([task.identifier for task in self.manager.pending], [first])
        self.assertFalse(self.manager.target_reserved(destination / "second.raw"))

    def test_parallel_card_tasks_share_slots_when_regular_queue_is_serial(self) -> None:
        root = Path(self.temp.name)
        destination = root / "destination"
        destination.mkdir()
        first_source = root / "first.raw"
        second_source = root / "second.raw"
        first_source.write_bytes(b"first")
        second_source.write_bytes(b"second")

        with patch.object(self.manager._executor, "submit"):
            first = self.manager.enqueue(
                [TransferEntry(first_source, destination / first_source.name)],
                destination,
                move=False,
                parallel=True,
            )
            second = self.manager.enqueue(
                [TransferEntry(second_source, destination / second_source.name)],
                destination,
                move=False,
                parallel=True,
            )

        self.assertEqual(set(self.manager.active), {first, second})

    def test_panel_is_visible_only_while_manager_has_work(self) -> None:
        root = Path(self.temp.name)
        source = root / "source.raw"
        source.write_bytes(b"data")
        destination = root / "destination"
        destination.mkdir()
        panel = TransferQueuePanel(self.manager)
        self.assertFalse(panel.isVisible())

        with patch.object(self.manager, "_pump"):
            identifier = self.manager.enqueue(
                [TransferEntry(source, destination / source.name)], destination, move=False
            )
        panel.refresh()
        self.assertFalse(panel.isHidden())

        self.manager.cancel(identifier)
        panel.refresh()
        self.assertTrue(panel.isHidden())
        panel.deleteLater()

    def test_enter_confirms_selected_quick_destination(self) -> None:
        destination = Path(self.temp.name)
        accepted = []
        dialog = QuickTransferDialog(
            "скопировать",
            [destination],
            QKeySequence("Shift+C"),
            lambda path, remember: accepted.append((path, remember)),
        )

        QTimer.singleShot(25, lambda: QTest.keyClick(dialog, Qt.Key.Key_Return))
        QTimer.singleShot(500, dialog.reject)
        dialog.exec()

        self.assertEqual(accepted, [(destination, True)])
        dialog.deleteLater()

    def test_escape_rejects_quick_transfer_without_submitting(self) -> None:
        destination = Path(self.temp.name)
        accepted = []
        dialog = QuickTransferDialog(
            "переместить",
            [destination],
            QKeySequence("Shift+M"),
            lambda path, remember: accepted.append((path, remember)),
        )

        QTimer.singleShot(25, lambda: QTest.keyClick(dialog, Qt.Key.Key_Escape))
        QTimer.singleShot(500, dialog.reject)
        result = dialog.exec()

        self.assertEqual(result, QDialog.DialogCode.Rejected)
        self.assertEqual(accepted, [])
        dialog.deleteLater()

    def test_window_close_can_be_rejected_while_transfer_is_active(self) -> None:
        window = SimpleNamespace(
            _closing=False,
            transfer_manager=SimpleNamespace(active={"running": object()}, pending=[]),
        )
        event = SimpleNamespace(ignore=lambda: setattr(event, "ignored", True))
        event.ignored = False

        with patch(
            "rawww.app.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            MainWindow.closeEvent(window, event)

        self.assertTrue(event.ignored)
        self.assertFalse(window._closing)


if __name__ == "__main__":
    unittest.main()
