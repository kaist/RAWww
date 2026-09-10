## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Дисковый кэш миниатюр и получение серверных превью Яндекс.Диска."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtGui import QImage

from .yandex_disk import YandexDiskClient, YandexDiskError, YandexDiskItem


class YandexPreviewCache:
    """Владеет производным дисковым кэшем миниатюр одного приложения."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, account_id: str, item: YandexDiskItem, size: int) -> Path:
        """Строит устойчивый ключ с ревизией, чтобы изменённый кадр не был устаревшим."""
        identity = f"{item.resource_id or item.path}\0{item.revision or 0}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        return self.root / account_id / str(size) / f"{digest}.jpg"

    def load(self, account_id: str, item: YandexDiskItem, size: int) -> QImage | None:
        path = self.path_for(account_id, item, size)
        image = QImage(str(path))
        return image if not image.isNull() else None

    def obtain(
        self,
        client: YandexDiskClient,
        account_id: str,
        item: YandexDiskItem,
        size: int,
        url: str | None = None,
    ) -> tuple[Path, QImage]:
        """Возвращает кэш либо скачивает серверное превью и заменяет файл атомарно."""
        target = self.path_for(account_id, item, size)
        cached = QImage(str(target))
        if not cached.isNull():
            return target, cached
        preview_url = url or client.preview_url(item.path, size)
        if not preview_url:
            raise YandexDiskError("Для этого файла Яндекс.Диск не предоставил превью.")
        data = client.read_url(preview_url)
        image = QImage.fromData(data)
        if image.isNull():
            raise YandexDiskError("Яндекс.Диск вернул повреждённое превью.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target, image

    @staticmethod
    def fetch(
        client: YandexDiskClient,
        item: YandexDiskItem,
        size: int,
        url: str | None = None,
    ) -> QImage:
        """Загружает превью только в память, не оставляя полноразмерный кэш на диске."""
        preview_url = url or client.preview_url(item.path, size)
        if not preview_url:
            raise YandexDiskError("Для этого файла Яндекс.Диск не предоставил превью.")
        image = QImage.fromData(client.read_url(preview_url))
        if image.isNull():
            raise YandexDiskError("Яндекс.Диск вернул повреждённое превью.")
        return image


def download_original(client: YandexDiskClient, remote_path: str, target: Path) -> Path:
    """Выгружает оригинал атомарно; просмотр никогда не вызывает эту функцию."""
    client.download_file(remote_path, target)
    return target
