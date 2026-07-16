## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import patch

from rawww.windows_activation import activate_foreground_window


class _Function:
    """Имитирует функцию user32 и позволяет ctypes назначить её сигнатуру."""

    def __init__(self, name: str, calls: list[tuple], result=1) -> None:
        self.name = name
        self.calls = calls
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        self.calls.append((self.name, *arguments))
        return self.result


class _User32:
    def __init__(self, calls: list[tuple]) -> None:
        self.IsIconic = _Function("IsIconic", calls, 1)
        self.ShowWindow = _Function("ShowWindow", calls)
        self.SetForegroundWindow = _Function("SetForegroundWindow", calls)
        self.BringWindowToTop = _Function("BringWindowToTop", calls)
        self.SetActiveWindow = _Function("SetActiveWindow", calls)


class WindowsActivationTests(unittest.TestCase):
    def test_minimized_window_is_restored_before_foreground_activation(self) -> None:
        calls: list[tuple] = []
        user32 = _User32(calls)

        with (
            patch("rawww.windows_activation.sys.platform", "win32"),
            patch("rawww.windows_activation.ctypes.WinDLL", return_value=user32),
        ):
            self.assertTrue(activate_foreground_window(123))

        self.assertEqual(
            [call[0] for call in calls],
            [
                "IsIconic",
                "ShowWindow",
                "SetForegroundWindow",
                "BringWindowToTop",
                "SetActiveWindow",
            ],
        )
        self.assertEqual(calls[1][2], 9)  # SW_RESTORE


if __name__ == "__main__":
    unittest.main()
