## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Оценка резкости кадра и поиск брака по фокусу и смазу.

Метрика построена вокруг двух идей, важных именно для отбора фотографий:

* Резкость меряется **по регионам**, а не по всему кадру. Иначе портрет с
  малой глубиной резкости (объект чёткий, фон в боке) считался бы браком
  наравне с промахом фокуса. Кадр берём как «резкий», если резок хотя бы один
  значимый регион; за брак — когда даже самый чёткий регион размыт.
* Резкость региона оценивается методом повторного размытия (Crete, «The blur
  effect», 2007): изображение ещё раз слегка размывается, и считается, какая
  доля перепадов яркости при этом теряется. У уже размытого региона теряется
  мало (мера близко к 1), у резкого — много (мера близко к 0). Мера
  относительная, поэтому устойчива к контенту, экспозиции и, частично, к шуму —
  тот присутствует и в оригинале, и в повторно размытой копии.

Горизонталь и вертикаль считаются отдельно: за резкость региона отвечает его
**худшее** направление, поэтому смаз (потеря деталей вдоль движения) не
маскируется чёткостью поперёк. Сильная разница направлений выдаёт смаз, слабая
при общей размытости — промах фокуса.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
from PIL import Image

# Длинная сторона, к которой приводим кадр перед анализом. Уменьшение заодно
# усредняет сенсорный шум, не убивая реальные края, а объём работы держит
# скромным даже для фонового процесса.
FOCUS_LONG_SIDE = 512
# Сетка регионов: по ней ищем самый резкий значимый участок кадра. Мелкая
# сетка позволяет поймать небольшой резкий объект на размытом фоне.
FOCUS_GRID = 16
# Окно повторного размытия в пикселях (нечётное). Масштаб подобран под
# FOCUS_LONG_SIDE: слишком маленькое не отличит лёгкое мыло, большое — сотрёт
# разницу между «резко» и «слегка мягко».
REBLUR_WINDOW = 9
# Квантиль «самого резкого» региона: низкий перцентиль вместо чистого минимума,
# чтобы один случайно чёткий (или шумный) регион не спасал кадр.
SHARPEST_QUANTILE = 0.06
# Регион учитывается, только если в нём достаточно перепадов яркости: на ровном
# небе мера размытия не определена и лишь вносит шум.
MIN_REGION_ENERGY = 2.0
# Порог браковки по резкости самого чёткого региона (мера размытия 0..1).
FOCUS_BLUR_THRESHOLD = 0.32
# При каком перекосе направлений размытие считаем смазом, а не расфокусом.
MOTION_ANISOTROPY = 0.28


@dataclass(frozen=True)
class FocusResult:
    """Итог анализа фокуса одного кадра.

    ``score`` — резкость самого чёткого значимого региона (0..1, больше —
    резче). ``blur`` — обратная мера размытия того же региона. ``sharp_ratio``
    — во сколько раз самый резкий регион чётче медианного; большое значение
    указывает на малую ГРИП (резкий объект на размытом фоне), близкое к
    единице — на равномерное мыло. ``anisotropy`` — перекос размытия по
    направлениям в чётком регионе. ``blur_type`` — ``sharp`` | ``defocus`` |
    ``motion``. ``subject`` — резкость в рамке ключевого лица, если она
    передана, иначе ``None``.
    """

    score: float
    blur: float
    sharp_ratio: float
    anisotropy: float
    blur_type: str
    subject: float | None = None

    def is_defect(self) -> bool:
        """Кадр — брак по фокусу/смазу, если даже лучший регион размыт.

        Если известна резкость ключевого лица, доверяем ей: резкое лицо
        оправдывает кадр (объект в фокусе), а размытое бракует даже при резком
        фоне — это фронт/бэк-фокус, который порегионная метрика по всему кадру
        пропустила бы.
        """
        if self.subject is not None:
            return self.subject < (1.0 - FOCUS_BLUR_THRESHOLD)
        return self.blur >= FOCUS_BLUR_THRESHOLD

    def to_json(self) -> str:
        payload = {
            "score": round(self.score, 4),
            "blur": round(self.blur, 4),
            "sharp_ratio": round(self.sharp_ratio, 4),
            "anisotropy": round(self.anisotropy, 4),
            "blur_type": self.blur_type,
        }
        if self.subject is not None:
            payload["subject"] = round(self.subject, 4)
        return json.dumps(payload, separators=(",", ":"))


