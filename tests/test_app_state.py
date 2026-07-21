## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import signal
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMainWindow, QMenu, QStackedWidget, QWidget

from rawww.app import ChromeTabBar, FullView, MainWindow, Workspace, _application_settings, _format_remaining_time, _install_interrupt_shutdown, _plan_xmp_sidecar_relocation, _relocate_xmp_sidecars, _scan_directory, _scan_xmp_task
from rawww.hotkeys import FIXED_HOTKEYS
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

    def test_xmp_scan_skips_missing_sidecars_without_opening_each_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = [folder / f"photo-{index}.xmp" for index in range(2_000)]
            with patch("rawww.app.read_sidecar") as read:
                result = _scan_xmp_task(paths, {}, set(), False)

            self.assertEqual(result, [])
            read.assert_not_called()

    def test_full_xmp_scan_uses_directory_snapshot_for_missing_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = [folder / f"photo-{index}.xmp" for index in range(2_000)]
            known = {path.name: (0, 0, None) for path in paths}
            with patch("rawww.app.read_sidecar") as read:
                result = _scan_xmp_task(paths, known, set(), True)

            self.assertEqual(len(result), len(paths))
            self.assertTrue(all(not snapshot.exists for _path, snapshot in result))
            read.assert_not_called()

    def test_xmp_card_update_does_not_rebuild_unfiltered_view(self) -> None:
        host = SimpleNamespace(
            rating_filter=SimpleNamespace(currentData=lambda: None),
            color_filter=SimpleNamespace(currentIndex=lambda: 0),
            sort_combo=SimpleNamespace(currentData=lambda: "time"),
            search_edit=SimpleNamespace(text=lambda: ""),
        )

        self.assertFalse(Workspace._xmp_change_requires_view_rebuild(host, {"rating", "comment"}))
        host.rating_filter = SimpleNamespace(currentData=lambda: 5)
        self.assertTrue(Workspace._xmp_change_requires_view_rebuild(host, {"rating"}))

    def test_renaming_one_member_of_raw_jpeg_pair_keeps_and_copies_xmp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "IMG_1.CR3").write_bytes(b"raw")
            (folder / "IMG_1.JPG").write_bytes(b"jpeg")
            source = folder / "IMG_1.xmp"
            source.write_bytes(b"metadata")

            plan = _plan_xmp_sidecar_relocation(
                folder, {"IMG_1.CR3": "RENAMED.CR3", "IMG_1.JPG": "IMG_1.JPG"}
            )
            _relocate_xmp_sidecars(plan)

            self.assertEqual(source.read_bytes(), b"metadata")
            self.assertEqual((folder / "RENAMED.xmp").read_bytes(), b"metadata")

    def test_ctrl_c_schedules_normal_window_close(self) -> None:
        window = Mock()
        captured = {}

        def remember_handler(signal_number, handler) -> None:
            captured[signal_number] = handler

        with (
            patch("rawww.app.signal.signal", side_effect=remember_handler),
            patch("rawww.app.QTimer.singleShot") as single_shot,
        ):
            _install_interrupt_shutdown(self.app, window)
            captured[signal.SIGINT](None, None)

        single_shot.assert_called_once_with(0, window.close)
        self.app._interrupt_heartbeat.stop()

    def test_grid_filter_rebuild_keeps_surviving_cursor_and_selection(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        first = Path("/photos/first.jpg")
        second = Path("/photos/second.jpg")
        removed = Path("/photos/removed.jpg")

        def item(path: Path) -> QListWidgetItem:
            result = QListWidgetItem(path.name)
            result.setData(Qt.ItemDataRole.UserRole, str(path))
            return result

        old_items = {path: item(path) for path in (first, second, removed)}
        for old_item in old_items.values():
            workspace.grid.addItem(old_item)
        workspace.items_by_path = old_items
        workspace.grid.setCurrentItem(old_items[second])
        old_items[first].setSelected(True)
        old_items[second].setSelected(True)
        old_items[removed].setSelected(True)

        workspace._remember_view_context()
        workspace.workspace_active = True
        workspace._begin_view_context_restore()
        self.assertFalse(workspace.grid.updatesEnabled())
        workspace.grid.clear()
        new_items = {path: item(path) for path in (first, second)}
        for new_item in new_items.values():
            workspace.grid.addItem(new_item)
        workspace.items_by_path = new_items
        workspace._restore_pending_view_cursor()
        # Завершение пакетного наполнения после фильтра не является повторной
        # загрузкой папки и не должно затем сбрасывать курсор на первый файл.
        workspace._restore_folder_grid_context()

        self.assertIs(workspace.grid.currentItem(), new_items[second])
        self.assertTrue(new_items[first].isSelected())
        self.assertTrue(new_items[second].isSelected())
        self.assertTrue(workspace.grid.updatesEnabled())
        workspace.close()
        workspace.deleteLater()

    def test_ai_filters_reset_when_folder_lacks_ai_data(self) -> None:
        # Скрытый активный AI-фильтр не должен оставлять пустой список при
        # переходе в папку без соответствующих данных.
        workspace = Workspace(defer_initial_scan=True)
        workspace.photo_details = {"a.jpg": {"quality": {"quality": 5.0, "aesthetic": 5.0}}}
        workspace.quality_button._quality_slider.setValue(4)
        workspace._reset_unavailable_ai_filters()
        self.assertEqual(workspace.quality_button.quality_threshold(), 4)

        workspace.photo_details = {"b.jpg": {}}
        workspace._reset_unavailable_ai_filters()
        self.assertEqual(workspace.quality_button.quality_threshold(), 0)
        self.assertEqual(workspace.quality_button.aesthetic_threshold(), 0)
        workspace.close()
        workspace.deleteLater()

    def test_full_view_filter_rebuild_uses_open_file_as_single_cursor(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        old_grid_path = Path("/photos/old-grid.jpg")
        open_path = Path("/photos/open.jpg")

        old_grid_item = QListWidgetItem(old_grid_path.name)
        old_grid_item.setData(Qt.ItemDataRole.UserRole, str(old_grid_path))
        workspace.grid.addItem(old_grid_item)
        workspace.items_by_path = {old_grid_path: old_grid_item}
        workspace.grid.setCurrentItem(old_grid_item)
        workspace.stack.setCurrentWidget(workspace.full_view)
        workspace.current_path = open_path

        workspace._remember_view_context()
        workspace.grid.clear()
        new_items = {}
        for path in (old_grid_path, open_path):
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            workspace.grid.addItem(item)
            new_items[path] = item
        workspace.items_by_path = new_items
        workspace._restore_pending_view_cursor()

        self.assertIs(workspace.grid.currentItem(), new_items[open_path])
        self.assertEqual(workspace.grid.selectedItems(), [new_items[open_path]])
        workspace.close()
        workspace.deleteLater()

    def test_repeated_filter_rebuild_does_not_merge_transient_selection(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        original = Path("/photos/original.jpg")
        transient = Path("/photos/transient.jpg")

        items = {}
        for path in (original, transient):
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            workspace.grid.addItem(item)
            items[path] = item
        workspace.items_by_path = items
        workspace.grid.setCurrentItem(items[original])
        workspace._remember_view_context()

        workspace.grid.clearSelection()
        workspace.grid.setCurrentItem(items[transient])
        workspace._remember_view_context()
        workspace._restore_pending_view_cursor()

        self.assertIs(workspace.grid.currentItem(), items[original])
        self.assertEqual(workspace.grid.selectedItems(), [items[original]])
        workspace.close()
        workspace.deleteLater()

    def test_slow_filter_rebuild_reuses_delayed_folder_loader(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        path = Path("/photos/current.jpg")
        item = QListWidgetItem(path.name)
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        workspace.grid.addItem(item)
        workspace.items_by_path = {path: item}
        workspace.grid.setCurrentItem(item)
        workspace.workspace_active = True

        workspace._remember_view_context()
        workspace._begin_view_context_restore()

        self.assertTrue(workspace.grid_restore_loader_timer.isActive())
        self.assertTrue(workspace.grid_restore_loader.isHidden())
        workspace._show_grid_restore_loader_if_needed()
        self.assertFalse(workspace.grid_restore_loader.isHidden())
        self.assertEqual(workspace.grid_restore_loader_label.text(), "Обновляю список")

        workspace._restore_pending_view_cursor()

        self.assertTrue(workspace.grid_restore_loader.isHidden())
        self.assertFalse(workspace.grid_restore_loader_timer.isActive())
        workspace.close()
        workspace.deleteLater()

    def test_file_mutation_waits_for_running_decoder_and_shows_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), defer_initial_scan=True)
            path = Path(directory) / "busy.raw"
            path.touch()
            running = Future()
            self.assertTrue(running.set_running_or_notify_cancel())
            workspace.scheduler.pending[(path, 256)] = running
            operation = Mock()

            workspace._run_after_file_consumers_release(
                [path],
                operation,
                loading_text="Выполняется удаление",
            )
            self.app.processEvents()

            operation.assert_not_called()
            self.assertFalse(workspace.grid_restore_loader.isHidden())
            self.assertEqual(
                workspace.grid_restore_loader_label.text(),
                "Выполняется удаление",
            )

            running.set_result(None)
            QTest.qWait(60)

            operation.assert_called_once_with()
            self.assertTrue(workspace.grid_restore_loader.isHidden())
            workspace.close()
            workspace.deleteLater()

    def test_delete_waits_for_busy_photo_before_unlinking_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), defer_initial_scan=True)
            workspace.settings.setValue("behavior/delete_without_confirmation", True)
            path = Path(directory) / "busy.raw"
            path.touch()
            running = Future()
            self.assertTrue(running.set_running_or_notify_cancel())
            workspace.scheduler.pending[(path, 256)] = running

            workspace._delete_paths([path], permanent=True)
            self.app.processEvents()

            self.assertTrue(path.exists())
            self.assertEqual(
                workspace.grid_restore_loader_label.text(),
                "Выполняется удаление",
            )

            running.set_result(None)
            QTest.qWait(60)

            self.assertFalse(path.exists())
            self.assertTrue(workspace.grid_restore_loader.isHidden())
            workspace.close()
            workspace.deleteLater()

    def test_return_from_full_view_replaces_stale_grid_selection(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        old_path = Path("/photos/old.jpg")
        current_path = Path("/photos/current.jpg")
        items = {}
        for path in (old_path, current_path):
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            workspace.grid.addItem(item)
            items[path] = item
        workspace.items_by_path = items
        workspace.grid.setCurrentItem(items[old_path])
        workspace.stack.setCurrentWidget(workspace.full_view)
        workspace.current_path = current_path
        workspace.workspace_state.current_photo = current_path

        workspace._restore_grid_context()

        self.assertIs(workspace.grid.currentItem(), items[current_path])
        self.assertEqual(workspace.grid.selectedItems(), [items[current_path]])
        workspace.close()
        workspace.deleteLater()

    def test_face_search_loader_is_hidden_in_grid_and_full_view(self) -> None:
        host = SimpleNamespace(
            full_view=SimpleNamespace(set_face_search_loading=Mock()),
            grid_restore_loader_label=SimpleNamespace(setText=Mock()),
            _set_grid_restore_loader_visible=Mock(),
            _restoring_folder_grid_context=False,
        )

        Workspace._set_face_search_loading(host, True)
        Workspace._set_face_search_loading(host, False)

        self.assertEqual(
            host.full_view.set_face_search_loading.call_args_list,
            [call(True), call(False)],
        )
        self.assertEqual(
            host._set_grid_restore_loader_visible.call_args_list,
            [call(True), call(False)],
        )

    def test_ready_face_filter_immediately_populates_grid_and_strip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(defer_initial_scan=True)
            paths = [Path(directory) / f"photo-{index}.jpg" for index in range(4)]
            for path in paths:
                path.touch()
            workspace.workspace_active = True
            workspace.all_paths = paths
            workspace.photo_details = {path.name: {} for path in paths}
            workspace.face_reference = [1.0, 0.0]
            workspace._face_match_names = {paths[1].name, paths[3].name}

            workspace._apply_view()
            workspace.current_path = paths[1]
            workspace._refresh_full_view_navigation(paths[1])

            self.assertEqual(workspace.view_paths, [paths[1], paths[3]])
            self.assertEqual(workspace.grid.count(), 2)
            self.assertEqual(workspace.full_view.photo_strip.count(), 2)
            workspace.close()
            workspace.deleteLater()

    def test_face_filter_clear_reveals_grid_only_after_cursor_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(defer_initial_scan=True)
            paths = [Path(directory) / f"photo-{index}.jpg" for index in range(4)]
            for path in paths:
                path.touch()
            workspace.workspace_active = True
            workspace.cache_ready = True
            workspace.all_paths = paths
            workspace.photo_details = {path.name: {} for path in paths}
            workspace._apply_view()
            self.app.processEvents()
            workspace.grid.setCurrentItem(workspace.items_by_path[paths[2]])

            workspace.face_reference = [1.0, 0.0]
            workspace._face_match_names = {paths[1].name, paths[2].name}
            workspace._apply_view()
            self.assertFalse(workspace.grid.updatesEnabled())
            self.app.processEvents()
            self.assertIs(
                workspace.grid.currentItem(),
                workspace.items_by_path[paths[2]],
            )

            workspace._clear_face_search()

            self.assertFalse(workspace.grid.updatesEnabled())
            self.app.processEvents()
            self.assertTrue(workspace.grid.updatesEnabled())
            self.assertIs(
                workspace.grid.currentItem(),
                workspace.items_by_path[paths[2]],
            )
            workspace.close()
            workspace.deleteLater()

    def test_eyes_closed_flags_a_single_closed_face(self) -> None:
        detail = {"faces": [{"bbox": {"width": 0.4}, "eyes_open": 0.2}]}
        self.assertTrue(Workspace._eyes_closed(detail))

    def test_eyes_closed_ignores_a_single_open_face(self) -> None:
        detail = {"faces": [{"bbox": {"width": 0.4}, "eyes_open": 0.8}]}
        self.assertFalse(Workspace._eyes_closed(detail))

    def test_eyes_closed_when_largest_face_is_closed(self) -> None:
        detail = {
            "faces": [
                {"bbox": {"width": 0.5}, "eyes_open": 0.1},
                {"bbox": {"width": 0.2}, "eyes_open": 0.9},
            ]
        }
        self.assertTrue(Workspace._eyes_closed(detail))

    def test_eyes_closed_for_small_group_when_any_face_is_closed(self) -> None:
        detail = {
            "faces": [
                {"bbox": {"width": 0.5}, "eyes_open": 0.9},
                {"bbox": {"width": 0.3}, "eyes_open": 0.1},
            ]
        }
        self.assertTrue(Workspace._eyes_closed(detail))

    def test_eyes_open_in_a_large_group_when_only_a_small_face_is_closed(self) -> None:
        faces = [{"bbox": {"width": 0.5}, "eyes_open": 0.9}]
        faces += [{"bbox": {"width": 0.1}, "eyes_open": 0.9} for _ in range(3)]
        faces.append({"bbox": {"width": 0.1}, "eyes_open": 0.1})
        self.assertFalse(Workspace._eyes_closed({"faces": faces}))

    def test_eyes_closed_skips_faces_without_eye_state(self) -> None:
        detail = {"faces": [{"bbox": {"width": 0.4}}]}
        self.assertFalse(Workspace._eyes_closed(detail))

    def test_photo_face_uses_matching_saved_face_as_canonical_reference(self) -> None:
        saved = {"embedding": [1.0, 0.0], "avatar": ""}
        host = SimpleNamespace(
            face_sets=[saved],
            _face_similarity=Workspace._face_similarity,
            _face_avatar_from_entry=Mock(return_value=None),
            _current_face_avatar=Mock(return_value=None),
            _set_face_reference=Mock(),
        )

        Workspace._filter_face_from_full_view(
            host,
            {"embedding": [0.95, 0.05]},
        )

        host._set_face_reference.assert_called_once_with(
            saved["embedding"],
            None,
            show_loading=True,
        )

    def test_restored_face_filter_waits_for_folder_cache(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        workspace.face_reference = [1.0, 0.0]
        workspace.cache_ready = False
        workspace.photo_details = {}

        workspace._apply_face_search_view()

        self.assertIsNone(workspace._face_search_index)
        self.assertIsNone(workspace._face_search_future)
        self.assertIsNone(workspace._face_match_names)
        workspace.close()
        workspace.deleteLater()

    def test_remaining_time_format_is_compact(self) -> None:
        self.assertEqual(_format_remaining_time(12.4), "≈ 12 с")
        self.assertEqual(_format_remaining_time(75), "≈ 1 мин 15 с")
        self.assertEqual(_format_remaining_time(7_250), "≈ 2 ч 0 мин")

    def test_cancel_ai_analysis_stops_pipeline_and_auto_restart(self) -> None:
        pipeline = Mock()
        pipeline.pending_count.return_value = 1
        workspace = SimpleNamespace(
            _ai_pipeline=pipeline,
            current_dir=Path("photos"),
            ai_progress_timer=Mock(),
            _ai_progress_started_at=1.0,
            _ai_requested_generation=4,
            _cache_ai_waiting=True,
            _cache_ai_paths={Path("photos/a.jpg")},
            _auto_ai_generation=-1,
            view_generation=4,
            ai_analysis_available=True,
            _refresh_status_panel=Mock(),
        )

        Workspace._cancel_ai_analysis(workspace)

        pipeline.shutdown.assert_called_once_with()
        workspace.ai_progress_timer.stop.assert_called_once_with()
        self.assertIsNone(workspace._ai_pipeline)
        self.assertIsNone(workspace._ai_progress_started_at)
        self.assertFalse(workspace._cache_ai_waiting)
        self.assertEqual(workspace._cache_ai_paths, set())
        self.assertEqual(workspace._auto_ai_generation, 4)

    def test_close_button_does_not_activate_tab_before_requesting_close(self) -> None:
        tabs = ChromeTabBar()
        tabs.addTab("Первая")
        tabs.addTab("Вторая")
        tabs.setCurrentIndex(0)
        closed = []
        tabs.closeRequested.connect(closed.append)
        tabs.resize(440, 38)
        tabs.show()
        self.app.processEvents()

        QTest.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs._close_rect(1).center())

        self.assertEqual(tabs.currentIndex(), 0)
        self.assertEqual(closed, [1])
        tabs.close()
        tabs.deleteLater()

    def test_fixed_hotkeys_include_workspace_navigation(self) -> None:
        self.assertIn(("Следующая вкладка", "Ctrl+Right"), FIXED_HOTKEYS)
        self.assertIn(("Предыдущая вкладка", "Ctrl+Left"), FIXED_HOTKEYS)

    def test_folder_context_menu_opens_a_separate_tab_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), defer_initial_scan=True)
            menu = QMenu()
            opened = []
            workspace.openFolderRequested.connect(opened.append)

            workspace._populate_folder_context_menu(menu, Path(directory))

            actions = menu.actions()
            self.assertEqual(actions[0].text(), "Открыть в новой вкладке")
            self.assertTrue(actions[1].isSeparator())
            self.assertEqual(
                [action.text() for action in actions[2:]],
                ["Создать папку", "Переименовать", "Удалить"],
            )
            actions[0].trigger()
            self.assertEqual(opened, [Path(directory)])
            workspace.close()
            workspace.deleteLater()
            menu.deleteLater()

    def test_open_folder_from_context_menu_reuses_existing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow()
            window._open_folder_tab(Path(directory))
            count_after_first_open = window.tabs.count()

            window._open_folder_tab(Path(directory))

            self.assertEqual(window.tabs.count(), count_after_first_open)
            self.assertEqual(window.workspace_stack.currentWidget().current_dir, Path(directory))
            window.close()
            window.deleteLater()

    @unittest.skipUnless(os.name == "nt", "Системное меню Проводника есть только в Windows")
    def test_photo_context_menu_uses_windows_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "photo.jpg"
            photo.touch()
            workspace = Workspace(Path(directory), defer_initial_scan=True)

            with patch("rawww.app.show_file_context_menu") as show_menu:
                workspace._show_grid_context_menu(photo, QPoint(17, 23))

            show_menu.assert_called_once()
            arguments = show_menu.call_args.args
            self.assertEqual(arguments[0], photo)
            self.assertEqual(arguments[2:], (17, 23))
            workspace.close()
            workspace.deleteLater()

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

    def test_directory_scan_defers_read_access_check_until_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            photo = folder / "photo.jpg"
            photo.write_bytes(b"image")

            with patch.object(Path, "open", side_effect=PermissionError):
                self.assertEqual(_scan_directory(folder), [photo])

    def test_rename_uses_one_pass_when_names_do_not_intersect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "first.jpg").write_bytes(b"first")
            (folder / "second.jpg").write_bytes(b"second")
            workspace = SimpleNamespace(
                current_dir=folder,
                _rename_step_count=Workspace._rename_step_count,
            )

            Workspace._rename_files_safely(
                workspace,
                {"first.jpg": "new-first.jpg", "second.jpg": "new-second.jpg"},
            )

            self.assertEqual((folder / "new-first.jpg").read_bytes(), b"first")
            self.assertEqual((folder / "new-second.jpg").read_bytes(), b"second")

    def test_rename_preserves_files_when_names_are_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "first.jpg").write_bytes(b"first")
            (folder / "second.jpg").write_bytes(b"second")
            workspace = SimpleNamespace(
                current_dir=folder,
                _rename_step_count=Workspace._rename_step_count,
            )

            Workspace._rename_files_safely(
                workspace,
                {"first.jpg": "second.jpg", "second.jpg": "first.jpg"},
            )

            self.assertEqual((folder / "first.jpg").read_bytes(), b"second")
            self.assertEqual((folder / "second.jpg").read_bytes(), b"first")

    def test_full_navigation_reuses_snapshot_until_view_changes(self) -> None:
        paths = [Path(f"/photos/{index}.jpg") for index in range(4_000)]
        workspace = SimpleNamespace(
            view_generation=7,
            view_paths=paths,
            series_toggle=SimpleNamespace(isChecked=lambda: False),
            series_cards={},
            _full_navigation_generation=-1,
            _full_navigation_paths=[],
            _full_navigation_indices={},
            _full_navigation_series={},
            _full_navigation_cards={},
        )

        with patch.object(Path, "is_file", return_value=True):
            first = Workspace._full_navigation_snapshot(workspace)
            second = Workspace._full_navigation_snapshot(workspace)

        self.assertTrue(first[-1])
        self.assertFalse(second[-1])
        self.assertIs(first[0], second[0])
        self.assertEqual(second[1][paths[-1]], len(paths) - 1)

    def test_directory_card_is_never_sent_to_image_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scheduler = SimpleNamespace(submit_decode=Mock())
            workspace = SimpleNamespace(closing=False, scheduler=scheduler)

            Workspace._submit_decode(
                workspace,
                Path(temporary),
                256,
                full_priority=False,
            )

            scheduler.submit_decode.assert_not_called()

    def test_hidden_workspace_retires_preview_and_ai_work(self) -> None:
        timer = lambda: SimpleNamespace(stop=Mock(), start=Mock())
        ai = SimpleNamespace(pending_count=Mock(return_value=1), shutdown=Mock())
        scheduler = SimpleNamespace(abandon_preview_decode_work=Mock(), cancel_pending=Mock())
        workspace = SimpleNamespace(
            workspace_active=True,
            video_thumbnailer=SimpleNamespace(set_active=Mock()),
            populate_timer=timer(),
            thumb_timer=timer(),
            visible_thumb_timer=timer(),
            grid_full_request_timer=timer(),
            full_request_timer=timer(),
            ai_progress_timer=timer(),
            pending_full_request=Path("/photos/a.jpg"),
            pending_grid_full_request=Path("/photos/a.jpg"),
            scheduler=scheduler,
            _ai_pipeline=ai,
            _resume_ai_when_active=False,
            full_view=SimpleNamespace(video_player=SimpleNamespace(pause=Mock())),
        )

        Workspace.set_workspace_active(workspace, False)

        scheduler.abandon_preview_decode_work.assert_called_once_with()
        scheduler.cancel_pending.assert_called_once_with()
        ai.shutdown.assert_called_once_with()
        self.assertIsNone(workspace._ai_pipeline)
        self.assertTrue(workspace._resume_ai_when_active)

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

    def test_ai_queue_notifies_until_previews_are_ready(self) -> None:
        workspace = SimpleNamespace(
            closing=False,
            cache_ready=True,
            folder_cache=object(),
            _previews_ready_for_manual_ai=lambda: False,
            view_generation=7,
            _ai_requested_generation=-1,
            ai_analysis_available=True,
            _show_viewer_toast=Mock(),
            _refresh_status_panel=Mock(),
        )

        Workspace._start_ai_analysis(workspace)

        self.assertEqual(workspace._ai_requested_generation, 7)
        self.assertFalse(workspace.ai_analysis_available)
        workspace._show_viewer_toast.assert_called_once_with("AI-анализ поставлен в очередь")
        workspace._refresh_status_panel.assert_called_once_with()

    def test_manual_ai_can_restart_without_pending_cache_paths(self) -> None:
        launch = Mock()
        workspace = SimpleNamespace(
            closing=False,
            cache_ready=True,
            folder_cache=object(),
            _previews_ready_for_manual_ai=lambda: True,
            _launch_ai_analysis=launch,
        )

        Workspace._start_ai_analysis(workspace)

        launch.assert_called_once_with()

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
