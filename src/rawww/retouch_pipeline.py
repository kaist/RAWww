## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Изолированный ONNX-пайплайн пакетной ретуши.

Этот модуль импортируется только дочерним процессом. Так сессии ONNX и их
нативная память никогда не попадают в процесс интерфейса Контрольки.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


@dataclass(frozen=True)
class RetouchSettings:
    """Параметры этапов ретуши, передаваемые воркеру как простые данные."""

    tone_strength: float = 0.50
    matte_strength: float = 0.0
    dodge_burn: float = 0.0
    neural_retouch: bool = True
    neural_strength: float = 0.50
    # Тон и цвет кадра идут после ретуши кожи: -1..1, где 0 — без изменений.
    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    lut_path: str = ""
    lut_strength: float = 1.0


@dataclass(frozen=True)
class SkinMasks:
    """Маски кадра: мягкая маска кожи, доля лица в пикселе и размер лица.

    Считается один раз на кадр и переиспользуется при движении ползунков:
    ползунки меняют только цветовые этапы, но не то, где находится кожа.
    """

    skin: np.ndarray
    face_area: np.ndarray | None
    face_scale: float


@dataclass(frozen=True)
class CubeLut:
    """Таблица .cube: значения в порядке файла и границы входного диапазона.

    Хранится плоским списком именно потому, что в таком виде её принимает
    `ImageFilter.Color3DLUT`: порядок обхода у Pillow и у формата .cube один и
    тот же — быстрее всего меняется красный канал.
    """

    size: int
    table: list[float]
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]


def load_cube_lut(path: str | Path) -> CubeLut:
    """Читает 3D-таблицу .cube (Adobe Cube LUT Specification 1.0).

    Одномерные таблицы не поддерживаются: в них нет смысла для творческих
    пресетов, а ошибку лучше показать пользователю сразу, чем молча испортить
    цвет всей партии.
    """
    size = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    table: list[float] = []
    with Path(path).open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            head, _, rest = line.partition(" ")
            key = head.upper()
            if key == "LUT_3D_SIZE":
                size = int(rest)
            elif key == "DOMAIN_MIN":
                domain_min = tuple(float(value) for value in rest.split())
            elif key == "DOMAIN_MAX":
                domain_max = tuple(float(value) for value in rest.split())
            elif key == "LUT_1D_SIZE":
                raise ValueError("одномерные таблицы .cube не поддерживаются")
            elif key in {"TITLE", "LUT_3D_INPUT_RANGE"}:
                continue
            else:
                table.extend(float(value) for value in line.split())
    if size < 2 or len(table) != size ** 3 * 3:
        raise ValueError("файл .cube неполный или без LUT_3D_SIZE")
    return CubeLut(size, table, domain_min, domain_max)


def apply_lut(rgb: np.ndarray, lut: CubeLut, strength: float) -> np.ndarray:
    """Накладывает 3D-таблицу с заданной силой поверх готового кадра.

    Интерполяцию считает Pillow в нативном коде, а сила — обычное смешивание с
    исходным кадром: так ползунок работает предсказуемо на любой таблице.
    """
    strength = float(np.clip(strength, 0, 1))
    if strength <= 0.0:
        return rgb
    source = rgb
    if lut.domain_min != (0.0, 0.0, 0.0) or lut.domain_max != (1.0, 1.0, 1.0):
        # Нестандартный домен: сжимаем вход в 0..1 таблицы, иначе Pillow
        # обрежет края и картинка поедет по контрасту.
        low = np.array(lut.domain_min, dtype=np.float32) * 255
        high = np.array(lut.domain_max, dtype=np.float32) * 255
        span = np.maximum(high - low, np.float32(1e-3))
        source = np.clip((rgb.astype(np.float32) - low) / span * 255, 0, 255).astype(np.uint8)
    filtered = np.asarray(
        Image.fromarray(source, "RGB").filter(ImageFilter.Color3DLUT(lut.size, lut.table)),
        dtype=np.uint8,
    )
    if strength >= 1.0:
        return filtered
    result = np.empty(rgb.shape, dtype=np.uint8)
    for y in range(0, rgb.shape[0], 512):
        base = rgb[y:y + 512].astype(np.float32)
        result[y:y + 512] = np.clip(base + (filtered[y:y + 512].astype(np.float32) - base) * strength, 0, 255).astype(np.uint8)
    return result


def adjust_colour(rgb: np.ndarray, brightness: float, contrast: float, saturation: float) -> np.ndarray:
    """Правит яркость, контраст и насыщенность средствами Pillow.

    Значения задаются как -1..1 вокруг нуля: ноль на ползунке обязан оставлять
    кадр неизменным, поэтому в множитель Pillow уходит `1 + значение`.
    """
    amounts = (
        (ImageEnhance.Brightness, float(np.clip(brightness, -1, 1))),
        (ImageEnhance.Contrast, float(np.clip(contrast, -1, 1))),
        (ImageEnhance.Color, float(np.clip(saturation, -1, 1))),
    )
    if not any(amount for _, amount in amounts):
        return rgb
    image = Image.fromarray(rgb, "RGB")
    for enhancer, amount in amounts:
        if amount:
            image = enhancer(image).enhance(1.0 + amount)
    return np.asarray(image, dtype=np.uint8)


