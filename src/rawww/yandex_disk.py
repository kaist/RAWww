## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Локальная OAuth-авторизация и REST-доступ к Яндекс.Диску.

Модуль не знает о Qt-виджетах и не скачивает оригиналы для просмотра. Он
возвращает метаданные и URL серверных превью; интерфейс решает, когда и как их
показать. Токены намеренно хранятся в ``QSettings`` по выбору приложения.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable
from uuid import uuid4

from PySide6.QtCore import QSettings


OAUTH_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
OAUTH_TOKEN_URL = "https://oauth.yandex.ru/token"
DISK_API_URL = "https://cloud-api.yandex.net/v1/disk"
SETTINGS_PREFIX = "yandex_disk"


class YandexDiskError(RuntimeError):
    """Описывает отказ OAuth или REST API без привязки к форме ответа."""


@dataclass(frozen=True)
class YandexOAuthConfig:
    """Хранит публичные параметры OAuth-приложения для desktop-flow.

    ``client_secret`` здесь принципиально отсутствует: PKCE связывает код с
    приложением и позволяет обменять его локально, не раскрывая секрет сборке.
    """

    client_id: str
    redirect_uri: str = "https://oauth.yandex.ru/verification_code"

    @classmethod
    def from_local_file(cls, path: Path) -> "YandexOAuthConfig":
        """Читает локальный файл разработки, не делая его частью поставки."""
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise YandexDiskError("Не найдены параметры OAuth Яндекс.Диска.") from exc
        for line in lines:
            key, separator, value = line.strip().partition(" ")
            if separator and key:
                values[key.casefold()] = value.strip()
        client_id = values.get("client_id", "")
        if not client_id:
            raise YandexDiskError("В параметрах OAuth отсутствует client_id.")
        return cls(client_id=client_id, redirect_uri=values.get("redirect_uri") or cls.redirect_uri)


@dataclass(frozen=True)
class YandexDiskItem:
    """Представляет объект Диска без локального ``Path`` и файловых побочных эффектов."""

    path: str
    name: str
    is_dir: bool
    size: int
    modified: str
    preview_url: str | None
    mime_type: str | None
    resource_id: str | None
    revision: int | None


