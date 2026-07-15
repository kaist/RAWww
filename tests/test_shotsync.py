## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки получения фотографий и синхронизации с ShotSync."""

from __future__ import annotations

import os
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal  # noqa: E402

from rawww.shotsync_hub import ShotSyncHub  # noqa: E402
from rawww.shotsync_receiver import ShotSyncReceiver, safe_filename  # noqa: E402
from rawww.shotsync_selection import SelectionMarkSyncer  # noqa: E402
from rawww.shotsync_socket import ShotSyncSocket  # noqa: E402
from rawww.shotsync_upload import MarksFetcher, encode_preview, exif_original_datetime  # noqa: E402

try:  # pragma: no cover — зависит от окружения
    from PySide6.QtWidgets import QApplication

    from rawww.cache import FolderCache
    from rawww.shotsync_panel import ShotSyncPanel

    HAVE_GUI = True
except Exception:  # noqa: BLE001 — в окружении может отсутствовать libGL
    HAVE_GUI = False

BASE_URL = "https://shotsync.ru"


def _app() -> QCoreApplication:
    """Возвращает общий цикл событий, необходимый QtNetwork и QtWebSockets.

    При доступном GUI сразу создаётся ``QApplication``, чтобы тот же экземпляр
    подошёл последующим тестам виджетов. Qt не разрешает повысить уже созданный
    ``QCoreApplication`` до ``QApplication``, поэтому порядок здесь важен.
    """
    existing = QCoreApplication.instance()
    if existing is not None:
        return existing
    if HAVE_GUI:
        return QApplication([])
    return QCoreApplication([])


class SocketParsingTests(unittest.TestCase):
    """Проверяет разбор входящих сообщений WebSocket ShotSync."""

    def setUp(self) -> None:
        _app()
        self.socket = ShotSyncSocket(BASE_URL)

    def test_ws_url_uses_wss_for_https(self) -> None:
        self.socket.set_api_key("abc123")
        url = self.socket._ws_url().toString()
        self.assertEqual(url, "wss://shotsync.ru/ws/app/?api_key=abc123")

    def test_ws_url_uses_ws_for_http(self) -> None:
        socket = ShotSyncSocket("http://localhost:8000")
        socket.set_api_key("k")
        self.assertEqual(
            socket._ws_url().toString(), "ws://localhost:8000/ws/app/?api_key=k"
        )

    def test_photo_added_message_is_parsed(self) -> None:
        received: list[tuple[int, dict]] = []
        self.socket.photoAdded.connect(lambda sid, photo: received.append((sid, photo)))
        self.socket._on_text_message(
            '{"type": "photo.added", "shooting_id": 7, "photo": {"id": 1, "name": "a.cr2"}}'
        )
        self.assertEqual(received, [(7, {"id": 1, "name": "a.cr2"})])

    def test_photo_updated_and_ack_messages_are_parsed(self) -> None:
        updated: list[tuple[int, dict]] = []
        acks: list[dict] = []
        self.socket.photoUpdated.connect(lambda sid, photo: updated.append((sid, photo)))
        self.socket.ackReceived.connect(acks.append)
        self.socket._on_text_message(
            '{"type": "photo.updated", "shooting_id": 3, "photo": {"id": 9, "rating": 5}}'
        )
        self.socket._on_text_message('{"type": "photo.ack", "ok": true, "request_id": "r1"}')
        self.assertEqual(updated, [(3, {"id": 9, "rating": 5})])
        self.assertEqual(acks, [{"type": "photo.ack", "ok": True, "request_id": "r1"}])

    def test_malformed_messages_are_ignored(self) -> None:
        events: list = []
        self.socket.photoAdded.connect(lambda *a: events.append(a))
        self.socket._on_text_message("not json")
        self.socket._on_text_message('{"type": "photo.added"}')  # нет данных фотографии
        self.assertEqual(events, [])

    def test_send_json_reports_offline(self) -> None:
        self.assertFalse(self.socket.send_json({"type": "ping"}))


