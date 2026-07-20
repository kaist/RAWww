## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки подготовки импорта карт памяти до постановки в очередь."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from rawww.card_import import (
    CardImportScan,
    build_backup_entries,
    build_import_entries,
    is_importable_file,
    merge_scans,
    scan_card,
)
from rawww.dialogs import CardImportDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class CardImportTests(unittest.TestCase):
    """Проверяет фильтр расширений, структуру и объединение нескольких карт."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dialog_does_not_turn_empty_backup_path_into_current_directory(self) -> None:
        _app()
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        settings.setValue("card_import/backup_destination", ".")
        settings.setValue("card_import/backup_enabled", True)
        dialog = CardImportDialog([self.root], settings)

        dialog.destination_edit.setText(str(self.root / "destination"))
        dialog._start()

        self.assertIsNone(dialog.options)
        self.assertIn("резервной копии", dialog.status.text())
        dialog.deleteLater()

    def test_dialog_requires_name_when_name_mode_is_selected(self) -> None:
        _app()
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        dialog = CardImportDialog([self.root], settings)
        dialog.destination_edit.setText(str(self.root / "destination"))
        dialog.name_mode.setChecked(True)
        dialog.shoot_name.clear()
        dialog._start()

        self.assertIsNone(dialog.options)
        self.assertIn("название съёмки", dialog.status.text())
        dialog.deleteLater()

    def test_service_extensions_are_skipped_without_using_directory_name(self) -> None:
        card = self.root / "card"
        nested = card / "PRIVATE" / "PANA_GRP"
        nested.mkdir(parents=True)
        (nested / "clip.mov").write_bytes(b"movie")
        (nested / "index.dat").write_bytes(b"index")
        (card / "CANONMSC").mkdir()
        (card / "CANONMSC" / "catalog.ctg").write_bytes(b"catalog")

        scan = scan_card(card)

        self.assertEqual(scan.files, (nested / "clip.mov",))
        self.assertFalse(is_importable_file(Path("photo.CTG")))
        self.assertTrue(is_importable_file(Path("photo.xmp")))

    def test_flattened_import_renames_same_names_from_different_cards(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        (first / "DCIM").mkdir(parents=True)
        (second / "DCIM").mkdir(parents=True)
        first_file = first / "DCIM" / "IMG_0001.CR3"
        second_file = second / "DCIM" / "IMG_0001.CR3"
        first_file.write_bytes(b"one")
        second_file.write_bytes(b"two")
        scan = merge_scans([
            CardImportScan(first, (first_file,), date(2026, 7, 20), (first,)),
            CardImportScan(second, (second_file,), date(2026, 7, 20), (second,)),
        ])

        entries = build_import_entries(scan, self.root / "import", flatten=True)

        self.assertEqual([entry.target.name for entry in entries], ["IMG_0001.CR3", "IMG_0001 (2).CR3"])

    def test_structure_and_backup_keep_relative_paths(self) -> None:
        card = self.root / "card"
        source = card / "DCIM" / "101CAM" / "photo.raw"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"raw")
        scan = CardImportScan(card, (source,), date(2026, 7, 20), (card,))
        imported = build_import_entries(scan, self.root / "import", flatten=False)

        backup = build_backup_entries(imported, self.root / "import", self.root / "backup", flatten=False)

        self.assertEqual(imported[0].target, self.root / "import" / "DCIM" / "101CAM" / "photo.raw")
        self.assertEqual(backup[0].target, self.root / "backup" / "DCIM" / "101CAM" / "photo.raw")

    def test_existing_identical_file_is_skipped_but_different_one_is_renamed(self) -> None:
        card = self.root / "card"
        source = card / "DCIM" / "photo.raw"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"first" + b"x" * 300_000 + b"last")
        destination = self.root / "destination"
        destination.mkdir()
        target = destination / "photo.raw"
        target.write_bytes(source.read_bytes())
        scan = CardImportScan(card, (source,), date(2026, 7, 20), (card,))

        self.assertEqual(build_import_entries(scan, destination, flatten=True), [])

        target.write_bytes(b"first" + b"y" * 300_000 + b"last")
        entries = build_import_entries(scan, destination, flatten=True)
        self.assertEqual([entry.target.name for entry in entries], ["photo (2).raw"])


if __name__ == "__main__":
    unittest.main()
