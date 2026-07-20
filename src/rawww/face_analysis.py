## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Поиск лиц и эмбеддингов на CPU с моделями, поставляемыми вместе с приложением."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .runtime_paths import data_path


MODEL_DIR = data_path("models") / "insightface" / "models" / "buffalo_s_shotsync"
DETECTOR_MODEL = MODEL_DIR / "det_500m.onnx"
RECOGNITION_MODEL = MODEL_DIR / "w600k_mbf.onnx"
EYE_STATE_MODEL = data_path("models") / "eye_state" / "mobilenetv2_eyes.onnx"
DETECTOR_SIZE = (640, 640)
EYE_STATE_SIZE = 224
# Половина стороны квадрата глаза как доля межзрачкового расстояния: 0.45 даёт
# лучшее разделение открытых и закрытых на тестах, оставляя веко в кадре.
EYE_CROP_FRACTION = 0.45
FACE_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.3655]],
    dtype=np.float32,
)


@dataclass(frozen=True)
class Face:
    """Найденное лицо: рамка, ориентиры и вектор для сравнения с наборами.

    ``eyes_open`` — вероятность, что глаза открыты, как ``max`` по двум глазам:
    так «закрыто» означает закрытые оба глаза, а подмигивание кадр не бракует.
    ``None`` — если модель состояния глаз недоступна.
    """

    bbox: np.ndarray
    landmarks: np.ndarray
    confidence: float
    embedding: np.ndarray
    eyes_open: float | None = None


_detector_session = None
_recognition_session = None
_eye_state_session = None


def _session(model: Path):
    from onnxruntime import GraphOptimizationLevel, InferenceSession, SessionOptions

    options = SessionOptions()
    options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
    options.use_deterministic_compute = True
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
    session.disable_fallback()
    return session


def _detector():
    global _detector_session
    if _detector_session is None:
        _detector_session = _session(DETECTOR_MODEL)
    return _detector_session


def _recognition():
    global _recognition_session
    if _recognition_session is None:
        _recognition_session = _session(RECOGNITION_MODEL)
    return _recognition_session


def _eye_state():
    global _eye_state_session
    if _eye_state_session is None:
        _eye_state_session = _session(EYE_STATE_MODEL)
    return _eye_state_session


def _eye_patch(image: Image.Image, center: np.ndarray, half: float) -> np.ndarray:
    """Вырезает квадрат вокруг глаза и готовит вход классификатора (mean=std=0.5)."""
    cx, cy = float(center[0]), float(center[1])
    patch = image.crop((cx - half, cy - half, cx + half, cy + half)).resize(
        (EYE_STATE_SIZE, EYE_STATE_SIZE), Image.Resampling.BILINEAR
    )
    values = (np.asarray(patch, dtype=np.float32) / 255.0 - 0.5) / 0.5
    return values.transpose(2, 0, 1)


def _classify_eyes(image: Image.Image, landmarks: np.ndarray) -> list[float | None]:
    """Возвращает вероятность открытых глаз для каждого лица одним прогоном модели.

    Для каждого лица классифицируются оба глаза (точки 0 и 1 ориентиров), а
    итог — ``max`` двух вероятностей: закрытым лицо считается лишь когда закрыты
    оба глаза. При отсутствии модели глаз возвращаются ``None``, чтобы конвейер
    лиц продолжал работать без состояния глаз.
    """
    if not len(landmarks):
        return []
    try:
        session = _eye_state()
    except Exception:
        return [None] * len(landmarks)
    patches = []
    for points in landmarks:
        left, right = points[0], points[1]
        half = max(6.0, EYE_CROP_FRACTION * float(np.hypot(left[0] - right[0], left[1] - right[1])))
        patches.append(_eye_patch(image, left, half))
        patches.append(_eye_patch(image, right, half))
    try:
        logits = session.run(None, {session.get_inputs()[0].name: np.stack(patches)})[0]
    except Exception:
        return [None] * len(landmarks)
    logits = logits - logits.max(axis=1, keepdims=True)
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    open_scores = probabilities[:, 1].reshape(-1, 2)
    return [float(pair.max()) for pair in open_scores]


def _detector_input(image: Image.Image) -> tuple[np.ndarray, float]:
    width, height = image.size
    scale = min(DETECTOR_SIZE[0] / width, DETECTOR_SIZE[1] / height)
    resized_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    resized = image.resize(resized_size, Image.Resampling.BILINEAR)
    canvas = np.zeros((DETECTOR_SIZE[1], DETECTOR_SIZE[0], 3), dtype=np.float32)
    canvas[:resized.height, :resized.width] = np.asarray(resized, dtype=np.float32)
    values = (canvas - 127.5) / 128.0
    return np.ascontiguousarray(values.transpose(2, 0, 1)[None]), scale