def _to_luma(image: Image.Image) -> np.ndarray:
    """Приводит кадр к матрице яркости нужного масштаба (float32, 0..255)."""
    prepared = image.convert("L")
    long_side = max(prepared.size)
    if long_side > FOCUS_LONG_SIDE:
        scale = FOCUS_LONG_SIDE / long_side
        size = (max(1, round(prepared.width * scale)), max(1, round(prepared.height * scale)))
        prepared = prepared.resize(size, Image.Resampling.BILINEAR)
    return np.asarray(prepared, dtype=np.float32)


def _moving_average(values: np.ndarray, axis: int) -> np.ndarray:
    """Быстрое скользящее среднее окном ``REBLUR_WINDOW`` вдоль оси."""
    window = REBLUR_WINDOW
    pad = window // 2
    length = values.shape[axis]
    widths = [(0, 0), (0, 0)]
    widths[axis] = (pad, pad)
    padded = np.pad(values, widths, mode="edge")
    cumulative = np.cumsum(padded, axis=axis, dtype=np.float32)
    zero_shape = list(padded.shape)
    zero_shape[axis] = 1
    prefixed = np.concatenate([np.zeros(zero_shape, dtype=np.float32), cumulative], axis=axis)
    upper = np.take(prefixed, range(window, window + length), axis=axis)
    lower = np.take(prefixed, range(0, length), axis=axis)
    return (upper - lower) / window


def _directional_maps(luma: np.ndarray, axis: int) -> tuple[np.ndarray, np.ndarray]:
    """Возвращает потерю перепадов при повторном размытии вдоль оси.

    ``original`` — модуль соседних перепадов яркости, ``lost`` — та их часть,
    что исчезает после повторного размытия. Оба массива приведены к форме кадра
    (недостающий столбец/строка дополнены нулём), чтобы резать их по регионам.
    """
    blurred = _moving_average(luma, axis)
    original = np.abs(np.diff(luma, axis=axis))
    reblurred = np.abs(np.diff(blurred, axis=axis))
    lost = np.maximum(0.0, original - reblurred)
    widths = [(0, 0), (0, 0)]
    widths[axis] = (0, 1)
    return np.pad(original, widths), np.pad(lost, widths)


def _region_blur(
    original: np.ndarray, lost: np.ndarray, box: tuple[int, int, int, int] | None = None
) -> tuple[float, float]:
    """Мера размытия региона (0..1) и его энергия перепадов яркости."""
    if box is not None:
        top, left, bottom, right = box
        original = original[top:bottom, left:right]
        lost = lost[top:bottom, left:right]
    energy = float(original.sum())
    if energy <= 0.0:
        return 1.0, energy
    return float(1.0 - lost.sum() / energy), energy


def _grid_boxes(height: int, width: int) -> list[tuple[int, int, int, int]]:
    rows = np.linspace(0, height, FOCUS_GRID + 1, dtype=int)
    cols = np.linspace(0, width, FOCUS_GRID + 1, dtype=int)
    boxes = []
    for row in range(FOCUS_GRID):
        for col in range(FOCUS_GRID):
            top, bottom = int(rows[row]), int(rows[row + 1])
            left, right = int(cols[col]), int(cols[col + 1])
            if bottom > top and right > left:
                boxes.append((top, left, bottom, right))
    return boxes


