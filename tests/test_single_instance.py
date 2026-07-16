## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from rawww.single_instance import SingleInstance


class _Signal:
    """Минимальный сигнал для изолированной проверки алгоритма локального сокета."""

    def connect(self, _callback) -> None:
        pass


class _Server:
    """Запоминает обращения к серверу без создания настоящего межпроцессного сокета."""

    outcomes: list[bool] = []
    removed = 0

    def __init__(self, _parent) -> None:
        self.newConnection = _Signal()

    def listen(self, _name: str) -> bool:
        return self.outcomes.pop(0)

    @classmethod
    def removeServer(cls, _name: str) -> None:  # noqa: N802
        cls.removed += 1


class _Socket:
    """Эмулирует успешное или неуспешное подключение ко владельцу сокета."""

    connected = False
    written: list[bytes] = []

    def connectToServer(self, _name: str) -> None:  # noqa: N802
        pass

    def waitForConnected(self, _timeout: int) -> bool:  # noqa: N802
        return self.connected

    def write(self, payload: bytes) -> int:
        self.written.append(payload)
        return len(payload)

    def waitForBytesWritten(self, _timeout: int) -> bool:  # noqa: N802
        return True

    def disconnectFromServer(self) -> None:  # noqa: N802
        pass


class _Lock:
    """Эмулирует межпроцессный замок без записи файлов во временный каталог."""

    acquired = False
    unlocked = 0

    def __init__(self, _path: str) -> None:
        pass

    def tryLock(self, _timeout: int) -> bool:  # noqa: N802
        return self.acquired

    def unlock(self) -> None:
        self.unlocked += 1


class _IncomingSocket:
    """Отдаёт порцию байтов так, как это делает сокет уже запущенного экземпляра."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def readAll(self) -> bytes:  # noqa: N802
        payload, self.payload = self.payload, b""
        return payload


class SingleInstanceTests(unittest.TestCase):
    """Проверяет, что живой экземпляр не теряет имя сокета при повторном запуске."""

    def setUp(self) -> None:
        _Server.removed = 0
        _Socket.written = []
        _Lock.unlocked = 0

    @patch("rawww.single_instance.QLockFile", _Lock)
    @patch("rawww.single_instance.QLocalSocket", _Socket)
    @patch("rawww.single_instance.QLocalServer", _Server)
    def test_second_instance_sends_request_without_removing_live_server(self) -> None:
        _Lock.acquired = False
        _Server.outcomes = []
        _Socket.connected = True

        instance = SingleInstance()

        target = Path("C:/photos")
        self.assertTrue(instance.start(target))
        self.assertEqual(_Server.removed, 0)
        self.assertEqual(_Socket.written, [(str(target) + "\n").encode("utf-8")])

    @patch("rawww.single_instance.QLockFile", _Lock)
    @patch("rawww.single_instance.QLocalSocket", _Socket)
    @patch("rawww.single_instance.QLocalServer", _Server)
    def test_stale_server_name_is_removed_only_after_connection_failure(self) -> None:
        _Lock.acquired = True
        _Server.outcomes = [False, True]
        _Socket.connected = False

        instance = SingleInstance()

        self.assertFalse(instance.start(None))
        self.assertEqual(_Server.removed, 1)

    @patch("rawww.single_instance.QLockFile", _Lock)
    @patch("rawww.single_instance.QLocalServer", _Server)
    def test_running_instance_emits_target_received_from_second_process(self) -> None:
        """Путь второго процесса доходит до обработчика уже запущенного окна."""
        instance = SingleInstance()
        received: list[Path | None] = []
        instance.target_received.connect(received.append)
        socket = _IncomingSocket(b"C:/photos/from-explorer\n")
        instance._buffers[socket] = bytearray()

        instance._read(socket)

        self.assertEqual(received, [Path("C:/photos/from-explorer")])
