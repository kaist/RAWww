## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки сетевого видео, поздних ответов и общих файловых операций."""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from rawww.app import FullView, Workspace
from rawww.imaging import DecodedImage


class CloudVideoTests(unittest.TestCase):
    """Изолирует UI от сети и не меняет пользовательские файлы."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_waiting_video_clears_previous_photo_and_has_no_local_source(self):
        view = FullView()
        try:
            image = QImage(20, 10, QImage.Format.Format_RGB32)
            view.set_image(DecodedImage(Path("photo.jpg"), image, 20, 10))
            view.set_video(Path("remote.mp4"), source=QUrl())
            self.assertFalse(view.has_image)
            self.assertTrue(view.video_player.source().isEmpty())
            self.assertFalse(view.video_play_button.isEnabled())
            view.set_video_preview(Path("remote.mp4"), image)
            self.assertTrue(view.has_image)
            view.set_image(DecodedImage(Path("photo.jpg"), image, 20, 10))
            self.assertFalse(view._is_video)
        finally:
            view.close()
            view.deleteLater()

    def workspace(self):
        view = Mock(_is_video=True)
        stack = Mock()
        stack.currentWidget.return_value = view
        return SimpleNamespace(
            closing=False, cloud_account_id="account", cloud_generation=2,
            _cloud_video_request_id=3, current_path=Path("remote.mp4"),
            stack=stack, full_view=view,
        )

    def result(self):
        return dict(account="account", generation=2, request_id=3,
                    key=Path("remote.mp4"), url="https://example.invalid/video", error=False)

    def test_stale_video_urls_are_discarded(self):
        for changes in [dict(account="other"), dict(generation=1), dict(request_id=1),
                        dict(key=Path("other.mp4"))]:
            with self.subTest(changes=changes):
                owner = self.workspace()
                Workspace._on_cloud_video_loaded(owner, self.result() | changes)
                owner.full_view.video_player.setSource.assert_not_called()
        for changes in [dict(closing=True), dict(current_path=None)]:
            owner = self.workspace()
            for key, value in changes.items():
                setattr(owner, key, value)
            Workspace._on_cloud_video_loaded(owner, self.result())
            owner.full_view.video_player.setSource.assert_not_called()

    def test_current_video_url_is_delivered_and_error_does_not_expose_url(self):
        owner = self.workspace()
        Workspace._on_cloud_video_loaded(owner, self.result())
        owner.full_view.video_player.setSource.assert_called_once_with(QUrl(self.result()["url"]))
        owner.full_view.video_play_button.setEnabled.assert_called_once_with(True)
        owner = self.workspace()
        Workspace._on_cloud_video_loaded(owner, self.result() | dict(error=True))
        owner.full_view.video_player.setSource.assert_not_called()
        self.assertNotIn("https:", owner.full_view.info_label.setText.call_args.args[0])

    def test_quick_copy_and_move_accept_cloud_video_without_local_file(self):
        path = Path("nonexistent-cloud-video.mp4")
        remote = Mock(name="video.mp4", is_dir=False)
        for move in [False, True]:
            owner = SimpleNamespace(cloud_account_id="account", cloud_items_by_key={path: remote},
                                    _download_cloud_items=Mock())
            Workspace._quick_transfer_to(owner, [path], Path("destination"), move, False)
            owner._download_cloud_items.assert_called_once_with([remote], Path("destination"), move=move)
