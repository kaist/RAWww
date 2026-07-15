## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Определяет файловый путь, с которым приложение запустил проводник."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


def target_from_argv(argv: Sequence[str] | None = None) -> Path | None:
    """Возвращает первый существующий путь из аргументов запуска.

    Файловый менеджер передаёт его при открытии ассоциации или команды папки.
    Параметры запуска пропускаются, а путь, начинающийся с дефиса, можно явно
    указать после ``--``.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    positional = False
    for argument in arguments:
        if argument == "--":
            positional = True
            continue
        if not positional and argument.startswith("-"):
            continue
        path = Path(argument).expanduser()
        try:
            return path.resolve(strict=True)
        except OSError:
            return None
    return None
