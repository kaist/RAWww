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


def detached_process_kwargs() -> dict:
    """Отделяет внешнее приложение от управляемого дерева Контрольки.

    Используется только для программ, которые пользователь явно просит оставить
    самостоятельными, например редактора или Проводника. Внутренним воркерам
    этот выход из Job Object/группы процессов давать нельзя.
    """
    if sys.platform == "win32":
        kwargs = no_window_kwargs()
        kwargs["creationflags"] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        return kwargs
    return {"start_new_session": True}
