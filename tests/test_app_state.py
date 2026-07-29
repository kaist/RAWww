## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import signal
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMainWindow, QMenu, QStackedWidget, QVBoxLayout, QWidget

from rawww.app import ChromeTabBar, FullView, MainWindow, VideoThumbnailer, Workspace, _application_settings, _delete_materialized_burst_files, _drive_key, _install_interrupt_shutdown, _plan_xmp_sidecar_relocation, _relocate_xmp_sidecars, _scan_directory, _scan_xmp_task
from rawww.widgets import format_remaining_time
from rawww.canon_burst import BurstFrame
from rawww.hotkeys import FIXED_HOTKEYS
from rawww.theme import apply_theme


class _Settings:
    """Минимальная память настроек для тестов без настоящего QSettings."""

    def __init__(self) -> None:
        self.values = []

    def setValue(self, key: str, value: object) -> None:
        self.values.append((key, value))


class _MemorySettings:
    """Настройки в памяти с чтением и записью, как у QSettings, для тестов."""

    def __init__(self, initial: dict | None = None) -> None:
        self.store = dict(initial or {})

    def contains(self, key: str) -> bool:
        return key in self.store

    def value(self, key: str, default: object = None, type: object = None) -> object:
        return self.store.get(key, default)

    def setValue(self, key: str, value: object) -> None:
        self.store[key] = value


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

    def test_full_view_delete_shortcut_reports_shift_modifier(self) -> None:
        parent = QWidget()
        view = FullView(parent)
        requested = []
        view.deleteRequested.connect(requested.append)
        view.show()
        view.setFocus()

        QTest.keyClick(view, Qt.Key.Key_Delete, Qt.KeyboardModifier.ShiftModifier)

        self.assertEqual(requested, [True])
        view.deleteLater()
        parent.deleteLater()

    def test_full_view_burst_extract_shortcut(self) -> None:
        parent = QWidget()
        view = FullView(parent)
        requested = []
        view.burstExtractRequested.connect(lambda: requested.append(True))
        layout = QVBoxLayout(parent)
        layout.addWidget(view)
        parent.resize(800, 600)
        parent.show()
        view.show()
        view.set_burst_extract_state(visible=True, extracted=False)
        view.image_view.setFocus()
        QApplication.processEvents()

        QTest.keyClick(view.image_view, Qt.Key.Key_X)

        self.assertEqual(requested, [True])
        view.set_burst_extract_state(visible=True, extracted=True)
        self.assertTrue(view.burst_extract_button.isEnabled())
        QTest.keyClick(view.image_view, Qt.Key.Key_X)
        self.assertEqual(requested, [True, True])
        QTest.mouseClick(view.burst_extract_button, Qt.MouseButton.LeftButton)
        self.assertEqual(requested, [True, True, True])
        view.deleteLater()
        parent.deleteLater()

    def test_repeated_burst_extract_removes_owned_file(self) -> None:
        frame = BurstFrame(Path("/photos/roll.cr3"), 0, 3)
        target = Path("/photos/roll_001.cr3")
        host = SimpleNamespace(
            current_path=frame,
            burst_materialized={frame: target},
            _burst_removing=set(),
            full_view=SimpleNamespace(
                burst_extract_button=Mock(),
                burst_extract_action=Mock(),
            ),
            _remove_materialized_burst=Mock(),
        )

        Workspace._extract_current_burst(host)

        host._remove_materialized_burst.assert_called_once_with(
            frame, target, preserve_selection=True,
        )
        host.full_view.burst_extract_button.setEnabled.assert_called_once_with(False)
        host.full_view.burst_extract_action.setEnabled.assert_called_once_with(False)

    def test_active_burst_mutation_does_not_reload_folder_from_watcher(self) -> None:
        frame = BurstFrame(Path("/photos/roll.cr3"), 0, 3)
        host = SimpleNamespace(
            _selection_progress=None,
            _upload_progress=None,
            _file_mutation_waiting=False,
            _burst_pending_changes={frame: {}},
            _burst_removing=set(),
            _ignore_folder_changes_until=0.0,
            closing=False,
            current_dir=frame.source.parent,
            folder_change_timer=Mock(),
        )

        Workspace._folder_changed(host, str(frame.source.parent))

        host.folder_change_timer.start.assert_not_called()

    def test_explicit_burst_removal_preserves_virtual_selection(self) -> None:
        frame = BurstFrame(Path("/photos/roll.cr3"), 0, 3)
        target = Path("/photos/roll_001.cr3")
        future = Future()
        future.set_result(target)
        host = SimpleNamespace(
            _burst_removing={frame},
            _burst_removal_preserves_selection={frame},
            closing=False,
            current_dir=frame.source.parent,
            current_path=frame,
            burst_materialized={frame: target},
            all_paths=[frame.source, target],
            _cache_ai_paths={target},
            preview_finished_paths={target},
            decode_cache=SimpleNamespace(remove_path=Mock()),
            photo_details={frame.name: {"rating": 4}, target.name: {"rating": 4}},
            folder_cache=None,
            cache_ready=False,
            full_view=SimpleNamespace(set_burst_extract_state=Mock()),
            workspace_state=SimpleNamespace(current_photo=None),
            _suppress_own_folder_refresh=Mock(),
            _refresh_full_view_navigation=Mock(),
            _apply_view=Mock(),
        )

        Workspace._on_burst_removed(host, frame, target, future)

        self.assertEqual(host.photo_details[frame.name], {"rating": 4})
        self.assertNotIn(target.name, host.photo_details)
        self.assertNotIn(frame, host.burst_materialized)
        self.assertEqual(host.current_path, frame)
        self.assertEqual(host.workspace_state.current_photo, frame)
        host._refresh_full_view_navigation.assert_called_once_with(frame)
        host.full_view.set_burst_extract_state.assert_called_once_with(
            visible=True, extracted=False,
        )

    def test_burst_toggle_deletes_raw_and_sidecar_permanently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "roll_001.cr3"
            sidecar = target.with_suffix(".xmp")
            target.write_bytes(b"raw")
            sidecar.write_bytes(b"xmp")

            _delete_materialized_burst_files(target)

            self.assertFalse(target.exists())
            self.assertFalse(sidecar.exists())

    def test_vertical_series_panel_follows_portrait_strip_width(self) -> None:
        parent = QWidget()
        view = FullView(parent)

        landscape_width = view.series_panel.width()
        view.set_vertical(True)

        self.assertEqual(view.series_panel.width(), view.series_strip.width() + 2)
        self.assertLess(view.series_panel.width(), landscape_width)
        view.deleteLater()
        parent.deleteLater()

    def test_long_vertical_series_grid_fits_viewport_without_side_gap(self) -> None:
        parent = QWidget()
        view = FullView(parent)
        layout = QVBoxLayout(parent)
        layout.addWidget(view)
        parent.resize(900, 600)
        view.set_vertical(True)
        paths = [Path(f"/photos/frame-{index:03d}.cr3") for index in range(64)]
        view.set_navigation(paths, paths[0], {}, {}, paths, 1)
        parent.show()
        QApplication.processEvents()

        self.assertTrue(view.series_strip.verticalScrollBar().isVisible())
        self.assertEqual(
            view.series_strip.viewport().width(),
            view.series_strip.gridSize().width(),
        )
        self.assertLess(view.series_up.width(), view.series_strip.width())
        self.assertEqual(view.series_up.width(), view.series_down.width())
        self.assertEqual(view.series_up.geometry().center().x(), view.series_panel.rect().center().x())
        self.assertEqual(view.series_down.geometry().center().x(), view.series_panel.rect().center().x())
        view.deleteLater()
        parent.deleteLater()

    def test_burst_series_keeps_virtual_objects_in_grid_items(self) -> None:
        source = Path("/photos/roll.cr3")
        frames = tuple(BurstFrame(source, index, 3) for index in range(3))
        host = SimpleNamespace(
            series_toggle=SimpleNamespace(isChecked=lambda: True),
            burst_frames={source: frames},
            expanded_series={source},
            burst_materialized={},
            series_cards={},
            _embedding_similarity=lambda _left, _right: -1.0,
        )

        paths = Workspace._grid_paths_with_series(host, [source])
        self.assertEqual(paths, [source, *frames])
        self.assertTrue(host.series_cards[frames[1]]["member"])

    def test_video_thumbnail_cancel_clears_media_source_and_timeout(self) -> None:
        thumbnailer = VideoThumbnailer()
        path = Path("/photos/preview.mp4")

        thumbnailer.request(path)
        self.assertEqual(thumbnailer._player.source(), QUrl.fromLocalFile(str(path)))
        self.assertTrue(thumbnailer._timeout_timer.isActive())

        thumbnailer.cancel()

        self.assertTrue(thumbnailer._player.source().isEmpty())
        self.assertFalse(thumbnailer._timeout_timer.isActive())
        self.assertIsNone(thumbnailer._current)
        thumbnailer.deleteLater()
        self.app.processEvents()

    def test_full_view_stop_media_clears_sources(self) -> None:
        view = FullView()
        video = Path("/photos/open.mp4")
        audio = Path("/photos/comment.wav")
        view.set_video(video)
        view.audio_player.setSource(QUrl.fromLocalFile(str(audio)))

        view.stop_video()
        view.stop_audio()

        self.assertTrue(view.video_player.source().isEmpty())
        self.assertTrue(view.audio_player.source().isEmpty())
        view.deleteLater()
        self.app.processEvents()

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

    def test_repeated_quick_color_mark_clears_to_empty_string(self) -> None:
        path = Path("/photos/photo.jpg")
        host = SimpleNamespace(
            quick_mark=("color_label", "red"),
            current_path=None,
            stack=SimpleNamespace(currentWidget=lambda: None),
            full_view=object(),
            photo_details={path.name: {"color_label": "red"}},
            _selected_paths=lambda: [path],
            _update_selection=Mock(),
        )

        Workspace._apply_quick_mark(host)

        host._update_selection.assert_called_once_with(color_label="")

    def test_repeated_quick_rating_mark_clears_to_none(self) -> None:
        path = Path("/photos/photo.jpg")
        host = SimpleNamespace(
            quick_mark=("rating", 5),
            current_path=None,
            stack=SimpleNamespace(currentWidget=lambda: None),
            full_view=object(),
            photo_details={path.name: {"rating": 5}},
            _selected_paths=lambda: [path],
            _update_selection=Mock(),
        )

        Workspace._apply_quick_mark(host)

        host._update_selection.assert_called_once_with(rating=None)

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
        workspace.photo_details = {"a.jpg": {"faces": [{"eyes_open": False}]}}
        workspace.eyes_filter.setCurrentIndex(workspace.eyes_filter.findData("closed"))
        workspace._reset_unavailable_ai_filters()
        self.assertEqual(workspace.eyes_filter.currentData(), "closed")

        workspace.photo_details = {"b.jpg": {}}
        workspace._reset_unavailable_ai_filters()
        self.assertIsNone(workspace.eyes_filter.currentData())
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

    def test_file_panel_paths_uses_open_photo_in_full_view(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        selected_path = Path("/photos/selected-in-grid.jpg")
        open_path = Path("/photos/open-in-view.jpg")
        item = QListWidgetItem(selected_path.name)
        item.setData(Qt.ItemDataRole.UserRole, str(selected_path))
        workspace.grid.addItem(item)
        item.setSelected(True)
        workspace.current_path = open_path
        workspace.stack.setCurrentWidget(workspace.full_view)

        self.assertEqual(workspace._file_panel_paths(), [open_path])
        workspace.close()
        workspace.deleteLater()

    def test_removing_open_photo_in_full_view_opens_next_photo(self) -> None:
        workspace = Workspace(defer_initial_scan=True)
        current = Path("/photos/current.jpg")
        next_path = Path("/photos/next.jpg")
        workspace.all_paths = [current, next_path]
        workspace.view_paths = [current, next_path]
        workspace.paths = [current, next_path]
        workspace.current_path = current
        for path in workspace.paths:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            workspace.grid.addItem(item)
            workspace.items_by_path[path] = item
        workspace.stack.setCurrentWidget(workspace.full_view)
        workspace.open_full = Mock()

        workspace._remove_paths_from_grid([current])

        workspace.open_full.assert_called_once_with(next_path)
        self.assertEqual(workspace.paths, [next_path])
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

    def test_folder_rename_is_deferred_until_file_consumers_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old"
            old_path.mkdir()
            workspace = Workspace(old_path, defer_initial_scan=True)
            editor = Mock()
            editor.property.return_value = str(old_path)
            editor.text.return_value = "new"
            workspace._folder_name_editor = editor
            workspace._run_after_file_consumers_release = Mock()

            workspace._commit_folder_name()

            args, kwargs = workspace._run_after_file_consumers_release.call_args
            self.assertEqual(args[0], [old_path])
            self.assertFalse(kwargs["restart_consumers"])
            self.assertTrue(old_path.is_dir())
            args[1]()
            self.assertTrue((root / "new").is_dir())
            workspace.close()
            workspace.deleteLater()
            self.app.processEvents()

    def test_directory_switch_releases_completed_ai_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            workspace = Workspace(target, defer_initial_scan=True)
            pipeline = Mock()
            pipeline.pending_count.return_value = 0
            pipeline.progress.return_value = (0, 0, False)
            workspace._ai_pipeline = pipeline

            workspace.load_directory(target)

            pipeline.release_analysis_workers.assert_called_once_with()
            workspace.close()
            workspace.deleteLater()
            self.app.processEvents()

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
            self.assertTrue(workspace.grid_restore_loader.isHidden())

            running.set_result(None)
            QTest.qWait(60)

            self.assertFalse(path.exists())
            self.assertTrue(workspace.grid_restore_loader.isHidden())
            workspace.close()
            workspace.deleteLater()

    def test_mass_delete_shows_determinate_grid_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), defer_initial_scan=True)
            workspace.settings.setValue("behavior/delete_without_confirmation", True)
            paths = [Path(directory) / f"photo-{index}.raw" for index in range(3)]
            for path in paths:
                path.touch()
            started = Event()
            release = Event()

            def slow_delete(targets, permanent, progress):
                progress(1, len(targets))
                started.set()
                release.wait(2)
                return [], [], []

            with patch("rawww.app._delete_paths_task", side_effect=slow_delete):
                workspace._delete_paths(paths, permanent=True)
                for _attempt in range(20):
                    self.app.processEvents()
                    if started.wait(0.01):
                        break
                self.app.processEvents()

                self.assertTrue(started.is_set())
                self.assertFalse(workspace.grid_restore_loader.isHidden())
                self.assertEqual(workspace.grid_restore_loader_label.text(), "Выполняется удаление")
                self.assertEqual(workspace.grid_restore_loader_progress.maximum(), 3)
                self.assertEqual(workspace.grid_restore_loader_progress.value(), 1)
                progress = workspace.grid_restore_loader_progress
                self.assertTrue(progress.isTextVisible())
                self.assertTrue(progress.property("hasText"))
                self.assertGreaterEqual(
                    progress.maximumHeight(),
                    progress.fontMetrics().height(),
                )

                release.set()
                QTest.qWait(60)

            self.assertTrue(workspace.grid_restore_loader.isHidden())
            workspace.close()
            workspace.deleteLater()

    def test_preview_progress_names_cache_read_and_generation(self) -> None:
        """Строка состояния различает чтение готовых превью и их генерацию."""
        workspace = Workspace(defer_initial_scan=True)
        paths = {Path("/photos/a.jpg"), Path("/photos/b.jpg")}
        workspace.preview_paths = set(paths)
        workspace.preview_progress_total = len(paths)
        workspace.preview_finished_paths = {Path("/photos/a.jpg")}

        workspace.scheduler.preview_decode_pending.clear()
        workspace._refresh_status_panel()
        self.assertEqual(workspace.status_progress.format(), "Чтение превью: 1/2")

        workspace.scheduler.preview_decode_pending.add(Path("/photos/b.jpg"))
        workspace._refresh_status_panel()
        self.assertEqual(workspace.status_progress.format(), "Генерация превью: 1/2")

        workspace.close()
        workspace.deleteLater()

    def test_leaving_full_view_without_stored_geometry_frees_taskbar(self) -> None:
        """Открытый из Проводника кадр не оставляет окно в полном экране после сетки."""
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "single.jpg"
            photo.touch()
            window = MainWindow(photo)
            window.show()
            self.app.processEvents()

            window._leave_full_view()
            self.app.processEvents()

            self.assertFalse(window.isFullScreen())
            screen = window.screen() or QGuiApplication.primaryScreen()
            available = screen.availableGeometry()
            self.assertTrue(available.contains(window.geometry()) or window.isMaximized())
            window.close()
            window.deleteLater()

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
        self.assertEqual(format_remaining_time(12.4), "≈ 12 с")
        self.assertEqual(format_remaining_time(75), "≈ 1 мин 15 с")
        self.assertEqual(format_remaining_time(7_250), "≈ 2 ч 0 мин 50 с")

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

    def test_dropping_folders_into_favorites_adds_them_without_duplicates(self) -> None:
        """Сброс папок в список избранного добавляет их, пропуская уже сохранённые."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            workspace = Workspace(defer_initial_scan=True)
            before = workspace.favorites_list.count()

            workspace._add_folders_to_favorites([first, second, first])

            stored = {
                workspace.favorites_list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(workspace.favorites_list.count())
            }
            self.assertIn(str(first), stored)
            self.assertIn(str(second), stored)
            self.assertEqual(workspace.favorites_list.count(), before + 2)

            workspace._add_folders_to_favorites([first])
            self.assertEqual(workspace.favorites_list.count(), before + 2)
            workspace.close()
            workspace.deleteLater()

    def test_dropping_folders_on_tab_bar_opens_new_tabs(self) -> None:
        """Папки, брошенные на пустое место панели вкладок, открываются каждая в своей вкладке."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a"
            second = root / "b"
            first.mkdir()
            second.mkdir()
            window = MainWindow()
            before = window.tabs.count()

            window._open_folders_in_new_tabs([first, second])
            self.assertEqual(window.tabs.count(), before + 2)

            window._open_folders_in_new_tabs([first])
            self.assertEqual(window.tabs.count(), before + 2)
            window.close()
            window.deleteLater()

    @unittest.skipIf(os.name == "nt", "Проверяется POSIX-идентификатор тома")
    def test_posix_volume_keys_are_distinct_per_mount_point(self) -> None:
        """На POSIX якорь у всех путей общий, поэтому ключ тома берётся из точки монтирования."""
        self.assertNotEqual(_drive_key(Path("/")), _drive_key(Path("/Volumes/CARD")))
        self.assertNotEqual(
            _drive_key(Path("/media/user/A")),
            _drive_key(Path("/media/user/B")),
        )

    def test_repeated_active_drive_click_opens_volume_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            volume_a = base / "volume-a"
            volume_b = base / "volume-b"
            current = volume_a / "shoot"
            remembered = volume_b / "remembered"
            current.mkdir(parents=True)
            remembered.mkdir(parents=True)
            host = SimpleNamespace(
                current_dir=current,
                _deactivate_shotsync=Mock(),
                _set_tree_root_for_path=Mock(),
                _last_directory_for_volume=Mock(return_value=remembered),
                load_directory=Mock(),
            )
            volume_key = lambda path: str(path).casefold()

            with (
                patch("rawww.app._mounted_volume_paths", return_value=[volume_a, volume_b]),
                patch("rawww.app._drive_key", side_effect=volume_key),
            ):
                Workspace._drive_selected(host, volume_a)
                host.load_directory.assert_called_once_with(volume_a)

                host.load_directory.reset_mock()
                Workspace._drive_selected(host, volume_b)
                host.load_directory.assert_called_once_with(remembered)

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

    def test_directory_scan_skips_hidden_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            hidden = folder / ".hidden"
            hidden.mkdir()
            (hidden / "photo.jpg").write_bytes(b"image")
            photo = folder / "photo.jpg"
            photo.write_bytes(b"image")

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

    def test_orientation_toggle_saves_globally_and_per_folder(self) -> None:
        settings = _Settings()
        applied: list[bool] = []
        folder = Path("/tmp/shoot")
        workspace = SimpleNamespace(
            settings=settings,
            vertical_orientation=False,
            current_dir=folder,
            _folder_settings_prefix=Workspace._folder_settings_prefix,
            _apply_orientation=lambda vertical: applied.append(vertical),
        )

        Workspace._toggle_orientation(workspace)

        prefix = Workspace._folder_settings_prefix(folder)
        self.assertEqual(
            settings.values,
            [
                ("interface/vertical_orientation", True),
                (f"{prefix}/vertical_orientation", True),
            ],
        )
        self.assertEqual(applied, [True])

    def test_folder_orientation_prefers_per_folder_value(self) -> None:
        folder = Path("/tmp/shoot")
        prefix = Workspace._folder_settings_prefix(folder)
        settings = _MemorySettings({
            "interface/vertical_orientation": False,
            f"{prefix}/vertical_orientation": True,
        })
        workspace = SimpleNamespace(
            settings=settings,
            _folder_settings_prefix=Workspace._folder_settings_prefix,
        )

        self.assertTrue(Workspace._folder_orientation(workspace, folder))

    def test_folder_orientation_falls_back_to_global_default(self) -> None:
        folder = Path("/tmp/other")
        settings = _MemorySettings({"interface/vertical_orientation": True})
        workspace = SimpleNamespace(
            settings=settings,
            _folder_settings_prefix=Workspace._folder_settings_prefix,
        )

        self.assertTrue(Workspace._folder_orientation(workspace, folder))

    def test_restore_orientation_applies_stored_folder_value(self) -> None:
        folder = Path("/tmp/shoot")
        prefix = Workspace._folder_settings_prefix(folder)
        settings = _MemorySettings({f"{prefix}/vertical_orientation": True})
        applied: list[bool] = []
        workspace = SimpleNamespace(
            settings=settings,
            vertical_orientation=False,
            _folder_settings_prefix=Workspace._folder_settings_prefix,
            _apply_orientation=lambda vertical: applied.append(vertical),
        )
        workspace._folder_orientation = lambda directory: Workspace._folder_orientation(workspace, directory)

        Workspace._restore_orientation(workspace, folder)

        self.assertEqual(applied, [True])

    def test_restore_orientation_skips_when_unchanged(self) -> None:
        folder = Path("/tmp/shoot")
        settings = _MemorySettings({"interface/vertical_orientation": False})
        applied: list[bool] = []
        workspace = SimpleNamespace(
            settings=settings,
            vertical_orientation=False,
            _folder_settings_prefix=Workspace._folder_settings_prefix,
            _apply_orientation=lambda vertical: applied.append(vertical),
        )
        workspace._folder_orientation = lambda directory: Workspace._folder_orientation(workspace, directory)

        Workspace._restore_orientation(workspace, folder)

        self.assertEqual(applied, [])