def _anchor_centers(height: int, width: int, stride: int) -> np.ndarray:
    centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
    centers = (centers * stride).reshape((-1, 2))
    return np.repeat(centers, 2, axis=0)


def _nms(detections: np.ndarray, threshold: float = 0.4) -> np.ndarray:
    if not len(detections):
        return np.empty(0, dtype=np.intp)
    x1, y1, x2, y2, scores = detections.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        index = order[0]
        keep.append(index)
        xx1 = np.maximum(x1[index], x1[order[1:]])
        yy1 = np.maximum(y1[index], y1[order[1:]])
        xx2 = np.minimum(x2[index], x2[order[1:]])
        yy2 = np.minimum(y2[index], y2[order[1:]])
        overlap = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        order = order[np.where(overlap / (areas[index] + areas[order[1:]] - overlap) <= threshold)[0] + 1]
    return np.asarray(keep, dtype=np.intp)


def _detect(image: Image.Image, threshold: float = 0.5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Запускает детектор и возвращает рамки, ориентиры и уверенность после NMS."""
    values, scale = _detector_input(image)
    session = _detector()
    outputs = session.run(None, {session.get_inputs()[0].name: values})
    boxes, landmarks, scores = [], [], []
    for index, stride in enumerate((8, 16, 32)):
        score = outputs[index].reshape(-1)
        valid = np.where(score >= threshold)[0]
        if not len(valid):
            continue
        centers = _anchor_centers(DETECTOR_SIZE[1] // stride, DETECTOR_SIZE[0] // stride, stride)
        distances = outputs[index + 3] * stride
        keypoints = outputs[index + 6] * stride
        box = np.column_stack((
            centers[:, 0] - distances[:, 0], centers[:, 1] - distances[:, 1],
            centers[:, 0] + distances[:, 2], centers[:, 1] + distances[:, 3],
        ))
        points = np.empty((len(centers), 5, 2), dtype=np.float32)
        points[:, :, 0] = centers[:, 0, None] + keypoints[:, 0::2]
        points[:, :, 1] = centers[:, 1, None] + keypoints[:, 1::2]
        boxes.append(box[valid] / scale)
        landmarks.append(points[valid] / scale)
        scores.append(score[valid])
    if not boxes:
        return (np.empty((0, 4), dtype=np.float32), np.empty((0, 5, 2), dtype=np.float32),
                np.empty(0, dtype=np.float32))
    boxes = np.vstack(boxes)
    landmarks = np.vstack(landmarks)
    scores = np.concatenate(scores)
    keep = _nms(np.column_stack((boxes, scores)))
    return boxes[keep], landmarks[keep], scores[keep]


def _aligned_face(image: Image.Image, landmarks: np.ndarray) -> Image.Image:
    equations = np.empty((10, 4), dtype=np.float32)
    targets = np.empty(10, dtype=np.float32)
    equations[0::2] = np.column_stack((landmarks[:, 0], -landmarks[:, 1], np.ones(5), np.zeros(5)))
    equations[1::2] = np.column_stack((landmarks[:, 1], landmarks[:, 0], np.zeros(5), np.ones(5)))
    targets[0::2] = FACE_TEMPLATE[:, 0]
    targets[1::2] = FACE_TEMPLATE[:, 1]
    scale, rotation, translate_x, translate_y = np.linalg.lstsq(equations, targets, rcond=None)[0]
    forward = np.array(
        [[scale, -rotation, translate_x], [rotation, scale, translate_y], [0.0, 0.0, 1.0]],
        dtype=np.float32,
    )
    inverse = np.linalg.inv(forward)[:2].reshape(-1)
    return image.transform((112, 112), Image.Transform.AFFINE, inverse, Image.Resampling.BILINEAR)


def recognize(image: Image.Image) -> list[Face]:
    """Находит лица в RGB-изображении и строит для каждого 512-мерный эмбеддинг."""
    boxes, landmarks, scores = _detect(image)
    if not len(boxes):
        return []
    crops = []
    for points in landmarks:
        aligned = _aligned_face(image, points)
        values = (np.asarray(aligned, dtype=np.float32) - 127.5) / 127.5
        crops.append(values.transpose(2, 0, 1))
    session = _recognition()
    input_name = session.get_inputs()[0].name
    embeddings = np.vstack([
        session.run(None, {input_name: crop[None]})[0]
        for crop in crops
    ])
    eye_states = _classify_eyes(image, landmarks)
    return [
        Face(box, points, float(score), embedding, eyes_open)
        for box, points, score, embedding, eyes_open
        in zip(boxes, landmarks, scores, embeddings, eye_states)
    ]
