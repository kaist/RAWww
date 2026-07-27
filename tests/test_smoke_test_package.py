## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Тесты проверок уже собранного пакета приложения."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from scripts.smoke_test_package import MACOS_DISPLAY_NAMES, _check_macos_bundle_names


class MacosBundleNameTests(unittest.TestCase):
    def test_accepts_stable_bundle_with_localized_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "ctrlka.app"
            resources = app / "Contents" / "Resources"
            resources.mkdir(parents=True)
            with (app / "Contents" / "Info.plist").open("wb") as target:
                plistlib.dump({"CFBundleDisplayName": "Controlka"}, target)
            for language, name in MACOS_DISPLAY_NAMES.items():
                localized = resources / f"{language}.lproj"
                localized.mkdir()
                with (localized / "InfoPlist.strings").open("wb") as target:
                    plistlib.dump({"CFBundleDisplayName": name}, target)

            _check_macos_bundle_names(app)

    def test_rejects_localized_filesystem_bundle_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ctrlka.app"):
            _check_macos_bundle_names(Path("Контролька.app"))


if __name__ == "__main__":
    unittest.main()
