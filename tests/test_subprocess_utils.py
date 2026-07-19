## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from rawww.subprocess_utils import detached_process_kwargs


class SubprocessUtilsTests(unittest.TestCase):
    """Разделяет внутренние воркеры и самостоятельные внешние приложения."""

    def test_posix_external_process_starts_own_session(self) -> None:
        with patch("rawww.subprocess_utils.sys.platform", "linux"):
            self.assertEqual(detached_process_kwargs(), {"start_new_session": True})

    @unittest.skipUnless(hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"), "Windows only")
    def test_windows_external_process_can_leave_job(self) -> None:
        with patch("rawww.subprocess_utils.sys.platform", "win32"):
            kwargs = detached_process_kwargs()

        self.assertTrue(
            kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
