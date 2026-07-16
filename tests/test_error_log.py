## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rawww import error_log


class ErrorLogTests(unittest.TestCase):
    """Проверяет сохранение stderr и выбор каталога для разных видов сборки."""

    def test_stream_duplicates_stderr_into_log_and_clear_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "errors.log"
            original = io.StringIO()
            with patch("rawww.error_log.error_log_path", return_value=path):
                stream = error_log._ErrorLogStream(original)
                self.assertEqual(stream.write("worker failed\n"), len("worker failed\n"))
                self.assertEqual(original.getvalue(), "worker failed\n")
                self.assertEqual(error_log.read_error_log(), "worker failed\n")
                error_log.clear_error_log()
                self.assertEqual(error_log.read_error_log(), "")

    def test_portable_log_stays_next_to_application(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("rawww.error_log.PORTABLE", True),
                patch("rawww.error_log.work_path", return_value=Path(directory) / "work"),
            ):
                self.assertEqual(
                    error_log.error_log_path(),
                    Path(directory) / "work" / "logs" / "errors.log",
                )

    def test_installed_windows_log_uses_user_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("rawww.error_log.PORTABLE", False),
                patch("rawww.error_log.sys.platform", "win32"),
                patch.dict(os.environ, {"LOCALAPPDATA": directory}),
            ):
                self.assertEqual(
                    error_log.error_log_path(),
                    Path(directory) / "ShotSync" / "Ctrlka" / "errors.log",
                )
