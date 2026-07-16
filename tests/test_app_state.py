## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QSettings, Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget

from rawww.app import FullView, MainWindow, Workspace, _application_settings
from rawww.theme import apply_theme


class _Settings:
    """Минимальная память настроек для тестов без настоящего QSettings."""

    def __init__(self) -> None:
        self.values = []

    def setValue(self, key: str, value: object) -> None:
        self.values.append((key, value))


class _Signal:
    """Простая запись подключённых обработчиков вместо сигнала Qt."""

    def __init__(self) -> None:
        self.values = []

    def emit(self, value: object) -> None:
        self.values.append(value)


class _ToastHost(QMainWindow):
    """Тестовое окно, на котором проверяется размещение уведомлений."""

    _show_viewer_toast = Workspace._show_viewer_toast
    _clear_viewer_toast = Workspace._clear_viewer_toast


class _WindowShowRecorder(QObject):
    """Запоминает показ нативных окон во время тестового запуска."""

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
    """Проверяет восстановление и изменение состояния интерфейса приложения."""

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

    def test_portable_settings_use_an_ini_file_in_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_directory = Path(directory)
            with (
                patch("rawww.app.PORTABLE", True),
                patch("rawww.app.work_path", return_value=work_directory),
            ):
                settings = _application_settings()
                settings.setValue("portable-test", "saved")
                settings.sync()

            settings_file = work_directory / "settings" / "ctrlka.ini"
            self.assertTrue(settings_file.is_file())
            reloaded = QSettings(
                str(settings_file),
                QSettings.Format.IniFormat,
            )
            self.assertEqual(reloaded.value("portable-test"), "saved")

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

    def test_single_photo_preview_does_not_create_tab_and_g_opens_folder(self) -> None:
        """Файл из проводника показывается временно, пока пользователь не нажмёт G."""
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "single.jpg"
            photo.touch()
            window = MainWindow()
            initial_tab_count = window.tabs.count()

            window._present_single_photo(photo)
            preview = window._single_photo_workspace

            self.assertIsNotNone(preview)
            self.assertEqual(window.tabs.count(), initial_tab_count)
            self.assertEqual(window.workspace_stack.currentWidget(), preview)
            self.assertTrue(preview.single_photo_mode)

            window._open_single_photo_folder(preview)

            self.assertIsNone(window._single_photo_workspace)
            self.assertEqual(window.tabs.count(), initial_tab_count + 1)
            workspace = window.workspace_stack.currentWidget()
            self.assertIsInstance(workspace, Workspace)
            self.assertEqual(workspace.current_dir, photo.parent)
            window.close()
            window.deleteLater()

    def test_single_photo_escape_restores_existing_workspace_without_tab(self) -> None:
        """Esc закрывает временный просмотр и не меняет сохранённый набор вкладок."""
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "single.jpg"
            photo.touch()
            window = MainWindow()
            original = window.workspace_stack.currentWidget()
            initial_tab_count = window.tabs.count()

            window._present_single_photo(photo)
            preview = window._single_photo_workspace
            window._exit_single_photo(preview)

            self.assertIsNone(window._single_photo_workspace)
            self.assertEqual(window.tabs.count(), initial_tab_count)
            self.assertEqual(window.workspace_stack.currentWidget(), original)
            window.close()
            window.deleteLater()

    def test_external_request_restores_minimized_window_before_opening_target(self) -> None:
        """Внешнее открытие не оставляет свёрнутую Контрольку на панели задач."""
        calls: list[str] = []

        class _Window:
            def windowState(self):  # noqa: N802
                return Qt.WindowState.WindowMinimized | Qt.WindowState.WindowMaximized

            def showMaximized(self) -> None:  # noqa: N802
                calls.append("maximized")

            def show(self) -> None:
                calls.append("show")

            def raise_(self) -> None:
                calls.append("raise")

            def activateWindow(self) -> None:  # noqa: N802
                calls.append("activate")

        MainWindow._restore_and_activate(_Window())

        self.assertEqual(calls, ["maximized", "show", "raise", "activate"])

    def test_external_request_activates_normal_window_without_changing_its_state(self) -> None:
        """Запрос Проводника активирует только главное окно, не меняя его режим."""
        calls: list[str] = []

        class _Window:
            def windowState(self):  # noqa: N802
                return Qt.WindowState.WindowNoState

            def show(self) -> None:
                calls.append("show")

            def raise_(self) -> None:
                calls.append("raise")

            def activateWindow(self) -> None:  # noqa: N802
                calls.append("activate")

        MainWindow._restore_and_activate(_Window())

        self.assertEqual(calls, ["show", "raise", "activate"])

    def test_external_folder_is_prepared_before_window_is_activated(self) -> None:
        """Проводник показывает уже открытую папку, а не прежнее содержимое окна."""
        calls: list[str] = []

        class _Window:
            def _open_folder_tab(self, _target: Path) -> None:
                calls.append("open")

            def _restore_and_activate(self) -> None:
                calls.append("activate")

        with tempfile.TemporaryDirectory() as directory:
            MainWindow.open_external_target(_Window(), Path(directory))

        self.assertEqual(calls, ["open", "activate"])

    def test_external_file_does_not_restore_minimized_state_after_preview(self) -> None:
        """Закрытие файла из Проводника не должно снова сворачивать окно."""
        calls: list[tuple[str, bool] | str] = []

        class _Window:
            def _present_single_photo(self, _target: Path, *, preserve_window_state_on_exit: bool) -> None:
                calls.append(("open", preserve_window_state_on_exit))

            def _restore_and_activate(self) -> None:
                calls.append("activate")

        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "photo.jpg"
            photo.touch()
            MainWindow.open_external_target(_Window(), photo)

        self.assertEqual(calls, [("open", False), "activate"])

    def test_external_request_selects_existing_folder_tab(self) -> None:
        """Повторное открытие папки из Проводника не создаёт дубликат вкладки."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            window = MainWindow()
            window._open_folder_tab(folder, defer_initial_scan=True)
            tab_count = window.tabs.count()

            equivalent_folder = folder / ".." / folder.name
            with patch.object(window, "_restore_and_activate"):
                window.open_external_target(equivalent_folder)

            self.assertEqual(window.tabs.count(), tab_count)
            workspace = window.workspace_stack.currentWidget()
            self.assertIsInstance(workspace, Workspace)
            self.assertEqual(workspace.current_dir, folder)
            window.close()
            window.deleteLater()

    def test_initial_single_photo_has_no_tab_and_escape_closes_window(self) -> None:
        """Первый запуск с файлом не восстанавливает вкладку и завершается по Esc."""
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "single.jpg"
            photo.touch()
            window = MainWindow(photo)
            preview = window._single_photo_workspace

            self.assertIsNotNone(preview)
            self.assertEqual(window.tabs.count(), 0)
            with patch.object(window, "close") as close:
                window._exit_single_photo(preview)
            close.assert_called_once()
            window.deleteLater()

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

    def test_filter_dropdowns_show_short_lists_without_scrollbars(self) -> None:
        apply_theme(self.app)
        window = MainWindow()
        window.show()
        self.app.processEvents()
        workspace = window.workspace_stack.currentWidget()
        self.assertIsInstance(workspace, Workspace)

        for combo in (
            workspace.rating_filter,
            workspace.color_filter,
            workspace.media_filter,
            workspace.file_type_filter,
            workspace.shot_filter,
            workspace.sort_combo,
        ):
            combo.showPopup()
            self.app.processEvents()
            self.assertEqual(
                combo.view().verticalScrollBar().maximum(),
                0,
                combo.currentText(),
            )
            self.assertEqual(
                combo.view().font().pixelSize(),
                combo.font().pixelSize(),
                combo.currentText(),
            )
            self.assertEqual(
                combo.view().palette().color(QPalette.ColorRole.Text),
                combo.palette().color(QPalette.ColorRole.Text),
                combo.currentText(),
            )
            self.assertEqual(
                combo.view().palette().color(QPalette.ColorRole.Base).name(),
                "#484848",
                combo.currentText(),
            )
            self.assertEqual(
                combo.view().palette().color(QPalette.ColorRole.Highlight).name(),
                "#606060",
                combo.currentText(),
            )
            combo.hidePopup()

        window.close()
        window.deleteLater()

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
