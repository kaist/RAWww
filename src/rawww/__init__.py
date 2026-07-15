## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

__all__ = ["main"]


def main(*args, **kwargs):
    """Ленивая точка входа.

    Импорт ``.app`` подтягивает QtGui и QtWidgets, которым нужен графический стек.
    Отложенный импорт позволяет использовать лёгкие модули вроде
    ``shotsync_socket`` в тестах и окружениях без дисплея.
    """
    from .app import main as _main

    return _main(*args, **kwargs)
from .version import __version__
