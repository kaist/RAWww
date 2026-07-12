"""Single shared WebSocket connection to ShotSync (``ws/app``).

The whole application keeps **one** live socket, no matter how many tabs or
shootings are being observed at once.  The server pushes every event for the
signed-in user through ``wss://shotsync.ru/ws/app/?api_key=…``:

* ``connection.ready``           - sent once right after the socket opens.
* ``photo.added``   ``{shooting_id, photo}``  - a new photo finished processing.
* ``photo.updated`` ``{shooting_id, photo}``  - rating/color/comment changed.
* ``shooting.updated`` ``{shooting}``          - shooting metadata changed.
* ``shooting.deleted`` ``{shooting_id}``       - shooting was removed.
* ``photo.ack`` ``{ok, request_id, …}``        - reply to a mark we sent.
* ``pong``                                     - reply to our heartbeat ping.

The same socket is used to *send* owner marks (features 2 and 3) via
:meth:`send_json`; callers that need durability keep their own on-disk queue
and re-send through here once :attr:`connected` turns back on.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

# Reconnect backoff (milliseconds): quick first retry, capped so a long outage
# does not hammer the server.
_RECONNECT_START_MS = 1000
_RECONNECT_MAX_MS = 30000
_HEARTBEAT_MS = 25000


class ShotSyncSocket(QObject):
    """Auto-reconnecting wrapper around a single :class:`QWebSocket`."""

    connectionChanged = Signal(bool)          # True when the socket is live
    photoAdded = Signal(int, dict)            # shooting_id, photo payload
    photoUpdated = Signal(int, dict)          # shooting_id, photo payload
    shootingUpdated = Signal(dict)            # shooting payload
    shootingDeleted = Signal(int)              # shooting_id
    ackReceived = Signal(dict)                # photo.ack payload

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key: str = ""
        self._want_running = False
        self._connected = False
        self._reconnect_ms = _RECONNECT_START_MS

        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.textMessageReceived.connect(self._on_text_message)
        self._socket.errorOccurred.connect(lambda _err: self._schedule_reconnect())

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._open)

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._send_ping)

    # ----- public state --------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def set_api_key(self, key: str | None) -> None:
        """Update the credential; reconnect if it changed while running."""
        new_key = (key or "").strip()
        if new_key == self._api_key:
            return
        self._api_key = new_key
        if self._want_running:
            self._reopen()

    def start(self) -> None:
        """Begin (and keep) a live connection while a key is available."""
        self._want_running = True
        if not self._api_key:
            return
        if not self._connected:
            self._open()

    def stop(self) -> None:
        """Tear the socket down and stop reconnecting."""
        self._want_running = False
        self._reconnect_timer.stop()
        self._heartbeat.stop()
        self._socket.close()

    def send_json(self, payload: dict) -> bool:
        """Send a JSON message. Returns ``False`` when the socket is offline."""
        if not self._connected:
            return False
        self._socket.sendTextMessage(json.dumps(payload))
        return True

    # ----- connection lifecycle -----------------------------------------
    def _ws_url(self) -> QUrl:
        scheme = "wss" if self._base_url.startswith("https") else "ws"
        host = self._base_url.split("://", 1)[-1]
        return QUrl(f"{scheme}://{host}/ws/app/?api_key={self._api_key}")

    def _open(self) -> None:
        if not self._want_running or not self._api_key or self._connected:
            return
        self._socket.open(self._ws_url())

    def _reopen(self) -> None:
        self._socket.close()
        self._reconnect_ms = _RECONNECT_START_MS
        self._open()

    def _schedule_reconnect(self) -> None:
        if not self._want_running or not self._api_key:
            return
        if self._reconnect_timer.isActive():
            return
        self._reconnect_timer.start(self._reconnect_ms)
        # Exponential backoff, capped.
        self._reconnect_ms = min(self._reconnect_ms * 2, _RECONNECT_MAX_MS)

    def _on_connected(self) -> None:
        self._connected = True
        self._reconnect_ms = _RECONNECT_START_MS
        self._heartbeat.start()
        self.connectionChanged.emit(True)

    def _on_disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._heartbeat.stop()
        if was_connected:
            self.connectionChanged.emit(False)
        self._schedule_reconnect()

    def _send_ping(self) -> None:
        self.send_json({"type": "ping"})

    # ----- inbound messages ----------------------------------------------
    def _on_text_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        message_type = data.get("type")
        if message_type == "photo.added":
            photo = data.get("photo")
            if isinstance(photo, dict):
                self.photoAdded.emit(int(data.get("shooting_id") or 0), photo)
        elif message_type == "photo.updated":
            photo = data.get("photo")
            if isinstance(photo, dict):
                self.photoUpdated.emit(int(data.get("shooting_id") or 0), photo)
        elif message_type == "shooting.updated":
            shooting = data.get("shooting")
            if isinstance(shooting, dict):
                self.shootingUpdated.emit(shooting)
        elif message_type == "shooting.deleted":
            shooting_id = int(data.get("shooting_id") or 0)
            if shooting_id:
                self.shootingDeleted.emit(shooting_id)
        elif message_type == "photo.ack":
            self.ackReceived.emit(data)
