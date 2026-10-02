## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Кэширует оптимизированные ONNX-графы по модели, устройству и версии среды."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

from .runtime_paths import application_cache_path


_CACHE_VERSION = 1


@lru_cache(maxsize=1)
def _video_drivers() -> str:
    """Читает идентификаторы адаптеров и версии драйверов для сброса GPU-кэша."""
    if sys.platform != "win32":
        return ""
    command = (
        "Get-CimInstance Win32_VideoController | Sort-Object PNPDeviceID | "
        "ForEach-Object { $_.PNPDeviceID + '|' + $_.DriverVersion }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, errors="replace", timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _cache_path(model: Path, provider: str, ort_version: str) -> Path | None:
    """Строит аппаратно зависимый ключ; без версии GPU-драйвера кэш не создаётся."""
    if sys.platform != "win32":
        return None
    device = _video_drivers() if provider == "DmlExecutionProvider" else platform.processor()
    if provider == "DmlExecutionProvider" and not device:
        return None
    with model.open("rb") as stream:
        model_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    # Производный LaMa меняет имя при обновлении исходника; семейство остаётся прежним.
    model_name = re.sub(r"-[0-9a-f]{16}-v\d+$", "", model.stem)
    identity = f"{model.parent.name}/{model_name}".casefold().encode("utf-8")
    family = hashlib.sha256(identity).hexdigest()[:12]
    key = hashlib.sha256(json.dumps((
        _CACHE_VERSION, model_digest, ort_version, provider, device,
        platform.machine(), platform.version(),
    ), separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    return application_cache_path() / "onnx" / "optimized" / f"{family}-{provider}-{key}.onnx"


def _discard_old(path: Path) -> None:
    """Удаляет старые производные графы той же модели и провайдера после записи нового."""
    family, provider, _key = path.stem.split("-", 2)
    for old in path.parent.glob(f"{family}-{provider}-*.onnx"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                # Другой процесс может ещё читать прежний граф на Windows.
                pass


def invalidate_cache(model: Path, provider: str) -> None:
    """Убирает граф после сбоя Run, чтобы следующий запуск построил его заново."""
    import onnxruntime as ort

    try:
        cache = _cache_path(model, provider, ort.__version__)
        if cache is not None:
            cache.unlink(missing_ok=True)
    except OSError:
        pass


def create_session(model: Path, options, provider: str):
    """Загружает проверенный кэш или сохраняет новый граф после инициализации."""
    import onnxruntime as ort

    providers = [provider, "CPUExecutionProvider"] if provider != "CPUExecutionProvider" else [provider]
    try:
        cache = _cache_path(model, provider, ort.__version__)
    except OSError:
        cache = None
    if cache is None:
        return ort.InferenceSession(str(model), sess_options=options, providers=providers)
    if cache.is_file():
        original_level = options.graph_optimization_level
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        try:
            cached = ort.InferenceSession(str(cache), sess_options=options, providers=providers)
            if provider in cached.get_providers():
                return cached
        except Exception:
            pass
        finally:
            options.graph_optimization_level = original_level
        try:
            cache.unlink()
        except OSError:
            pass
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".onnx-opt-", suffix=".onnx", dir=cache.parent)
        os.close(descriptor)
    except OSError:
        return ort.InferenceSession(str(model), sess_options=options, providers=providers)
    temporary = Path(name)
    try:
        options.optimized_model_filepath = str(temporary)
        try:
            session = ort.InferenceSession(str(model), sess_options=options, providers=providers)
        except Exception:
            # Недоступный для записи кэш не должен запрещать запуск исходной модели.
            options.optimized_model_filepath = ""
            return ort.InferenceSession(str(model), sess_options=options, providers=providers)
        if provider in session.get_providers() and temporary.is_file():
            try:
                if not cache.is_file():
                    os.replace(temporary, cache)
                _discard_old(cache)
            except OSError:
                pass
        return session
    finally:
        options.optimized_model_filepath = ""
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
