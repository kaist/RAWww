## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Оценка качества кадра моделями NIMA: техническое качество и эстетика.

Две лёгкие модели MobileNet-NIMA (обучены idealo на TID2013 и AVA) выдают
распределение оценок 1..10; итог — взвешенное среднее, то есть привычная
«оценка кадра» по десятибалльной шкале. Технический балл реагирует на резкость,
шум и артефакты, эстетический — на композицию и общий вид. Оба балла отдаём в
UI, где пользователь порогами-ползунками отсекает худшие кадры.

Модели живут в рабочем процессе AI и грузятся лениво: обычный просмотр не должен
оплачивать память за нейросети, которыми ещё не воспользовались. Вход у моделей
— квадрат 512×512 (баланс точности и скорости/памяти на CPU); препроцесс совпадает
с ``keras.applications.mobilenet.preprocess_input`` (RGB, значения в диапазоне
[-1, 1]) — именно на нём модели обучались.
"""

from __future__ import annotations

import json

import numpy as np
from PIL import Image

from .runtime_paths import data_path

NIMA_ROOT = data_path("models") / "nima"
TECHNICAL_MODEL = NIMA_ROOT / "technical.onnx"
AESTHETIC_MODEL = NIMA_ROOT / "aesthetic.onnx"
# Сторона квадратного входа модели. Больше — точнее к размытию, но дороже по
# памяти активаций; 512 выбран как компромисс для фонового CPU-процесса.
NIMA_INPUT = 512
# Размер пакета: модели быстрые, но большой пакет на входе 512×512 заметно
# поднимает пиковую память активаций, поэтому держим его скромным.
NIMA_BATCH_SIZE = 4

_sessions: dict[str, object] = {}


def _session(model_path):
    """Лениво создаёт и кэширует ONNX-сессию модели в текущем процессе."""
    key = str(model_path)
    session = _sessions.get(key)
    if session is None:
        from onnxruntime import GraphOptimizationLevel, InferenceSession, SessionOptions

        options = SessionOptions()
        options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
        options.use_deterministic_compute = True
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        session.disable_fallback()
        _sessions[key] = session
    return session


def _preprocess(image: Image.Image) -> np.ndarray:
    """Готовит один кадр под вход NIMA: квадрат 512×512, RGB, диапазон [-1, 1]."""
    resized = image.convert("RGB").resize((NIMA_INPUT, NIMA_INPUT), Image.Resampling.BILINEAR)
    values = np.asarray(resized, dtype=np.float32) / 127.5 - 1.0
    return values  # NHWC: модель ожидает каналы последней осью


def _mean_score(distribution: np.ndarray) -> float:
    """Свёртывает распределение вероятностей 1..10 во взвешенное среднее."""
    return float(np.dot(distribution, np.arange(1, distribution.shape[0] + 1)))


def score_images(images: list[Image.Image]) -> list[tuple[float, float]]:
    """Возвращает пары (техническое качество, эстетика) для кадров, батчами."""
    scores: list[tuple[float, float]] = []
    technical = _session(TECHNICAL_MODEL)
    aesthetic = _session(AESTHETIC_MODEL)
    technical_input = technical.get_inputs()[0].name
    aesthetic_input = aesthetic.get_inputs()[0].name
    for start in range(0, len(images), NIMA_BATCH_SIZE):
        batch = np.stack([_preprocess(image) for image in images[start:start + NIMA_BATCH_SIZE]])
        technical_probs = technical.run(None, {technical_input: batch})[0]
        aesthetic_probs = aesthetic.run(None, {aesthetic_input: batch})[0]
        for technical_row, aesthetic_row in zip(technical_probs, aesthetic_probs):
            scores.append((_mean_score(technical_row), _mean_score(aesthetic_row)))
    return scores


def quality_json(technical: float, aesthetic: float) -> str:
    """Сериализует оба балла в компактный JSON для кэша папки."""
    return json.dumps(
        {"quality": round(technical, 3), "aesthetic": round(aesthetic, 3)},
        separators=(",", ":"),
    )


def quality_scores(detail: dict) -> tuple[float | None, float | None]:
    """Достаёт (качество, эстетику) из сохранённого JSON; ``None`` — нет данных."""
    quality = detail.get("quality")
    if not isinstance(quality, dict):
        return None, None

    def _value(key: str) -> float | None:
        raw = quality.get(key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return _value("quality"), _value("aesthetic")
