from __future__ import annotations

import json
import math
import os
import threading
import warnings
from io import BytesIO
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .cache import FolderCache
from .imaging import RAW_EXTENSIONS
from .worker_priority import lower_background_priority


warnings.filterwarnings(
    "ignore",
    message=r"`estimate` is deprecated.*",
    category=FutureWarning,
)


MODEL_ROOT = Path(__file__).with_name("models")
CLIP_MODEL = MODEL_ROOT / "clip" / "patch32_v1.onnx"
INSIGHTFACE_ROOT = MODEL_ROOT / "insightface"
INSIGHTFACE_NAME = "buffalo_s_shotsync"
EMBEDDING_BATCH_SIZE = 16
FACE_BATCH_SIZE = 4
FACE_LONG_SIDE = 640
ANALYSIS_SOURCE_BATCH_SIZE = 8

_clip_session = None
_face_app = None


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
    """Extract one reusable detector-sized JPEG per source without touching disk."""
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


def _faces():
    global _face_app
    if _face_app is None:
        from insightface.app import FaceAnalysis

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            _face_app = FaceAnalysis(
                name=INSIGHTFACE_NAME, root=str(INSIGHTFACE_ROOT),
                providers=["CPUExecutionProvider"], allowed_modules=["detection", "recognition"],
            )
            _face_app.prepare(ctx_id=-1, det_size=(640, 640))
    return _face_app


def recognize_face_batch(paths: list[str | tuple[str, bytes]]) -> list[tuple[str, str]]:
    import cv2

    lower_background_priority()
    results = []
    for item in paths:
        path = item[0] if isinstance(item, tuple) else item
        try:
            with _load_rgb(item) as image:
                image.thumbnail((FACE_LONG_SIDE, FACE_LONG_SIDE), Image.Resampling.LANCZOS)
                width, height = image.size
                faces = _faces().get(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
            records = []
            for face in faces:
                embedding = getattr(face, "embedding", None)
                if embedding is None:
                    continue
                left, top, right, bottom = (float(value) for value in face.bbox)
                records.append({
                    "bbox": {"x": max(0.0, left / width), "y": max(0.0, top / height),
                             "width": max(0.0, (right - left) / width),
                             "height": max(0.0, (bottom - top) / height)},
                    "embedding": [round(float(value), 6) for value in embedding],
                    "confidence": float(face.det_score),
                })
            results.append((path, json.dumps(records, separators=(",", ":"))))
        except Exception:
            continue
    return results


class AiPipeline:
    """Two independent background process queues with persistent ONNX models."""

    def __init__(self) -> None:
        self.embedding_workers: ProcessPoolExecutor | None = None
        self.face_workers: ProcessPoolExecutor | None = None
        self.futures: set[Future] = set()
        self._futures_lock = threading.Lock()
        self._shutting_down = False

    def scan(self, paths: list[Path], cache: FolderCache, preview_workers: ProcessPoolExecutor) -> None:
        if os.environ.get("RAWWW_DISABLE_AI") == "1":
            return
        embedding_paths = cache.missing_ai_paths(paths, "image_embeddings")
        face_paths = cache.missing_ai_paths(paths, "face_analysis")
        embedding_names = {str(path) for path in embedding_paths}
        face_names = {str(path) for path in face_paths}
        analysis_paths = list(dict.fromkeys([*embedding_paths, *face_paths]))
        if not analysis_paths:
            return
        self._ensure_analysis_workers()
        for start in range(0, len(analysis_paths), ANALYSIS_SOURCE_BATCH_SIZE):
            future = preview_workers.submit(
                prepare_analysis_batch,
                [str(path) for path in analysis_paths[start:start + ANALYSIS_SOURCE_BATCH_SIZE]],
            )
            self._track(future)
            future.add_done_callback(
                lambda done, embeddings=embedding_names, faces=face_names, target=cache:
                    self._analysis_sources_finished(done, embeddings, faces, target)
            )

    def _submit_batches(self, pool, function, paths, batch_size, store) -> None:
        for start in range(0, len(paths), batch_size):
            with self._futures_lock:
                if self._shutting_down:
                    return
            batch = paths[start:start + batch_size]
            try:
                future = pool.submit(function, [str(path) if isinstance(path, Path) else path for path in batch])
            except RuntimeError:
                return
            self._track(future)
            future.add_done_callback(lambda done, sink=store: self._finished(done, sink))

    def _track(self, future: Future) -> None:
        with self._futures_lock:
            self.futures.add(future)

    def _analysis_sources_finished(self, future, embedding_names, face_names, cache) -> None:
        try:
            with self._futures_lock:
                if self._shutting_down:
                    return
            if future.cancelled():
                return
            sources = future.result()
            self._dispatch_analysis_sources(sources, embedding_names, face_names, cache)
        except Exception:
            pass
        finally:
            # Keep this future visible until its dependent batches are queued.
            with self._futures_lock:
                self.futures.discard(future)

    def _dispatch_analysis_sources(self, sources, embedding_names, face_names, cache) -> None:
        embedding_sources = [source for source in sources if source[0] in embedding_names]
        face_sources = [source for source in sources if source[0] in face_names]
        if self.embedding_workers is None or self.face_workers is None:
            return
        self._submit_batches(self.embedding_workers, extract_embedding_batch, embedding_sources,
                             EMBEDDING_BATCH_SIZE, cache.store_image_embeddings)
        self._submit_batches(self.face_workers, recognize_face_batch, face_sources,
                             FACE_BATCH_SIZE, cache.store_face_analysis)

    def pending_count(self) -> int:
        with self._futures_lock:
            return len(self.futures)

    def _ensure_analysis_workers(self) -> None:
        if self.embedding_workers is None:
            self.embedding_workers = ProcessPoolExecutor(max_workers=1)
        if self.face_workers is None:
            self.face_workers = ProcessPoolExecutor(max_workers=1)

    def release_analysis_workers(self) -> None:
        """Release loaded ONNX models after a manually requested run."""
        embedding_workers, self.embedding_workers = self.embedding_workers, None
        face_workers, self.face_workers = self.face_workers, None
        if embedding_workers is not None:
            embedding_workers.shutdown(wait=False, cancel_futures=True)
        if face_workers is not None:
            face_workers.shutdown(wait=False, cancel_futures=True)

    def _finished(self, future: Future, store) -> None:
        with self._futures_lock:
            self.futures.discard(future)
        if future.cancelled():
            return
        try:
            store(future.result())
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._futures_lock:
            self._shutting_down = True
            futures = tuple(self.futures)
            self.futures.clear()
        for future in futures:
            future.cancel()
        self.release_analysis_workers()
