## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Общий координатор соединения ShotSync для всех вкладок приложения."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from .shotsync_receiver import ShotSyncReceiver
from .shotsync_selection import SelectionDownloader
from .shotsync_socket import ShotSyncSocket
from .shotsync_upload import FolderUploader, MarksFetcher

_RECEIVERS_SETTING = "shotsync/receivers"


class ShotSyncHub(QObject):
    """Координирует весь обмен с ShotSync.

    Хаб владеет единственным сокетом, загрузчиками и привязкой принимаемых
    съёмок к локальным папкам. Вкладки подписываются на его сигналы и не плодят
    собственные соединения — серверу и без этого есть чем заняться.
    """

    connectionChanged = Signal(bool)
    receivingChanged = Signal()  # изменился набор принимаемых съёмок
    photoDownloaded = Signal(int, str, str)  # съёмка, папка и имя нового файла
    markUpdated = Signal(int, str, dict)  # съёмка, папка и новые метки фотографии
    photoUpdated = Signal(int, dict)  # съёмка и обновлённые данные фотографии
    shootingDeleted = Signal(int)  # идентификатор удалённой съёмки
    ackReceived = Signal(dict)  # ответ сервера на отправленную нами метку
    receiveProgress = Signal(int, int, int, int)

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings("Контролька", "Контролька")
        self.socket = ShotSyncSocket(base_url, self)
        self.receiver = ShotSyncReceiver(base_url, self)
        self.downloader = SelectionDownloader(base_url, self)
        self.uploader = FolderUploader(base_url, self)
        self.marks_fetcher = MarksFetcher(base_url, self)

        self.socket.photoAdded.connect(self.receiver.on_photo_added)
        self.socket.photoUpdated.connect(self.receiver.on_photo_updated)
        self.socket.photoUpdated.connect(self.photoUpdated)
        self.socket.shootingDeleted.connect(self._on_shooting_deleted)
        self.socket.connectionChanged.connect(self.connectionChanged)
        self.socket.ackReceived.connect(self.ackReceived)
        self.receiver.photoDownloaded.connect(self.photoDownloaded)
        self.receiver.markUpdated.connect(self.markUpdated)
        self.receiver.syncProgress.connect(self.receiveProgress)

    def set_api_key(self, key: str | None) -> None:
        self.socket.set_api_key(key)
        self.receiver.set_api_key(key)
        self.downloader.set_api_key(key)
        self.uploader.set_api_key(key)
        self.marks_fetcher.set_api_key(key)
        if key:
            self.socket.start()
            self._restore_targets()
        else:
            self.socket.stop()

    @property
    def connected(self) -> bool:
        return self.socket.connected

    def send_json(self, payload: dict) -> bool:
        return self.socket.send_json(payload)

    def start_receiving(self, shooting_id: int, folder: Path, name: str) -> None:
        self.receiver.start_receiving(shooting_id, folder, name)
        self.receiver.sync_existing(shooting_id)
        self._persist_targets()
        self.receivingChanged.emit()

    def stop_receiving(self, shooting_id: int) -> None:
        self.receiver.stop_receiving(shooting_id)
        self._persist_targets()
        self.receivingChanged.emit()

    def is_receiving(self, shooting_id: int) -> bool:
        return self.receiver.is_receiving(shooting_id)

    def receiving_ids(self) -> set[int]:
        return self.receiver.receiving_ids()

    def folder_for(self, shooting_id: int) -> Path | None:
        return self.receiver.folder_for(shooting_id)

    def _on_shooting_deleted(self, shooting_id: int) -> None:
        self.stop_receiving(shooting_id)
        self.shootingDeleted.emit(shooting_id)

    def _restore_targets(self) -> None:
        raw = self._settings.value(_RECEIVERS_SETTING, "", str)
        if not raw:
            return
        try:
            stored = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(stored, dict):
            return
        changed = False
        for shooting_id, config in stored.items():
            if not isinstance(config, dict):
                continue
            folder = Path(str(config.get("folder", "")))
            name = str(config.get("name", ""))
            if folder and folder.is_dir():
                self.receiver.start_receiving(int(shooting_id), folder, name)
                self.receiver.sync_existing(int(shooting_id))
                changed = True
        if changed:
            self.receivingChanged.emit()

    def _persist_targets(self) -> None:
        payload = {
            str(shooting_id): {
                "folder": str(self.receiver.folder_for(shooting_id)),
                "name": self.receiver._targets[shooting_id].name,  # noqa: SLF001
            }
            for shooting_id in self.receiver.receiving_ids()
        }
        self._settings.setValue(_RECEIVERS_SETTING, json.dumps(payload))


_HUB: ShotSyncHub | None = None


def shotsync_hub(base_url: str) -> ShotSyncHub:
    """Возвращает общий хаб процесса, лениво создавая его при первом обращении."""
    global _HUB
    if _HUB is None:
        _HUB = ShotSyncHub(base_url)
    return _HUB
