## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Тесты проверки минимальной версии Mach-O для macOS-сборки."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_macos_min_version import _min_macos


class MacosMinVersionTests(unittest.TestCase):
    def test_reads_only_macos_deployment_commands(self) -> None:
        dump = """
Load command 1
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 12.3
      sdk 15.0
Load command 2
      cmd LC_ID_DYLIB
  cmdsize 48
  current version 99.0.0
Load command 3
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 2
    minos 17.0
"""
        completed = subprocess.CompletedProcess(
            args=["otool"], returncode=0, stdout=dump, stderr=""
        )

        with patch(
            "scripts.check_macos_min_version.subprocess.run", return_value=completed
        ):
            self.assertEqual(_min_macos(Path("library.dylib")), (12, 3))

    def test_uses_highest_requirement_from_universal_binary(self) -> None:
        dump = """
Load command 1
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 12.0
Load command 2
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 14.0
"""
        completed = subprocess.CompletedProcess(
            args=["otool"], returncode=0, stdout=dump, stderr=""
        )

        with patch(
            "scripts.check_macos_min_version.subprocess.run", return_value=completed
        ):
            self.assertEqual(_min_macos(Path("universal.dylib")), (14, 0))


if __name__ == "__main__":
    unittest.main()
