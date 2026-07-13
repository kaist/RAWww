from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit

from rawww.shotsync_client import ShotSyncClient
from rawww.widgets import CodeReplacementsEditor


class CodeReplacementsEditorTests(unittest.TestCase):
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