def _blur(channel: np.ndarray, radius: float) -> np.ndarray:
    image = Image.fromarray(np.clip(channel, 0, 255).astype(np.uint8), "L")
    return np.asarray(image.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


# Векторы пигментов кожи в пространстве оптической плотности RGB. Модель кожи
# Tsumura и др. (ICA of skin color image): по закону Ламберта—Бера плотность
# кожи раскладывается на меланин, гемоглобин и общий множитель освещённости.
# Точность векторов не критична: алгоритм подавляет не абсолютные значения
# компонент, а лишь их пространственную неровность.
_MELANIN = np.array((.4143, .3570, .8372), dtype=np.float32)
_HEMOGLOBIN = np.array((.2988, .6838, .6657), dtype=np.float32)
_SHADING = np.array((1., 1., 1.), dtype=np.float32) / math.sqrt(3.0)
_PIGMENT_BASIS = np.stack((_MELANIN, _HEMOGLOBIN, _SHADING), axis=1).astype(np.float32)
_PIGMENT_SPLIT = np.linalg.inv(_PIGMENT_BASIS).astype(np.float32)
_DENSITY_FLOOR = np.float32(1e-6)
_LUMA = np.array((.2126729, .7151522, .0721750), dtype=np.float32)
# Доля яркостного изменения, которую разрешается оставить. Выравнивание
# тона отвечает за цвет, а светотенью занимается второй ползунок: если тон
# меняет светлоту заметно, кожа сразу выглядит замыленной. Небольшая доля
# всё же остаётся, иначе тёмно-красное пятно так и осталось бы тёмным.
_LUMA_SHARE = np.float32(.2)
# Как сильно гасится зональный избыток гемоглобина против медианы кожи.
_ZONAL_SHARE = np.float32(.7)
# Матирование. Радиус — доля лица, на которой ищется матовый тон кожи и
# средний уровень блеска: он обязан быть заметно больше жирного пятна на лбу.
_MATTE_RADIUS = .35
# Радиус сглаживания карты блеска в долях лица: срезается только низкая
# частота, поры и микроблёстки в неё не попадают.
_MATTE_DETAIL = .03
# Доля яркости пикселя, которую вообще разрешено считать блеском, и предел
# затемнения: полностью гасить блик нельзя, иначе на его месте мёртвое пятно.
_MATTE_CEILING = np.float32(.75)
_MATTE_FLOOR = np.float32(.45)
# Длинная сторона рабочего холста масок кожи. Сегментатор отвечает сеткой 256,
# парсинг лица — 512 пикселей, поэтому собирать и размывать маски на полном
# кадре бессмысленно: результат тот же, а времени и памяти уходит в разы больше.
_MASK_SIDE = 1600


def _box_pass(values: np.ndarray, radius: int) -> np.ndarray:
    """Усредняет столбцы окном 2*radius+1 через префиксные суммы."""
    padded = np.pad(values, ((radius + 1, radius), (0, 0)), mode="edge")
    integral = np.cumsum(padded, axis=0, dtype=np.float32)
    window = 2 * radius + 1
    return (integral[window:] - integral[:-window]) / np.float32(window)


def _smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    """Приближает гаусс тремя проходами бокс-фильтра.

    Pillow умеет размывать только 8-битные каналы, а плотности пигментов нужны
    в float: квантование в uint8 стирает как раз те доли процента, из которых
    и состоит пятнистость кожи. Тройной бокс-фильтр на префиксных суммах стоит
    O(N) независимо от радиуса, поэтому большие зональные радиусы бесплатны.
    """
    radius = int(round(math.sqrt(max(sigma, 0.0) ** 2 * 4.0 + 1.0) / 2.0))
    if radius < 1:
        return values.astype(np.float32, copy=True)
    result = values.astype(np.float32, copy=False)
    for _ in range(3):
        result = _box_pass(result, radius)
        result = np.ascontiguousarray(_box_pass(np.ascontiguousarray(result.T), radius).T)
    return result


def _masked_smooth(values: np.ndarray, weight: np.ndarray, sigma: float) -> np.ndarray:
    """Сглаживает канал, не подмешивая ничего из-за границы маски кожи."""
    return _smooth(values * weight, sigma) / np.maximum(_smooth(weight, sigma), 1e-4)


def _resize_map(values: np.ndarray, size: tuple[int, int], resample: Image.Resampling) -> np.ndarray:
    return np.asarray(Image.fromarray(values.astype(np.float32), "F").resize(size, resample), dtype=np.float32)


def _reduce_rgb(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Уменьшает кадр целиком: три канала за один проход дешевле трёх float-карт."""
    return np.asarray(Image.fromarray(rgb, "RGB").resize(size, Image.Resampling.BOX))


def _reduce_weight(weights: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Уменьшает маску 0..1 по 8-битному пути: шаг 1/255 для маски незаметен."""
    packed = np.clip(weights * 255.0 + .5, 0, 255).astype(np.uint8)
    return np.asarray(Image.fromarray(packed, "L").resize(size, Image.Resampling.BOX), dtype=np.float32) / 255.0


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    if rgb.dtype == np.uint8:
        # Вход 8-битный, поэтому таблица из 256 значений точна по определению и
        # заменяет возведение в степень на кадре в десятки мегапикселей.
        return _LINEAR_LUT[rgb]
    source = rgb.astype(np.float32) / 255.0
    return np.where(source <= .04045, source / 12.92, ((source + .055) / 1.055) ** 2.4)


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    clipped = np.clip(linear, 0.0, 1.0)
    return np.where(clipped <= .0031308, clipped * 12.92, 1.055 * clipped ** (1 / 2.4) - .055)


_LINEAR_LUT = _srgb_to_linear(np.arange(256, dtype=np.float32)).astype(np.float32)
# Кодирование обратно в 8 бит идёт через таблицу по корню из линейного света:
# корень сгущает отсчёты у чёрного, где кривая sRGB круче всего, поэтому 65536
# ступеней дают побитово тот же результат, что прямое возведение в степень.
_ENCODE_STEPS = 65536
_ENCODE_LUT = np.clip(
    _linear_to_srgb((np.arange(_ENCODE_STEPS, dtype=np.float32) / (_ENCODE_STEPS - 1)) ** 2) * 255.0 + .5,
    0,
    255,
).astype(np.uint8)


def _encode(linear: np.ndarray) -> np.ndarray:
    """Переводит линейный свет в 8-битный sRGB через таблицу."""
    index = np.sqrt(np.clip(linear, 0.0, 1.0)) * np.float32(_ENCODE_STEPS - 1)
    return _ENCODE_LUT[index.astype(np.uint16)]


def _pigments(rgb: np.ndarray) -> np.ndarray:
    """Раскладывает sRGB на меланин, гемоглобин и освещённость.

    Пол яркости держится глубоко под чёрным пикселем: он обязан спасти
    логарифм от бесконечности, но не сдвинуть нетронутые тёмные пиксели при
    обратной сборке.
    """
    return (-np.log(np.maximum(_srgb_to_linear(rgb), _DENSITY_FLOOR))) @ _PIGMENT_SPLIT.T


def even_skin_tone(
    rgb: np.ndarray,
    weights: np.ndarray,
    strength: float,
    face_scale: float,
    face_weight: np.ndarray | None = None,
) -> np.ndarray:
    """Выравнивает тон кожи, подавляя неровность пигментов в полосе частот пятен.

    Кожа переводится в оптическую плотность и раскладывается на меланин,
    гемоглобин и освещённость. Пятнистость (краснота, сосуды, пигментные
    неровности) живёт в средних пространственных частотах карт пигментов, тогда
    как поры и шум — в высоких, а светотень и анатомия — в низких. Поэтому
    вычитается полосовой остаток карт пигментов: текстура и объём лица
    остаются нетронутыми, а канал освещённости не меняется вовсе. Для
    гемоглобина к полосе добавляется зональный избыток над медианой кожи —
    иначе краснота размером с нос или скулу целиком лежит в низких частотах и
    не правится вообще.

    Результат приводится к исходной светлоте: тон отвечает за цвет, светотенью
    занимается отдельный ползунок dodge/burn.

    `weights` — маска кожи 0..1, `face_scale` — характерный размер лица в
    пикселях (задаёт радиусы), `face_weight` — доля лица в пикселе: руки, шея и
    плечи выравниваются слабее, там неровность обычно и есть натуральный вид.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return rgb.copy()
    height, width = rgb.shape[:2]

    # Оценка неровности считается на уменьшенной копии: пятна — крупные детали,
    # а полный размер тратил бы память и время на заведомо сглаживаемое.
    scale = min(1.0, 128.0 / max(face_scale, 32.0))
    small = (max(24, min(width, int(round(width * scale)))), max(24, min(height, int(round(height * scale)))))
    small_scale = min(small[0] / width, small[1] / height)
    fine_sigma = max(1.0, face_scale * small_scale * .035)
    base_sigma = max(4.0, face_scale * small_scale * .42)
    small_weight = _reduce_weight(weights, small)
    small_pigments = _pigments(_reduce_rgb(rgb, small))
    confident = small_weight > .5
    if np.count_nonzero(confident) <= 24:
        confident = np.ones_like(small_weight, dtype=bool)
    residual = np.empty((small[1], small[0], 2), dtype=np.float32)
    for index in (0, 1):
        channel = small_pigments[..., index]
        fine = _masked_smooth(channel, small_weight, fine_sigma)
        base = _masked_smooth(channel, small_weight, base_sigma)
        band = fine - base
        if index == 1:
            # Красный нос или скулы — пятно размером с часть лица: оно целиком
            # сидит в низких частотах и полосовой остаток его не видит. Поэтому
            # зональный избыток гемоглобина считается от медианы по всей коже и
            # добавляется к полосе. Гасится только избыток: бледные зоны вроде
            # лба подкрашивать нельзя, иначе уйдёт естественный рельеф лица.
            level = float(np.median(channel[confident]))
            band = band + _ZONAL_SHARE * np.maximum(base - level, 0.0)
        # Мягкое ограничение выбросов: протечка маски на волосы или тень от
        # очков не должна оставить пятно-ореол. Порог считается отдельно для
        # меланина и гемоглобина: их масштабы отличаются на порядок. Перцентиль
        # рядом с медианным разбросом нужен для чистой кожи с парой ярких
        # пятен: там медиана почти нулевая и одна бы задавила всю коррекцию.
        magnitude = np.abs(band[confident])
        limit = max(
            float(np.median(magnitude)) * 1.4826 * 4.0,
            float(np.quantile(magnitude, .98)),
            1e-3,
        )
        residual[..., index] = limit * np.tanh(band / limit)

    # Слегка нелинейная шкала: середина уже хорошо видна, а 100 % убирает
    # пятнистость почти полностью.
    gain = strength * (.75 + .25 * strength)
    # Избыток гемоглобина (краснота, сосуды) убирается охотнее, чем добавляется
    # в бледные участки, а меланин трогаем сдержанно: полное выравнивание
    # веснушек и загара превращает кожу в пластик.
    scales = np.where(residual > 0, np.float32(1.0), np.float32(.72))
    scales[..., 0] *= np.float32(.55)
    residual *= scales * gain
    if face_weight is not None:
        # Граница разбора лица жёсткая и обрывается по рамке детектора, а
        # разная сила на лице и теле сделала бы из неё видимый прямоугольный
        # шов посередине щеки. Поэтому доля лица размазывается на четверть
        # лица и входит в карту коррекции ещё до увеличения.
        share = _smooth(_reduce_weight(face_weight, small), max(2.0, face_scale * small_scale * .25))
        residual *= (.55 + .45 * np.clip(share, 0.0, 1.0))[..., None]
    else:
        residual *= np.float32(.85)
    correction = np.stack(
        [_resize_map(residual[..., index], (width, height), Image.Resampling.BICUBIC) for index in (0, 1)],
        axis=-1,
    )

    # Применение идёт полосами: на кадре в десятки мегапикселей полноразмерные
    # промежуточные float-массивы стоят больше гигабайта памяти воркера.
    result = np.empty(rgb.shape, dtype=np.uint8)
    for y in range(0, height, 256):
        rows = slice(y, y + 256)
        linear = np.maximum(_srgb_to_linear(rgb[rows]), _DENSITY_FLOOR)
        pigments = (-np.log(linear)) @ _PIGMENT_SPLIT.T
        # Около чёрного и в выбитых бликах плотность недостоверна: там любая
        # правка даёт цветной шум, а не ровный тон.
        luminance = linear @ _LUMA
        gate = np.clip((luminance - .004) / .03, 0.0, 1.0) * np.clip((.995 - luminance) / .06, 0.0, 1.0)
        alpha = weights[rows] * gate
        pigments[..., 0:2] -= correction[rows] * alpha[..., None]
        corrected = np.exp(-(pigments @ _PIGMENT_BASIS.T))
        # Коррекция возвращается к исходной светлоте: меняется почти только
        # цветность, а светотень и объём остаются как в кадре. Нетронутые
        # пиксели при этом получают множитель ровно 1.
        target = luminance + _LUMA_SHARE * ((corrected @ _LUMA) - luminance)
        corrected *= (target / np.maximum(corrected @ _LUMA, 1e-5))[..., None]
        result[rows] = _encode(corrected)
    return result


def matte_skin(rgb: np.ndarray, weights: np.ndarray, strength: float, face_scale: float) -> np.ndarray:
    """Матирует кожу: гасит жирный блеск, сохраняя текстуру и цвет кожи.

    Блик — это нейтральный свет поверх кожи, поэтому пиксель раскладывается по
    двухцветной модели Клинкера: `наблюдаемое = диффузная кожа * её цвет +
    блеск * (1,1,1)`. Цвет матовой кожи берётся с соседних пикселей темнее
    локальной базы — на них блеска почти нет. Из полученной карты блеска
    вычитается её же средний уровень по коже: ровный общий подсвет трогать
    нельзя, иначе просто темнеет всё лицо, а гасить надо локальный избыток —
    лоб, нос, скулы.

    Дальше два шага. Светлота тушится умножением, а не вычитанием света: так
    локальный контраст сохраняется и шум почти выбитой области не вылезает
    вместе с блеском. Затем возвращается хроматичность: в блике красный канал
    уходит в потолок, поэтому одно затемнение оставляет серое пятно — цвет
    подтягивается к матовой коже рядом при неизменной светлоте.

    `weights` — маска кожи 0..1, `face_scale` — характерный размер лица в
    пикселях: он задаёт все радиусы, поэтому портрет и ростовой кадр
    обрабатываются одинаково.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return rgb.copy()
    height, width = rgb.shape[:2]

    # Блеск и цвет матовой кожи — крупные детали: анализ идёт на уменьшенной
    # копии, полный размер тратил бы память и время на заведомо сглаживаемое.
    scale = min(1.0, 160.0 / max(face_scale, 32.0))
    small = (max(24, min(width, int(round(width * scale)))), max(24, min(height, int(round(height * scale)))))
    small_scale = min(small[0] / width, small[1] / height)
    radius = max(3.0, face_scale * small_scale * _MATTE_RADIUS)
    detail = max(1.0, face_scale * small_scale * _MATTE_DETAIL)
    small_weight = _reduce_weight(weights, small)
    linear = _srgb_to_linear(_reduce_rgb(rgb, small))
    luminance = linear @ _LUMA
    confident = small_weight > .5
    if np.count_nonzero(confident) <= 24:
        return rgb.copy()

    base = _masked_smooth(luminance, small_weight, radius)
    dark = small_weight * np.clip((base - luminance) / np.maximum(base * .12, 1e-4) + .5, 0.0, 1.0)
    reference = np.stack([_masked_smooth(linear[..., channel], dark, radius) for channel in range(3)], axis=-1)
    reference /= np.maximum(reference @ _LUMA, 1e-5)[..., None]

    # Наименьшие квадраты для двух неизвестных (диффузная доля и блеск) на три
    # канала: 2x2 нормальные уравнения решаются в лоб.
    white = np.float32(3.0)
    ref_ref = (reference * reference).sum(-1)
    ref_white = reference.sum(-1)
    determinant = np.maximum(ref_ref * white - ref_white * ref_white, 1e-6)
    shine = (ref_ref * linear.sum(-1) - ref_white * (reference * linear).sum(-1)) / determinant
    shine = _smooth(np.minimum(np.maximum(shine, 0.0), luminance * _MATTE_CEILING) * small_weight, detail)
    excess = np.maximum(shine - _masked_smooth(shine, small_weight, radius), 0.0)
    unit = float(np.quantile(excess[confident], .999))
    if unit <= 1e-4:
        return rgb.copy()
    # Мягкое ограничение: у самого сильного блика убирается не всё, иначе на
    # его месте остаётся плоское пятно без светотени.
    excess = _smooth(np.float32(unit) * np.tanh(excess / np.float32(unit)) * small_weight, detail * .5) * strength

    # Карта блеска и цвет матовой кожи увеличиваются до кадра: синий канал
    # цвета восстанавливается из условия единичной яркости, поэтому в памяти
    # живут три карты вместо четырёх.
    maps = np.stack(
        [_resize_map(values, (width, height), Image.Resampling.BICUBIC) for values in (excess, reference[..., 0], reference[..., 1])],
        axis=-1,
    )

    result = np.empty(rgb.shape, dtype=np.uint8)
    for y in range(0, height, 256):
        rows = slice(y, y + 256)
        linear = _srgb_to_linear(rgb[rows])
        luminance = linear @ _LUMA
        amount = np.maximum(maps[rows, :, 0], 0.0) * weights[rows]
        gain = np.clip(1.0 - amount / np.maximum(luminance, 1e-4), _MATTE_FLOOR, 1.0)
        matted = linear * gain[..., None]
        target = matted @ _LUMA
        reference = np.stack(
            (
                maps[rows, :, 1],
                maps[rows, :, 2],
                (1.0 - _LUMA[0] * maps[rows, :, 1] - _LUMA[1] * maps[rows, :, 2]) / _LUMA[2],
            ),
            axis=-1,
        )
        share = np.clip(amount / np.float32(unit), 0.0, 1.0)
        mixed = matted + (np.maximum(reference, 0.0) * target[..., None] - matted) * share[..., None]
        # Возврат светлоты: цвет правится, а тушением занимается только gain.
        # У нетронутых пикселей и множитель, и доля ровно нулевые.
        mixed *= (target / np.maximum(mixed @ _LUMA, 1e-5))[..., None]
        result[rows] = _encode(mixed)
    return result


def _lightness(luminance: np.ndarray) -> np.ndarray:
    """Переводит линейную яркость в светлоту CIE L*, растянутую на 0..255."""
    delta = 6.0 / 29.0
    f = np.where(luminance > delta ** 3, np.cbrt(luminance), luminance / (3 * delta ** 2) + 4 / 29)
    return (116.0 * f - 16.0) * 2.55


def _luminance(lightness: np.ndarray) -> np.ndarray:
    """Обратный перевод светлоты 0..255 в линейную яркость."""
    delta = 6.0 / 29.0
    f = (lightness / 2.55 + 16.0) / 116.0
    return np.where(f > delta, f ** 3, 3 * delta ** 2 * (f - 4 / 29))


def dodge_burn(rgb: np.ndarray, weights: np.ndarray, strength: float) -> np.ndarray:
    """Гасит мелкие перепады светлоты на коже: локальные dodge и burn.

    Меняется только светлота, поэтому вместо полного Lab считается один канал
    L*, а к кадру применяется множитель яркости: цветность пикселя не едет, а
    памяти нужно втрое меньше — на кадре в 24 Мп это сотни мегабайт.
    """
    height, width = rgb.shape[:2]
    light = np.empty((height, width), dtype=np.float32)
    for y in range(0, height, 256):
        light[y:y + 256] = _lightness(_srgb_to_linear(rgb[y:y + 256]) @ _LUMA)
    sigma = max(3.5, min(height, width) * .006)
    local_weight = _blur(weights * 255, sigma) / 255
    local = _blur(light * weights, sigma) / np.maximum(local_weight, 1e-4)
    detail = light - local
    gate = np.clip((np.abs(detail) - 2.7) / 9, 0, 1) * (np.abs(detail) < 18) * (light > 52)
    shift = np.clip(-detail * .62, -7, 7) * gate * weights * strength
    result = np.empty(rgb.shape, dtype=np.uint8)
    for y in range(0, height, 256):
        rows = slice(y, y + 256)
        linear = _srgb_to_linear(rgb[rows])
        luminance = linear @ _LUMA
        target = _luminance(light[rows] + shift[rows])
        # Там, где ворота фильтра закрыты, пиксель обязан остаться прежним:
        # округление обратного перевода светлоты иначе шевелит младший бит и
        # добавляет в поры собственный шум.
        ratio = np.where(shift[rows] == 0, np.float32(1.0), target / np.maximum(luminance, 1e-5))
        result[rows] = _encode(linear * ratio[..., None])
    return result


class SkinRetoucher:
    """Владеет ONNX-сессиями только до завершения процесса-воркера."""

    def __init__(self, models_dir: Path) -> None:
        import onnxruntime as ort

        segmenter = models_dir / "selfie_multiclass_256x256.onnx"
        retoucher = models_dir / "opt.onnx"
        face_parser = models_dir / "face_parsing_resnet18.onnx"
        missing = [path.name for path in (segmenter, retoucher, face_parser) if not path.is_file()]
        if missing:
            raise RuntimeError("Не найдены ONNX-модели: " + ", ".join(missing))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._segmenter = ort.InferenceSession(str(segmenter), options, providers=["CPUExecutionProvider"])
        self._retoucher = ort.InferenceSession(str(retoucher), options, providers=["CPUExecutionProvider"])
        self._face_parser = ort.InferenceSession(str(face_parser), options, providers=["CPUExecutionProvider"])
        self._segmenter_input = self._segmenter.get_inputs()[0].name
        self._retoucher_input = self._retoucher.get_inputs()[0].name
        self._face_parser_input = self._face_parser.get_inputs()[0].name
        cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 4
        # Воркер живёт в отдельном процессе, а интерфейс в это время ждёт его
        # результата, поэтому плитки нейроретуши занимают все ядра: это самый
        # дорогой этап и он линейно ускоряется потоками.
        self._workers = max(1, cpus)
        self._lut_cache: tuple[tuple[str, int], CubeLut] | None = None

    def skin_masks(self, rgb: np.ndarray) -> SkinMasks:
        """Считает маски кожи отдельно от обработки.

        Маски зависят только от кадра, поэтому предпросмотр считает их один раз и
        передаёт в `process` при каждом движении ползунка: сегментация и
        разбор лица стоят больше, чем весь цветовой этап на кадре превью.
        """
        return SkinMasks(*self._skin_masks(rgb))

    def _skin_masks(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray | None, float]:
        height, width = rgb.shape[:2]
        resized = np.asarray(Image.fromarray(rgb).resize((256, 256), Image.Resampling.BILINEAR), dtype=np.float32) / 255
        logits = self._segmenter.run(None, {self._segmenter_input: resized[None]})[0][0]
        classes = np.argmax(logits, axis=-1)
        selected = np.where((classes == 2) | (classes == 3), 255, 0).astype(np.uint8)
        binary = Image.fromarray(selected).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        # Вся арифметика масок идёт на рабочем холсте: сама маска родом из
        # сеток 256 и 512 пикселей, так что размывать и склеивать её на полном
        # кадре в десятки мегапикселей значит платить за точность, которой там нет.
        canvas = self._canvas(width, height)
        binary = binary.resize(canvas, Image.Resampling.NEAREST)
        skin_image = binary.filter(ImageFilter.GaussianBlur(max(1.4, min(canvas) * .0028)))
        face_skin, face_coverage, face_area, face_scale = self._facial_masks(rgb, canvas)
        if face_skin is not None and face_coverage is not None:
            # Сегментатор человека не различает детали лица. Внутри рамки лица его
            # ответ заменяется семантической маской, иначе губы и белки глаз иногда
            # попадают в ретушь даже при идеальных лендмарках.
            coverage = face_coverage.astype(np.float32) / 255.0
            merged = np.asarray(skin_image, dtype=np.float32) * (1.0 - coverage) + face_skin.astype(np.float32)
            skin_image = Image.fromarray(np.clip(merged, 0, 255).astype(np.uint8), "L")
        skin = np.asarray(skin_image.resize((width, height), Image.Resampling.BILINEAR), dtype=np.uint8)
        if face_area is not None:
            face_area = _resize_map(face_area, (width, height), Image.Resampling.BILINEAR)
        if face_scale <= 0:
            # Без найденного лица (кроп 100 %, спина, руки) остаётся оценка по кадру.
            face_scale = min(height, width) * .38
        return skin, face_area, face_scale

    @staticmethod
    def _canvas(width: int, height: int) -> tuple[int, int]:
        """Размер рабочего холста масок: длинная сторона не больше _MASK_SIDE."""
        share = min(1.0, _MASK_SIDE / max(width, height))
        return max(64, round(width * share)), max(64, round(height * share))

    def mask(self, rgb: np.ndarray) -> np.ndarray:
        """Возвращает маску кожи для внешних проверок без деталей лица."""
        return self._skin_masks(rgb)[0]

    def _facial_masks(self, rgb: np.ndarray, canvas: tuple[int, int]) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, float]:
        """Уточняет кожу лица по классам и сообщает характерный размер лица.

        Лендмарки/детектор используются только для быстрого поиска и кадрирования
        лиц. Принадлежность пикселя коже определяет отдельная модель парсинга:
        геометрические контуры не способны надёжно исключить помаду, глаза и волосы.
        Размер лица нужен выравниванию тона: радиусы фильтров задаются в долях
        лица, иначе на портрете и ростовом кадре сглаживаются разные детали.

        Разбор лица идёт по кропу оригинала, а маски собираются на холсте
        `canvas`: ответ парсинга всё равно приходит сеткой 512 пикселей.
        """
        try:
            from .face_analysis import _detect

            boxes, _landmarks, _scores = _detect(Image.fromarray(rgb), threshold=.55)
        except Exception:
            return None, None, None, 0.0
        if not len(boxes):
            return None, None, None, 0.0
        width, height = rgb.shape[1], rgb.shape[0]
        share = min(canvas[0] / width, canvas[1] / height)
        parsed_skin = Image.new("L", canvas, 0)
        coverage = Image.new("L", canvas, 0)
        skin_draw = ImageDraw.Draw(parsed_skin)
        coverage_draw = ImageDraw.Draw(coverage)
        image = Image.fromarray(rgb, "RGB")
        widths: list[float] = []
        for box in boxes:
            left, top, right, bottom = (float(value) for value in box[:4])
            face_width, face_height = right - left, bottom - top
            if face_width < 18 or face_height < 18:
                continue
            x0 = max(0, math.floor(left - face_width * .20))
            y0 = max(0, math.floor(top - face_height * .24))
            x1 = min(width, math.ceil(right + face_width * .20))
            y1 = min(height, math.ceil(bottom + face_height * .18))
            crop = image.crop((x0, y0, x1, y1)).resize((512, 512), Image.Resampling.BILINEAR)
            values = np.asarray(crop, dtype=np.float32) / 255.0
            values = (values - np.array((.485, .456, .406), dtype=np.float32)) / np.array((.229, .224, .225), dtype=np.float32)
            logits = self._face_parser.run(None, {self._face_parser_input: values.transpose(2, 0, 1)[None]})[0][0]
            labels = np.argmax(logits, axis=0).astype(np.uint8)
            # 1 — кожа, 7 и 8 — уши, 10 — нос, 14 — шея. Нос и уши модель
            # выделяет отдельными классами, и без них краснота носа оставалась
            # нетронутой — ровно там, где она заметнее всего. Класс 15 — это
            # ожерелье, а не шея. Губы, глаза, брови, волосы, зубы и одежда
            # по-прежнему исключаются до смешивания результата.
            crop_skin = np.where(np.isin(labels, (1, 7, 8, 10, 14)), 255, 0).astype(np.uint8)
            place = (round(x0 * share), round(y0 * share))
            box_size = (max(1, round(x1 * share) - place[0]), max(1, round(y1 * share) - place[1]))
            restored = Image.fromarray(crop_skin).resize(box_size, Image.Resampling.NEAREST)
            skin_draw.bitmap(place, restored, fill=255)
            coverage_draw.rectangle((place[0], place[1], place[0] + box_size[0], place[1] + box_size[1]), fill=255)
            widths.append(face_width)
        # Мягкий край маски не даёт заметного контура на стыке лица и тела.
        softness = max(2.0, min(canvas) * .004)
        skin = np.asarray(parsed_skin.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8)
        return (
            skin,
            np.asarray(coverage.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8),
            skin.astype(np.float32) / 255.0,
            float(np.median(widths)) if widths else 0.0,
        )

    @staticmethod
    def _regions(mask: np.ndarray, margin: int) -> list[tuple[int, int, int, int]]:
        """Ограничивает дорогие цветовые операции расширенными областями кожи."""
        height, width = mask.shape
        small = np.asarray(Image.fromarray(mask).resize((min(256, width), min(256, height)), Image.Resampling.BOX)) >= 8
        ys, xs = np.nonzero(small)
        if not len(xs):
            return []
        x0 = max(0, math.floor(xs.min() * width / small.shape[1]) - margin)
        y0 = max(0, math.floor(ys.min() * height / small.shape[0]) - margin)
        x1 = min(width, math.ceil((xs.max() + 1) * width / small.shape[1]) + margin)
        y1 = min(height, math.ceil((ys.max() + 1) * height / small.shape[0]) + margin)
        return [(x0, y0, x1, y1)]

    def _neural(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        tile, core, border = 278, 214, 32
        height, width = rgb.shape[:2]
        padded_h = math.ceil(height / core) * core
        padded_w = math.ceil(width / core) * core
        padded = np.pad(rgb, ((border, border + padded_h - height), (border, border + padded_w - width), (0, 0)), mode="reflect")
        result = padded[border:border + padded_h, border:border + padded_w].copy()
        padded_mask = np.pad(mask, ((0, padded_h - height), (0, padded_w - width)))
        jobs = [(y, x) for y in range(0, padded_h, core) for x in range(0, padded_w, core) if np.any(padded_mask[y:y + core, x:x + core])]

        def infer(job: tuple[int, int]) -> tuple[int, int, np.ndarray]:
            y, x = job
            patch = padded[y:y + tile, x:x + tile].astype(np.float32).transpose(2, 0, 1)[None] / 255
            prediction = self._retoucher.run(None, {self._retoucher_input: patch})[0][0]
            return y, x, np.clip(prediction.transpose(1, 2, 0) * 255, 0, 255).astype(np.uint8)

        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            for y, x, prediction in executor.map(infer, jobs):
                result[y:y + core, x:x + core] = prediction
        return result[:height, :width]

    def neural_retouch(self, rgb: np.ndarray, mask: np.ndarray, strength: float) -> np.ndarray:
        """Накладывает нейроретушь отдельным шагом поверх готового кадра.

        Выделена из `process` ради предпросмотра: сначала на экран показывается
        быстрый цветовой результат, а самый дорогой этап догоняет его потом.
        """
        strength = float(np.clip(strength, 0, 1))
        if strength <= 0.0:
            return rgb
        cleaned = self._neural(rgb, mask)
        alpha = _blur(mask.astype(np.float32), 1.3)[:, :, None] / 255 * strength
        result = np.empty(rgb.shape, dtype=np.uint8)
        for y in range(0, rgb.shape[0], 256):
            source = rgb[y:y + 256].astype(np.float32)
            result[y:y + 256] = np.clip(source + (cleaned[y:y + 256].astype(np.float32) - source) * alpha[y:y + 256], 0, 255).astype(np.uint8)
        return result

    def lut(self, path: str) -> CubeLut | None:
        """Даёт разобранную таблицу, держа её в кэше по пути и времени правки.

        Разбор текстового .cube на 33³ узла — сотни тысяч чисел, а таблица одна на
        всю партию и на каждое движение ползунка в предпросмотре.
        """
        if not path:
            return None
        try:
            key = (path, Path(path).stat().st_mtime_ns)
        except OSError:
            return None
        if self._lut_cache is not None and self._lut_cache[0] == key:
            return self._lut_cache[1]
        lut = load_cube_lut(path)
        self._lut_cache = (key, lut)
        return lut

    def finish(self, rgb: np.ndarray, settings: RetouchSettings) -> np.ndarray:
        """Доводит кадр после ретуши кожи: тон и цвет, затем таблица.

        Порядок важен: творческий LUT пишут под готовую картинку, поэтому
        яркость и насыщенность правятся до него, а не после.
        """
        result = adjust_colour(rgb, settings.brightness, settings.contrast, settings.saturation)
        lut = self.lut(settings.lut_path)
        if lut is not None:
            result = apply_lut(result, lut, settings.lut_strength)
        return result

    def process(self, rgb: np.ndarray, settings: RetouchSettings, masks: SkinMasks | None = None) -> np.ndarray:
        return self.finish(self.retouch_skin(rgb, settings, masks), settings)

    def retouch_skin(self, rgb: np.ndarray, settings: RetouchSettings, masks: SkinMasks | None = None) -> np.ndarray:
        """Выполняет только этапы по маске кожи, без тона, цвета и таблицы.

        Отделена от `finish` ради предпросмотра: нейроретушь догоняет быстрый
        ответ позже, но ложится в пайплайне до цвета и LUT, а не поверх них.
        """
        tone = float(np.clip(settings.tone_strength, 0, 1))
        matte = float(np.clip(settings.matte_strength, 0, 1))
        burn = float(np.clip(settings.dodge_burn, 0, 1))
        neural = settings.neural_retouch and settings.neural_strength > 0
        if not (tone or matte or burn or neural):
            # Ни одного этапа по коже: сегментацию и разбор лица считать незачем.
            return rgb.copy()
        ready = masks if masks is not None else self.skin_masks(rgb)
        mask, face_area, face_scale = ready.skin, ready.face_area, ready.face_scale
        if np.count_nonzero(mask) < 300:
            return rgb.copy()
        result = rgb.copy()
        if tone or matte or burn:
            # Запас вокруг кожи берётся по самому широкому фильтру выравнивания.
            regions = self._regions(mask, math.ceil(max(face_scale * .42, 16.0) * 3))
            if regions:
                x0, y0, x1, y1 = regions[0]
                weights = mask[y0:y1, x0:x1].astype(np.float32) / 255
                # Матирование идёт первым: в блике красный канал выбит, и
                # разложение на меланин с гемоглобином по нему врёт.
                if matte:
                    result[y0:y1, x0:x1] = matte_skin(result[y0:y1, x0:x1], weights, matte, face_scale)
                if tone:
                    result[y0:y1, x0:x1] = even_skin_tone(
                        result[y0:y1, x0:x1],
                        weights,
                        tone,
                        face_scale,
                        None if face_area is None else face_area[y0:y1, x0:x1],
                    )
                if burn:
                    result[y0:y1, x0:x1] = dodge_burn(result[y0:y1, x0:x1], weights, burn)
        if settings.neural_retouch:
            result = self.neural_retouch(result, mask, settings.neural_strength)
        return result
