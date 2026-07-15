## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Общий WebSocket-клиент ShotSync с автоматическим переподключением."""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

_RECONNECT_START_MS = 1000
_RECONNECT_MAX_MS = 30000
_HEARTBEAT_MS = 25000


class ShotSyncSocket(QObject):
    """Поддерживает одно живое WebSocket-соединение с ShotSync.

    Сам переподключается с растущей задержкой, отправляет heartbeat и разбирает
    входящие события. Остальному приложению оставляет сигналы, а сетевую кухню
    прячет здесь — ей совершенно незачем расползаться по интерфейсу.
    """

    connectionChanged = Signal(bool)  # соединение с сервером установлено
    photoAdded = Signal(int, dict)  # съёмка и данные новой фотографии
    photoUpdated = Signal(int, dict)  # съёмка и обновлённые данные фотографии
    shootingUpdated = Signal(dict)  # данные изменившейся съёмки
    shootingDeleted = Signal(int)  # идентификатор удалённой съёмки
    ackReceived = Signal(dict)  # содержимое подтверждения photo.ack

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

    @property
    def connected(self) -> bool:
        return self._connected

    def set_api_key(self, key: str | None) -> None:
        """Обновляет ключ и переподключается, если активные данные изменились."""
        new_key = (key or "").strip()
        if new_key == self._api_key:
            return
        self._api_key = new_key
        if self._want_running:
            self._reopen()

    def start(self) -> None:
        """Запускает и поддерживает соединение, пока задан ключ API."""
        self._want_running = True
        if not self._api_key:
            return
        if not self._connected:
            self._open()

    def stop(self) -> None:
        """Закрывает сокет и запрещает автоматическое переподключение."""
        self._want_running = False
        self._reconnect_timer.stop()
        self._heartbeat.stop()
        self._socket.close()

    def send_json(self, payload: dict) -> bool:
        """Отправляет JSON и возвращает ``False``, если соединения сейчас нет."""
        if not self._connected:
            return False
        self._socket.sendTextMessage(json.dumps(payload))
        return True

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

    def _on_text_message(self, message: str) -> None:
        """Разбирает входящее событие и направляет его в подходящий сигнал Qt."""
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
