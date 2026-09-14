## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет кисть, навигацию и асинхронное закрытие окна удаления объектов."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from rawww.inpaint_dialog import InpaintDialog, InpaintView


class _Settings:
    """Минимальный QSettings для проверки сохранения флажка без профиля ОС."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None, type=None):  # noqa: A002
        return self.values.get(key, default)

    def setValue(self, key, value):  # noqa: N802
        self.values[key] = value


class InpaintViewTests(unittest.TestCase):
    """Маска остаётся в координатах исходника при зуме и переносе."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = InpaintView()
        self.view.resize(800,600)
        self.view.show()
        image = QImage(1600,1000,QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.view.show_image(image, reset=True)
        self.view.editable = True
        self.app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def wheel(self, delta, point=QPoint(300,250)):
        event = QWheelEvent(QPointF(point), QPointF(self.view.viewport().mapToGlobal(point)),
                            QPoint(), QPoint(0,delta), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.view.viewport(), event)

    def test_zoom_is_anchored_and_cannot_go_below_fit(self):
        point = QPoint(300,250)
        before = self.view.mapToScene(point)
        self.wheel(240, point)
        after = self.view.mapToScene(point)
        self.assertLess((before-after).manhattanLength(), 5)
        self.wheel(-1200)
        self.assertEqual(self.view._zoom, 1)
        self.assertAlmostEqual(self.view.transform().m11(), self.view._fit_scale())

    def test_click_mask_and_space_pan(self):
        pos = QPoint(400,300)
        point = self.view.mapToScene(pos)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(len(self.view.strokes), 1)
        self.assertEqual(self.view.strokes[0]["points"][0], [point.x(),point.y()])
        QTest.keyPress(self.view, Qt.Key.Key_Space)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        QTest.keyRelease(self.view, Qt.Key.Key_Space)
        self.assertEqual(len(self.view.strokes), 1)
        diameter = self.view.diameter
        QTest.keyClick(self.view, Qt.Key.Key_BracketRight)
        self.assertGreater(self.view.diameter, diameter)
        self.assertNotEqual(self.view.brush_colour(point).name(), "#ffe100")

    def test_full_result_keeps_draft_screen_scale_and_centre(self):
        draft = QImage(1600, 1000, QImage.Format.Format_RGB888)
        draft.fill(QColor("yellow"))
        self.view.show_image(draft, reset=True)
        self.wheel(120, QPoint(330, 220))
        old_scale = self.view.transform().m11()
        old_center = self.view.mapToScene(self.view.viewport().rect().center())
        full = QImage(3200, 2000, QImage.Format.Format_RGB888)
        full.fill(QColor("yellow"))
        self.view.show_image(full, reset=False)
        self.assertAlmostEqual(self.view.transform().m11(), old_scale / 2)
        new_center = self.view.mapToScene(self.view.viewport().rect().center())
        self.assertLess((new_center - old_center * 2).manhattanLength(), 4)

    def test_crop_frame_drags_with_hand_cursor_without_leaving_photo(self):
        self.view.crop_active = True
        self.view.crop_by_edge(Qt.Key.Key_Down)
        start = self.view.mapFromScene(self.view.crop_box.center())
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(self.view.viewport().cursor().shape(), Qt.CursorShape.ClosedHandCursor)
        QTest.mouseMove(self.view.viewport(), start + QPoint(40, 0))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(40, 0))
        self.assertGreater(self.view.crop_box.left(), 0)
        self.assertEqual(self.view.viewport().cursor().shape(), Qt.CursorShape.OpenHandCursor)
        self.assertTrue(self.view.move_crop(Qt.Key.Key_Left, fraction=1))
        self.assertGreaterEqual(self.view.crop_box.left(), 0)
        self.assertTrue(self.view.sceneRect().contains(self.view.crop_box))
        self.assertTrue(self.view.backgroundBrush().texture().isNull())

    def test_crop_handles_resize_and_never_start_a_brush_stroke(self):
        self.view.crop_active = True
        self.view.crop_by_edge(Qt.Key.Key_Down)
        outside = self.view.mapFromScene(QPointF(0, 0))
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=outside)
        self.assertFalse(self.view.strokes)
        before = self.view.crop_box.size()
        handle = self.view.mapFromScene(self.view.crop_box.topLeft())
        QTest.mouseMove(self.view.viewport(), handle)
        self.assertEqual(self.view.viewport().cursor().shape(), Qt.CursorShape.SizeFDiagCursor)
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=handle)
        QTest.mouseMove(self.view.viewport(), handle + QPoint(30, 20))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=handle + QPoint(30, 20))
        self.assertLess(self.view.crop_box.width(), before.width())
        self.assertLess(self.view.crop_box.height(), before.height())
        edge = self.view.mapFromScene(QPointF(self.view.crop_box.right(), self.view.crop_box.center().y()))
        QTest.mouseMove(self.view.viewport(), edge)
        self.assertEqual(self.view.viewport().cursor().shape(), Qt.CursorShape.SizeHorCursor)
        self.view.crop_locked = False
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=edge)
        QTest.mouseMove(self.view.viewport(), edge + QPoint(-30, 0))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=edge + QPoint(-30, 0))
        self.assertNotAlmostEqual(self.view.crop_box.width() / self.view.crop_box.height(), 1.6)


