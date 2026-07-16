## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from rawww.runtime_paths import filesystem_name_key, filesystem_path_key


class RuntimePathTests(unittest.TestCase):
    """Проверяет ключи, которыми настройки не должны склеивать разные пути Linux."""

    def test_linux_keeps_filename_case(self) -> None:
        with patch("rawww.runtime_paths.sys.platform", "linux"):
            self.assertEqual(filesystem_name_key("Frame.JPG"), "Frame.JPG")
            self.assertNotEqual(filesystem_path_key(Path("Album/Frame")), filesystem_path_key(Path("album/frame")))

    def test_windows_and_macos_fold_filename_case(self) -> None:
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), patch("rawww.runtime_paths.sys.platform", platform):
                self.assertEqual(filesystem_name_key("Frame.JPG"), "frame.jpg")
