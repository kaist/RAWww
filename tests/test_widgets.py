## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit

from rawww.app import ViewerStrip
from rawww.shotsync_client import ShotSyncClient
from rawww.widgets import CodeReplacementsEditor


class ViewerStripExtendTests(unittest.TestCase):
    """Проверяет догрузку соседних страниц ленты без пересборки карточек."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_extend_appends_and_prepends_without_duplicates(self) -> None:
        strip = ViewerStrip(vertical=True)
        base = [Path(f"/photos/p_{i:03d}.jpg") for i in range(10)]
        strip.set_paths(base, base[0], {}, {})

        tail = [Path(f"/photos/p_{i:03d}.jpg") for i in range(10, 15)]
        strip.extend_paths(tail, {}, {}, at_start=False)
        self.assertEqual(strip._paths, base + tail)

        head = [Path(f"/photos/p_h{i}.jpg") for i in range(3)]
        strip.extend_paths(head, {}, {}, at_start=True)
        self.assertEqual(strip._paths, head + base + tail)
        self.assertEqual(strip.count(), len(head + base + tail))

        # Повторная догрузка уже показанных путей ничего не меняет.
        strip.extend_paths(tail, {}, {}, at_start=False)
        self.assertEqual(strip._paths, head + base + tail)
        strip.deleteLater()


class CodeReplacementsEditorTests(unittest.TestCase):
    """Проверяет редактор кодов замены и его локальное состояние."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_pending_local_code_is_committed_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.ini"
            settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
            client = ShotSyncClient("https://shotsync.invalid")
            editor = CodeReplacementsEditor(client, settings, lambda _sets: None, lambda: False)
            row = editor.table.rowCount() - 1
            code = editor.table.cellWidget(row, 0)
            value = editor.table.cellWidget(row, 1)
            self.assertIsInstance(code, QLineEdit)
            self.assertIsInstance(value, QLineEdit)
            code.setText("name")
            value.setText("Имя")

            self.assertTrue(editor.commit_pending_code())

            reloaded = QSettings(str(settings_path), QSettings.Format.IniFormat)
            sets = reloaded.value("code_replacements/local_sets", [], list)
            saved = sets[0]["codes"][0]
            self.assertTrue(saved["id"])
            self.assertEqual(saved["code"], "name")
            self.assertEqual(saved["value"], "Имя")
            editor.deleteLater()

    def test_incomplete_pending_code_prevents_dialog_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(
                str(Path(directory) / "settings.ini"),
                QSettings.Format.IniFormat,
            )
            client = ShotSyncClient("https://shotsync.invalid")
            editor = CodeReplacementsEditor(client, settings, lambda _sets: None, lambda: False)
            code = editor.table.cellWidget(editor.table.rowCount() - 1, 0)
            self.assertIsInstance(code, QLineEdit)
            code.setText("name")

            self.assertFalse(editor.commit_pending_code())
            self.assertEqual(editor.status.text(), "Заполните код и значение.")
            editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