class InpaintDialogTests(unittest.TestCase):
    """Ответ старого кадра и завершение старой записи не должны менять новый."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.start = patch.object(InpaintDialog, "_start")
        self.start.start()
        self.settings = _Settings()
        self.dialog = InpaintDialog([Path("a.png"),Path("b.png")], Path("a.png"), self.settings)
        self.dialog._ready = True
        self.dialog._models = {"inpaint": "ready", "horizon": "ready"}
        self.dialog.revisions["a.png"] = 1
        self.dialog.saved["a.png"] = 0
        self.send = patch.object(self.dialog, "_send")
        self.sent = self.send.start()

    def tearDown(self):
        self.dialog._closed = True
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.send.stop()
        self.start.stop()

    def test_manual_unsaved_navigation_requires_confirmation(self):
        self.dialog.autosave.setChecked(False)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Cancel) as question:
            self.dialog.navigate(1)
            self.assertEqual(self.dialog.index, 0)
            question.assert_called_once()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Discard):
            self.dialog.navigate(1)
        self.assertEqual(self.dialog.index, 1)
        self.sent.assert_any_call("discard", path="a.png")

    def test_autosaving_navigation_and_old_completion(self):
        self.dialog.save()
        self.dialog.navigate(1)
        self.assertEqual(self.dialog.index, 1)
        self.dialog._event({"event":"saved", "path":"a.png", "revision":1})
        self.assertEqual(self.dialog.path, "b.png")
        self.assertEqual(self.dialog.saved["a.png"], 1)

    def test_navigation_is_available_while_models_start(self):
        self.dialog._ready = False
        self.dialog._models = {"inpaint": "loading", "horizon": "downloading"}
        self.dialog.saved["a.png"] = 1
        self.dialog._update()
        self.assertTrue(self.dialog.next.isEnabled())
        self.dialog.navigate(1)
        self.assertEqual(self.dialog.path, "b.png")

    def test_inpaint_continues_after_navigation_and_keeps_its_own_result(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.saved["a.png"] = 1
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog.view.strokes = [{"points": [[10.0, 10.0]], "diameter": 20.0, "colour": "#ffffff"}]
        self.dialog._update()

        self.dialog.apply()
        self.assertFalse(self.dialog._processing)
        self.assertIn("a.png", self.dialog._background_inpaint)
        self.assertTrue(self.dialog.view.strokes)
        self.dialog.navigate(1)
        self.assertEqual(self.dialog.path, "b.png")
        self.assertFalse(self.dialog.view.strokes)

        result = QImage(320, 200, QImage.Format.Format_RGB888)
        result.fill(QColor("green"))
        self.dialog._event({"event": "result", "path": "a.png", "request": 0,
                            "revision": 2, "saved_revision": 1, "can_undo": True, "can_redo": False,
                            "width": 320, "height": 200}, result.bits().tobytes())
        self.assertEqual(self.dialog.path, "b.png")
        self.assertEqual(self.dialog.revisions["a.png"], 2)
        self.assertNotIn("a.png", self.dialog._background_inpaint)

    def test_close_waits_for_writes_without_blocking(self):
        self.dialog.save()
        with patch.object(self.dialog, "_stop_process") as stop:
            self.dialog._begin_close()
            self.assertTrue(self.dialog._closing)
            stop.assert_not_called()
            self.dialog._event({"event":"saved", "path":"a.png", "revision":1})
            stop.assert_called_once()

    def test_save_failure_keeps_window_and_changes(self):
        self.dialog.save()
        self.dialog._begin_close()
        with patch.object(QMessageBox, "warning"):
            self.dialog._event({"event":"save_error", "path":"a.png", "revision":1, "error":"disk full"})
        self.assertFalse(self.dialog._closing)
        self.assertTrue(self.dialog._dirty("a.png"))

    def test_stale_frame_cannot_replace_current(self):
        self.dialog.request = 3
        self.dialog._event({"event":"frame", "path":"a.png", "request":2})
        self.assertTrue(self.dialog.view.image.isNull())

    def test_failed_open_never_edits_previous_photo(self):
        self.dialog._displayed_path = "a.png"
        self.dialog.index = 1
        self.dialog.revisions["b.png"] = 0
        self.dialog._open()
        with patch.object(QMessageBox, "warning"):
            self.dialog._event({"event":"error", "path":"b.png", "request":self.dialog.request, "error":"bad image"})
        self.assertFalse(self.dialog.view.editable)

    def test_confirmed_save_defers_navigation(self):
        self.dialog.autosave.setChecked(False)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Save):
            self.dialog.navigate(1)
        self.assertEqual(self.dialog.index, 0)
        self.assertTrue(self.dialog._after_save)
        self.dialog._event({"event":"saved", "path":"a.png", "revision":1})
        self.assertEqual(self.dialog.index, 1)

    def test_autosave_is_restored_and_saved_immediately(self):
        self.assertTrue(self.dialog.autosave.isChecked())
        self.dialog.saved["a.png"] = self.dialog.revisions["a.png"]
        self.dialog._update()
        self.assertTrue(self.dialog.save_group.isHidden())
        self.dialog.autosave.setChecked(False)
        self.assertFalse(self.settings.values["inpaint/autosave"])
        self.assertFalse(self.dialog.save_group.isHidden())
        other = InpaintDialog([Path("a.png")], Path("a.png"), self.settings)
        try:
            self.assertFalse(other.autosave.isChecked())
        finally:
            other._closed = True
            other.close()
            other.deleteLater()

    def test_ctrl_s_saves_when_the_canvas_has_focus(self):
        self.dialog.show()
        self.dialog.view.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.dialog.view, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.sent.assert_any_call("save", path="a.png", revision=1)

    def test_crop_hotkeys_trim_and_move_the_frame(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        QTest.keyClick(self.dialog, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.dialog.view.crop_box.top(), 1.0)
        self.assertAlmostEqual(self.dialog.view.crop_box.width() / self.dialog.view.crop_box.height(), 1.6)
        width = self.dialog.view.crop_box.width()
        QTest.keyClick(self.dialog, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.dialog.view.crop_box.width(), width)
        self.assertTrue(self.dialog.view.move_crop(Qt.Key.Key_Right))
        self.assertGreater(self.dialog.view.crop_box.left(), 0)
        self.assertTrue(self.dialog.crop_toggle.isChecked())

    def test_crop_hotkey_reaches_dialog_from_canvas(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.show()
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.view.setFocus()
        QTest.keyClick(self.dialog.view, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.dialog.view.crop_box.top(), 1.0)

    def test_rotation_hotkeys_work_while_crop_is_open(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.crop_toggle.setChecked(True)
        QTest.keyClick(self.dialog, Qt.Key.Key_Comma)
        self.sent.assert_any_call("rotate", path="a.png", request=0, revision=1, degrees=-.5,
                                  draft_size=[320, 200], neighbors=["b.png"])
        self.assertEqual(self.dialog._processing_tool, "rotate")

    def test_ctrl_rotation_hotkeys_turn_image_by_90_degrees(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.show()
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.view.setFocus()
        QTest.keyClick(self.dialog.view, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        self.sent.assert_any_call("rotate", path="a.png", request=0, revision=1, degrees=-90,
                                  draft_size=[320, 200], neighbors=["b.png"])

    def test_crop_and_rotation_shortcuts_reach_dialog_from_canvas(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.show()
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.view.setFocus()
        QTest.keyClick(self.dialog.view, Qt.Key.Key_C)
        self.assertTrue(self.dialog.crop_toggle.isChecked())
        QTest.keyClick(self.dialog.view, Qt.Key.Key_Period)
        self.sent.assert_any_call("rotate", path="a.png", request=0, revision=1, degrees=.5,
                                  draft_size=[320, 200], neighbors=["b.png"])

    def test_mouse_resize_enables_crop_apply_and_period_rotates(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.show()
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.crop_toggle.setChecked(True)
        self.dialog.view.crop_by_edge(Qt.Key.Key_Down)
        self.assertTrue(self.dialog.crop_apply_button.isEnabled())
        handle = self.dialog.view.mapFromScene(self.dialog.view.crop_box.bottomRight())
        QTest.mousePress(self.dialog.view.viewport(), Qt.MouseButton.LeftButton, pos=handle)
        QTest.mouseMove(self.dialog.view.viewport(), handle + QPoint(-20, -20))
        QTest.mouseRelease(self.dialog.view.viewport(), Qt.MouseButton.LeftButton, pos=handle + QPoint(-20, -20))
        self.assertTrue(self.dialog.crop_apply_button.isEnabled())
        QTest.keyClick(self.dialog.view, Qt.Key.Key_Period)
        self.sent.assert_any_call("rotate", path="a.png", request=0, revision=1, degrees=.5,
                                  draft_size=[320, 200], neighbors=["b.png"])

    def test_crop_orientation_button_turns_selected_ratio(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.crop_ratio.setCurrentIndex(1)  # 16:9
        self.assertGreater(self.dialog.view.crop_box.width(), self.dialog.view.crop_box.height())
        self.dialog.view.reset_crop()
        self.dialog.crop_vertical_button.click()
        self.assertLess(self.dialog.view.crop_box.width(), self.dialog.view.crop_box.height())

    def test_crop_orientation_button_turns_original_ratio(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog._set_crop_ratio(None)
        self.assertFalse(self.dialog._crop_vertical)
        self.dialog.crop_vertical_button.click()
        self.assertTrue(self.dialog._crop_vertical)
        self.assertLess(self.dialog.view.crop_box.width(), self.dialog.view.crop_box.height())

    def test_enter_applies_crop_while_crop_mode_is_active(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.crop_toggle.setChecked(True)
        self.dialog.view.crop_by_edge(Qt.Key.Key_Down)
        QTest.keyClick(self.dialog, Qt.Key.Key_Return)
        self.sent.assert_any_call(
            "crop", path="a.png", request=0, revision=1,
            box=unittest.mock.ANY, draft_size=[320, 200], neighbors=["b.png"],
        )

    def test_russian_layout_comma_and_period_keys_rotate_from_canvas(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.show()
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.view.setFocus()
        QApplication.sendEvent(self.dialog.view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_B,
                                                            Qt.KeyboardModifier.NoModifier, "б"))
        self.sent.assert_any_call("rotate", path="a.png", request=0, revision=1, degrees=-.5,
                                  draft_size=[320, 200], neighbors=["b.png"])

    def test_autosave_debounces_result_until_timer_fires(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog._event({"event": "result", "path": "a.png", "request": 0,
                            "revision": 2, "saved_revision": 0, "width": 320, "height": 200},
                           bytes(image.sizeInBytes()))
        self.assertTrue(self.dialog._autosave_timer.isActive())
        self.assertTrue(self.dialog.save_group.isHidden())
        self.sent.assert_not_called()
        self.dialog._flush_autosave()
        self.sent.assert_any_call("save", path="a.png", revision=2)

    def test_auto_horizon_sends_current_draft_to_worker(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        self.dialog.straighten()
        self.sent.assert_any_call("straighten", path="a.png", request=0,
                                  draft_size=[320, 200], neighbors=["b.png"])
        self.assertTrue(self.dialog._processing)

    def test_h_shortcut_starts_auto_horizon(self):
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog.view.show_image(image, reset=True)
        self.dialog._displayed_path = "a.png"
        self.dialog._update()
        QTest.keyClick(self.dialog, Qt.Key.Key_H)
        self.sent.assert_any_call("straighten", path="a.png", request=0,
                                  draft_size=[320, 200], neighbors=["b.png"])

    def test_auto_horizon_rejects_large_rotation(self):
        with patch.object(QMessageBox, "warning") as warning:
            self.dialog._event({"event": "error", "path": "a.png", "request": 0,
                                "error": "horizon_angle_out_of_range:22.5"})
        self.assertFalse(self.dialog._processing)
        self.assertIn("22.5", warning.call_args.args[2])

    def test_photo_opens_while_tools_wait_for_models(self):
        self.dialog._models = {"inpaint": "loading", "horizon": "downloading"}
        image = QImage(320, 200, QImage.Format.Format_RGB888)
        image.fill(QColor("yellow"))
        self.dialog._displayed_path = "a.png"
        self.dialog.view.show_image(image, reset=True)
        self.dialog._update()
        self.assertFalse(self.dialog.view.editable)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertFalse(self.dialog.horizon_button.isEnabled())
        self.dialog._event({"event": "model_ready", "model": "horizon"})
        self.assertTrue(self.dialog.horizon_button.isEnabled())
        self.assertFalse(self.dialog.view.editable)
        self.dialog._event({"event": "model_ready", "model": "inpaint"})
        self.assertTrue(self.dialog.view.editable)

    def test_model_download_has_byte_progress(self):
        self.dialog._event({"event": "model_downloading", "model": "inpaint", "downloaded": 50, "total": 100})
        self.assertFalse(self.dialog.download_progress.isHidden())
        self.assertEqual(self.dialog.download_progress.maximum(), 100)
        self.assertEqual(self.dialog.download_progress.value(), 50)
        self.assertIn("MiB", self.dialog.status.text())


if __name__ == "__main__":
    unittest.main()
