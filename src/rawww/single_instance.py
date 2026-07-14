"""Single-instance handoff for file-manager activation requests."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


SERVER_NAME = "rawww-single-instance-v1"


class SingleInstance(QObject):
    """Keep one GUI process and forward later launches to it."""

    target_received = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connection)
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def start(self, target: Path | None) -> bool:
        """Return True in a secondary process after forwarding its request."""
        if self.server.listen(SERVER_NAME):
            return False

        # A crashed process can leave a stale Unix-domain socket behind.
        # On Windows this is normally a no-op, and an active server is never
        # removed because the connect attempt above succeeds in that case.
        QLocalServer.removeServer(SERVER_NAME)
        if self.server.listen(SERVER_NAME):
            return False

        socket = QLocalSocket()
        socket.connectToServer(SERVER_NAME)
        if not socket.waitForConnected(500):
            return False
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