class YandexOAuth:
    """Ведёт PKCE-вход и сохраняет обновляемые токены в настройках приложения."""

    def __init__(self, settings: QSettings, config: YandexOAuthConfig, account_id: str = "default") -> None:
        self.settings = settings
        self.config = config
        self.account_id = account_id

    def _key(self, name: str) -> str:
        """Изолирует OAuth-пару одного диска от остальных подключений."""
        return f"{SETTINGS_PREFIX}/accounts/{self.account_id}/{name}"

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        """Создаёт verifier/challenge для одноразового подтверждения входа."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def authorization_url(self) -> str:
        """Запоминает PKCE-состояние и возвращает URL, открываемый браузером."""
        verifier, challenge = self._pkce_pair()
        device_id = self.settings.value(self._key("device_id"), "", str)
        if not device_id:
            device_id = str(uuid4())
            self.settings.setValue(self._key("device_id"), device_id)
        self.settings.setValue(self._key("pkce_verifier"), verifier)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "device_id": device_id,
                "device_name": "Контролька",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{OAUTH_AUTHORIZE_URL}?{query}"

    def complete(self, code: str) -> None:
        """Меняет показанный Яндексом одноразовый код на пару OAuth-токенов."""
        verifier = self.settings.value(self._key("pkce_verifier"), "", str)
        if not verifier:
            raise YandexDiskError("Сначала откройте страницу входа Яндекс.Диска.")
        payload = self._post_form(
            OAUTH_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self.config.client_id,
                "device_id": self.settings.value(self._key("device_id"), "", str),
                "code_verifier": verifier,
            },
        )
        self._store_tokens(payload)
        self.settings.remove(self._key("pkce_verifier"))

    def access_token(self) -> str:
        """Возвращает действующий токен, обновляя его только перед REST-запросом."""
        token = self.settings.value(self._key("access_token"), "", str)
        expires_at = self.settings.value(self._key("expires_at"), 0, int)
        if token and expires_at > int(time.time()) + 60:
            return token
        refresh = self.settings.value(self._key("refresh_token"), "", str)
        if not refresh:
            raise YandexDiskError("Войдите в Яндекс.Диск.")
        payload = self._post_form(
            OAUTH_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.config.client_id,
            },
        )
        self._store_tokens(payload)
        return self.settings.value(self._key("access_token"), "", str)

    def disconnect(self) -> None:
        """Удаляет только локальную сессию; файлы на Диске не затрагиваются."""
        self.settings.remove(f"{SETTINGS_PREFIX}/accounts/{self.account_id}")

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        token = str(payload.get("access_token") or "")
        if not token:
            raise YandexDiskError("Яндекс не вернул токен доступа.")
        self.settings.setValue(self._key("access_token"), token)
        if refresh := payload.get("refresh_token"):
            self.settings.setValue(self._key("refresh_token"), str(refresh))
        self.settings.setValue(
            self._key("expires_at"), int(time.time()) + int(payload.get("expires_in") or 0)
        )

    @staticmethod
    def _post_form(url: str, payload: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return _read_json(request)


class YandexDiskClient:
    """Выполняет REST-операции Диска и отдаёт простые объекты фоновой очереди."""

    def __init__(self, oauth: YandexOAuth) -> None:
        self.oauth = oauth

    def list_directory(self, path: str = "disk:/", *, limit: int = 500) -> list[YandexDiskItem]:
        """Читает весь каталог страницами вместе с URL серверных миниатюр."""
        result: list[YandexDiskItem] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode(
                {"path": path, "limit": limit, "offset": offset, "preview_size": "M"}
            )
            payload = self._request("GET", f"/resources?{query}")
            embedded = payload.get("_embedded") or {}
            entries = list(embedded.get("items") or [])
            result.extend(self._item(entry) for entry in entries)
            offset += len(entries)
            total = int(embedded.get("total") or offset)
            if not entries or offset >= total:
                return result

    def disk_info(self) -> dict[str, Any]:
        """Возвращает сведения о владельце и квоте для подписи подключения."""
        return self._request("GET", "")

    def mkdir(self, path: str) -> None:
        """Создаёт облачную папку; API сам сообщает конфликт имени."""
        self._request("PUT", f"/resources?{urllib.parse.urlencode({'path': path})}")

    def move(self, source: str, destination: str, *, overwrite: bool = False) -> None:
        """Перемещает или переименовывает объект на стороне Яндекс.Диска."""
        self._operation("move", source, destination, overwrite)

    def copy(self, source: str, destination: str, *, overwrite: bool = False) -> None:
        """Копирует объект на стороне Яндекс.Диска без загрузки через компьютер."""
        self._operation("copy", source, destination, overwrite)

    def delete(self, path: str, *, permanently: bool = False) -> None:
        """Отправляет объект в корзину, если пользователь явно не выбрал удаление навсегда."""
        query = urllib.parse.urlencode({"path": path, "permanently": str(permanently).lower()})
        self._wait_operation(self._request("DELETE", f"/resources?{query}"))

    def download_url(self, path: str) -> str:
        """Получает короткоживущую ссылку для потоковой записи оригинала в цель."""
        payload = self._request("GET", f"/resources/download?{urllib.parse.urlencode({'path': path})}")
        href = str(payload.get("href") or "")
        if not href:
            raise YandexDiskError("Яндекс.Диск не вернул ссылку на скачивание.")
        return href

    def upload_url(self, path: str, *, overwrite: bool = False) -> str:
        """Получает короткоживущую ссылку для загрузки локального файла."""
        query = urllib.parse.urlencode({"path": path, "overwrite": str(overwrite).lower()})
        payload = self._request("GET", f"/resources/upload?{query}")
        href = str(payload.get("href") or "")
        if not href:
            raise YandexDiskError("Яндекс.Диск не вернул ссылку на загрузку.")
        return href

    def upload_file(
        self,
        source: Path,
        destination: str,
        *,
        overwrite: bool = False,
        progress: Callable[[int], None] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        """Потоково отправляет файл по выданной API ссылке, не занимая память целиком."""
        href = self.upload_url(destination, overwrite=overwrite)
        try:
            with source.open("rb") as stream:
                body = _ProgressReader(stream, progress, checkpoint)
                request = urllib.request.Request(
                    href,
                    data=body,
                    headers={"Content-Length": str(source.stat().st_size)},
                    method="PUT",
                )
                with urllib.request.urlopen(request, timeout=300):  # noqa: S310 - URL выдан API
                    pass
        except OSError as exc:
            raise YandexDiskError("Не удалось загрузить файл на Яндекс.Диск.") from exc

    def download_file(
        self,
        remote_path: str,
        target: Path,
        *,
        progress: Callable[[int], None] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        """Потоково выгружает оригинал в атомарную временную цель с прогрессом."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
        request = urllib.request.Request(
            self.download_url(remote_path),
            headers={"Authorization": f"OAuth {self.oauth.access_token()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response, temporary.open("xb") as output:  # noqa: S310 - URL выдан API
                while True:
                    if checkpoint is not None:
                        checkpoint()
                    chunk = response.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    if progress is not None:
                        progress(len(chunk))
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def preview_url(self, path: str, size: int) -> str | None:
        """Возвращает URL серверного JPEG заданной стороны без скачивания оригинала."""
        query = urllib.parse.urlencode({"path": path, "preview_size": f"{size}x{size}"})
        payload = self._request("GET", f"/resources?{query}")
        preview = payload.get("preview")
        return str(preview) if preview else None

    def read_url(self, url: str) -> bytes:
        """Скачивает выданное API превью или оригинал с OAuth-заголовком."""
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"OAuth {self.oauth.access_token()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - URL выдан API
                return response.read()
        except urllib.error.HTTPError as exc:
            raise YandexDiskError(f"Ошибка Яндекс.Диска: HTTP {exc.code}") from exc
        except OSError as exc:
            raise YandexDiskError("Не удалось скачать файл с Яндекс.Диска.") from exc

    def _operation(self, action: str, source: str, destination: str, overwrite: bool) -> None:
        query = urllib.parse.urlencode({"from": source, "path": destination, "overwrite": str(overwrite).lower()})
        self._wait_operation(self._request("POST", f"/resources/{action}?{query}"))

    def _wait_operation(self, payload: dict[str, Any]) -> None:
        """Дожидается серверной копии или переноса, если API вернул асинхронную операцию."""
        href = str(payload.get("href") or "")
        if not href:
            return
        for _attempt in range(600):
            request = urllib.request.Request(
                href,
                headers={"Authorization": f"OAuth {self.oauth.access_token()}"},
            )
            state = _read_json(request)
            status = str(state.get("status") or "").casefold()
            if status == "success":
                return
            if status == "failed":
                raise YandexDiskError("Серверная операция Яндекс.Диска завершилась ошибкой.")
            time.sleep(0.25)
        raise YandexDiskError("Яндекс.Диск слишком долго выполняет операцию.")

    def _request(self, method: str, suffix: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{DISK_API_URL}{suffix}",
            headers={"Authorization": f"OAuth {self.oauth.access_token()}"},
            method=method,
        )
        return _read_json(request)

    @staticmethod
    def _item(entry: dict[str, Any]) -> YandexDiskItem:
        return YandexDiskItem(
            path=str(entry.get("path") or ""),
            name=str(entry.get("name") or ""),
            is_dir=entry.get("type") == "dir",
            size=int(entry.get("size") or 0),
            modified=str(entry.get("modified") or ""),
            preview_url=str(entry["preview"]) if entry.get("preview") else None,
            mime_type=str(entry["mime_type"]) if entry.get("mime_type") else None,
            resource_id=str(entry["resource_id"]) if entry.get("resource_id") else None,
            revision=int(entry["revision"]) if entry.get("revision") is not None else None,
        )


def _read_json(request: urllib.request.Request) -> dict[str, Any]:
    """Читает JSON и сводит сетевые ошибки к сообщению, пригодному для UI."""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL фиксирован API или выдан им
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("message")
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = None
        raise YandexDiskError(str(message or f"Ошибка Яндекс.Диска: HTTP {exc.code}")) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YandexDiskError("Не удалось подключиться к Яндекс.Диску.") from exc


class _ProgressReader:
    """Добавляет паузу, отмену и подсчёт байтов к читаемому телу HTTP PUT."""

    def __init__(
        self,
        stream: BinaryIO,
        progress: Callable[[int], None] | None,
        checkpoint: Callable[[], None] | None,
    ) -> None:
        self.stream = stream
        self.progress = progress
        self.checkpoint = checkpoint

    def read(self, size: int = -1) -> bytes:
        if self.checkpoint is not None:
            self.checkpoint()
        chunk = self.stream.read(size)
        if chunk and self.progress is not None:
            self.progress(len(chunk))
        return chunk
