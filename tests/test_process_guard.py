## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from rawww.process_guard import (
    JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _ExtendedLimitInformation,
    install_process_tree_guard,
)


class ProcessGuardTests(unittest.TestCase):
    """Проверяет ABI-флаги защиты дерева процессов без изменения job тестов."""

    def test_kill_on_close_flag_fits_limit_information(self) -> None:
        limits = _ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )

        self.assertEqual(
            limits.basic_limit_information.limit_flags,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        )

    def test_unknown_platform_is_left_unchanged(self) -> None:
        from unittest.mock import patch

        with patch("rawww.process_guard.os.name", "unknown"):
            self.assertFalse(install_process_tree_guard())


if __name__ == "__main__":
    unittest.main()
