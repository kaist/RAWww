## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет сохранение работы при ошибке DirectML после создания сессии."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from rawww.onnx_sessions import ModelSession
from rawww.onnx_cache import _cache_path


class ModelSessionTests(unittest.TestCase):
    """Проверяет границу GPU и CPU без загрузки нативной модели."""

    def test_gpu_runs_of_different_models_do_not_overlap(self) -> None:
        """Несколько моделей DirectML делят один GPU без параллельных Run."""
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()

        def first_run(_outputs, _feeds):
            first_entered.set()
            release.wait(2)
            return ["first"]

        def second_run(_outputs, _feeds):
            second_entered.set()
            return ["second"]

        def session(run):
            owner = ModelSession.__new__(ModelSession)
            owner.gpu = True
            owner._lock = threading.Lock()
            owner._session = SimpleNamespace(run=run)
            return owner

        first, second = session(first_run), session(second_run)
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(first.run, None, {})
            self.assertTrue(first_entered.wait(2))

            def run_second():
                second_started.set()
                return second.run(None, {})

            b = executor.submit(run_second)
            try:
                self.assertTrue(second_started.wait(2))
                self.assertFalse(second_entered.wait(.1))
            finally:
                release.set()
            self.assertEqual(a.result(), ["first"])
            self.assertEqual(b.result(), ["second"])

    def test_gpu_run_failure_retries_original_model_on_cpu(self) -> None:
        """Сбой производного GPU-графа не должен прервать редактирование."""
        gpu = mock.Mock()
        gpu.get_providers.return_value = ["DmlExecutionProvider", "CPUExecutionProvider"]
        gpu.run.side_effect = RuntimeError("gpu failure")
        cpu = mock.Mock()
        cpu.get_providers.return_value = ["CPUExecutionProvider"]
        cpu.run.return_value = ["result"]
        options = SimpleNamespace(enable_mem_pattern=True, execution_mode=None)
        with mock.patch("rawww.onnx_sessions.directml_available", return_value=True), \
             mock.patch("onnxruntime.InferenceSession", side_effect=[gpu, cpu]) as create:
            session = ModelSession(Path("derived.onnx"), options, cpu_model=Path("original.onnx"))
            self.assertEqual(session.run(None, {"image": "input"}), ["result"])
            self.assertEqual(session.run(None, {"image": "next"}), ["result"])
        self.assertFalse(session.gpu)
        self.assertFalse(options.enable_mem_pattern)
        self.assertEqual(create.call_args_list[1].args[0], "original.onnx")
        self.assertEqual(gpu.run.call_count, 1)
        self.assertEqual(cpu.run.call_count, 2)

    def test_optimized_cache_key_changes_with_model_and_driver(self) -> None:
        """Замена модели и драйвера должна обходить сохранённый граф."""
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.write_bytes(b"first")
            with mock.patch("rawww.onnx_cache.sys.platform", "win32"), \
                 mock.patch("rawww.onnx_cache._video_drivers", return_value="adapter|1"):
                first = _cache_path(model, "DmlExecutionProvider", "1.24")
                model.write_bytes(b"second")
                changed_model = _cache_path(model, "DmlExecutionProvider", "1.24")
            with mock.patch("rawww.onnx_cache.sys.platform", "win32"), \
                 mock.patch("rawww.onnx_cache._video_drivers", return_value="adapter|2"):
                changed_driver = _cache_path(model, "DmlExecutionProvider", "1.24")
            self.assertNotEqual(first, changed_model)
            self.assertNotEqual(changed_model, changed_driver)


if __name__ == "__main__":
    unittest.main()
