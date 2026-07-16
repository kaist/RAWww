## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Фоновый конвейер AI: CLIP-эмбеддинги и распознавание лиц.

Модели и подготовка изображений живут в рабочих процессах, а этот модуль
связывает их с UI, не заставляя главный поток заниматься машинным зрением.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from io import BytesIO
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .cache import FolderCache
from .face_analysis import recognize
from .imaging import RAW_EXTENSIONS
from .runtime_paths import data_path
from .task_lifecycle import retire_executor
from .worker_priority import lower_background_priority

MODEL_ROOT = data_path("models")
CLIP_MODEL = MODEL_ROOT / "clip" / "patch32_v1.onnx"
EMBEDDING_BATCH_SIZE = 16
FACE_BATCH_SIZE = 4
FACE_LONG_SIDE = 640
ANALYSIS_SOURCE_BATCH_SIZE = 8

_clip_session = None


@dataclass
class _AiJob:
    """Состояние одного пакетного задания AI от подготовки до записи в кэш."""

    folder: Path
    paths: tuple[Path, ...]
    cache_root: Path | None
    cache: FolderCache | None = None
    total: int = 0
    completed: int = 0
    pending: int = 0
    remaining_kinds: dict[str, int] = field(default_factory=dict)


def _load_rgb(source: str | tuple[str, bytes]) -> Image.Image:
    path, data = source if isinstance(source, tuple) else (source, None)
    try:
        with Image.open(BytesIO(data) if data is not None else path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, ValueError):
        import rawpy

        with rawpy.imread(path) as raw:
            pixels = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
        return Image.fromarray(pixels, "RGB")


def prepare_analysis_batch(paths: list[str]) -> list[tuple[str, bytes]]:
    """Готовит по одному JPEG подходящего размера для каждого детектора, не записывая его на диск.
    """
    import rawpy

    lower_background_priority()
    results = []
    for path in paths:
        try:
            if Path(path).suffix.lower() in RAW_EXTENSIONS:
                with rawpy.imread(path) as raw:
                    try:
                        thumb = raw.extract_thumb()
                    except rawpy.LibRawNoThumbnailError:
                        image = Image.fromarray(
                            raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
                        ).convert("RGB")
                        thumb = None
                if thumb is None:
                    source = None
                elif thumb.format == rawpy.ThumbFormat.JPEG:
                    source = BytesIO(thumb.data)
                else:
                    image = Image.fromarray(thumb.data).convert("RGB")
                    source = None
            else:
                source = path
            if source is not None:
                with Image.open(source) as opened:
                    if opened.format == "JPEG":
                        opened.draft("RGB", (FACE_LONG_SIDE, FACE_LONG_SIDE))
                    image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((FACE_LONG_SIDE, FACE_LONG_SIDE), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, "JPEG", quality=85, optimize=False, progressive=False)
            results.append((path, output.getvalue()))
        except Exception:
            continue
    return results


def _clip():
    global _clip_session
    if _clip_session is None:
        from onnxruntime import GraphOptimizationLevel, InferenceSession, SessionOptions

        options = SessionOptions()
        options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
        options.use_deterministic_compute = True
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        _clip_session = InferenceSession(str(CLIP_MODEL), options, providers=["CPUExecutionProvider"])
        _clip_session.disable_fallback()
    return _clip_session


