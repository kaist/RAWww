## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Асинхронный HTTP-клиент интеграции с ShotSync."""

from __future__ import annotations

import json

from PySide6.QtCore import QByteArray, QFile, QObject, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import (
    QHttpMultiPart,
    QHttpPart,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

DEFAULT_BASE_URL = "https://shotsync.ru"
API_KEY_HEADER = b"X-Api-Key"


class ShotSyncClient(QObject):
    """Выполняет HTTP-запросы ShotSync, не блокируя поток интерфейса.

    Клиент хранит ключ API, создаёт запросы через ``QNetworkAccessManager`` и
    приводит ответы разных методов к единым callback-сигнатурам. Виджеты не
    разбирают статусы и сетевые ошибки сами — этой кухни им и так хватает.
    """

    loginSucceeded = Signal(dict, str)  # профиль пользователя и ключ API
    loginFailed = Signal(str)  # понятное пользователю описание ошибки
    sessionVerified = Signal(dict)  # профиль пользователя из /me
    sessionInvalid = Signal(str)  # сохранённый ключ больше не работает
    sessionCheckFailed = Signal(str)  # сетевая ошибка; ключ пока сохраняем
    shootingsLoaded = Signal(list)  # список съёмок
    shootingsFailed = Signal(str)
    avatarLoaded = Signal(QImage)  # декодированный аватар профиля

    def __init__(self, base_url: str = DEFAULT_BASE_URL, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key: str = ""
        self._manager = QNetworkAccessManager(self)

    @property
    def api_key(self) -> str:
        return self._api_key

    def set_api_key(self, key: str | None) -> None:
        self._api_key = (key or "").strip()

    def has_key(self) -> bool:
        return bool(self._api_key)

    def logout(self) -> None:
        """Удаляет локальный ключ и завершает клиентский сеанс.

        Ключ выдаётся отдельно для входа, поэтому дополнительный запрос выхода
        серверу не требуется.
        """
        self._api_key = ""

    def _url(self, path: str) -> QUrl:
        return QUrl(f"{self._base_url}/{path.lstrip('/')}")

    def _request(self, path: str, *, with_key: bool = False) -> QNetworkRequest:
        request = QNetworkRequest(self._url(path))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        if with_key and self._api_key:
            request.setRawHeader(API_KEY_HEADER, self._api_key.encode("utf-8"))
        return request

    @staticmethod
    def _parse_json(reply: QNetworkReply) -> dict:
        raw = bytes(reply.readAll())
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error_message(data: dict, reply: QNetworkReply, fallback: str) -> str:
        message = data.get("error")
        if not message:
            errors = data.get("errors")
            if isinstance(errors, dict):
                for value in errors.values():
                    if isinstance(value, (list, tuple)) and value:
                        message = str(value[0])
                        break
                    if isinstance(value, str):
                        message = value
                        break
        if not message and reply.error() != QNetworkReply.NetworkError.NoError:
            message = reply.errorString()
        return message or fallback

    def login(self, login: str, password: str) -> None:
        request = self._request("/api/users/login/")
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        body = QByteArray(json.dumps({"login": login, "password": password}).encode("utf-8"))
        reply = self._manager.post(request, body)
        reply.finished.connect(lambda: self._handle_login(reply))

    def verify_session(self) -> None:
        if not self._api_key:
            self.sessionInvalid.emit("Требуется авторизация.")
            return
        reply = self._manager.get(self._request("/api/users/me/", with_key=True))
        reply.finished.connect(lambda: self._handle_me(reply))

    def fetch_shootings(self) -> None:
        if not self._api_key:
            self.shootingsFailed.emit("Требуется авторизация.")
            return
        reply = self._manager.get(self._request("/api/shootings/", with_key=True))
        reply.finished.connect(lambda: self._handle_shootings(reply))

    def fetch_avatar(self, avatar_url: str) -> None:
        if not avatar_url:
            return
        url = avatar_url
        if url.startswith("/"):
            url = f"{self._base_url}{url}"
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._handle_avatar(reply))

    def request_json(self, path: str, callback, *, method: str = "GET", payload: dict | None = None) -> None:
        """Вызывает авторизованный JSON-метод без блокировки интерфейса.

        ``callback`` получает ``(ok, payload, error)``. Общая реализация нужна,
        чтобы все возможности ShotSync одинаково работали с ключом, редиректами
        и ошибками, а не изобретали собственный интернет в каждом виджете.
        """
        if not self._api_key:
            callback(False, {}, "Требуется авторизация в ShotSync.")
            return
        request = self._request(path, with_key=True)
        method = method.upper()
        if method == "GET":
            reply = self._manager.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            body = QByteArray(json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"))
            reply = self._manager.post(request, body)
        reply.finished.connect(lambda: self._finish_json_request(reply, callback))

    def upload_file(self, path: str, file_path: str, callback) -> None:
        """Отправляет файл полем ``file`` на авторизованный адрес импорта кодов."""
        if not self._api_key:
            callback(False, {}, "Требуется авторизация в ShotSync.")
            return
        file = QFile(file_path)
        if not file.open(QFile.OpenModeFlag.ReadOnly):
            callback(False, {}, "Не удалось открыть файл импорта.")
            return
        multi = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
        part = QHttpPart()
        filename = file.fileName().replace(chr(92), "/").rsplit("/", 1)[-1]
        part.setHeader(
            QNetworkRequest.KnownHeaders.ContentDispositionHeader,
            f'form-data; name="file"; filename="{filename}"',
        )
        part.setBodyDevice(file)
        file.setParent(multi)
        multi.append(part)
        reply = self._manager.post(self._request(path, with_key=True), multi)
        multi.setParent(reply)
        reply.finished.connect(lambda: self._finish_json_request(reply, callback))

    def post_multipart(
        self, path: str, fields: dict[str, str], photo: tuple[str, bytes, str] | None, callback
    ) -> None:
        """Отправляет текстовые поля и необязательный файл через multipart POST.

        ``photo`` имеет вид ``(filename, data, content_type)``. Этот путь нужен,
        например, синхронизации лиц: имя, рамка и эмбеддинг идут полями, а
        вырезанный аватар — файлом.
        """
        if not self._api_key:
            callback(False, {}, "Требуется авторизация в ShotSync.")
            return
        multi = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
        for name, value in fields.items():
            part = QHttpPart()
            part.setHeader(
                QNetworkRequest.KnownHeaders.ContentDispositionHeader,
                f'form-data; name="{name}"',
            )
            part.setBody(QByteArray(str(value).encode("utf-8")))
            multi.append(part)
        if photo is not None:
            filename, data, content_type = photo
            part = QHttpPart()
            part.setHeader(
                QNetworkRequest.KnownHeaders.ContentDispositionHeader,
                f'form-data; name="photo"; filename="{filename}"',
            )
            part.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, content_type)
            part.setBody(QByteArray(data))
            multi.append(part)
        reply = self._manager.post(self._request(path, with_key=True), multi)
        multi.setParent(reply)
        reply.finished.connect(lambda: self._finish_json_request(reply, callback))

    def fetch_bytes(self, url: str, callback) -> None:
        """Загружает байты по ``url`` и передаёт в callback пару ``(ok, data)``."""
        if not url:
            callback(False, b"")
            return
        if url.startswith("/"):
            url = f"{self._base_url}{url}"
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        reply = self._manager.get(request)

        def done() -> None:
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else b""
            reply.deleteLater()
            callback(ok, data)

        reply.finished.connect(done)

    def _finish_json_request(self, reply: QNetworkReply, callback) -> None:
        data = self._parse_json(reply)
        ok = bool(data.get("ok")) and reply.error() == QNetworkReply.NetworkError.NoError
        error = "" if ok else self._error_message(data, reply, "Не удалось выполнить запрос к ShotSync.")
        reply.deleteLater()
        callback(ok, data, error)

    def _handle_login(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        data = self._parse_json(reply)
        key = data.get("key")
        user = data.get("user")
        if data.get("ok") and key and isinstance(user, dict):
            self._api_key = str(key)
            self.loginSucceeded.emit(user, self._api_key)
            return
        self.loginFailed.emit(
            self._error_message(data, reply, "Не удалось войти. Попробуйте ещё раз.")
        )

    def _handle_me(self, reply: QNetworkReply) -> None:
        data = self._parse_json(reply)
        network_error = reply.error() != QNetworkReply.NetworkError.NoError
        error = self._error_message(data, reply, "Не удалось проверить сессию ShotSync.")
        reply.deleteLater()
        user = data.get("user")
        if data.get("ok") and isinstance(user, dict):
            self.sessionVerified.emit(user)
            return
        if network_error:
            self.sessionCheckFailed.emit(error)
            return
        self._api_key = ""
        self.sessionInvalid.emit(error or "Сессия истекла, войдите заново.")

    def _handle_shootings(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        data = self._parse_json(reply)
        shootings = data.get("shootings")
        if data.get("ok") and isinstance(shootings, list):
            self.shootingsLoaded.emit(shootings)
            return
        self.shootingsFailed.emit(
            self._error_message(data, reply, "Не удалось загрузить съёмки.")
        )

    def _handle_avatar(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return
        image = QImage()
        if image.loadFromData(bytes(reply.readAll())) and not image.isNull():
            self.avatarLoaded.emit(image)
