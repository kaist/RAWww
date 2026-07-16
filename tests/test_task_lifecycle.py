## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from rawww.task_lifecycle import retire_executor, wait_for_retired_executors


class _Executor:
    def __init__(self) -> None:
        self.calls = []

    def shutdown(self, **kwargs) -> None:
        self.calls.append(kwargs)


class TaskLifecycleTests(unittest.TestCase):
    """Проверяет двухфазное завершение очередей закрытых папок и вкладок."""

    def test_retired_executor_is_waited_during_final_phase(self) -> None:
        executor = _Executor()
        retire_executor(executor, cancel_futures=True)
        wait_for_retired_executors()

        self.assertEqual(
            executor.calls,
            [
                {"wait": False, "cancel_futures": True},
                {"wait": True, "cancel_futures": False},
            ],
        )
