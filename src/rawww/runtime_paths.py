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
        return Path(sys.executable).resolve().parent / "data" / name
    return Path(__file__).with_name(name)
