## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Общий учёт фоновых очередей, которые штатно завершаются после отмены."""

from __future__ import annotations

import threading
from concurrent.futures import Executor


_lock = threading.Lock()
_retired: list[Executor] = []


def retire_executor(executor: Executor, *, cancel_futures: bool = True) -> None:
    """Запрещает новую работу, сохраняя очередь для ожидания при выходе.

    Уже начатая функция заканчивается штатно. Сильная ссылка нужна не для
    самого ``concurrent.futures``, а чтобы финальная фаза приложения могла
    явно дождаться в том числе пулов от прежних папок и закрытых вкладок.
    """
    executor.shutdown(wait=False, cancel_futures=cancel_futures)
    with _lock:
        _retired.append(executor)


def wait_for_retired_executors() -> None:
    """Дожидается всех учтённых очередей после запрета новой работы в UI."""
    while True:
        with _lock:
            if not _retired:
                return
            executors = tuple(_retired)
            _retired.clear()
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=False)
