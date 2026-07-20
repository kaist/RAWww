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
LANDMARK_MODEL = MODEL_DIR / "2d106det.onnx"
DETECTOR_SIZE = (640, 640)
LANDMARK_SIZE = 192
# Сторона квадратного кропа лица как доля большей стороны рамки: так
# insightface выравнивает лицо под модель 106 точек (без поворота).
LANDMARK_SCALE = 1.5
# Индексы контура глаз в разметке 106 точек: углы (out/inn) и веки
# (top/bot) для eye aspect ratio. Левый и правый глаз на кадре.
LEFT_EYE = {"out": 35, "inn": 42, "top": (40, 41), "bot": (33, 36, 37, 39)}
RIGHT_EYE = {"out": 93, "inn": 89, "top": (94, 95, 96), "bot": (87, 90, 91)}
FACE_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.3655]],
    dtype=np.float32,
)


@dataclass(frozen=True)
class Face:
    """Найденное лицо: рамка, ориентиры и вектор для сравнения с наборами.

    ``eyes_open`` — раскрытость более открытого глаза (eye aspect ratio) по
    разметке 106 точек: ``max`` по двум глазам, так «закрыто» означает
    закрытые оба глаза, а подмигивание кадр не бракует. Значение —
    геометрическое отношение, а не вероятность. ``None`` — если модель
    разметки недоступна.
    """

    bbox: np.ndarray
    landmarks: np.ndarray
    confidence: float
    embedding: np.ndarray
    eyes_open: float | None = None


_detector_session = None
_recognition_session = None
_landmark_session = None


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


def _landmark():
    global _landmark_session
    if _landmark_session is None:
        _landmark_session = _session(LANDMARK_MODEL)
    return _landmark_session


def _landmark_input(image: Image.Image, box: np.ndarray) -> np.ndarray:
    """Готовит квадратный кроп лица под модель 106 точек (сырые RGB 0..255)."""
    left, top, right, bottom = (float(value) for value in box[:4])
    center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
    side = max(right - left, bottom - top) * LANDMARK_SCALE
    crop = image.crop((
        center_x - side / 2.0, center_y - side / 2.0,
        center_x + side / 2.0, center_y + side / 2.0,
    )).resize((LANDMARK_SIZE, LANDMARK_SIZE), Image.Resampling.BILINEAR)
    return np.asarray(crop.convert("RGB"), dtype=np.float32).transpose(2, 0, 1)


def _eye_aspect_ratio(points: np.ndarray, eye: dict) -> float:
    """Считает eye aspect ratio: раскрытие век к ширине глаза.

    Раскрытие берётся как расстояние между средними точками век вдоль
    нормали к оси глаза (угол–угол), поэтому наклон головы не мешает.
    """
    outer, inner = points[eye["out"]], points[eye["inn"]]
    axis = inner - outer
    width = float(np.hypot(axis[0], axis[1]))
    if width < 1e-3:
        return 0.0
    normal = np.array([-axis[1], axis[0]], dtype=np.float32) / width
    top = float(np.mean([np.dot(points[i] - outer, normal) for i in eye["top"]]))
    bottom = float(np.mean([np.dot(points[i] - outer, normal) for i in eye["bot"]]))
    return abs(bottom - top) / width


def _classify_eyes(image: Image.Image, boxes: np.ndarray) -> list[float | None]:
    """Возвращает раскрытость глаз (EAR) для каждого лица одним прогоном модели.

    Для каждого лица модель 106 точек даёт контур век, по которому геометрически
    считается eye aspect ratio каждого глаза; итог — ``max`` двух, так что лицо
    считается закрытым лишь когда закрыты оба глаза. Геометрия устойчивее
    к контровому свету, чем вероятность сети. Прогон идёт по одному лицу, как и
    распознавание: у модели фиксирован размер пакета 1. При отсутствии модели
    разметки возвращаются ``None``, чтобы конвейер лиц продолжал работать.
    """
    if not len(boxes):
        return []
    try:
        session = _landmark()
    except Exception:
        return [None] * len(boxes)
    input_name = session.get_inputs()[0].name
    result = []
    for box in boxes:
        try:
            prediction = session.run(None, {input_name: _landmark_input(image, box)[None]})[0]
        except Exception:
            result.append(None)
            continue
        points = (prediction.reshape(-1, 2) + 1.0) * (LANDMARK_SIZE / 2.0)
        left = _eye_aspect_ratio(points, LEFT_EYE)
        right = _eye_aspect_ratio(points, RIGHT_EYE)
        result.append(max(left, right))
    return result


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
    eye_states = _classify_eyes(image, boxes)
    return [
        Face(box, points, float(score), embedding, eyes_open)
        for box, points, score, embedding, eyes_open
        in zip(boxes, landmarks, scores, embeddings, eye_states)
    ]
