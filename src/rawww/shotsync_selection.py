"""ShotSync "take a shooting for selection" feature (feature 2).

Two pieces cooperate here:

* :class:`SelectionDownloader` pulls the shooting's photo list from the server
  and downloads the 1920px previews into a local folder under the app data
  directory, seeding the folder cache with the server photo ids and the marks
  that already exist.  This turns a cloud shooting into an ordinary local
  folder the rest of the app can open as a tab.

* :class:`SelectionMarkSyncer` watches marks the user makes inside such a
  folder and ships them back to the server over the shared socket.  Every mark
  is written to a durable on-disk queue first, so nothing is lost while the
  connection is down; the queue is drained (and cleared on ``photo.ack``) as
  soon as the socket is live again.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .shotsync_receiver import safe_filename

if TYPE_CHECKING:  # avoid importing the GUI/QtGui stack at module load
    from .cache import FolderCache

API_KEY_HEADER = b"X-Api-Key"
SELECTION_DIR = "shotsync-selections"
MAX_INFLIGHT_DOWNLOADS = 3
RETRY_MAX_MS = 30_000


def selection_root() -> Path:
    """Directory that holds locally downloaded selection folders."""
    from .cache import cache_root

    return cache_root().parent.parent / SELECTION_DIR


def selection_folder(shooting_id: int, title: str) -> Path:
    """Stable local folder for a given shooting."""
    safe = "".join(c if c not in '<>:"/\\|?*' else "_" for c in str(title or "").strip())
    safe = safe.rstrip(". ").strip()
    suffix = f"-{safe}" if safe else ""
    return selection_root() / f"{shooting_id}{suffix}"


class SelectionDownloader(QObject):
    """Downloads every preview of a shooting into a local folder."""

    progress = Signal(int, int, int)          # shooting_id, done, total
    finished = Signal(int, str)               # shooting_id, folder
    failed = Signal(int, str)                 # shooting_id, human message

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._manager = QNetworkAccessManager(self)
        # Per-run state keyed by shooting_id so concurrent downloads are safe.
        self._runs: dict[int, dict] = {}

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    def is_running(self, shooting_id: int) -> bool:
        return int(shooting_id) in self._runs

    # ----- list fetch ----------------------------------------------------
    def start(self, shooting_id: int, title: str) -> None:
        shooting_id = int(shooting_id)
        if shooting_id in self._runs:
            return
        folder = selection_folder(shooting_id, title)
        folder.mkdir(parents=True, exist_ok=True)
        self._runs[shooting_id] = {
            "folder": folder,
            "title": title,
            "total": 0,
            "done": 0,
            "mapping": [],   # (name, photo_id, shooting_id)
            "selection": [], # (name, rating, color_label, comment, original_datetime)
            "queue": [],
            "inflight": 0,
            "retrying": 0,
        }
        request = self._request(
            f"{self._base_url}/api/shootings/{shooting_id}/downloads/photos/"
        )
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._on_list(reply, shooting_id))

    def _on_list(self, reply: QNetworkReply, shooting_id: int) -> None:
        reply.deleteLater()
        run = self._runs.get(shooting_id)
        if run is None:
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._fail(shooting_id, reply.errorString())
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except (ValueError, TypeError):
            self._fail(shooting_id, "Некорректный ответ сервера.")
            return
        if not data.get("ok"):
            self._fail(shooting_id, str(data.get("error") or "Ошибка сервера."))
            return
        photos = [p for p in data.get("photos", []) if isinstance(p, dict)]
        run["total"] = len(photos)
        self.progress.emit(shooting_id, 0, len(photos))
        if not photos:
            self._finalize(shooting_id)
            return
        run["queue"] = [(photo, 0) for photo in photos]
        self._pump(shooting_id)

    # ----- per-photo download --------------------------------------------
    def _pump(self, shooting_id: int) -> None:
        run = self._runs.get(shooting_id)
        if run is None:
            return
        while run["queue"] and run["inflight"] < MAX_INFLIGHT_DOWNLOADS:
            photo, attempt = run["queue"].pop(0)
            self._download_photo(shooting_id, photo, attempt)
        if not run["queue"] and not run["inflight"] and not run["retrying"]:
            self._finalize(shooting_id)

    def _download_photo(self, shooting_id: int, photo: dict, attempt: int = 0) -> None:
        run = self._runs[shooting_id]
        # Prefer the 1920px preview; fall back to original then mini.
        url = photo.get("thumb_url") or photo.get("url") or photo.get("mini_url")
        photo_id = int(photo.get("id") or 0)
        name = _unique_local_name(
            safe_filename(photo.get("name") or f"photo-{photo_id}.jpg"),
            {entry[0] for entry in run["mapping"]},
        )
        if not url or not photo_id:
            self._advance(shooting_id)
            return
        run["mapping"].append((name, photo_id, shooting_id))
        run["selection"].append((
            name,
            photo.get("rating"),
            photo.get("color_label") or "",
            photo.get("comment") or "",
            photo.get("original_datetime") or None,
        ))
        destination = run["folder"] / name
        if destination.is_file() and destination.stat().st_size > 0:
            self._advance(shooting_id)
            return
        run["inflight"] += 1
        reply = self._manager.get(self._request(self._absolute(url)))
        reply.finished.connect(
            lambda: self._on_photo(reply, shooting_id, photo, destination, attempt)
        )

    def _on_photo(self, reply: QNetworkReply, shooting_id: int, photo: dict, destination: Path, attempt: int) -> None:
        reply.deleteLater()
        run = self._runs.get(shooting_id)
        if run is None:
            return
        run["inflight"] -= 1
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
            if data:
                temp = destination.with_suffix(destination.suffix + ".part")
                try:
                    temp.write_bytes(data)
                    temp.replace(destination)
                except OSError:
                    temp.unlink(missing_ok=True)
                self._advance(shooting_id)
                self._pump(shooting_id)
                return
        run["retrying"] += 1
        delay = min(1000 * (2 ** min(attempt, 5)), RETRY_MAX_MS)
        QTimer.singleShot(delay, lambda: self._retry_photo(shooting_id, photo, attempt + 1))
        self._pump(shooting_id)

    def _retry_photo(self, shooting_id: int, photo: dict, attempt: int) -> None:
        run = self._runs.get(shooting_id)
        if run is None:
            return
        run["retrying"] -= 1
        run["queue"].append((photo, attempt))
        self._pump(shooting_id)

    def _advance(self, shooting_id: int) -> None:
        run = self._runs.get(shooting_id)
        if run is None:
            return
        run["done"] += 1
        self.progress.emit(shooting_id, run["done"], run["total"])
        if run["done"] >= run["total"]:
            self._finalize(shooting_id)

    def _finalize(self, shooting_id: int) -> None:
        run = self._runs.pop(shooting_id, None)
        if run is None:
            return
        folder = run["folder"]
        names = {name for name, *_ in run["mapping"]}
        try:
            from .cache import FolderCache

            cache = FolderCache(folder, live_names=names, load_from_disk=True)
            stale_names = set(cache.shotsync_photo_names()) - names
            for name in stale_names:
                (folder / name).unlink(missing_ok=True)
            cache.set_shotsync_session(shooting_id, run["title"])
            cache.replace_shotsync_photos(run["mapping"])
            for name, rating, color, comment, _original_datetime in run["selection"]:
                cache.store_photo_selection(
                    name, rating=rating, color_label=color, comment=comment
                )
            metadata = [
                (str(folder / name), json.dumps({"original_datetime": original_datetime}))
                for name, _rating, _color, _comment, original_datetime in run["selection"]
                if original_datetime
            ]
            if metadata:
                cache.store_photo_metadata(metadata)
            cache.close(flush=True)
        except Exception as exc:  # noqa: BLE001 - surface a readable message
            self.failed.emit(shooting_id, f"Не удалось сохранить кэш: {exc}")
            return
        self.finished.emit(shooting_id, str(folder))

    def _fail(self, shooting_id: int, message: str) -> None:
        self._runs.pop(shooting_id, None)
        self.failed.emit(shooting_id, message)

    # ----- helpers -------------------------------------------------------
    def _request(self, url: str) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        if self._api_key and url.startswith(self._base_url):
            request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        return request

    def _absolute(self, url: str) -> str:
        return f"{self._base_url}{url}" if url.startswith("/") else url


def _unique_local_name(name: str, used_names: set[str]) -> str:
    """Avoid overwriting two server photos that share a filename."""
    if name not in used_names:
        return name
    path = Path(name)
    index = 2
    while True:
        candidate = f"{path.stem} ({index}){path.suffix}"
        if candidate not in used_names:
            return candidate
        index += 1


class SelectionMarkSyncer(QObject):
    """Ships local marks for one selection folder back to the server."""

    pendingChanged = Signal(int)              # number of queued (unsent) marks

    def __init__(self, hub, cache: FolderCache, shooting_id: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hub = hub
        self._cache = cache
        self._shooting_id = int(shooting_id)
        self._inflight: dict[str, tuple[int, str]] = {}   # request_id -> (photo_id, kind)
        hub.ackReceived.connect(self._on_ack)
        hub.connectionChanged.connect(self._on_connection)
        self.flush()

    def detach(self) -> None:
        """Stop listening; called when the folder/tab goes away."""
        try:
            self._hub.ackReceived.disconnect(self._on_ack)
            self._hub.connectionChanged.disconnect(self._on_connection)
        except (RuntimeError, TypeError):
            pass

    def pending_count(self) -> int:
        return self._cache.pending_shotsync_count()

    def queue_mark(self, name: str, *, detail: dict, changes: dict) -> None:
        """Persist and try to send marks implied by ``changes`` for one file."""
        photo_id = self._cache.shotsync_photo_id(name)
        if not photo_id:
            return
        if "rating" in changes:
            self._cache.enqueue_shotsync_mark(
                photo_id=photo_id,
                shooting_id=self._shooting_id,
                kind="rating",
                payload_json=json.dumps({"rating": detail.get("rating")}),
            )
        if "color_label" in changes or "comment" in changes:
            self._cache.enqueue_shotsync_mark(
                photo_id=photo_id,
                shooting_id=self._shooting_id,
                kind="meta",
                payload_json=json.dumps(
                    {
                        "color_label": detail.get("color_label", ""),
                        "comment": detail.get("comment", ""),
                    }
                ),
            )
        self.pendingChanged.emit(self.pending_count())
        self.flush()

    def flush(self) -> None:
        """Send every queued mark that is not already in flight."""
        if not self._hub.connected:
            return
        for mark in self._cache.pending_shotsync_marks():
            photo_id, kind = mark["photo_id"], mark["kind"]
            if (photo_id, kind) in self._inflight.values():
                continue
            try:
                payload = json.loads(mark["payload_json"])
            except (ValueError, TypeError):
                self._cache.clear_shotsync_mark(photo_id, kind)
                continue
            request_id = uuid.uuid4().hex
            message = {
                "type": "photo.rate" if kind == "rating" else "photo.meta",
                "shooting_id": mark["shooting_id"],
                "photo_ids": [photo_id],
                "request_id": request_id,
                **payload,
            }
            if self._hub.send_json(message):
                self._inflight[request_id] = (photo_id, kind)

    def _on_ack(self, data: dict) -> None:
        request_id = data.get("request_id")
        target = self._inflight.pop(request_id, None) if request_id else None
        if target is None:
            return
        photo_id, kind = target
        if data.get("ok"):
            self._cache.clear_shotsync_mark(photo_id, kind)
            self.pendingChanged.emit(self.pending_count())
        # On failure the mark stays queued and will be retried on next flush.

    def _on_connection(self, connected: bool) -> None:
        if connected:
            self._inflight.clear()
            self.flush()
