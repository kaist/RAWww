## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет предпросмотр ретуши: стабильность сцены, шторку и кэш воркера."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import queue
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from rawww.retouch_dialog import BatchRetouchDialog, RetouchPreviewView
from rawww.retouch_pipeline import RetouchSettings, SkinMasks
from rawww.retouch_worker import _Preview


class _Capture:
    """Подменяет stdout: воркер пишет протокол в ``sys.stdout.buffer`` байтами."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.buffer.getvalue().splitlines() if line.strip()]


def _pixmap(width: int, height: int, colour: str) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(colour))
    return pixmap


class PreviewViewTests(unittest.TestCase):
    """Смена содержимого и шторка не должны сдвигать кадр под курсором."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._views: list[RetouchPreviewView] = []

    def tearDown(self) -> None:
        # Виджет, собранный сборщиком мусора раньше своей C++-части, роняет
        # следующие тесты набора: закрываем и отпускаем вручную.
        for view in self._views:
            view.close()
            view.setScene(None)
        self._views.clear()

    def _view(self) -> RetouchPreviewView:
        view = RetouchPreviewView()
        view.resize(400, 300)
        self._views.append(view)
        return view

    def test_region_result_keeps_scene_position(self) -> None:
        view = self._view()
        view.show_frame(_pixmap(600, 400, "#404040"), QPixmap(), (0, 0), (6000, 4000))
        view.set_fit_mode(False)
        view.set_full_canvas((6000, 4000))
        view.centerOn(4200, 2600)
        before = view.visible_scene_rect()

        # Пришёл обработанный фрагмент вокруг видимой области: это ровно то
        # событие, на котором раньше кадр прыгал в левый верхний угол.
        view.show_frame(_pixmap(800, 600, "#606060"), _pixmap(800, 600, "#202020"), (3800, 2300), (6000, 4000))

        self.assertEqual(view.visible_scene_rect(), before)
        self.assertEqual(view.sceneRect().width(), 6000)

    def test_wipe_keeps_zoom_and_centre(self) -> None:
        view = self._view()
        view.show_frame(_pixmap(600, 400, "#404040"), _pixmap(600, 400, "#101010"), (0, 0), (6000, 4000))
        view.set_fit_mode(False)
        view.centerOn(300, 200)
        scale = view.transform().m11()
        visible = view.visible_scene_rect()

        view.set_wipe(0.35)

        self.assertEqual(view.transform().m11(), scale)
        self.assertEqual(view.visible_scene_rect(), visible)

    def test_wipe_shows_original_left_of_divider(self) -> None:
        view = self._view()
        view.show_frame(_pixmap(400, 300, "#ffffff"), _pixmap(400, 300, "#000000"), (0, 0), (400, 300))
        view.set_fit_mode(False)
        view.set_wipe(0.5)

        shot = view.viewport().grab().toImage()
        centre_y = shot.height() // 2
        left = QColor(shot.pixel(shot.width() // 4, centre_y))
        right = QColor(shot.pixel(shot.width() * 3 // 4, centre_y))

        self.assertLess(left.red(), 60, "слева обязан быть оригинал")
        self.assertGreater(right.red(), 200, "справа обязан быть результат")


class _StubRetoucher:
    """Заменяет ONNX-пайплайн: считает, сколько раз что вызывали."""

    def __init__(self) -> None:
        self.mask_calls = 0
        self.process_calls = 0
        self.neural_calls = 0
        self.finish_calls = 0

    def skin_masks(self, rgb: np.ndarray) -> SkinMasks:
        self.mask_calls += 1
        return SkinMasks(np.full(rgb.shape[:2], 255, dtype=np.uint8), None, 100.0)

    def retouch_skin(self, rgb: np.ndarray, settings: RetouchSettings, masks: SkinMasks | None = None) -> np.ndarray:
        self.process_calls += 1
        assert masks is not None, "предпросмотр обязан переиспользовать маски"
        assert not settings.neural_retouch, "нейроретушь идёт отдельным шагом"
        return rgb

    def finish(self, rgb: np.ndarray, settings: RetouchSettings) -> np.ndarray:
        # Цвет и таблица обязаны ложиться после нейроретуши, а не до неё.
        self.finish_calls += 1
        assert self.finish_calls > self.neural_calls
        return rgb

    def neural_retouch(self, rgb: np.ndarray, mask: np.ndarray, strength: float) -> np.ndarray:
        self.neural_calls += 1
        return rgb


class RegionMaskTests(unittest.TestCase):
    """При 100 % маски берутся у целого снимка, а не считаются по кропу."""

    def setUp(self) -> None:
        from rawww.retouch_worker import _Frame, _Preview

        self.retoucher = _StubRetoucher()
        self.preview = _Preview(self.retoucher)
        # Кроп 400×300 из снимка 4000×3000, левый верхний угол (1200, 900).
        self.frame = _Frame(("photo.jpg", None, (1200, 900, 400, 300)), np.zeros((300, 400, 3), np.uint8), (1200, 900), (4000, 3000))

    def test_region_masks_come_from_the_whole_photo(self) -> None:
        whole = np.zeros((1200, 1600, 3), np.uint8)
        with mock.patch("rawww.retouch_worker._read", return_value=(whole, (0, 0), (4000, 3000))) as read:
            masks = self.preview._masks(Path("photo.jpg"), self.frame, (1200, 900, 400, 300))
            again = self.preview._masks(Path("photo.jpg"), self.frame, (1200, 900, 400, 300))

        self.assertEqual(masks.skin.shape, (300, 400))
        self.assertEqual(again.skin.shape, (300, 400))
        # Целый снимок читается и сегментируется один раз на фото: прокрутка при
        # 100 % не платит за сегментацию повторно.
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.retoucher.mask_calls, 1)
        # Кроп увеличивает лицо в 2.5 раза относительно кадра масок 1600 px.
        self.assertAlmostEqual(masks.face_scale, 100.0 * 400 / 160, places=3)


class PreviewWorkerTests(unittest.TestCase):
    """Один процесс предпросмотра держит кадр и маски между задачами."""

    def setUp(self) -> None:
        self.image = np.full((64, 48, 3), 180, dtype=np.uint8)
        self.retoucher = _StubRetoucher()
        self.preview = _Preview(self.retoucher)
        self.preview._frame = None

    def _run(self, settings: RetouchSettings) -> list[dict]:
        capture = _Capture()
        with redirect_stdout(capture):
            self.preview.run({"source": "photo.jpg"}, settings, stale=lambda: False)
        return capture.events()

    def _prepare_frame(self) -> None:
        from rawww.retouch_worker import _Frame

        self.preview._frame = _Frame(("photo.jpg", None, None), self.image, (0, 0), (48, 64))

    def test_masks_are_computed_once_for_the_frame(self) -> None:
        self._prepare_frame()
        self._run(RetouchSettings(neural_retouch=False))
        self._run(RetouchSettings(tone_strength=0.9, neural_retouch=False))

        self.assertEqual(self.retoucher.mask_calls, 1)
        self.assertEqual(self.retoucher.process_calls, 2)

    def test_original_is_sent_once_per_frame(self) -> None:
        self._prepare_frame()
        first = self._run(RetouchSettings(neural_retouch=False))
        second = self._run(RetouchSettings(neural_retouch=False))

        self.assertIn("before", first[0])
        self.assertNotIn("before", second[0])

    def test_colour_result_arrives_before_neural(self) -> None:
        self._prepare_frame()
        events = self._run(RetouchSettings(neural_retouch=True))

        self.assertEqual([event["exact"] for event in events], [False, True])
        self.assertEqual(self.retoucher.neural_calls, 1)

    def test_stale_request_skips_neural_stage(self) -> None:
        """Ползунок уехал дальше: дорогой этап для устаревшей задачи не считается."""
        self._prepare_frame()
        checks = iter((False, True, True))
        capture = _Capture()
        with redirect_stdout(capture):
            self.preview.run({"source": "photo.jpg"}, RetouchSettings(neural_retouch=True), stale=lambda: next(checks))

        self.assertEqual([event["exact"] for event in capture.events()], [False])
        self.assertEqual(self.retoucher.neural_calls, 0)


class WorkerStreamTests(unittest.TestCase):
    """Задача, пришедшая вместе с концом потока, обязана быть выполнена."""

    def test_skipped_preview_is_still_reported(self) -> None:
        """За схлопнутую задачу нужен ответ: по ним интерфейс гасит спиннер."""
        from rawww import retouch_worker

        jobs = [
            {"settings": {"tone_strength": share}, "tasks": [{"source": "photo.jpg"}], "preview": True}
            for share in (.1, .2, .3)
        ]
        incoming: queue.Queue = queue.Queue()
        for job in jobs:
            incoming.put(job)
        incoming.put(None)
        served: list[dict] = []
        capture = _Capture()

        class _Stub:
            def run(self, task: dict, settings: RetouchSettings, stale) -> None:
                served.append(task)

        with mock.patch.object(retouch_worker, "_jobs", side_effect=lambda _control: incoming), \
             mock.patch.object(retouch_worker, "SkinRetoucher", return_value=object()), \
             mock.patch.object(retouch_worker, "_Preview", return_value=_Stub()), \
             redirect_stdout(capture):
            retouch_worker.main(["--stdin"])

        # Посчитана только последняя задача, но ответов ровно столько, сколько
        # задач отправил интерфейс.
        self.assertEqual(len(served), 1)
        self.assertEqual([event.get("event") for event in capture.events()].count("finished"), len(jobs))

    def test_batch_survives_immediately_closed_channel(self) -> None:
        from rawww import retouch_worker

        job = {
            "settings": {"tone_strength": 0.0, "neural_retouch": False},
            "tasks": [{"source": "a.jpg", "target": "b.jpg", "max_side": None}],
            "preview": False,
        }
        done: list[list[dict]] = []
        capture = _Capture()
        # Интерфейс пишет строку и сразу закрывает канал, поэтому задача и
        # признак конца потока лежат в очереди одновременно.
        with mock.patch.object(retouch_worker, "_jobs", side_effect=lambda _control: _filled_queue(job)), \
             mock.patch.object(retouch_worker, "SkinRetoucher", return_value=object()), \
             mock.patch.object(
                 retouch_worker, "_batch",
                 side_effect=lambda _r, tasks, _s, _control: done.append(tasks),
             ), \
             redirect_stdout(capture):
            code = retouch_worker.main(["--stdin"])

        self.assertEqual(code, 0)
        self.assertEqual(done, [job["tasks"]])
        self.assertIn("finished", [event.get("event") for event in capture.events()])


class BatchControlTests(unittest.TestCase):
    """Команды управления пакетом идут мимо очереди задач."""

    def test_stop_ends_batch_on_frame_boundary(self) -> None:
        """Остановленный пакет не берёт следующий кадр и сообщает об этом."""
        from rawww import retouch_worker

        control = retouch_worker._BatchControl()
        control.apply("stop")
        capture = _Capture()
        with redirect_stdout(capture):
            retouch_worker._batch(object(), [{"source": "a.jpg", "target": "b.jpg"}], RetouchSettings(), control)

        events = capture.events()
        self.assertEqual([event.get("event") for event in events], ["stopped"])
        self.assertEqual(events[0]["done"], 0)

    def test_pause_holds_batch_until_resume(self) -> None:
        from rawww import retouch_worker

        control = retouch_worker._BatchControl()
        control.apply("pause")
        released: list[bool] = []

        def resume() -> None:
            released.append(True)
            control.apply("resume")

        threading.Timer(0.05, resume).start()
        self.assertTrue(control.wait_while_paused())
        self.assertEqual(released, [True])

    def test_reader_applies_commands_without_queueing_them(self) -> None:
        """Команда должна дойти до управления, пока пакет ещё считает кадры."""
        from rawww import retouch_worker

        job = {"settings": {}, "tasks": [], "preview": False}
        stream = io.BytesIO(
            json.dumps(job).encode("utf-8") + b"\n"
            + json.dumps({"command": "stop"}).encode("utf-8") + b"\n"
        )
        control = retouch_worker._BatchControl()
        with mock.patch.object(retouch_worker.sys, "stdin", mock.Mock(buffer=stream)):
            incoming = retouch_worker._jobs(control)
            self.assertEqual(incoming.get(timeout=2), job)
            self.assertIsNone(incoming.get(timeout=2))
        self.assertFalse(control.wait_while_paused())


class ClosedDialogCallbackTests(unittest.TestCase):
    """Поздние сигналы процесса не должны трогать удалённые виджеты окна."""

    def test_closed_dialog_ignores_process_callbacks(self) -> None:
        # Виджетов в заглушке нет намеренно: любое обращение к ним провалит тест
        # так же, как падало бы на уже удалённом C++-объекте.
        closed = SimpleNamespace(_closed=True, _streams={"batch": b""})
        process = SimpleNamespace()

        BatchRetouchDialog._finished(closed, process, False)
        BatchRetouchDialog._worker_start_error(closed, process)
        BatchRetouchDialog._read_events(closed, process, False)

    def test_detach_removes_every_subscription(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        process = QProcess()
        calls: list[str] = []
        process.readyReadStandardOutput.connect(lambda: calls.append("read"))
        process.finished.connect(lambda _code, _status: calls.append("finished"))
        process.errorOccurred.connect(lambda _error: calls.append("error"))

        BatchRetouchDialog._detach(process)
        process.readyReadStandardOutput.emit()
        process.finished.emit(0, QProcess.ExitStatus.NormalExit)
        process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)

        self.assertEqual(calls, [])


def _filled_queue(job: dict) -> queue.Queue:
    incoming: queue.Queue = queue.Queue()
    incoming.put(job)
    incoming.put(None)
    return incoming


if __name__ == "__main__":
    unittest.main()
