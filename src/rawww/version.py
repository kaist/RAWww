## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Определение версии приложения для исходной и собранной поставки."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_BASE_VERSION_FILE = _ROOT / "VERSION"


def _base_version() -> str:
    """Читает общую для пакетов базовую часть номера версии."""
    value = _BASE_VERSION_FILE.read_text(encoding="utf-8").strip()
    parts = value.split(".")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise RuntimeError(f"Invalid base version in {_BASE_VERSION_FILE}: {value!r}")
    return value


def _no_window_kwargs() -> dict:
    """Локальная копия ``subprocess_utils.no_window_kwargs``.

    ``setuptools`` выполняет этот модуль отдельно, чтобы прочитать динамическую
    версию, когда пакет ``rawww`` ещё не установлен. Поэтому относительный импорт
    здесь сломал бы сборку — небольшое дублирование в данном месте полезнее
    красивой, но неработающей абстракции.
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


def _git_revision() -> int | None:
    if getattr(sys, "frozen", False):
        return None

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None

    try:
        revision = int(result.stdout.strip())
    except ValueError:
        return None
    return revision if revision >= 0 else None


def _resolve_version() -> str:
    try:
        from ._build_version import VERSION
    except ImportError:
        return f"{_base_version()}.{_git_revision() or 0}"
    return VERSION


__version__ = _resolve_version()
