## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки локальной OAuth-сессии и запросов REST Яндекс.Диска."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSettings
from PySide6.QtGui import QImage

from rawww.yandex_disk import YandexDiskClient, YandexOAuth, YandexOAuthConfig
from rawww.storage_sources import SourceLocation, YandexAccounts
from rawww.yandex_cloud import YandexPreviewCache


class YandexOAuthTests(unittest.TestCase):
    """Проверяет, что PKCE-вход не нуждается в секрете desktop-приложения."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat
        )
        self.oauth = YandexOAuth(self.settings, YandexOAuthConfig("public-client"))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_authorization_url_has_pkce_and_remembers_device(self) -> None:
        query = parse_qs(urlparse(self.oauth.authorization_url()).query)

        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["public-client"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertTrue(self.settings.value("yandex_disk/accounts/default/pkce_verifier", "", str))
        self.assertTrue(self.settings.value("yandex_disk/accounts/default/device_id", "", str))

    def test_local_config_reads_only_public_parameters(self) -> None:
        path = Path(self.directory.name) / "ya_auth.txt"
        path.write_text(
            "client_id public-client\nclient_secret must-not-be-used\n"
            "redirect_uri https://oauth.yandex.ru/verification_code\n",
            encoding="utf-8",
        )

        config = YandexOAuthConfig.from_local_file(path)

        self.assertEqual(config.client_id, "public-client")
        self.assertEqual(config.redirect_uri, "https://oauth.yandex.ru/verification_code")

    @patch("rawww.yandex_disk.YandexOAuth._post_form")
    def test_complete_stores_tokens_in_settings(self, post_form) -> None:
        self.oauth.authorization_url()
        post_form.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
        }

        self.oauth.complete("1234567")

        self.assertEqual(self.settings.value("yandex_disk/accounts/default/access_token", "", str), "access")
        self.assertEqual(self.settings.value("yandex_disk/accounts/default/refresh_token", "", str), "refresh")
        self.assertFalse(self.settings.value("yandex_disk/accounts/default/pkce_verifier", "", str))


class YandexDiskClientTests(unittest.TestCase):
    """Проверяет преобразование ответа API без настоящего сетевого подключения."""

    def test_directory_items_keep_preview_without_local_file(self) -> None:
        oauth = Mock()
        oauth.access_token.return_value = "token"
        client = YandexDiskClient(oauth)
        payload = {
            "_embedded": {
                "items": [{
                    "path": "disk:/photo.jpg", "name": "photo.jpg", "type": "file",
                    "size": 42, "modified": "2026-09-10T12:00:00+00:00",
                    "preview": "https://preview.invalid/photo", "resource_id": "id:1", "revision": 2,
                }]
            }
        }
        with patch.object(client, "_request", return_value=payload):
            items = client.list_directory()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, "disk:/photo.jpg")
        self.assertEqual(items[0].preview_url, "https://preview.invalid/photo")
        self.assertFalse(items[0].is_dir)

    def test_preview_url_requests_exact_1920_variant(self) -> None:
        oauth = Mock()
        oauth.access_token.return_value = "token"
        client = YandexDiskClient(oauth)
        with patch.object(client, "_request", return_value={"preview": "https://preview.invalid/1920"}) as request:
            url = client.preview_url("disk:/photo.jpg", 1920)

        self.assertEqual(url, "https://preview.invalid/1920")
        self.assertIn("preview_size=1920x1920", request.call_args.args[1])

    def test_upload_url_keeps_remote_operation_explicit(self) -> None:
        client = YandexDiskClient(Mock())
        with patch.object(client, "_request", return_value={"href": "https://upload.invalid/file"}) as request:
            url = client.upload_url("disk:/photo.jpg")

        self.assertEqual(url, "https://upload.invalid/file")
        self.assertIn("overwrite=false", request.call_args.args[1])


class YandexAccountsTests(unittest.TestCase):
    """Проверяет независимость нескольких облачных подключений."""

    def test_accounts_have_stable_separate_locations(self) -> None:
        with TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            accounts = YandexAccounts(settings)
            first = accounts.add("Рабочий")
            second = accounts.add("Личный")

            self.assertEqual([item.title for item in accounts.list()], ["Рабочий", "Личный"])
            self.assertNotEqual(
                SourceLocation(first.id, "disk:/", "yandex"),
                SourceLocation(second.id, "disk:/", "yandex"),
            )

            accounts.rename(second.id, "Архив")
            settings.setValue(accounts.token_key(second.id, "refresh_token"), "secret")
            accounts.remove(second.id)

            self.assertEqual([item.title for item in accounts.list()], ["Рабочий"])
            self.assertFalse(settings.contains(accounts.token_key(second.id, "refresh_token")))


class YandexPreviewCacheTests(unittest.TestCase):
    """Проверяет, что крупное превью остаётся только в оперативной памяти."""

    def test_fetch_full_preview_does_not_create_disk_cache(self) -> None:
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(QImage(1, 1, QImage.Format.Format_RGB32).save(buffer, "PNG"))
        png = bytes(payload)
        item = Mock(path="disk:/photo.png")
        client = Mock()
        client.preview_url.return_value = "https://preview.invalid/1920"
        client.read_url.return_value = png
        with TemporaryDirectory() as directory:
            cache = YandexPreviewCache(Path(directory))

            image = cache.fetch(client, item, 1920)

            self.assertFalse(image.isNull())
            self.assertEqual(list(Path(directory).iterdir()), [])

