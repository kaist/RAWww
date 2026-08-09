## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from tempfile import TemporaryDirectory
import multiprocessing
import sys
import types
import unittest
from unittest.mock import patch

import rawww
from rawww import i18n
from rawww.launch import target_from_argv


class LaunchTargetTests(unittest.TestCase):
    """Проверяет выбор папки или файла из аргументов запуска."""

    def test_returns_existing_file_or_folder(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            image = folder / "image.jpg"
            image.touch()

            self.assertEqual(target_from_argv([str(image)]), image.resolve())
            self.assertEqual(target_from_argv([str(folder)]), folder.resolve())

    def test_ignores_options_and_missing_paths(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(target_from_argv(["--platform", str(folder)]), folder.resolve())
            self.assertIsNone(target_from_argv([str(folder / "missing")]))

    def test_freeze_support_runs_before_i18n_and_app_import(self):
        events = []
        fake_app = types.ModuleType("rawww.app")
        fake_app.main = lambda *args, **kwargs: events.append("app")

        with (
            patch.object(multiprocessing, "freeze_support", side_effect=lambda: events.append("freeze")),
            patch.object(i18n, "activate", side_effect=lambda: events.append("i18n")),
            patch.dict(sys.modules, {"rawww.app": fake_app}),
            patch.object(sys, "argv", ["rawww"]),
        ):
            rawww.main()

        self.assertEqual(events, ["freeze", "i18n", "app"])