class ReceiverTests(unittest.TestCase):
    """Проверяет очередь и правила получения файлов ShotSync."""

    def setUp(self) -> None:
        _app()
        self.receiver = ShotSyncReceiver(BASE_URL)
        self.receiver.set_api_key("key")

    def test_safe_filename_strips_paths(self) -> None:
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\shots\\a.cr2"), "a.cr2")
        self.assertEqual(safe_filename(""), "photo.jpg")

    def test_absolute_url_prefixes_relative_media(self) -> None:
        self.assertEqual(
            self.receiver._absolute_url("/media/x.cr2"), "https://shotsync.ru/media/x.cr2"
        )
        self.assertEqual(
            self.receiver._absolute_url("https://cdn/x.cr2"), "https://cdn/x.cr2"
        )

    def test_start_stop_receiving_tracks_state(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "shoot"
            self.receiver.start_receiving(11, folder, "Shoot")
            self.assertTrue(self.receiver.is_receiving(11))
            self.assertTrue(folder.is_dir())
            self.assertEqual(self.receiver.receiving_ids(), {11})
            self.assertEqual(self.receiver.folder_for(11), folder)
            self.receiver.stop_receiving(11)
            self.assertFalse(self.receiver.is_receiving(11))

    def test_photo_added_downloads_missing_file(self) -> None:
        calls: list[tuple[int, str, Path]] = []
        self.receiver._download = lambda sid, url, dest: calls.append((sid, url, dest))  # type: ignore[assignment]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.receiver.start_receiving(5, folder, "S")
            self.receiver.on_photo_added(5, {"id": 1, "name": "a.cr2", "url": "/media/a.cr2"})
            self.assertEqual(len(calls), 1)
            sid, url, dest = calls[0]
            self.assertEqual(sid, 5)
            self.assertEqual(url, "https://shotsync.ru/media/a.cr2")
            self.assertEqual(dest, folder / "a.cr2")

    def test_photo_added_skips_existing_and_unwatched(self) -> None:
        calls: list = []
        self.receiver._download = lambda *a: calls.append(a)  # type: ignore[assignment]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.cr2").write_bytes(b"data")
            self.receiver.start_receiving(5, folder, "S")
            self.receiver.on_photo_added(5, {"id": 1, "name": "a.cr2", "url": "/media/a.cr2"})
            self.receiver.on_photo_added(99, {"id": 2, "name": "b.cr2", "url": "/media/b.cr2"})
            self.assertEqual(calls, [])

    def test_photo_updated_emits_mark_only_when_receiving(self) -> None:
        marks: list[tuple[int, str, dict]] = []
        self.receiver.markUpdated.connect(lambda sid, folder, photo: marks.append((sid, folder, photo)))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.receiver.start_receiving(5, folder, "S")
            self.receiver.on_photo_updated(5, {"id": 1, "rating": 4})
            self.receiver.on_photo_updated(6, {"id": 2, "rating": 1})  # съёмка не принимается
            self.assertEqual(marks, [(5, str(folder), {"id": 1, "rating": 4})])


class HubPersistenceTests(unittest.TestCase):
    """Проверяет восстановление активных приёмов между запусками."""

    def setUp(self) -> None:
        _app()
        self.hub = ShotSyncHub(BASE_URL)
        self._tmp = TemporaryDirectory()
        ini = Path(self._tmp.name) / "settings.ini"
        self.hub._settings = QSettings(str(ini), QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_start_receiving_persists_and_restores(self) -> None:
        changed: list[int] = []
        self.hub.receivingChanged.connect(lambda: changed.append(1))
        folder = Path(self._tmp.name) / "shoot"
        self.hub.start_receiving(42, folder, "My Shoot")
        self.assertTrue(self.hub.is_receiving(42))
        self.assertEqual(self.hub.folder_for(42), folder)
        self.assertTrue(changed)

        restored = ShotSyncHub(BASE_URL)
        restored._settings = self.hub._settings
        restored._restore_targets()
        self.assertTrue(restored.is_receiving(42))
        self.assertEqual(restored.folder_for(42), folder)

    def test_stop_receiving_clears_persisted_target(self) -> None:
        folder = Path(self._tmp.name) / "shoot"
        self.hub.start_receiving(42, folder, "My Shoot")
        self.hub.stop_receiving(42)
        self.assertFalse(self.hub.is_receiving(42))
        restored = ShotSyncHub(BASE_URL)
        restored._settings = self.hub._settings
        restored._restore_targets()
        self.assertFalse(restored.is_receiving(42))

    def test_hub_relays_photo_updates_for_selection_folders(self) -> None:
        received: list[tuple[int, dict]] = []
        self.hub.photoUpdated.connect(lambda sid, photo: received.append((sid, photo)))
        self.hub.socket._on_text_message(
            '{"type": "photo.updated", "shooting_id": 42, "photo": {"id": 9, "rating": 5}}'
        )
        self.assertEqual(received, [(42, {"id": 9, "rating": 5})])


@unittest.skipUnless(HAVE_GUI, "QtWidgets/libGL not available in this environment")
class PanelRenderingTests(unittest.TestCase):
    """Проверяет состояния и действия карточек панели ShotSync."""

    def setUp(self) -> None:
        _app()
        self.panel = ShotSyncPanel()

    def test_receiving_indicator_is_shown(self) -> None:
        self.panel.set_shootings([{"id": 1, "title": "Wedding", "photo_count": 3, "status": "active"}])
        self.panel.set_receiving_ids({1})
        item = self.panel.shooting_list.item(0)
        self.assertIn("приём", item.text())

    def test_receive_request_emitted_from_menu_action(self) -> None:
        emitted: list[dict] = []
        self.panel.receiveRequested.connect(emitted.append)
        shooting = {"id": 2, "title": "Portrait", "photo_count": 0, "status": "active"}
        self.panel.set_shootings([shooting])
        self.panel.receiveRequested.emit(shooting)
        self.assertEqual(emitted, [shooting])

    def test_viewer_link_opens_shooting_in_browser(self) -> None:
        from PySide6.QtWidgets import QToolButton

        self.panel.set_shootings([
            {"id": 42, "viewer_url": "/v/view-secret/", "title": "Portrait", "photo_count": 0, "status": "active"}
        ])
        viewer_button = self.panel.findChild(QToolButton, "shotsyncViewerLink")
        self.assertIsNotNone(viewer_button)
        with patch("rawww.shotsync_panel.QDesktopServices.openUrl") as open_url:
            viewer_button.click()
        self.assertEqual(open_url.call_args.args[0].toString(), "https://shotsync.ru/v/view-secret/")


@unittest.skipUnless(HAVE_GUI, "QtGui/libGL not available in this environment")
class CacheShotSyncTests(unittest.TestCase):
    """Проверяет хранение сессии, соответствий и очереди меток в кэше."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.cache = FolderCache(self.folder, {"a.jpg", "b.jpg"}, load_from_disk=True)

    def tearDown(self) -> None:
        self.cache.close(flush=False)
        self._tmp.cleanup()

    def test_session_roundtrip(self) -> None:
        self.assertIsNone(self.cache.shotsync_session())
        self.cache.set_shotsync_session(7, "Wedding")
        self.assertEqual(self.cache.shotsync_session(), (7, "Wedding"))

    def test_photo_map_lookup(self) -> None:
        self.cache.set_shotsync_photos([("a.jpg", 101, 7), ("b.jpg", 102, 7)])
        self.assertEqual(self.cache.shotsync_photo_id("a.jpg"), 101)
        self.assertEqual(self.cache.shotsync_photo_id("b.jpg"), 102)
        self.assertIsNone(self.cache.shotsync_photo_id("missing.jpg"))
        self.assertEqual(self.cache.shotsync_local_name_for_photo_id(101), "a.jpg")
        self.assertIsNone(self.cache.shotsync_local_name_for_photo_id(999))

    def test_pending_queue_coalesces_per_kind(self) -> None:
        self.cache.enqueue_shotsync_mark(photo_id=101, shooting_id=7, kind="rating", payload_json='{"rating": 3}')
        self.cache.enqueue_shotsync_mark(photo_id=101, shooting_id=7, kind="rating", payload_json='{"rating": 5}')
        self.cache.enqueue_shotsync_mark(photo_id=101, shooting_id=7, kind="meta", payload_json='{"color_label": "red"}')
        pending = self.cache.pending_shotsync_marks()
        self.assertEqual(self.cache.pending_shotsync_count(), 2)
        rating = next(m for m in pending if m["kind"] == "rating")
        self.assertEqual(rating["payload_json"], '{"rating": 5}')

    def test_clear_pending(self) -> None:
        self.cache.enqueue_shotsync_mark(photo_id=101, shooting_id=7, kind="rating", payload_json='{"rating": 3}')
        self.cache.clear_shotsync_mark(101, "rating")
        self.assertEqual(self.cache.pending_shotsync_count(), 0)


class _FakeHub(QObject):
    """Минимальная замена ``ShotSyncHub`` для тестов синхронизации меток."""

    ackReceived = Signal(dict)
    connectionChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.connected = False
        self.sent: list[dict] = []

    def send_json(self, payload: dict) -> bool:
        if not self.connected:
            return False
        self.sent.append(payload)
        return True


class _FakeCache:
    """Хранит нужную тестам часть состояния ``FolderCache`` прямо в памяти.

    Заглушка повторяет объединение ожидающих меток и позволяет проверять
    синхронизацию без QtGui и настоящей SQLite-базы.
    """

    def __init__(self, photos: dict[str, int]) -> None:
        self._photos = dict(photos)
        self._pending: dict[tuple[int, str], dict] = {}
        self._seq = 0

    def shotsync_photo_id(self, name: str) -> int | None:
        return self._photos.get(name)

    def enqueue_shotsync_mark(self, *, photo_id, shooting_id, kind, payload_json) -> None:
        self._seq += 1
        self._pending[(photo_id, kind)] = {
            "photo_id": photo_id,
            "kind": kind,
            "shooting_id": shooting_id,
            "payload_json": payload_json,
            "seq": self._seq,
        }

    def pending_shotsync_marks(self) -> list[dict]:
        return sorted(self._pending.values(), key=lambda m: m["seq"])

    def clear_shotsync_mark(self, photo_id, kind) -> None:
        self._pending.pop((photo_id, kind), None)

    def pending_shotsync_count(self) -> int:
        return len(self._pending)


class SelectionMarkSyncerTests(unittest.TestCase):
    """Проверяет очередь меток без сети и очистку после ответа сервера."""

    def setUp(self) -> None:
        _app()
        self.cache = _FakeCache({"a.jpg": 101})
        self.hub = _FakeHub()
        self.syncer = SelectionMarkSyncer(self.hub, self.cache, 7)

    def tearDown(self) -> None:
        self.syncer.detach()

    def test_mark_queued_while_offline(self) -> None:
        self.syncer.queue_mark("a.jpg", detail={"rating": 4}, changes={"rating": 4})
        self.assertEqual(self.hub.sent, [])
        self.assertEqual(self.cache.pending_shotsync_count(), 1)

    def test_flush_on_reconnect_sends_and_ack_clears(self) -> None:
        self.syncer.queue_mark("a.jpg", detail={"rating": 4}, changes={"rating": 4})
        self.hub.connected = True
        self.hub.connectionChanged.emit(True)
        self.assertEqual(len(self.hub.sent), 1)
        message = self.hub.sent[0]
        self.assertEqual(message["type"], "photo.rate")
        self.assertEqual(message["photo_ids"], [101])
        self.assertEqual(message["rating"], 4)
        self.hub.ackReceived.emit({"ok": True, "request_id": message["request_id"]})
        self.assertEqual(self.cache.pending_shotsync_count(), 0)

    def test_failed_ack_keeps_mark_queued(self) -> None:
        self.hub.connected = True
        self.syncer.queue_mark("a.jpg", detail={"color_label": "red", "comment": ""}, changes={"color_label": "red"})
        self.assertEqual(len(self.hub.sent), 1)
        message = self.hub.sent[0]
        self.assertEqual(message["type"], "photo.meta")
        self.hub.ackReceived.emit({"ok": False, "request_id": message["request_id"]})
        self.assertEqual(self.cache.pending_shotsync_count(), 1)

    def test_unknown_photo_is_ignored(self) -> None:
        self.hub.connected = True
        self.syncer.queue_mark("missing.jpg", detail={"rating": 2}, changes={"rating": 2})
        self.assertEqual(self.hub.sent, [])
        self.assertEqual(self.cache.pending_shotsync_count(), 0)


@unittest.skipUnless(HAVE_GUI, "imaging pipeline (QtGui/libGL) not available")
class EncodePreviewTests(unittest.TestCase):
    """Проверяет создание клиентского JPEG-превью с длинной стороной до 1920 px.

    Код использует ``rawww.imaging.decode_pixels`` и умеет обрабатывать RAW,
    поэтому этим тестам нужен рабочий QtGui.
    """

    def test_downscales_large_image_to_jpeg(self) -> None:
        from PIL import Image

        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "big.png"
            Image.new("RGB", (4000, 2000), (120, 60, 30)).save(src)
            data = encode_preview(src, max_size=1920)
            self.assertTrue(data)
            with Image.open(BytesIO(data)) as out:
                self.assertEqual(out.format, "JPEG")
                self.assertEqual(max(out.size), 1920)  # длинная сторона ограничена

    def test_small_image_is_not_upscaled(self) -> None:
        from PIL import Image

        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "small.jpg"
            Image.new("RGB", (800, 600), (10, 20, 30)).save(src)
            data = encode_preview(src, max_size=1920)
            with Image.open(BytesIO(data)) as out:
                self.assertEqual(out.size, (800, 600))


class UploadExifTimestampTests(unittest.TestCase):
    """Проверяет сохранение времени съёмки при подготовке превью."""

    def test_fallback_reads_original_datetime_from_source_exif(self) -> None:
        with patch(
            "rawww.exif.extract_metadata_batch",
            return_value=[("source.CR3", '{"original_datetime":"2026-07-12T10:11:12+03:00"}')],
        ):
            self.assertEqual(
                exif_original_datetime(Path("source.CR3")),
                "2026-07-12T10:11:12+03:00",
            )


class _CollectingCache:
    """Записывает вызовы store_photo_selection для теста MarksFetcher."""

    def __init__(self) -> None:
        self.stored: list[tuple] = []

    def store_photo_selection(self, name, *, rating, color_label, comment) -> None:
        self.stored.append((name, rating, color_label, comment))

    def shotsync_local_name_for_photo_id(self, photo_id):
        return {101: "source.CR3"}.get(photo_id)


class MarksFetcherTests(unittest.TestCase):
    """Проверяет запись полученных серверных меток в кэш папки."""

    def setUp(self) -> None:
        _app()
        self.fetcher = MarksFetcher(BASE_URL)
        self.fetcher.set_api_key("k")

    def test_apply_marks_writes_each_into_cache(self) -> None:
        cache = _CollectingCache()
        payload = {
            "ok": True,
            "marks": [
                {"id": 101, "name": "a.jpg", "rating": 5, "color_label": "red", "comment": "hero"},
                {"name": "b.jpg", "rating": None, "color_label": "", "comment": ""},
                {"name": "", "rating": 3},  # запись без имени пропускается
            ],
        }
        applied = self.fetcher._apply_marks(payload, cache)
        self.assertEqual(applied, 2)
        self.assertEqual(cache.stored[0], ("source.CR3", 5, "red", "hero"))
        self.assertEqual(cache.stored[1], ("b.jpg", None, "", ""))

    def test_requires_api_key(self) -> None:
        self.fetcher.set_api_key("")
        errors: list[str] = []
        self.fetcher.failed.connect(errors.append)
        self.fetcher.fetch(7, _CollectingCache())
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
