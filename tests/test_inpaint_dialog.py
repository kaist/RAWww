## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет кисть, навигацию и асинхронное закрытие окна удаления объектов."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent
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
        self.dialog.autosave.setChecked(False)
        self.assertFalse(self.settings.values["inpaint/autosave"])
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
