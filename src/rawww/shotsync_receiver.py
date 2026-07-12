"""Live "receive photos" feature for ShotSync (feature 1).

When the user chooses to *receive* a shooting, every ``photo.added`` event that
arrives on the shared :class:`~rawww.shotsync_socket.ShotSyncSocket` triggers a
background download of the **original** file into a chosen local folder.  Marks
(rating/color/comment) that arrive later as ``photo.updated`` are surfaced so
the app can mirror them into the local per-folder selection cache.

The receiver only knows about downloading and path bookkeeping; persisting the
folder-per-shooting mapping and refreshing the UI is the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API_KEY_HEADER = b"X-Api-Key"


def safe_filename(name: str, fallback: str = "photo.jpg") -> str:
    """Return just the basename, stripped of any path components."""
    cleaned = Path(str(name or "").replace("\\", "/")).name.strip()
    return cleaned or fallback


@dataclass
class ReceiveTarget:
    folder: Path
    name: str


class ShotSyncReceiver(QObject):
    """Downloads incoming originals for shootings the user is receiving."""

    photoDownloaded = Signal(int, str, str)   # shooting_id, folder, filename
    downloadFailed = Signal(int, str)         # shooting_id, human message
    markUpdated = Signal(int, str, dict)      # shooting_id, folder, photo payload

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._targets: dict[int, ReceiveTarget] = {}
        self._manager = QNetworkAccessManager(self)
        # Keep a reference to in-flight replies so they are not GC'd mid-flight.
        self._active: set[QNetworkReply] = set()

    # ----- configuration -------------------------------------------------
    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    def start_receiving(self, shooting_id: int, folder: Path, name: str) -> None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self._targets[int(shooting_id)] = ReceiveTarget(folder=folder, name=name)

    def stop_receiving(self, shooting_id: int) -> None:
        self._targets.pop(int(shooting_id), None)

    def is_receiving(self, shooting_id: int) -> bool:
        return int(shooting_id) in self._targets

    def receiving_ids(self) -> set[int]:
        return set(self._targets)

    def folder_for(self, shooting_id: int) -> Path | None:
        target = self._targets.get(int(shooting_id))
        return target.folder if target else None

    # ----- socket event handlers -----------------------------------------
    def on_photo_added(self, shooting_id: int, photo: dict) -> None:
        target = self._targets.get(int(shooting_id))
        if target is None:
            return
        url = photo.get("url") or photo.get("thumb_url")
        if not url:
            return
        filename = safe_filename(photo.get("name") or f"photo-{photo.get('id')}.jpg")
        destination = target.folder / filename
        # Skip files we already have (server may resend on reconnect).
        if destination.exists():
            return
        self._download(int(shooting_id), self._absolute_url(url), destination)

    def on_photo_updated(self, shooting_id: int, photo: dict) -> None:
        target = self._targets.get(int(shooting_id))
        if target is None:
            return
        self.markUpdated.emit(int(shooting_id), str(target.folder), photo)

    # ----- download plumbing ---------------------------------------------
    def _absolute_url(self, url: str) -> str:
        if url.startswith("/"):
            return f"{self._base_url}{url}"
        return url

    def _download(self, shooting_id: int, url: str, destination: Path) -> None:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        # Same-origin media may sit behind the API key; harmless elsewhere.
        if self._api_key and url.startswith(self._base_url):
            request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.get(request)
        self._active.add(reply)
        reply.finished.connect(lambda: self._finish_download(reply, shooting_id, destination))

    def _finish_download(self, reply: QNetworkReply, shooting_id: int, destination: Path) -> None:
        self._active.discard(reply)
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.downloadFailed.emit(shooting_id, reply.errorString())
            return
        data = bytes(reply.readAll())
        if not data:
            self.downloadFailed.emit(shooting_id, "Пустой файл.")
            return
        # Write atomically so a half-downloaded file is never seen by a scan.
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            temp_path.write_bytes(data)
            temp_path.replace(destination)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            self.downloadFailed.emit(shooting_id, str(exc))
            return
        self.photoDownloaded.emit(shooting_id, str(destination.parent), destination.name)
