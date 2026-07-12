"""Feature 3: send a local folder to ShotSync and pull marks back.

Two independent helpers, both driven by ``QNetworkAccessManager`` so they share
the Qt event loop and need no extra dependency:

* :class:`FolderUploader` — creates a shooting, encodes every local image to a
  1920px JPEG preview (in a small thread pool) and uploads them with bounded
  concurrency, reporting progress. On success the local folder is tagged as a
  ShotSync session so the mark-syncer (feature 2) takes over.
* :class:`MarksFetcher` — implements the "Получить" action: pulls current marks
  for a shooting and writes them into the folder cache.

Both keep the whole flow off the server's originals: the client generates the
previews itself via the same imaging path used for thumbnails.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal, QThreadPool, QRunnable
from PySide6.QtNetwork import (
    QHttpMultiPart,
    QHttpPart,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

if TYPE_CHECKING:  # avoid importing the GUI/QtGui stack at module load
    from .cache import FolderCache

API_KEY_HEADER = b"X-Api-Key"
PREVIEW_MAX_SIZE = 1920
PREVIEW_QUALITY = 85
MAX_INFLIGHT_UPLOADS = 3


def encode_preview(path: Path, max_size: int = PREVIEW_MAX_SIZE) -> bytes:
    """Encode ``path`` to a downscaled sRGB JPEG and return the bytes.

    Runs in a worker thread. Reuses the app's own imaging pipeline
    (:func:`rawww.imaging.decode_pixels`) instead of calling Pillow's
    ``Image.open`` directly — that path handles camera RAW (CR3/NEF/ARW/…) via
    ``rawpy``/the embedded preview, applies EXIF orientation and sRGB
    conversion, and matches exactly what the user sees as a thumbnail. Pillow
    alone cannot decode RAW and raised "cannot identify image file".
    """
    from PIL import Image  # local import keeps startup cheap

    from .imaging import decode_pixels

    pixel = decode_pixels(path, max_size)
    image = Image.frombytes(
        "RGBA", (pixel.width, pixel.height), pixel.pixels, "raw", "RGBA"
    ).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    return buffer.getvalue()


class _EncodeSignals(QObject):
    done = Signal(object, bytes)     # path, jpeg bytes
    failed = Signal(object, str)     # path, error


class _EncodeTask(QRunnable):
    """Encode one preview off the UI thread."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.signals = _EncodeSignals()

    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        try:
            data = encode_preview(self.path)
        except Exception as exc:  # noqa: BLE001 - report a readable message
            self.signals.failed.emit(self.path, str(exc))
            return
        self.signals.done.emit(self.path, data)


