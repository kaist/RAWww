from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget

from rawww.app import FullView, MainWindow, Workspace


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


class _ToastHost(QMainWindow):
    _show_viewer_toast = Workspace._show_viewer_toast
    _clear_viewer_toast = Workspace._clear_viewer_toast


class _WindowShowRecorder(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.shown = []

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.shown.append((watched.metaObject().className(), watched.objectName()))
        return False


class AppStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_player_widgets_are_never_top_level_windows(self) -> None:
        parent = QWidget()
        view = FullView(parent)
        self.assertFalse(view.isWindow())
        self.assertIsNone(view.video_widget)
        self.assertIsNotNone(view.video_controls.parentWidget())
        self.assertFalse(view.video_controls.isWindow())
        video_widget = view._ensure_video_widget()
        self.assertIsNotNone(video_widget.parentWidget())
        self.assertFalse(video_widget.isWindow())
        view.close()
        view.deleteLater()
        parent.deleteLater()

    def test_workspace_is_constructed_as_a_child_widget(self) -> None:
        parent = QStackedWidget()
        workspace = Workspace(defer_initial_scan=True, parent=parent)
        self.assertFalse(workspace.isWindow())
        self.assertFalse(workspace.full_view.isWindow())
        self.assertFalse(workspace.full_view.video_controls.isWindow())
        self.assertIsNone(workspace.shotsync_login_dialog)
        workspace.close()
        workspace.deleteLater()
        parent.deleteLater()

    def test_startup_has_no_hidden_app_owned_top_level_windows(self) -> None:
        recorder = _WindowShowRecorder()
        self.app.installEventFilter(recorder)
        existing_native_windows = QGuiApplication.allWindows()
        try:
            window = MainWindow()
            top_level_names = {widget.objectName() for widget in QApplication.topLevelWidgets()}
            native_windows = [
                (native.metaObject().className(), native.objectName())
                for native in QGuiApplication.allWindows()
                if all(native is not existing for existing in existing_native_windows)
            ]

            self.assertNotIn("overlayLabel", top_level_names)
            self.assertNotIn("shotsyncLoginDialog", top_level_names)
            self.assertNotIn("codeSuggestionPopup", top_level_names)
            self.assertEqual(native_windows, [])
            self.assertEqual(recorder.shown, [])
            window.close()
            window.deleteLater()
        finally:
            self.app.removeEventFilter(recorder)

    def test_deleted_viewer_toast_is_not_reused(self) -> None:
        host = _ToastHost()
        host.setCentralWidget(QWidget())
        host._show_viewer_toast("Первый")
        first = host._viewer_toast
        first.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        self.assertIsNone(host._viewer_toast)
        host._show_viewer_toast("Второй")
        self.assertEqual(host._viewer_toast.text(), "Второй")
        host.close()

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
