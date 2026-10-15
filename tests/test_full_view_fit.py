## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет стабильное качество большого кадра с первой отрисовки FullView."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from concurrent.futures import Future

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QSize, Qt, QRect
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rawww.app import FullView, Workspace, THUMB_SIZE
from rawww.imaging import DecodedImage


class FullViewFitTests(unittest.TestCase):
    """Сравнивает первые и установившиеся пиксели, отдельно от fallback 256."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        source = Image.effect_noise((1600, 1100), 80).convert("RGB")
        cls.image = QImage(source.tobytes(), source.width, source.height, source.width * 3,
                           QImage.Format.Format_RGB888).copy()

    def setUp(self):
        self.view = FullView()
        self.view.resize(900, 700)
        self.frame = DecodedImage(Path("large-frame.jpg"), self.image, self.image.width(), self.image.height())

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def test_large_frame_does_not_change_fit_quality_after_idle(self):
        self.view.set_image(self.frame, fallback=False)
        target = QSize(503, 347)
        first = self.view.image_view._fitted_for(target).toImage()
        QTest.qWait(180)
        settled = self.view.image_view._fitted_for(target).toImage()
        self.assertEqual(first, settled)

    def test_first_large_frame_uses_smooth_scaling(self):
        self.view.set_image(self.frame, fallback=False)
        target = QSize(503, 347)
        with patch.object(self.view.image_view, "devicePixelRatioF", return_value=1.0):
            first = self.view.image_view._fitted_for(target)
        expected = self.view._pixmap.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
        self.assertEqual(first.toImage(), expected.toImage())

    def test_fullscreen_transition_keeps_smooth_fit(self):
        self.view.set_image(self.frame, fallback=False)
        self.view.begin_fast_resize()
        self.assertTrue(self.view.image_view._smooth)
        self.view.finish_fast_resize()
        self.assertTrue(self.view.image_view._smooth)

    def test_new_frame_does_not_replace_requested_original_zoom(self):
        self.view.set_image(self.frame)
        self.view.image_view.request_zoom(None, temporary=False)
        with patch.object(self.view.image_view, "set_pixmap") as set_pixmap:
            self.view.set_image(self.frame)
        set_pixmap.assert_not_called()

    def test_repeated_cached_frame_preserves_pixmap_and_fitted_cache(self):
        self.view.set_image(self.frame)
        first = self.view.image_view._fitted_for(QSize(503, 347))
        pixmap_key = self.view._pixmap.cacheKey()
        self.view.set_image(self.frame)
        second = self.view.image_view._fitted_for(QSize(503, 347))
        self.assertEqual(self.view._pixmap.cacheKey(), pixmap_key)
        self.assertEqual(first.cacheKey(), second.cacheKey())

    def test_changed_image_replaces_reused_pixmap(self):
        self.view.set_image(self.frame)
        key = self.view._pixmap.cacheKey()
        changed = self.image.copy()
        changed.fill(Qt.GlobalColor.red)
        self.view.set_image(DecodedImage(self.frame.path, changed, changed.width(), changed.height()))
        self.assertNotEqual(self.view._pixmap.cacheKey(), key)


class FullPreviewSizeTests(unittest.TestCase):
    """Проверяет достаточность физического разрешения и приоритет показа кадра."""

    def workspace(self, width=171, height=256, dpr=1.5):
        preview = QImage(width, height, QImage.Format.Format_RGB888) if width else None
        return SimpleNamespace(
            current_path=Path("portrait.jpg"),
            screen=lambda: SimpleNamespace(geometry=lambda: QRect(0, 0, 1920, 1200), devicePixelRatio=lambda: dpr),
            _thumbnail_cache_get=lambda path: preview,
            _cache_get=lambda key: None,
        )

    def test_portrait_uses_screen_height_and_physical_pixels(self):
        workspace = self.workspace()
        self.assertEqual(Workspace._full_preview_size(workspace), 1856)
        self.assertGreaterEqual(1856, 1200 * 1.5)

    def test_landscape_square_and_unknown_aspect_remain_sufficient(self):
        for width, height in ((256, 171), (256, 256), (256, 85)):
            with self.subTest(width=width, height=height):
                workspace = self.workspace(width, height)
                size = Workspace._full_preview_size(workspace)
                fitted = QSize(width, height).scaled(QSize(1920, 1200), Qt.AspectRatioMode.KeepAspectRatio)
                self.assertGreaterEqual(size, max(fitted.width(), fitted.height()) * 1.5)
                self.assertLessEqual(size, 2880)
        self.assertEqual(Workspace._full_preview_size(self.workspace(width=0)), 2880)
        self.assertEqual(Workspace._full_preview_size(self.workspace(dpr=2)), 2432)

    def test_neighbor_preloads_use_each_frames_own_size(self):
        portrait, landscape = Path("p.jpg"), Path("l.jpg")
        workspace = SimpleNamespace(
            _full_navigation_context=lambda path: ([portrait, landscape], 0),
            last_move_direction=1,
            _full_preview_size=lambda path: 1856 if path == portrait else 2752,
            _submit_decode=Mock(),
        )
        Workspace._preload_neighbors(workspace, portrait)
        workspace._submit_decode.assert_called_once_with(landscape, 2752, full_priority=True)

    def test_larger_neighbor_work_is_kept_when_required_size_decreases(self):
        current, neighbor = Path("current.jpg"), Path("neighbor.jpg")
        larger, smaller = Future(), Future()
        workspace = SimpleNamespace(
            _full_navigation_context=lambda path: ([current, neighbor], 0),
            pending={(neighbor, 2880): larger, (neighbor, 1024): smaller},
            foreground_full_futures={},
            _full_preview_size=lambda path: 1856,
        )
        Workspace._cancel_outdated_full_tasks(workspace, current, 1856)
        self.assertFalse(larger.cancelled())
        self.assertTrue(smaller.cancelled())

    def test_current_decode_starts_before_navigation_widgets(self):
        path, full_view = Path("current.jpg"), object()
        calls = []
        workspace = SimpleNamespace(
            closing=False, current_path=path, full_view=full_view,
            stack=SimpleNamespace(currentWidget=lambda: full_view),
            _suspend_thumbnail_work=lambda: None,
            _cancel_outdated_full_tasks=lambda *args: None,
            _full_preview_size=lambda: 1856,
            _series_for_path=lambda path: [],
            _promote_current_full_task=lambda *args: None,
            _submit_decode=lambda *args, **kwargs: calls.append("decode"),
            _preload_neighbors=lambda path: None,
            _refresh_full_view_navigation=lambda path: calls.append("navigation"),
        )
        Workspace._finish_open_full(workspace, path, False)
        self.assertEqual(calls, ["decode", "navigation"])


if __name__ == "__main__":
    unittest.main()
