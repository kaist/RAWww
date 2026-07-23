## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Расположение ресурсов в исходниках и в собранном приложении."""

from __future__ import annotations

import sys
from pathlib import Path


PORTABLE = bool(
    getattr(sys, "frozen", False)
    and (Path(sys.executable).resolve().parent / "portable.flag").is_file()
)


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def work_path() -> Path:
    return application_directory() / "work"


def data_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        # На macOS готовое приложение — это .app: исполняемый файл лежит в
        # Contents/MacOS, а собранные ресурсы — в Contents/Frameworks
        # (``sys._MEIPASS``), откуда PyInstaller симлинкует ``data`` в
        # Contents/Resources. На Windows и Linux сборка кладёт ``data`` рядом с
        # исполняемым файлом, поэтому там ориентир — его каталог.
        if sys.platform == "darwin":
            return Path(sys._MEIPASS) / "data" / name  # type: ignore[attr-defined]
        return Path(sys.executable).resolve().parent / "data" / name
    return Path(__file__).with_name(name)


def filesystem_name_key(name: str) -> str:
    """Возвращает ключ имени с учётом обычной чувствительности файловой системы."""
    return name.casefold() if sys.platform in {"win32", "darwin"} else name


def filesystem_path_key(path: Path) -> str:
    """Нормализует путь для настроек, не склеивая разные по регистру пути Linux."""
    try:
        value = str(path.expanduser().resolve())
    except OSError:
        value = str(path.expanduser())
    return filesystem_name_key(value)