def _face_box(faces: list[dict], height: int, width: int) -> tuple[int, int, int, int] | None:
    """Возвращает рамку крупнейшего лица в пикселях уменьшенного кадра."""
    best = None
    best_size = 0.0
    for face in faces:
        if not isinstance(face, dict):
            continue
        bbox = face.get("bbox") or {}
        try:
            fw, fh = float(bbox.get("width", 0.0)), float(bbox.get("height", 0.0))
            fx, fy = float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))
        except (TypeError, ValueError):
            continue
        size = max(fw, fh)
        if size > best_size:
            best_size = size
            best = (fx, fy, fw, fh)
    if best is None or best_size <= 0.0:
        return None
    fx, fy, fw, fh = best
    left = max(0, min(width - 1, int(fx * width)))
    top = max(0, min(height - 1, int(fy * height)))
    right = max(left + 1, min(width, int((fx + fw) * width)))
    bottom = max(top + 1, min(height, int((fy + fh) * height)))
    return top, left, bottom, right


def analyze_focus(image: Image.Image, faces: list[dict] | None = None) -> FocusResult:
    """Оценивает резкость кадра и определяет тип брака по фокусу и смазу."""
    luma = _to_luma(image)
    height, width = luma.shape
    if height < REBLUR_WINDOW or width < REBLUR_WINDOW:
        return FocusResult(0.0, 1.0, 1.0, 0.0, "defocus", None)

    original_h, lost_h = _directional_maps(luma, axis=1)
    original_v, lost_v = _directional_maps(luma, axis=0)

    blur_values = []
    energies = []
    for box in _grid_boxes(height, width):
        blur_h, energy = _region_blur(original_h, lost_h, box)
        blur_v, _ = _region_blur(original_v, lost_v, box)
        blur_values.append(max(blur_h, blur_v))
        energies.append(energy)

    energies = np.asarray(energies, dtype=np.float32)
    blur_values = np.asarray(blur_values, dtype=np.float32)
    significant = energies >= (MIN_REGION_ENERGY * (height * width) / len(blur_values))
    if not significant.any():
        significant = energies > 0.0
    if not significant.any():
        return FocusResult(0.0, 1.0, 1.0, 0.0, "defocus", None)

    region_blurs = blur_values[significant]
    sharpest_blur = float(np.quantile(region_blurs, SHARPEST_QUANTILE))
    median_blur = float(np.median(region_blurs))

    # Направленность размытия оцениваем по всему кадру: у смаза одно направление
    # теряет детали сильнее другого, у расфокуса потери примерно равны.
    global_h, _ = _region_blur(original_h, lost_h)
    global_v, _ = _region_blur(original_v, lost_v)
    anisotropy = float(abs(global_h - global_v) / max(1e-3, global_h + global_v))

    score = 1.0 - sharpest_blur
    sharp_ratio = float((1.0 - sharpest_blur) / max(1e-3, 1.0 - median_blur))
    blur_type = _classify(sharpest_blur, anisotropy)

    subject = None
    if faces:
        box = _face_box(faces, height, width)
        if box is not None:
            blur_h, energy = _region_blur(original_h, lost_h, box)
            blur_v, _ = _region_blur(original_v, lost_v, box)
            if energy > 0.0:
                subject = float(1.0 - max(blur_h, blur_v))

    return FocusResult(score, sharpest_blur, sharp_ratio, anisotropy, blur_type, subject)


def _classify(sharpest_blur: float, anisotropy: float) -> str:
    if sharpest_blur < FOCUS_BLUR_THRESHOLD:
        return "sharp"
    return "motion" if anisotropy >= MOTION_ANISOTROPY else "defocus"


def focus_is_defect(detail: dict) -> bool:
    """Признак брака по фокусу из сохранённого JSON (для UI и фильтров).

    Повторяет логику :meth:`FocusResult.is_defect`, но работает по словарю из
    кэша, чтобы потребитель не пересобирал кадр. Отсутствие данных или лиц —
    не брак.
    """
    focus = detail.get("focus")
    if not isinstance(focus, dict):
        return False
    subject = focus.get("subject")
    if subject is not None:
        try:
            return float(subject) < (1.0 - FOCUS_BLUR_THRESHOLD)
        except (TypeError, ValueError):
            return False
    try:
        return float(focus.get("blur", 0.0)) >= FOCUS_BLUR_THRESHOLD
    except (TypeError, ValueError):
        return False
