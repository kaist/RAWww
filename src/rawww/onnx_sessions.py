## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Выбирает ONNX-провайдер в процессе модели и возвращается к CPU при сбое GPU."""

from __future__ import annotations

from pathlib import Path
import sys
import threading

from .onnx_cache import create_session, invalidate_cache


_DIRECTML_RUN_LOCK = threading.Lock()


def directml_available() -> bool:
    """Проверяет установленный провайдер без создания модели или обращения к GPU."""
    if sys.platform != "win32":
        return False
    import onnxruntime as ort

    return "DmlExecutionProvider" in ort.get_available_providers()


class ModelSession:
    """Владеет одной сессией; сериализует все DirectML-вызовы и при ошибке выбирает CPU.

    Соседние пайплайны знают только ``run`` и метаданные входов. После сбоя
    GPU все следующие вызовы идут на CPU без перезапуска фонового процесса.
    """

    def __init__(self, model: Path, options, *, prefer_gpu: bool = True, cpu_model: Path | None = None) -> None:
        import onnxruntime as ort

        self._model = model
        self._options = options
        self._cpu_model = cpu_model or model
        self._lock = threading.Lock()
        self.gpu = False
        if prefer_gpu and directml_available():
            # DirectML требует последовательный Run и отключённый memory pattern.
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            try:
                session = create_session(model, options, "DmlExecutionProvider")
                if "DmlExecutionProvider" in session.get_providers():
                    session.disable_fallback()
                    self._session = session
                    self.gpu = True
                    return
            except Exception:
                # CPU использует исходную модель даже при ошибке её GPU-копии.
                pass
        self._session = create_session(self._cpu_model, options, "CPUExecutionProvider")

    def get_inputs(self):
        """Возвращает входы активной модели для существующих пайплайнов."""
        return self._session.get_inputs()

    def get_outputs(self):
        """Возвращает выходы активной модели для диагностических вызовов."""
        return self._session.get_outputs()

    def get_providers(self):
        """Сообщает фактический провайдер, включая переход на CPU после ошибки."""
        return self._session.get_providers()

    def run(self, outputs, feeds):
        """Запускает модель и повторяет неудавшийся GPU-вызов на исходной CPU-модели."""
        if self.gpu:
            # На Intel Iris Xe параллельные вызовы разных DirectML-сессий
            # приводили к access violation внутри ONNX Runtime/драйвера.
            with _DIRECTML_RUN_LOCK, self._lock:
                if self.gpu:
                    try:
                        return self._session.run(outputs, feeds)
                    except Exception:
                        self._session = create_session(self._cpu_model, self._options, "CPUExecutionProvider")
                        self.gpu = False
                        invalidate_cache(model=self._model, provider="DmlExecutionProvider")
        return self._session.run(outputs, feeds)
