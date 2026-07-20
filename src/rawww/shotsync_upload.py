## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Отправка локальной папки в ShotSync и получение меток с сервера."""

from __future__ import annotations

import base64
import json
import threading
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal, QThreadPool, QRunnable
from PySide6.QtNetwork import (
    QHttpMultiPart,
    QHttpPart,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from .shotsync_receiver import _is_http2_stream_error

if TYPE_CHECKING:   # избегайте импорта стека GUI/QtGui при загрузке модуля
    from .cache import FolderCache

API_KEY_HEADER = b"X-Api-Key"
PREVIEW_MAX_SIZE = 1920
PREVIEW_QUALITY = 85
MAX_INFLIGHT_UPLOADS = 3
MAX_UPLOAD_ATTEMPTS = 5          # попыток на файл при транспортных сбоях
_UPLOAD_RETRY_MAX_MS = 30_000    # верхняя граница экспоненциальной паузы
ENCODE_WINDOW = MAX_INFLIGHT_UPLOADS * 2  # сколько превью держим в памяти сверх отправки


def encode_preview(path: Path, max_size: int = PREVIEW_MAX_SIZE) -> bytes:
    """Декодирует файл и возвращает уменьшенное JPEG-превью в sRGB.

    Функция работает в фоновом потоке и использует общий конвейер
    ``decode_pixels``. Благодаря этому RAW проходит через ``rawpy`` или встроенное
    превью, учитывает ориентацию EXIF и выглядит так же, как карточка в приложении.
    Прямой ``Pillow.Image.open`` здесь не подходит: большинство RAW он попросту
    не понимает, и в данном случае честно признаётся в этом исключением.
    """
    from PIL import Image   # местный импорт делает стартап дешевым

    from .imaging import decode_pixels

    pixel = decode_pixels(path, max_size)
    image = Image.frombytes(
        "RGBA", (pixel.width, pixel.height), pixel.pixels, "raw", "RGBA"
    ).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    return buffer.getvalue()


def exif_original_datetime(path: Path) -> str | None:
    """Возвращает исходное время съёмки из EXIF или ``None``."""
    from .exif import extract_metadata_batch

    results = extract_metadata_batch([str(path)])
    if not results:
        return None
    try:
        metadata = json.loads(results[0][1])
    except (IndexError, TypeError, ValueError):
        return None
    value = metadata.get("original_datetime") if isinstance(metadata, dict) else None
    return str(value) if value else None


class _AiAttacher:
    """Добавляет к загрузке CLIP-эмбеддинг и найденные лица.

    Готовые значения берутся из кэша папки. Недостающие вычисляются по уже
    созданному превью, чтобы не декодировать RAW второй раз, и сохраняются для
    следующих запусков. Вызовы моделей защищены блокировкой: кодировщиков может
    быть несколько, а тяжёлые модели любят порядок и личное пространство.
    """

    def __init__(self, cache: "FolderCache", lock: threading.Lock, embed_fn=None, faces_fn=None) -> None:
        self._cache = cache
        self._lock = lock
        self._embed_fn = embed_fn
        self._faces_fn = faces_fn
        self._embeddings = cache.load_image_embeddings()
        self._faces = cache.load_face_analysis()

    def _functions(self):
        if self._embed_fn is None or self._faces_fn is None:
            from .ai import extract_embedding_batch, recognize_face_batch

            self._embed_fn = self._embed_fn or extract_embedding_batch
            self._faces_fn = self._faces_fn or recognize_face_batch
        return self._embed_fn, self._faces_fn

    def resolve(self, path: Path, preview_bytes: bytes) -> tuple[bytes, str | None]:
        """Возвращает эмбеддинг и JSON лиц; ``None`` означает, что лица не проверялись."""
        name = path.name
        embedding = self._embeddings.get(name) or b""
        faces_json = self._faces.get(name)
        if embedding and faces_json is not None:
            return embedding, faces_json

        source = (str(path), preview_bytes)
        embed_fn, faces_fn = self._functions()
        with self._lock:
            if not embedding:
                emb = dict(embed_fn([source])).get(str(path))
                if emb:
                    embedding = bytes(emb)
                    self._cache.store_image_embeddings([(str(path), embedding)])
            if faces_json is None:
                computed = dict(faces_fn([source])).get(str(path))
                if computed is not None:
                    faces_json = computed
                    self._cache.store_face_analysis([(str(path), faces_json)])
        return embedding, faces_json


class _EncodeSignals(QObject):
    """Сигналы результата одной фоновой подготовки превью."""

    done = Signal(object, bytes, object, bytes, str)
    failed = Signal(object, str)      # путь, ошибка


class _EncodeTask(QRunnable):
    """Готовит одно превью вне потока интерфейса и возвращает результат сигналом."""

    def __init__(self, path: Path, original_datetime: str | None, attacher: "_AiAttacher | None" = None) -> None:
        super().__init__()
        self.path = path
        self.original_datetime = original_datetime
        self.attacher = attacher
        self.signals = _EncodeSignals()

    def run(self) -> None:  # noqa: D401 — точка входа QRunnable
        try:
            data = encode_preview(self.path)
        except Exception as exc:  # noqa: BLE001 — сообщаем понятную причину ошибки
            self.signals.failed.emit(self.path, str(exc))
            return
        original_datetime = self.original_datetime or exif_original_datetime(self.path)
        embedding = b""
        faces_json = ""
        if self.attacher is not None:
            try:
                embedding, resolved = self.attacher.resolve(self.path, data)
                faces_json = resolved if resolved is not None else ""
            except Exception:  # noqa: BLE001 — ошибка AI не должна отменять загрузку
                embedding, faces_json = b"", ""
        self.signals.done.emit(self.path, data, original_datetime, embedding, faces_json)


class FolderUploader(QObject):
    """Создаёт или дополняет съёмку ShotSync и отправляет превью файлов папки.

    Сначала создаётся серверная съёмка (или переиспользуется ранее связанная с
    папкой — тогда догружаются лишь недостающие файлы), затем изображения
    кодируются в пуле потоков и загружаются ограниченными параллельными порциями.
    Кодирование идёт «окном», чтобы не держать в памяти всю папку сразу. При
    включённом AI к каждому файлу добавляются эмбеддинг и лица.

    Сбой отдельного файла больше не рушит всю отправку: транспортные ошибки
    повторяются с растущей паузой, а если файл так и не удался — он помечается
    неудачным, остальные продолжают загружаться, и по завершении сообщается,
    сколько кадров не доехало. Повторный запуск той же папки догрузит недостающее.
    """

    progress = Signal(int, int)           # сделано, всего
    finished = Signal(int, str)  # идентификатор съёмки и папка
    finishedWithErrors = Signal(int, str, int)  # съёмка, папка и число неудавшихся файлов
    failed = Signal(str)                  # сообщение об ошибке
    deleteFinished = Signal(int)
    deleteFailed = Signal(str)

    def __init__(self, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = ""
        self._manager = QNetworkAccessManager(self)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, (QThreadPool.globalInstance().maxThreadCount() or 4) // 2))
        self._ai_lock = threading.Lock()
        self._reset()

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    @property
    def busy(self) -> bool:
        return self._folder is not None

    def delete_shooting(self, shooting_id: int) -> None:
        """Удаляет ранее загруженную съёмку с сервера."""
        if self.busy:
            self.deleteFailed.emit("Дождитесь завершения текущей отправки.")
            return
        if not self._api_key:
            self.deleteFailed.emit("Нет авторизации ShotSync.")
            return
        shooting_id = int(shooting_id)
        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{shooting_id}/delete/")
        )
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.post(request, QByteArray())
        reply.finished.connect(lambda: self._on_deleted(reply, shooting_id))

    def _on_deleted(self, reply: QNetworkReply, shooting_id: int) -> None:
        payload = _read_json(reply)
        error = reply.error()
        reply.deleteLater()
        if error != QNetworkReply.NetworkError.NoError or not (payload or {}).get("ok"):
            self.deleteFailed.emit("Не удалось удалить съёмку с сервера.")
            return
        self.deleteFinished.emit(shooting_id)

    def _reset(self) -> None:
        self._folder: Path | None = None
        self._title = ""
        self._shooting_id = 0
        self._ai_faces_series = False
        self._all_images: list[Path] = []
        self._pending: list[Path] = []
        self._encode_cursor = 0
        self._encoding = 0
        self._queue: list[tuple[Path, bytes, str | None, bytes, str, int]] = []
        self._original_datetimes: dict[str, str | None] = {}
        self._uploaded_mapping: list[tuple[str, int, int]] = []
        self._cache: "FolderCache | None" = None
        self._attacher: _AiAttacher | None = None
        self._inflight = 0
        self._succeeded = 0
        self._failed_names: list[str] = []
        self._total = 0
        self._aborted = False
        self._resume = False
        self._resume_uploaded: set[str] = set()
        self._http2_failed = False

    def start(
        self,
        folder: Path,
        title: str,
        original_datetimes: dict[str, str | None] | None = None,
        ai_faces_series: bool = False,
    ) -> None:
        """Начинает загрузку ``folder`` как новой съёмки с названием ``title``."""
        if self.busy:
            self.failed.emit("Отправка уже выполняется.")
            return
        if not self._api_key:
            self.failed.emit("Нет авторизации ShotSync.")
            return
        from .imaging import JPEG_EXTENSIONS, RAW_EXTENSIONS, is_supported_image, is_supported_video

        candidates = []
        try:
            for path in folder.iterdir():
                try:
                    if path.is_file() and is_supported_image(path) and not is_supported_video(path):
                        with path.open("rb"):
                            pass
                        candidates.append(path)
                except OSError:
                    continue
        except OSError as exc:
            self.failed.emit(f"Не удалось прочитать папку: {exc}")
            return
        candidates.sort()
        raw_stems = {path.stem.casefold() for path in candidates if path.suffix.lower() in RAW_EXTENSIONS}
        images = [
            path for path in candidates
            if not (path.suffix.lower() in JPEG_EXTENSIONS and path.stem.casefold() in raw_stems)
        ]
        if not images:
            self.failed.emit("В папке нет поддерживаемых изображений.")
            return
        self._reset()
        self._folder = folder
        self._title = title
        self._ai_faces_series = bool(ai_faces_series)
        self._original_datetimes = dict(original_datetimes or {})
        self._all_images = images

        session = self._existing_session(folder, {p.name for p in images})
        if session is not None:
            shooting_id, existing_title, uploaded = session
            self._shooting_id = shooting_id
            self._title = existing_title or title
            self._resume = True
            self._resume_uploaded = uploaded
            self._verify_shooting()
        else:
            self._begin_pending(images)
            self._create_shooting()

    def _existing_session(
        self, folder: Path, names: set[str]
    ) -> tuple[int, str, set[str]] | None:
        """Читает ранее сохранённую связь папки со съёмкой ShotSync.

        Возвращает ``(shooting_id, title, уже_загруженные_имена)`` или ``None``.
        Список уже загруженных нужен для докачки: файлы из него повторно не шлём.
        """
        try:
            from .cache import FolderCache

            cache = FolderCache(folder, live_names=names, load_from_disk=True)
        except Exception:  # noqa: BLE001 — нет кэша, значит и связи нет
            return None
        try:
            session = cache.shotsync_session()
            uploaded = set(cache.shotsync_photo_names()) if session else set()
        except Exception:  # noqa: BLE001
            session, uploaded = None, set()
        finally:
            try:
                cache.close(flush=False)
            except Exception:  # noqa: BLE001
                pass
        if not session or not session[0]:
            return None
        return int(session[0]), session[1], uploaded

    def _verify_shooting(self) -> None:
        """Проверяет, что связанная со съёмкой запись ещё жива на сервере."""
        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{self._shooting_id}/downloads/photos/")
        )
        self._apply_http_version_preference(request)
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._on_verify(reply))

    def _on_verify(self, reply: QNetworkReply) -> None:
        payload = _read_json(reply)
        error = reply.error()
        error_string = reply.errorString()
        reply.deleteLater()
        if self._folder is None or self._aborted:
            return
        if error != QNetworkReply.NetworkError.NoError:
            if _is_http2_stream_error(error_string):
                self._http2_failed = True
            self._abort("Не удалось связаться с сервером ShotSync.")
            return
        if not (payload or {}).get("ok"):
            # Съёмку удалили на сервере: забываем связь и начинаем с чистой съёмки.
            self._forget_stale_session()
            self._resume = False
            self._resume_uploaded = set()
            self._shooting_id = 0
            self._begin_pending(self._all_images)
            self._create_shooting()
            return
        pending = [p for p in self._all_images if p.name not in self._resume_uploaded]
        self._begin_pending(pending, total=len(self._all_images))
        self._succeeded = len(self._all_images) - len(pending)
        self._start_encoding()

    def _forget_stale_session(self) -> None:
        """Стирает устаревшую связь папки со съёмкой ShotSync."""
        if self._folder is None:
            return
        try:
            from .cache import FolderCache

            names = {p.name for p in self._all_images}
            cache = FolderCache(self._folder, live_names=names, load_from_disk=True)
            try:
                cache.clear_shotsync_session()
            finally:
                cache.close(flush=True)
        except Exception:  # noqa: BLE001 — не смогли — сервер сам разберётся с дублем
            pass

    def _begin_pending(self, pending, total: int | None = None) -> None:
        self._pending = list(pending)
        self._encode_cursor = 0
        self._total = len(self._pending) if total is None else total

    def _create_shooting(self) -> None:
        request = QNetworkRequest(QUrl(f"{self._base_url}/api/shootings/create/"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        body = QByteArray(json.dumps({
            "title": self._title,
            "ai_faces_series": self._ai_faces_series,
        }).encode("utf-8"))
        reply = self._manager.post(request, body)
        reply.finished.connect(lambda: self._on_shooting_created(reply))

    def _on_shooting_created(self, reply: QNetworkReply) -> None:
        payload = _read_json(reply)
        error = reply.error()
        reply.deleteLater()
        if self._folder is None or self._aborted:
            return
        if error != QNetworkReply.NetworkError.NoError:
            self._abort("Не удалось создать съёмку на сервере.")
            return
        shooting = (payload or {}).get("shooting") if payload else None
        shooting_id = int((shooting or {}).get("id") or 0)
        if not shooting_id:
            self._abort("Не удалось создать съёмку на сервере.")
            return
        self._shooting_id = shooting_id
        self._start_encoding()

    def _start_encoding(self) -> None:
        """Открывает кэш AI и запускает «окно» подготовки превью."""
        if self._ai_faces_series:
            self._open_ai_cache()
        self.progress.emit(self._succeeded, self._total)
        self._submit_encodes()
        self._maybe_finalize()

    def _submit_encodes(self) -> None:
        """Держит в работе не больше ``ENCODE_WINDOW`` готовящихся превью."""
        if self._folder is None or self._aborted:
            return
        while (
            self._encode_cursor < len(self._pending)
            and self._encoding + len(self._queue) < ENCODE_WINDOW
        ):
            path = self._pending[self._encode_cursor]
            self._encode_cursor += 1
            task = _EncodeTask(path, self._original_datetimes.get(path.name), self._attacher)
            task.signals.done.connect(self._on_encoded)
            task.signals.failed.connect(self._on_encode_failed)
            self._encoding += 1
            self._pool.start(task)

    def _open_ai_cache(self) -> None:
        if self._folder is None:
            return
        try:
            from .cache import FolderCache

            names = {p.name for p in self._all_images}
            self._cache = FolderCache(self._folder, live_names=names, load_from_disk=True)
            self._attacher = _AiAttacher(self._cache, self._ai_lock)
        except Exception:  # noqa: BLE001 — при ошибке кэша AI посчитает сервер
            self._close_cache(flush=False)
            self._attacher = None

    def _close_cache(self, *, flush: bool) -> None:
        cache, self._cache = self._cache, None
        if cache is not None:
            try:
                cache.close(flush=flush)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _append_field(multipart: QHttpMultiPart, name: str, value: bytes) -> None:
        part = QHttpPart()
        part.setHeader(
            QNetworkRequest.KnownHeaders.ContentDispositionHeader,
            f'form-data; name="{name}"',
        )
        part.setBody(QByteArray(value))
        multipart.append(part)

    def _on_encoded(
        self, path: Path, data: bytes, original_datetime: str | None,
        embedding: bytes, faces_json: str,
    ) -> None:
        if self._folder is None or self._aborted:
            return
        self._encoding = max(0, self._encoding - 1)
        self._queue.append((path, data, original_datetime, bytes(embedding), faces_json, 0))
        self._pump()
        self._submit_encodes()

    def _on_encode_failed(self, path: Path, error: str) -> None:
        if self._folder is None or self._aborted:
            return
        self._encoding = max(0, self._encoding - 1)
        # Не рушим всю отправку: кадр помечаем неудачным, остальные продолжают.
        self._record_failure(path.name)
        self._submit_encodes()
        self._maybe_finalize()

    def _pump(self) -> None:
        while self._queue and self._inflight < MAX_INFLIGHT_UPLOADS:
            self._upload_one(self._queue.pop(0))

    def _upload_one(self, item: tuple[Path, bytes, str | None, bytes, str, int]) -> None:
        """Формирует multipart-запрос одной фотографии и запускает отправку."""
        path, data, original_datetime, embedding, faces_json, _attempt = item
        multipart = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
        part = QHttpPart()
        part.setHeader(
            QNetworkRequest.KnownHeaders.ContentDispositionHeader,
            f'form-data; name="file"; filename="{path.stem}.jpg"',
        )
        part.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "image/jpeg")
        part.setBody(QByteArray(data))
        multipart.append(part)

        if original_datetime:
            datetime_part = QHttpPart()
            datetime_part.setHeader(
                QNetworkRequest.KnownHeaders.ContentDispositionHeader,
                'form-data; name="original_datetime"',
            )
            datetime_part.setBody(QByteArray(str(original_datetime).encode("utf-8")))
            multipart.append(datetime_part)

        if embedding:
            self._append_field(multipart, "image_embedding_q8", base64.b64encode(embedding))
        if faces_json:
            self._append_field(multipart, "faces", faces_json.encode("utf-8"))

        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{self._shooting_id}/photos/upload/")
        )
        self._apply_http_version_preference(request)
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.post(request, multipart)
        multipart.setParent(reply)   # привязать многочастное время жизни к ответу
        self._inflight += 1
        reply.finished.connect(lambda: self._on_uploaded(reply, item))

    def _on_uploaded(
        self, reply: QNetworkReply, item: tuple[Path, bytes, str | None, bytes, str, int]
    ) -> None:
        self._inflight = max(0, self._inflight - 1)
        payload = _read_json(reply)
        error = reply.error()
        error_string = reply.errorString()
        reply.deleteLater()
        if self._folder is None or self._aborted:
            return
        path = item[0]
        if error != QNetworkReply.NetworkError.NoError:
            # Транспортная ошибка (обрыв, таймаут, 5xx) — есть смысл повторить.
            if _is_http2_stream_error(error_string):
                self._http2_failed = True
            self._schedule_retry(item)
            self._pump()
            return
        photo_id = int(((payload or {}).get("photo") or {}).get("id") or 0)
        if not (payload or {}).get("ok") or not photo_id:
            # Бизнес-ошибка сервера (ok:false) — повтор не поможет, помечаем неудачным.
            self._record_failure(path.name)
            self._pump()
            self._submit_encodes()
            self._maybe_finalize()
            return
        self._uploaded_mapping.append((path.name, photo_id, self._shooting_id))
        self._succeeded += 1
        self.progress.emit(self._succeeded, self._total)
        self._pump()
        self._submit_encodes()
        self._maybe_finalize()

    def _schedule_retry(self, item: tuple[Path, bytes, str | None, bytes, str, int]) -> None:
        """Повторяет отправку файла с растущей паузой либо помечает его неудачным."""
        path = item[0]
        attempt = item[5]
        if attempt + 1 >= MAX_UPLOAD_ATTEMPTS:
            self._record_failure(path.name)
            self._maybe_finalize()
            return
        delay = min(1000 * (2 ** min(attempt, 5)), _UPLOAD_RETRY_MAX_MS)
        retry_item = (item[0], item[1], item[2], item[3], item[4], attempt + 1)
        QTimer.singleShot(delay, lambda: self._requeue(retry_item))

    def _requeue(self, item: tuple[Path, bytes, str | None, bytes, str, int]) -> None:
        if self._folder is None or self._aborted:
            return
        self._queue.append(item)
        self._pump()

    def _record_failure(self, name: str) -> None:
        self._failed_names.append(name)
        self.progress.emit(self._succeeded, self._total)

    def _maybe_finalize(self) -> None:
        if self._folder is None or self._aborted:
            return
        if self._succeeded + len(self._failed_names) < self._total:
            return
        self._finalize()

    def _finalize(self) -> None:
        """Фиксирует результат: связывает папку, сохраняет соответствия и сообщает итог."""
        folder = self._folder
        shooting_id = self._shooting_id
        title = self._title
        failed = len(self._failed_names)
        # Новая съёмка без единой успешной загрузки — убираем пустышку с сервера.
        if self._succeeded == 0 and not self._resume:
            self._close_cache(flush=False)
            self._delete_shooting_quietly(shooting_id)
            self._aborted = True
            self._folder = None
            self.failed.emit("Не удалось загрузить ни одной фотографии.")
            return
        try:
            from .cache import FolderCache

            names = {p.name for p in self._all_images}
            cache = self._cache
            if cache is None:
                cache = FolderCache(folder, live_names=names, load_from_disk=True)
            self._cache = None
            cache.set_shotsync_session(shooting_id, title)
            if self._uploaded_mapping:
                cache.set_shotsync_photos(self._uploaded_mapping)
            cache.close(flush=True)
        except Exception as exc:  # noqa: BLE001
            self._abort(f"Не удалось пометить папку: {exc}")
            return
        if failed:
            self.finishedWithErrors.emit(shooting_id, str(folder), failed)
        else:
            self.finished.emit(shooting_id, str(folder))
        self._reset()

    def _delete_shooting_quietly(self, shooting_id: int) -> None:
        """Удаляет съёмку на сервере без ожидания результата (очистка пустышки)."""
        if not shooting_id or not self._api_key:
            return
        request = QNetworkRequest(
            QUrl(f"{self._base_url}/api/shootings/{shooting_id}/delete/")
        )
        request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        reply = self._manager.post(request, QByteArray())
        reply.finished.connect(reply.deleteLater)

    def _apply_http_version_preference(self, request: QNetworkRequest) -> None:
        """После ошибки HTTP/2-потока переключается на HTTP/1.1 (капризные прокси)."""
        if self._http2_failed:
            request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)

    def _abort(self, message: str) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._close_cache(flush=False)
        self.failed.emit(message)
        self._folder = None

    def shutdown(self, *, wait: bool) -> None:
        """Отменяет очередь кодирования и при выходе ждёт уже начатые файлы."""
        self._aborted = True
        self._folder = None
        self._pool.clear()
        if wait:
            self._pool.waitForDone()
        self._close_cache(flush=False)


class MarksFetcher(QObject):
    """Получает актуальные метки съёмки и записывает их в кэш папки.

    Используется для ручной команды «Получить»: серверный рейтинг, цвет и
    комментарий сопоставляются локальным файлам по ID фотографии. Результат
    сообщается сигналом, а интерфейс уже решает, что и как перерисовать.
    """

    finished = Signal(int)                # количество выставленных оценок
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
        """Записывает полученные метки в ``cache`` и возвращает их количество."""
        applied = 0
        for mark in payload.get("marks", []):
            name = ""
            try:
                local_name_for_id = cache.shotsync_local_name_for_photo_id
            except AttributeError:
                local_name_for_id = None
            if local_name_for_id is not None:
                try:
                    name = local_name_for_id(int(mark.get("id") or 0)) or ""
                except (TypeError, ValueError):
                    pass
            name = name or str(mark.get("name") or "").strip()
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