def _clip_input(image: Image.Image) -> np.ndarray:
    side = max(image.size)
    square = Image.new("RGB", (side, side))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    resized = square.resize((224, 224), Image.Resampling.BICUBIC)
    values = np.asarray(resized, dtype=np.float32) / 255.0
    mean = np.asarray([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    std = np.asarray([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    return ((values - mean) / std).transpose(2, 0, 1)


def _quantize(values: np.ndarray) -> bytes:
    norm = math.sqrt(float(np.dot(values, values)))
    if not norm:
        return b""
    return bytes(np.clip(np.rint(values / norm * 127) + 128, 0, 255).astype(np.uint8))


def extract_embedding_batch(paths: list[str | tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    lower_background_priority()
    images = []
    good_paths = []
    for item in paths:
        try:
            with _load_rgb(item) as image:
                images.append(_clip_input(image))
            good_paths.append(item[0] if isinstance(item, tuple) else item)
        except Exception:
            continue
    if not images:
        return []
    input_name = _clip().get_inputs()[0].name
    embeddings = _clip().run(["embeddings"], {input_name: np.stack(images)})[0]
    return [(path, _quantize(embedding)) for path, embedding in zip(good_paths, embeddings)]


def recognize_face_batch(paths: list[str | tuple[str, bytes]]) -> list[tuple[str, str]]:
    lower_background_priority()
    results = []
    for item in paths:
        path = item[0] if isinstance(item, tuple) else item
        try:
            with _load_rgb(item) as image:
                image.thumbnail((FACE_LONG_SIDE, FACE_LONG_SIDE), Image.Resampling.LANCZOS)
                width, height = image.size
                faces = recognize(image)
            records = []
            for face in faces:
                left, top, right, bottom = (float(value) for value in face.bbox)
                records.append({
                    "bbox": {"x": max(0.0, left / width), "y": max(0.0, top / height),
                             "width": max(0.0, (right - left) / width),
                             "height": max(0.0, (bottom - top) / height)},
                    "embedding": [round(float(value), 6) for value in face.embedding],
                    "confidence": float(face.confidence),
                })
            results.append((path, json.dumps(records, separators=(",", ":"))))
        except Exception:
            continue
    return results


class AiPipeline:
    """Управляет фоновым AI-анализом всех изображений рабочей папки.

    Конвейер отдельно готовит источники, считает CLIP-эмбеддинги и распознаёт
    лица, а результаты записывает в ``FolderCache`` небольшими порциями. Модели
    и пулы создаются лениво: обычный просмотр фотографий не должен оплачивать
    память за AI, которым пользователь ещё не воспользовался.

    Каждое сканирование оформлено как ``_AiJob`` с собственным поколением,
    прогрессом и флагом отмены. Интерактивные декодеры просмотрщика не участвуют
    в анализе, поэтому листать кадры можно даже пока нейросети заняты своим
    важным совещанием в фоне.
    """

    def __init__(self) -> None:
        self.job_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-jobs")
        self.source_workers: ProcessPoolExecutor | None = None
        self.embedding_workers: ProcessPoolExecutor | None = None
        self.face_workers: ProcessPoolExecutor | None = None
        self.futures: set[Future] = set()
        self.jobs: dict[Path, _AiJob] = {}
        self._last_progress: dict[Path, tuple[int, int]] = {}
        self._completed_folders: set[Path] = set()
        self._futures_lock = threading.RLock()
        self._shutting_down = False

    def scan(self, paths: list[Path], *, cache_root: Path | None = None) -> bool:
        if os.environ.get("RAWWW_DISABLE_AI") == "1":
            return False
        unique_paths = tuple(dict.fromkeys(paths))
        if not unique_paths:
            return False
        folder = unique_paths[0].parent
        with self._futures_lock:
            if self._shutting_down or folder in self.jobs:
                return False
            job = _AiJob(folder=folder, paths=unique_paths, cache_root=cache_root)
            self.jobs[folder] = job
        future = self.job_workers.submit(self._prepare_job, job)
        self._track(job, future)
        future.add_done_callback(lambda done, target=job: self._job_prepared(target, done))
        return True

    @staticmethod
    def _prepare_job(job: _AiJob) -> tuple[FolderCache, list[Path], list[Path]]:
        cache = FolderCache(
            job.folder,
            {path.name for path in job.paths},
            load_from_disk=True,
            cache_root=job.cache_root,
        )
        try:
            embedding_paths = cache.missing_ai_paths(list(job.paths), "image_embeddings")
            face_paths = cache.missing_ai_paths(list(job.paths), "face_analysis")
            return cache, embedding_paths, face_paths
        except Exception:
            cache.close(flush=False)
            raise

    def _job_prepared(self, job: _AiJob, future: Future) -> None:
        """Принимает подготовленный кэш и запускает вычисления актуального задания."""
        try:
            cache, embedding_paths, face_paths = future.result()
            embedding_names = {str(path) for path in embedding_paths}
            face_names = {str(path) for path in face_paths}
            analysis_paths = list(dict.fromkeys([*embedding_paths, *face_paths]))
            with self._futures_lock:
                if self._shutting_down:
                    cache.close(flush=False)
                    return
                job.cache = cache
                job.total = len(analysis_paths)
                job.remaining_kinds = {
                    str(path): int(str(path) in embedding_names) + int(str(path) in face_names)
                    for path in analysis_paths
                }
                self._last_progress[job.folder] = (0, job.total)
            if analysis_paths:
                self._ensure_analysis_workers()
                source_workers = self.source_workers
                if source_workers is None:
                    raise RuntimeError("AI source worker did not start")
                for start in range(0, len(analysis_paths), ANALYSIS_SOURCE_BATCH_SIZE):
                    batch = analysis_paths[start:start + ANALYSIS_SOURCE_BATCH_SIZE]
                    source_future = source_workers.submit(
                        prepare_analysis_batch,
                        [str(path) for path in batch],
                    )
                    self._track(job, source_future)
                    source_future.add_done_callback(
                        lambda done, target=job, embeddings=embedding_names, faces=face_names:
                        self._analysis_sources_finished(target, done, embeddings, faces)
                    )
        except Exception:
            with self._futures_lock:
                job.total = max(job.total, len(job.paths))
                self._last_progress[job.folder] = (job.completed, job.total)
        finally:
            self._future_finished(job, future)

    def _submit_batches(self, job, pool, function, paths, batch_size, store) -> None:
        for start in range(0, len(paths), batch_size):
            with self._futures_lock:
                if self._shutting_down:
                    return
            batch = paths[start:start + batch_size]
            try:
                future = pool.submit(function, [str(path) if isinstance(path, Path) else path for path in batch])
            except RuntimeError:
                return
            self._track(job, future)
            future.add_done_callback(
                lambda done, target=job, sink=store:
                self._results_finished(target, done, sink)
            )

    def _track(self, job: _AiJob, future: Future) -> None:
        with self._futures_lock:
            self.futures.add(future)
            job.pending += 1

    def _analysis_sources_finished(self, job, future, embedding_names, face_names) -> None:
        try:
            with self._futures_lock:
                if self._shutting_down:
                    return
            if future.cancelled():
                return
            sources = future.result()
            self._dispatch_analysis_sources(job, sources, embedding_names, face_names)
        except Exception:
            pass
        finally:
            self._future_finished(job, future)

    def _dispatch_analysis_sources(self, job, sources, embedding_names, face_names) -> None:
        """Раздаёт подготовленные изображения моделям и связывает результаты с файлами."""
        embedding_sources = [source for source in sources if source[0] in embedding_names]
        face_sources = [source for source in sources if source[0] in face_names]
        if (
            job.cache is None
            or self.embedding_workers is None
            or self.face_workers is None
        ):
            return
        self._submit_batches(
            job,
            self.embedding_workers,
            extract_embedding_batch,
            embedding_sources,
            EMBEDDING_BATCH_SIZE,
            job.cache.store_image_embeddings,
        )
        self._submit_batches(
            job,
            self.face_workers,
            recognize_face_batch,
            face_sources,
            FACE_BATCH_SIZE,
            job.cache.store_face_analysis,
        )

    def pending_count(self, folder: Path | None = None) -> int:
        with self._futures_lock:
            if folder is not None:
                job = self.jobs.get(folder)
                return job.pending if job is not None else 0
            return len(self.futures)

    def progress(self, folder: Path) -> tuple[int, int, bool]:
        with self._futures_lock:
            job = self.jobs.get(folder)
            if job is not None:
                return job.completed, job.total, True
            completed, total = self._last_progress.get(folder, (0, 0))
            return completed, total, False

    def take_completed_folders(self) -> set[Path]:
        with self._futures_lock:
            completed = set(self._completed_folders)
            self._completed_folders.clear()
            return completed

    def _ensure_analysis_workers(self) -> None:
        process_context = get_context("spawn")
        if self.source_workers is None:
            self.source_workers = ProcessPoolExecutor(max_workers=1, mp_context=process_context)
        if self.embedding_workers is None:
            self.embedding_workers = ProcessPoolExecutor(max_workers=1, mp_context=process_context)
        if self.face_workers is None:
            self.face_workers = ProcessPoolExecutor(max_workers=1, mp_context=process_context)

    def release_analysis_workers(self) -> None:
        """Освобождает декодеры и модели ONNX после завершения всех запусков."""
        source_workers, self.source_workers = self.source_workers, None
        embedding_workers, self.embedding_workers = self.embedding_workers, None
        face_workers, self.face_workers = self.face_workers, None
        if source_workers is not None:
            retire_executor(source_workers)
        if embedding_workers is not None:
            retire_executor(embedding_workers)
        if face_workers is not None:
            retire_executor(face_workers)

    def _results_finished(self, job: _AiJob, future: Future, store) -> None:
        try:
            if future.cancelled():
                return
            results = future.result()
            store(results)
            with self._futures_lock:
                for path, _value in results:
                    remaining = job.remaining_kinds.get(path, 0)
                    if remaining <= 0:
                        continue
                    remaining -= 1
                    job.remaining_kinds[path] = remaining
                    if remaining == 0:
                        job.completed += 1
                self._last_progress[job.folder] = (job.completed, job.total)
        except Exception:
            pass
        finally:
            self._future_finished(job, future)

    def _future_finished(self, job: _AiJob, future: Future) -> None:
        cache = None
        with self._futures_lock:
            self.futures.discard(future)
            job.pending = max(0, job.pending - 1)
            if job.pending or self.jobs.get(job.folder) is not job:
                return
            self.jobs.pop(job.folder, None)
            self._last_progress[job.folder] = (job.completed, job.total)
            self._completed_folders.add(job.folder)
            cache, job.cache = job.cache, None
        if cache is not None:
            cache.close(flush=True)

    def shutdown(self) -> None:
        with self._futures_lock:
            self._shutting_down = True
            futures = tuple(self.futures)
            self.futures.clear()
            caches = [job.cache for job in self.jobs.values() if job.cache is not None]
            self.jobs.clear()
        for future in futures:
            future.cancel()
        for cache in caches:
            cache.close(flush=False)
        retire_executor(self.job_workers)
        self.release_analysis_workers()
