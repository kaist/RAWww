## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Получение новых фотографий ShotSync в локальную папку."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API_KEY_HEADER = b"X-Api-Key"
_RETRY_MAX_MS = 30_000
MAX_INFLIGHT_DOWNLOADS = 3


def safe_filename(name: str, fallback: str = "photo.jpg") -> str:
    """Оставляет только имя файла и защищает цель от компонентов пути."""
    cleaned = Path(str(name or "").replace("\\", "/")).name.strip()
    return cleaned or fallback


@dataclass
class ReceiveTarget:
    """Локальная папка и название съёмки, которую сейчас принимает ShotSync."""

    folder: Path
    name: str


class ShotSyncReceiver(QObject):
    """Принимает новые оригиналы выбранных съёмок ShotSync в локальные папки.

    Для каждой отслеживаемой съёмки хранится отдельная цель. События WebSocket
    ставят файлы в ограниченную HTTP-очередь, а начальная синхронизация получает
    список уже готовых фотографий, чтобы подключение посреди съёмки ничего не
    потеряло. Повторы и уже существующие файлы пропускаются.

    После неудачи HTTP/2 клиент переключается на HTTP/1.1: некоторые прокси
    роняют длинные потоки, и спорить с ними обычно медленнее, чем скачать фото.
    """

    photoDownloaded = Signal(int, str, str)  # съёмка, папка и имя файла
    downloadFailed = Signal(int, str)  # съёмка и понятное описание ошибки
    markUpdated = Signal(int, str, dict)  # съёмка, папка и новые метки
    syncProgress = Signal(int, int, int, int)  # съёмка, скачано, всего, повторы

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._targets: dict[int, ReceiveTarget] = {}
        self._manager = QNetworkAccessManager(self)
        self._active: set[QNetworkReply] = set()
        self._downloads: dict[tuple[int, str], tuple[str, int]] = {}
        self._download_queue: list[tuple[int, str, Path]] = []
        self._inflight_downloads = 0
        self._http2_failed = False
        self._sync: dict[int, dict] = {}

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    def start_receiving(self, shooting_id: int, folder: Path, name: str) -> None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self._targets[int(shooting_id)] = ReceiveTarget(folder=folder, name=name)

    def stop_receiving(self, shooting_id: int) -> None:
        self._targets.pop(int(shooting_id), None)
        self._sync.pop(int(shooting_id), None)

    def sync_existing(self, shooting_id: int) -> None:
        """Ставит в очередь уже готовые фотографии подключённой съёмки."""
        target = self._targets.get(int(shooting_id))
        if target is None or not self._api_key:
            return
        request = QNetworkRequest(QUrl(f"{self._base_url}/api/shootings/{int(shooting_id)}/downloads/photos/"))
        self._apply_http_version_preference(request)
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._on_existing_list(reply, int(shooting_id)))

    def is_receiving(self, shooting_id: int) -> bool:
        return int(shooting_id) in self._targets

    def receiving_ids(self) -> set[int]:
        return set(self._targets)

    def folder_for(self, shooting_id: int) -> Path | None:
        target = self._targets.get(int(shooting_id))
        return target.folder if target else None

    def on_photo_added(self, shooting_id: int, photo: dict) -> None:
        target = self._targets.get(int(shooting_id))
        if target is None:
            return
        url = photo.get("url") or photo.get("thumb_url")
        if not url:
            return
        filename = safe_filename(photo.get("name") or f"photo-{photo.get('id')}.jpg")
        destination = target.folder / filename
        self._queue_download(int(shooting_id), self._absolute_url(url), destination)

    def _queue_download(self, shooting_id: int, url: str, destination: Path, attempt: int = 0) -> None:
        if destination.exists():
            self._mark_done(shooting_id, destination.name)
            return
        key = (shooting_id, str(destination))
        if key in self._downloads:
            return
        self._downloads[key] = (url, attempt)
        self._download_queue.append((shooting_id, url, destination))
        self._pump_downloads()

    def on_photo_updated(self, shooting_id: int, photo: dict) -> None:
        target = self._targets.get(int(shooting_id))
        if target is None:
            return
        self.markUpdated.emit(int(shooting_id), str(target.folder), photo)

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
        self._apply_http_version_preference(request)
        if self._api_key and url.startswith(self._base_url):
            request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.get(request)
        self._inflight_downloads += 1
        self._active.add(reply)
        reply.finished.connect(lambda: self._finish_download(reply, shooting_id, destination))

    def _finish_download(self, reply: QNetworkReply, shooting_id: int, destination: Path) -> None:
        """Проверяет ответ, атомарно сохраняет файл и продолжает очередь."""
        self._active.discard(reply)
        self._inflight_downloads = max(0, self._inflight_downloads - 1)
        reply.deleteLater()
        key = (shooting_id, str(destination))
        url, attempt = self._downloads.get(key, ("", 0))
        if reply.error() != QNetworkReply.NetworkError.NoError:
            if _is_http2_stream_error(reply.errorString()):
                self._http2_failed = True
            self.downloadFailed.emit(shooting_id, reply.errorString())
            self._retry(shooting_id, destination, url, attempt)
            self._pump_downloads()
            return
        data = bytes(reply.readAll())
        if not data:
            self.downloadFailed.emit(shooting_id, "Пустой файл.")
            self._retry(shooting_id, destination, url, attempt)
            self._pump_downloads()
            return
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            temp_path.write_bytes(data)
            temp_path.replace(destination)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            self.downloadFailed.emit(shooting_id, str(exc))
            self._retry(shooting_id, destination, url, attempt)
            self._pump_downloads()
            return
        self._downloads.pop(key, None)
        self._mark_done(shooting_id, destination.name)
        self.photoDownloaded.emit(shooting_id, str(destination.parent), destination.name)
        self._pump_downloads()

    def _pump_downloads(self) -> None:
        """Заполняет ограниченную очередь загрузок, не открывая сотни соединений."""
        while self._download_queue and self._inflight_downloads < MAX_INFLIGHT_DOWNLOADS:
            shooting_id, url, destination = self._download_queue.pop(0)
            key = (shooting_id, str(destination))
            if shooting_id not in self._targets or key not in self._downloads:
                continue
            self._download(shooting_id, url, destination)

    def _apply_http_version_preference(self, request: QNetworkRequest) -> None:
        """Использует HTTP/2, пока ошибка потока не потребует отката на HTTP/1.1."""
        if self._http2_failed:
            request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)

    def _on_existing_list(self, reply: QNetworkReply, shooting_id: int) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            if _is_http2_stream_error(reply.errorString()):
                self._http2_failed = True
            return
        try:
            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        photos = payload.get("photos") if isinstance(payload, dict) else None
        if not payload or not payload.get("ok") or not isinstance(photos, list):
            return
        self._sync[shooting_id] = {"total": len(photos), "done": set(), "failed": set()}
        self._emit_progress(shooting_id)
        for photo in photos:
            if isinstance(photo, dict):
                self.on_photo_added(shooting_id, photo)

    def _retry(self, shooting_id: int, destination: Path, url: str, attempt: int) -> None:
        key = (shooting_id, str(destination))
        self._downloads.pop(key, None)
        if not url or shooting_id not in self._targets:
            return
        state = self._sync.get(shooting_id)
        if state is not None:
            state["failed"].add(destination.name)
        self._emit_progress(shooting_id)
        delay = min(1000 * (2 ** min(attempt, 5)), _RETRY_MAX_MS)
        QTimer.singleShot(delay, lambda: self._queue_download(shooting_id, url, destination, attempt + 1))

    def _mark_done(self, shooting_id: int, name: str) -> None:
        state = self._sync.get(shooting_id)
        if state is not None:
            state["done"].add(name)
            state["failed"].discard(name)
            self._emit_progress(shooting_id)

    def _emit_progress(self, shooting_id: int) -> None:
        state = self._sync.get(shooting_id)
        if state is None:
            return
        self.syncProgress.emit(shooting_id, len(state["done"]), state["total"], len(state["failed"]))


def _is_http2_stream_error(message: str) -> bool:
    text = (message or "").casefold()
    return "stream" in text and any(token in text for token in ("refused", "no longer needed"))
