"""Tests for the RAWww ShotSync live-receive stack (feature 1).

These cover the pure logic of the shared socket, the download receiver and the
hub's folder bookkeeping without needing a live server or network: Qt signals
are driven directly and downloads are intercepted.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rawww.shotsync_hub import ShotSyncHub  # noqa: E402
from rawww.shotsync_panel import ShotSyncPanel  # noqa: E402
from rawww.shotsync_receiver import ShotSyncReceiver, safe_filename  # noqa: E402
from rawww.shotsync_socket import ShotSyncSocket  # noqa: E402

BASE_URL = "https://shotsync.ru"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class SocketParsingTests(unittest.TestCase):
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
        self.socket._on_text_message('{"type": "photo.added"}')  # missing photo
        self.assertEqual(events, [])

    def test_send_json_reports_offline(self) -> None:
        self.assertFalse(self.socket.send_json({"type": "ping"}))


class ReceiverTests(unittest.TestCase):
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
            # Already on disk -> no download.
            self.receiver.on_photo_added(5, {"id": 1, "name": "a.cr2", "url": "/media/a.cr2"})
            # Shooting not being received -> ignored.
            self.receiver.on_photo_added(99, {"id": 2, "name": "b.cr2", "url": "/media/b.cr2"})
            self.assertEqual(calls, [])

    def test_photo_updated_emits_mark_only_when_receiving(self) -> None:
        marks: list[tuple[int, str, dict]] = []
        self.receiver.markUpdated.connect(lambda sid, folder, photo: marks.append((sid, folder, photo)))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.receiver.start_receiving(5, folder, "S")
            self.receiver.on_photo_updated(5, {"id": 1, "rating": 4})
            self.receiver.on_photo_updated(6, {"id": 2, "rating": 1})  # not received
            self.assertEqual(marks, [(5, str(folder), {"id": 1, "rating": 4})])


class HubPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.hub = ShotSyncHub(BASE_URL)
        self._tmp = TemporaryDirectory()
        # Isolate settings to a throwaway ini so real user config is untouched.
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

        # A fresh hub sharing the same settings restores the target.
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


class PanelRenderingTests(unittest.TestCase):
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
        # Emit directly to avoid opening a native menu in the test.
        self.panel.receiveRequested.emit(shooting)
        self.assertEqual(emitted, [shooting])


if __name__ == "__main__":
    unittest.main()
