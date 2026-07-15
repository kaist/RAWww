## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Запуск дочерних процессов без внезапного мигания консольных окон."""

from __future__ import annotations

import subprocess
import sys


def no_window_kwargs() -> dict:
    """Возвращает параметры ``subprocess``, скрывающие дочернюю консоль Windows.

    ExifTool и Git — консольные программы. Без этих флагов оконная сборка на
    Windows ненадолго показывает консоль при запуске и сканировании папок.
    Сочетание ``CREATE_NO_WINDOW`` и скрытого ``STARTUPINFO`` убирает мигание.
    На других платформах функция возвращает пустой словарь.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }
