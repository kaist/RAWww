## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Передаёт запросы из файлового менеджера уже запущенному экземпляру приложения."""

from __future__ import annotations

from pathlib import Path
from tempfile import gettempdir

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .windows_activation import grant_foreground_activation


SERVER_NAME = "rawww-single-instance-v1"
LOCK_PATH = Path(gettempdir()) / "rawww-single-instance.lock"


class SingleInstance(QObject):
    """Оставляет один GUI-процесс и передаёт ему запросы повторных запусков.

    Первый экземпляр слушает локальный сокет, последующие отправляют ему путь и
    завершаются. Так двойной щелчок по файлу открывает новую вкладку, а не вторую
    Контрольку со своим кэшем и собственным мнением о текущей папке.
    """

    target_received = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connection)
        self.lock = QLockFile(str(LOCK_PATH))
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def start(self, target: Path | None) -> bool:
        """Возвращает ``True`` во вторичном процессе после передачи запроса."""
        if not self.lock.tryLock(100):
            # Владелец блокировки ещё запускается либо уже слушает сокет. В
            # обоих случаях второму процессу нельзя становиться новым окном.
            self._send_to_running_instance(target)
            return True

        if self.server.listen(SERVER_NAME):
            return False

        # Замок уже принадлежит этому процессу. Значит, успешно подключившийся
        # сервер — это экземпляр, запущенный старой версией программы; ему
        # передаётся запрос, а текущий процесс освобождает замок и завершается.
        if self._send_to_running_instance(target):
            self.lock.unlock()
            return True

        # Без владельца замка можно безопасно очистить только устаревшее имя
        # сокета, оставшееся после аварийного завершения прошлого процесса.
        QLocalServer.removeServer(SERVER_NAME)
        if self.server.listen(SERVER_NAME):
            return False

        # Неожиданная гонка или ошибка ОС: безопаснее выйти, чем открыть второе
        # окно. При следующем запуске QLockFile сам распознает stale lock.
        self.lock.unlock()
        return True

    @staticmethod
    def _send_to_running_instance(target: Path | None) -> bool:
        """Передаёт путь владельцу сокета и сообщает, удалось ли подключиться."""
        socket = QLocalSocket()
        socket.connectToServer(SERVER_NAME)
        if not socket.waitForConnected(500):
            return False
        # Пока этот процесс ещё считается запущенным пользователем из
        # Проводника, он может передать первой копии право получить фокус.
        grant_foreground_activation()
        payload = str(target) if target is not None else ""
        socket.write((payload + "\n").encode("utf-8"))
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True

    def _accept_connection(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._forget(socket))

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        while b"\n" in buffer:
            raw, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            value = raw.decode("utf-8", errors="replace")
            self.target_received.emit(Path(value) if value else None)

    def _forget(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()