class FolderUploader(QObject):
    """Create a shooting and upload 1920px previews of every image in a folder."""

    progress = Signal(int, int)          # done, total
    finished = Signal(int, str)          # shooting_id, folder
    failed = Signal(str)                 # error message

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._manager = QNetworkAccessManager(self)
        self._pool = QThreadPool(self)
        # Encoding is CPU-bound; a couple of workers keeps the UI responsive
        # without starving the rest of the app.
        self._pool.setMaxThreadCount(max(2, (QThreadPool.globalInstance().maxThreadCount() or 4) // 2))
        self._reset()

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    @property
    def busy(self) -> bool:
        return self._folder is not None

    def _reset(self) -> None:
        self._folder: Path | None = None
        self._title = ""
        self._shooting_id = 0
        self._pending: list[Path] = []
        self._encoded: list[tuple[Path, bytes]] = []
        self._inflight = 0
        self._done = 0
        self._total = 0
        self._failed = False

    # ----- public API ----------------------------------------------------
    def start(self, folder: Path, title: str) -> None:
        """Begin uploading ``folder`` as a new shooting named ``title``."""
        if self.busy:
            self.failed.emit("Отправка уже выполняется.")
            return
        if not self._api_key:
            self.failed.emit("Нет авторизации ShotSync.")
            return
        from .imaging import is_supported_image, is_supported_video

        # Only still images are uploaded; videos are skipped entirely.
        images = sorted(
            p
            for p in folder.iterdir()
            if p.is_file() and is_supported_image(p) and not is_supported_video(p)
        )
        if not images:
            self.failed.emit("В папке нет поддерживаемых изображений.")
            return
        self._reset()
        self._folder = folder
        self._title = title
        self._pending = images
        self._total = len(images)
        self._create_shooting()

    # ----- shooting creation --------------------------------------------
    def _create_shooting(self) -> None:
        request = QNetworkRequest(QUrl(f"{self._base_url}/api/shootings/create/"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        body = QByteArray(json.dumps({"title": self._title}).encode("utf-8"))
        reply = self._manager.post(request, body)
        reply.finished.connect(lambda: self._on_shooting_created(reply))

    def _on_shooting_created(self, reply: QNetworkReply) -> None:
        payload = _read_json(reply)
        reply.deleteLater()
        shooting = (payload or {}).get("shooting") if payload else None
        shooting_id = int((shooting or {}).get("id") or 0)
        if not shooting_id:
            self._abort("Не удалось создать съёмку на сервере.")
            return
        self._shooting_id = shooting_id
        # Kick off encoding for every image; uploads start as bytes arrive.
        for path in self._pending:
            task = _EncodeTask(path)
            task.signals.done.connect(self._on_encoded)
            task.signals.failed.connect(self._on_encode_failed)
            self._pool.start(task)

    # ----- encode -> upload pipeline ------------------------------------
    def _on_encoded(self, path: Path, data: bytes) -> None:
        if self._folder is None or self._failed:
            return
        self._encoded.append((path, data))
        self._pump()

    def _on_encode_failed(self, path: Path, error: str) -> None:
        if self._folder is None or self._failed:
            return
        self._abort(f"Не удалось подготовить «{path.name}»: {error}")

    def _pump(self) -> None:
        while self._encoded and self._inflight < MAX_INFLIGHT_UPLOADS:
            path, data = self._encoded.pop(0)
            self._upload_one(path, data)

    def _upload_one(self, path: Path, data: bytes) -> None:
        multipart = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
        part = QHttpPart()
        part.setHeader(
            QNetworkRequest.KnownHeaders.ContentDispositionHeader,
            f'form-data; name="file"; filename="{path.stem}.jpg"',
        )
        part.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "image/jpeg")
        part.setBody(QByteArray(data))
        multipart.append(part)

        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{self._shooting_id}/photos/upload/")
        )
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.post(request, multipart)
        multipart.setParent(reply)  # tie multipart lifetime to the reply
        self._inflight += 1
        reply.finished.connect(lambda: self._on_uploaded(reply, path))

    def _on_uploaded(self, reply: QNetworkReply, path: Path) -> None:
        self._inflight -= 1
        payload = _read_json(reply)
        error = reply.error()
        reply.deleteLater()
        if self._failed:
            return
        if error != QNetworkReply.NetworkError.NoError or not (payload or {}).get("ok"):
            self._abort(f"Не удалось загрузить «{path.name}».")
            return
        self._done += 1
        self.progress.emit(self._done, self._total)
        if self._done >= self._total:
            self._complete()
        else:
            self._pump()

    # ----- completion ----------------------------------------------------
    def _complete(self) -> None:
        folder = self._folder
        shooting_id = self._shooting_id
        title = self._title
        try:
            from .cache import FolderCache

            names = {p.name for p in self._pending}
            cache = FolderCache(folder, live_names=names, load_from_disk=True)
            cache.set_shotsync_session(shooting_id, title)
            cache.close(flush=True)
        except Exception as exc:  # noqa: BLE001
            self._abort(f"Не удалось пометить папку: {exc}")
            return
        self.finished.emit(shooting_id, str(folder))
        self._reset()

    def _abort(self, message: str) -> None:
        if self._failed:
            return
        self._failed = True
        self.failed.emit(message)
        self._reset()


class MarksFetcher(QObject):
    """Pull current marks for a shooting and write them into a folder cache."""

    finished = Signal(int)               # number of marks applied
    failed = Signal(str)

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._manager = QNetworkAccessManager(self)

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    def fetch(self, shooting_id: int, cache: "FolderCache") -> None:
        if not self._api_key:
            self.failed.emit("Нет авторизации ShotSync.")
            return
        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{shooting_id}/marks/")
        )
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._on_marks(reply, cache))

    def _on_marks(self, reply: QNetworkReply, cache: "FolderCache") -> None:
        payload = _read_json(reply)
        reply.deleteLater()
        if not payload or not payload.get("ok"):
            self.failed.emit("Не удалось получить метки.")
            return
        applied = self._apply_marks(payload, cache)
        self.finished.emit(applied)

    def _apply_marks(self, payload: dict, cache: "FolderCache") -> int:
        """Write each returned mark into ``cache``; return how many applied."""
        applied = 0
        for mark in payload.get("marks", []):
            name = str(mark.get("name") or "").strip()
            if not name:
                continue
            cache.store_photo_selection(
                name,
                rating=mark.get("rating"),
                color_label=mark.get("color_label") or "",
                comment=mark.get("comment") or "",
            )
            applied += 1
        return applied


def _read_json(reply: QNetworkReply) -> dict | None:
    try:
        raw = bytes(reply.readAll()).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None
