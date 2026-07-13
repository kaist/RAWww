from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rawww.app import FullView, Workspace


class _Settings:
    def __init__(self) -> None:
        self.values = []

    def setValue(self, key: str, value: object) -> None:
        self.values.append((key, value))


class _Signal:
    def __init__(self) -> None:
        self.values = []

    def emit(self, value: object) -> None:
        self.values.append(value)


class AppStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_video_widget_is_never_a_top_level_window(self) -> None:
        view = FullView()
        self.assertIsNotNone(view.video_widget.parentWidget())
        self.assertFalse(view.video_widget.isWindow())
        view.close()
        view.deleteLater()

    def test_ai_waits_until_cached_previews_reach_the_ui(self) -> None:
        first = Path("/photos/first.jpg")
        second = Path("/photos/second.jpg")
        workspace = SimpleNamespace(
            workspace_active=True,
            cache_ready=True,
            folder_cache=object(),
            _cache_ai_paths={first, second},
            view_paths=[first, second],
            paths=[first, second],
            populate_index=2,
            preview_paths={first, second},
            preview_finished_paths={first},
        )

        self.assertFalse(Workspace._previews_ready_for_ai(workspace))
        workspace.preview_finished_paths.add(second)
        self.assertTrue(Workspace._previews_ready_for_ai(workspace))

    def test_series_mode_is_saved_globally(self) -> None:
        settings = _Settings()
        changed = _Signal()
        workspace = SimpleNamespace(
            settings=settings,
            _apply_view=lambda: None,
            seriesModeChanged=changed,
            _show_viewer_toast=lambda _message: None,
        )

        Workspace._series_toggle_changed(workspace, False)

        self.assertEqual(settings.values, [("view/series_enabled", False)])
        self.assertEqual(changed.values, [False])
