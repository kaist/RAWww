## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Измеряет CPU и CoreML на macOS в отдельных процессах, не прерывая сборку при сбое EP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

import numpy as np

from rawww.runtime_paths import data_path


MODELS = {
    "face_detector": data_path("models") / "insightface/models/buffalo_s_shotsync/det_500m.onnx",
    "face_recognition": data_path("models") / "insightface/models/buffalo_s_shotsync/w600k_mbf.onnx",
    "face_landmarks": data_path("models") / "insightface/models/buffalo_s_shotsync/2d106det.onnx",
    "skin_segmenter": data_path("models") / "retouch/selfie_multiclass_256x256.onnx",
    "face_parser": data_path("models") / "retouch/face_parsing_resnet18.onnx",
    "neural_retouch": data_path("models") / "retouch/opt.onnx",
}


def _model_paths(include_downloads: bool) -> dict[str, Path | None]:
    """Возвращает локальные модели и по запросу добавляет скачиваемые графы."""
    paths = dict(MODELS)
    if include_downloads:
        from rawww.inpaint_pipeline import ensure_horizon_model, ensure_inpaint_model

        for name, prepare in (("lama_sd", ensure_inpaint_model), ("horizon", ensure_horizon_model)):
            try:
                paths[name] = prepare()
            except Exception as exc:
                paths[name] = None
                print(f"{name}: модель недоступна: {exc}", file=sys.stderr, flush=True)
    return paths


def _feeds(session, name: str) -> dict[str, np.ndarray]:
    """Строит воспроизводимые входы с реальными размерами пайплайна."""
    random = np.random.default_rng(2026)
    feeds = {}
    for item in session.get_inputs():
        shape = [dim if isinstance(dim, int) and dim > 0 else 1 for dim in item.shape]
        if name == "face_detector":
            shape[-2:] = [640, 640]
        if "mask" in item.name.lower():
            value = np.zeros(shape, dtype=np.float32)
            value[..., shape[-2] // 3:2 * shape[-2] // 3,
                  shape[-1] // 3:2 * shape[-1] // 3] = 1
        elif item.type == "tensor(float)":
            value = random.random(shape, dtype=np.float32)
        else:
            raise ValueError(f"Неподдерживаемый вход {item.name}: {item.type}")
        feeds[item.name] = value
    return feeds


def _profile_providers(path: str) -> dict[str, int]:
    """Считает выполненные узлы по провайдеру из профиля ONNX Runtime."""
    counts: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as stream:
            events = json.load(stream)
        for event in events:
            if event.get("cat") == "Node":
                provider = event.get("args", {}).get("provider", "unknown")
                counts[provider] = counts.get(provider, 0) + 1
    except (OSError, ValueError):
        pass
    return counts


def _worker(name: str, model: Path, provider: str, output: Path) -> None:
    """Измеряет один провайдер; отдельный процесс переживает нативный сбой CoreML."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.enable_profiling = True
    with tempfile.TemporaryDirectory(prefix="ctrlka-coreml-") as directory:
        options.profile_file_prefix = str(Path(directory) / "profile")
        providers: list = ["CPUExecutionProvider"]
        if provider == "coreml":
            providers.insert(0, ("CoreMLExecutionProvider", {
                "ModelFormat": "MLProgram",
                "MLComputeUnits": "ALL",
                "ModelCacheDirectory": directory,
            }))
        started = time.perf_counter()
        session = ort.InferenceSession(str(model), sess_options=options, providers=providers)
        load_seconds = time.perf_counter() - started
        feeds = _feeds(session, name)
        session.run(None, feeds)
        times = []
        for _ in range(3):
            started = time.perf_counter()
            result = session.run(None, feeds)
            times.append(time.perf_counter() - started)
        profile = session.end_profiling()
        np.savez_compressed(output, **{f"output_{i}": value for i, value in enumerate(result)})
        session_providers = session.get_providers()
        del session
        options.enable_profiling = False
        started = time.perf_counter()
        session = ort.InferenceSession(str(model), sess_options=options, providers=providers)
        reload_seconds = time.perf_counter() - started
        session.run(None, feeds)
        print(json.dumps({
            "available_providers": ort.get_available_providers(),
            "session_providers": session_providers,
            "load_seconds": load_seconds,
            "reload_seconds": reload_seconds,
            "run_seconds_median": statistics.median(times),
            "assigned_nodes": _profile_providers(profile),
        }), flush=True)
        del session


def _compare(cpu: Path, coreml: Path) -> dict[str, float | bool]:
    """Проверяет численное расхождение одинаковых входов без хранения фото в отчёте."""
    max_abs = 0.0
    max_rel = 0.0
    with np.load(cpu) as reference, np.load(coreml) as accelerated:
        if set(reference.files) != set(accelerated.files):
            return {"same_shapes": False}
        for key in reference.files:
            left, right = reference[key], accelerated[key]
            if left.shape != right.shape:
                return {"same_shapes": False}
            delta = np.abs(left.astype(np.float64) - right.astype(np.float64))
            max_abs = max(max_abs, float(delta.max(initial=0)))
            max_rel = max(max_rel, float((delta / np.maximum(np.abs(left), 1e-4)).max(initial=0)))
    return {"same_shapes": True, "max_abs": max_abs, "max_rel": max_rel}


def _run_one(name: str, model: Path, provider: str, output: Path) -> dict:
    """Изолирует падение нативной библиотеки и ограничивает время компиляции."""
    command = [sys.executable, __file__, "--worker", name, str(model), provider, str(output)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=360, check=False)
    except subprocess.TimeoutExpired:
        return {"error": "timeout_360s"}
    if result.returncode != 0:
        return {"error": f"exit_{result.returncode}", "stderr_tail": result.stderr[-2000:]}
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (IndexError, ValueError):
        return {"error": "missing_result", "stderr_tail": result.stderr[-2000:]}


def main() -> None:
    """Печатает и сохраняет отчёт для каждого архитектурного раннера macOS."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-downloads", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker", nargs=4, metavar=("NAME", "MODEL", "PROVIDER", "OUTPUT"))
    args = parser.parse_args()
    if args.worker:
        name, model, provider, output = args.worker
        _worker(name, Path(model), provider, Path(output))
        return
    if sys.platform != "darwin":
        parser.error("измерение CoreML запускается только на macOS")
    import onnxruntime as ort

    report: dict = {"platform": platform.platform(), "architecture": platform.machine(),
                    "onnxruntime": ort.__version__, "providers": ort.get_available_providers(), "models": {}}
    with tempfile.TemporaryDirectory(prefix="ctrlka-compare-") as directory:
        for name, model in _model_paths(args.include_downloads).items():
            if model is None:
                report["models"][name] = {"error": "model_unavailable"}
                continue
            outputs = {provider: Path(directory) / f"{name}-{provider}.npz"
                       for provider in ("cpu", "coreml")}
            row = {"cpu": _run_one(name, model, "cpu", outputs["cpu"]),
                   "coreml": (_run_one(name, model, "coreml", outputs["coreml"])
                              if "CoreMLExecutionProvider" in report["providers"]
                              else {"error": "provider_unavailable"})}
            if all("error" not in row[provider] for provider in outputs):
                row["difference"] = _compare(outputs["cpu"], outputs["coreml"])
                row["speedup"] = row["cpu"]["run_seconds_median"] / row["coreml"]["run_seconds_median"]
            report["models"][name] = row
            print(f"{name}: {json.dumps(row, ensure_ascii=False)}", flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
