## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Передача права активации окна между экземплярами приложения в Windows."""

from __future__ import annotations

import ctypes
import sys


def grant_foreground_activation() -> bool:
    """Разрешает уже запущенному экземпляру вывести своё окно на передний план.

    Повторный процесс вызывается действием пользователя в Проводнике и поэтому
    обычно имеет право передать foreground-разрешение. Windows может отказать,
    например если за это время пользователь уже переключился в другое окно.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
        user32.AllowSetForegroundWindow.restype = ctypes.c_int
        return bool(user32.AllowSetForegroundWindow(0xFFFFFFFF))  # ASFW_ANY
    except (AttributeError, OSError):
        return False


def activate_foreground_window(window_handle: int) -> bool:
    """Активирует уже показанное Qt-окно через Win32 после передачи разрешения."""
    if sys.platform != "win32" or not window_handle:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = ctypes.c_void_p(window_handle)
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.BringWindowToTop.restype = ctypes.c_int
        user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
        user32.SetActiveWindow.restype = ctypes.c_void_p

        # Qt-запрос из фонового приложения Windows иногда оставляет окно
        # свёрнутым. SW_RESTORE сначала физически восстанавливает HWND.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # SetForegroundWindow отвечает за межпроцессную активацию; остальные
        # вызовы закрепляют Z-order и активное окно внутри GUI-потока Qt.
        foreground = bool(user32.SetForegroundWindow(hwnd))
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        return foreground
    except (AttributeError, OSError):
        return False
