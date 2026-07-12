"""Application-wide ShotSync coordinator shared by every tab.

There is exactly **one** hub per process (see :func:`shotsync_hub`).  It owns the
single live WebSocket and the download receiver, and it persists which shootings
are being received (and to which folder) between launches.

Individual :class:`~rawww.app.Workspace` tabs connect to the hub's signals to
refresh themselves when a file arrives or a mark changes, and drive it through
:meth:`start_receiving` / :meth:`stop_receiving`.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from .shotsync_receiver import ShotSyncReceiver
from .shotsync_selection import SelectionDownloader
from .shotsync_socket import ShotSyncSocket

_RECEIVERS_SETTING = "shotsync/receivers"


class ShotSyncHub(QObject):
    """Owns the shared socket + receiver and the receive-folder mapping."""

    connectionChanged = Signal(bool)
    receivingChanged = Signal()                # the set of received shootings changed
    photoDownloaded = Signal(int, str, str)    # shooting_id, folder, filename
    markUpdated = Signal(int, str, dict)       # shooting_id, folder, photo payload
    ackReceived = Signal(dict)                 # server reply to a mark we sent

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings("RAWww", "RAWww")
        self.socket = ShotSyncSocket(base_url, self)
        self.receiver = ShotSyncReceiver(base_url, self)
        self.downloader = SelectionDownloader(base_url, self)

        self.socket.photoAdded.connect(self.receiver.on_photo_added)
        self.socket.photoUpdated.connect(self.receiver.on_photo_updated)
        self.socket.connectionChanged.connect(self.connectionChanged)
        self.socket.ackReceived.connect(self.ackReceived)
        self.receiver.photoDownloaded.connect(self.photoDownloaded)
        self.receiver.markUpdated.connect(self.markUpdated)

    # ----- credential ----------------------------------------------------
    def set_api_key(self, key: str | None) -> None:
        self.socket.set_api_key(key)
        self.receiver.set_api_key(key)
        self.downloader.set_api_key(key)
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

    # ----- receive management --------------------------------------------
    def start_receiving(self, shooting_id: int, folder: Path, name: str) -> None:
        self.receiver.start_receiving(shooting_id, folder, name)
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

    # ----- persistence ---------------------------------------------------
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
    """Return the process-wide hub, creating it on first use."""
    global _HUB
    if _HUB is None:
        _HUB = ShotSyncHub(base_url)
    return _HUB
