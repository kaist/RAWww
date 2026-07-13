import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("RAWWW_PROFILE", "mobile")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from rawww.cache import FolderCache
from rawww.mobile import MobileWindow

_app = QApplication.instance() or QApplication([])


def _write_jpeg(path: Path) -> None:
    image = QImage(8, 8, QImage.Format.Format_RGB888)
    image.fill(0x808080)
    assert image.save(str(path), "JPG")


class MobileSelectionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.names = ["a.jpg", "b.jpg"]
        for name in self.names:
            _write_jpeg(self.folder / name)
        # Seed the folder as a downloaded ShotSync selection.
        cache = FolderCache(self.folder, live_names=set(self.names), load_from_disk=True)
        cache.set_shotsync_session(42, "Test shooting")
        cache.set_shotsync_photos([(name, index + 1, 42) for index, name in enumerate(self.names)])
        cache.close(flush=True)

        self.window = MobileWindow()

    def tearDown(self) -> None:
        self.window.close()
        self._tmp.cleanup()

    def test_open_folder_builds_grid_and_syncer(self) -> None:
        self.window._active_shooting = 42
        self.window._open_folder(self.folder, 42)
        self.assertEqual(self.window._names, self.names)
        self.assertIsNotNone(self.window._cache)
        self.assertIsNotNone(self.window._syncer)
        self.assertEqual(self.window.grid_list.count(), 2)

    def test_rating_is_persisted_and_queued_for_sync(self) -> None:
        self.window._active_shooting = 42
        self.window._open_folder(self.folder, 42)
        self.window._index = 0
        self.window._set_rating(5)

        # Persisted locally.
        self.assertEqual(self.window._details["a.jpg"]["rating"], 5)
        reloaded = self.window._cache.load_photo_details(include_metadata=False)
        self.assertEqual(reloaded["a.jpg"]["rating"], 5)

        # The shared socket is offline in the test, so the mark is durably
        # queued for delivery rather than lost.
        self.assertGreaterEqual(self.window._syncer.pending_count(), 1)

    def test_color_and_comment_persist(self) -> None:
        self.window._active_shooting = 42
        self.window._open_folder(self.folder, 42)
        self.window._index = 1
        self.window._set_color("green")
        self.window.comment_edit.setText("keep")
        self.window._commit_comment()

        reloaded = self.window._cache.load_photo_details(include_metadata=False)
        self.assertEqual(reloaded["b.jpg"]["color_label"], "green")
        self.assertEqual(reloaded["b.jpg"]["comment"], "keep")


if __name__ == "__main__":
    unittest.main()
