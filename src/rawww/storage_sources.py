## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Идентичность локальных и облачных расположений рабочего пространства.

Локальный ``Path`` не подходит для облака: у ресурса Диска есть аккаунт,
серверный путь и ревизия, но нет имени файла ОС. Этот модуль даёт UI устойчивую
ссылку на источник, не заставляя REST-прослойку притворяться файловой системой.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSettings


@dataclass(frozen=True)
class SourceLocation:
    """Адрес папки в конкретном источнике, пригодный для вкладки и дерева."""

    source_id: str
    path: str
    kind: str = "local"

    @classmethod
    def local(cls, path: Path) -> "SourceLocation":
        return cls("local", str(path), "local")


@dataclass(frozen=True)
class YandexAccount:
    """Описывает одно независимое OAuth-подключение без раскрытия токенов UI."""

    id: str
    title: str


class YandexAccounts:
    """Хранит перечень подключений и изолирует ключи токенов каждого аккаунта."""

    SETTINGS_KEY = "yandex_disk/accounts"

    def __init__(self, settings: QSettings) -> None:
        self.settings = settings

    def list(self) -> list[YandexAccount]:
        raw = self.settings.value(self.SETTINGS_KEY, "[]", str)
        try:
            entries = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return [YandexAccount(str(value["id"]), str(value["title"])) for value in entries]

    def add(self, title: str) -> YandexAccount:
        account = YandexAccount(uuid4().hex, title.strip() or "Яндекс.Диск")
        accounts = self.list() + [account]
        self.settings.setValue(self.SETTINGS_KEY, json.dumps([asdict(item) for item in accounts], ensure_ascii=False))
        return account

    def rename(self, account_id: str, title: str) -> None:
        """Обновляет подпись подключения, не меняя его OAuth-идентичность."""
        normalized = title.strip() or "Яндекс.Диск"
        accounts = [
            YandexAccount(item.id, normalized if item.id == account_id else item.title)
            for item in self.list()
        ]
        self.settings.setValue(
            self.SETTINGS_KEY,
            json.dumps([asdict(item) for item in accounts], ensure_ascii=False),
        )

    def remove(self, account_id: str) -> None:
        accounts = [item for item in self.list() if item.id != account_id]
        self.settings.setValue(self.SETTINGS_KEY, json.dumps([asdict(item) for item in accounts], ensure_ascii=False))
        self.settings.remove(f"yandex_disk/accounts/{account_id}")

    @staticmethod
    def token_key(account_id: str, name: str) -> str:
        return f"yandex_disk/accounts/{account_id}/{name}"
