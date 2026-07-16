## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Хранит stderr приложения в локальном журнале, доступном из интерфейса."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path
from typing import TextIO

from .runtime_paths import PORTABLE, work_path


MAX_LOG_SIZE = 2 * 1024 * 1024
LOG_NAME = "errors.log"
_write_lock = threading.Lock()
_installed = False


def error_log_path() -> Path:
    """Возвращает путь журнала с учётом переносимой и установленной версий."""
    if PORTABLE:
        return work_path() / "logs" / LOG_NAME
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "ShotSync" / "Ctrlka" / LOG_NAME


def _append(text: str) -> None:
    """Добавляет stderr в журнал, не позволяя ошибке логирования сломать приложение."""
    if not text:
        return
    try:
        path = error_log_path()
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(text.encode("utf-8", errors="replace")) > MAX_LOG_SIZE:
                backup = path.with_suffix(".previous.log")
                backup.unlink(missing_ok=True)
                path.replace(backup)
            with path.open("a", encoding="utf-8", errors="replace") as output:
                output.write(text)
    except OSError:
        # Логирование диагностическое: отсутствие доступа к диску не должно
        # превращать исходную ошибку в каскад новых исключений.
        return


class _ErrorLogStream:
    """Дублирует stderr в исходный поток и файл без буферизации между записями."""

    def __init__(self, original: TextIO) -> None:
        self._original = original

    def write(self, text: str) -> int:
        written = self._original.write(text)
        _append(text)
        return written

    def flush(self) -> None:
        self._original.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    @property
    def encoding(self) -> str | None:
        return self._original.encoding


def install_error_logging() -> None:
    """Перехватывает stderr и необработанные исключения главного и фоновых потоков."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.stderr = _ErrorLogStream(sys.stderr)  # type: ignore[assignment]

    def report_exception(exc_type, exc_value, exc_traceback) -> None:
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)

    def report_thread_exception(args: threading.ExceptHookArgs) -> None:
        report_exception(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = report_exception
    threading.excepthook = report_thread_exception


def log_exception(context: str, exception: BaseException) -> None:
    """Записывает перехваченную ошибку фоновой задачи вместе с её traceback."""
    print(context, file=sys.stderr)
    traceback.print_exception(type(exception), exception, exception.__traceback__, file=sys.stderr)


def read_error_log() -> str:
    """Возвращает журнал для окна диагностики; отсутствие файла считается пустым журналом."""
    try:
        return error_log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def clear_error_log() -> None:
    """Очищает текущий журнал и предыдущую ротацию по явному действию пользователя."""
    path = error_log_path()
    try:
        path.unlink(missing_ok=True)
        path.with_suffix(".previous.log").unlink(missing_ok=True)
    except OSError:
        return
