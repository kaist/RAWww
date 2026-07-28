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
from rawww.dialogs import BatchRenameDialog
from rawww.widgets import CodeReplacementsEditor, ScopeButtons


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


class ScopeButtonsTests(unittest.TestCase):
    """Две кнопки запуска: числа на обеих и запрет пустого запуска."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_counts_and_scope_reach_the_signal(self) -> None:
        scope = ScopeButtons("Обработать", 12, 3, "batchResize")
        self.assertEqual(scope.all_button.text(), "ОБРАБОТАТЬ ВСЕ (12)")
        self.assertEqual(scope.selected_button.text(), "ОБРАБОТАТЬ ВЫДЕЛЕННЫЕ (3)")
        requested: list[bool] = []
        scope.startRequested.connect(requested.append)

        scope.all_button.click()
        scope.selected_button.click()

        self.assertEqual(requested, [False, True])
        scope.deleteLater()

    def test_empty_selection_blocks_the_second_button(self) -> None:
        scope = ScopeButtons("Уменьшить", 5, 0, "batchResize")
        self.assertFalse(scope.selected_button.isEnabled())

        # Общая блокировка на время работы не имеет права включить запуск по
        # пустому выделению обратно.
        scope.setEnabled(False)
        scope.setEnabled(True)

        self.assertTrue(scope.all_button.isEnabled())
        self.assertFalse(scope.selected_button.isEnabled())
        scope.deleteLater()


class BatchRenameScopeTests(unittest.TestCase):
    """План переименования пересобирается под выбранный набор файлов."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_selected_scope_numbers_only_chosen_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(4):
                path = Path(directory) / f"src_{index}.jpg"
                path.write_bytes(b"")
                paths.append(path)
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("batch_rename/template", "IMG_{counter:03}")
            settings.setValue("batch_rename/counter_start", 1)
            dialog = BatchRenameDialog(paths, {}, settings, [paths[1], paths[3]])
            plans: list[dict] = []
            dialog.renameRequested.connect(plans.append)

            dialog.scope.selected_button.click()

            # Нумерация непрерывна внутри выбранного набора, а невыделенные
            # файлы в плане не участвуют вовсе.
            self.assertEqual(plans, [{"src_1.jpg": "IMG_001.jpg", "src_3.jpg": "IMG_002.jpg"}])
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
