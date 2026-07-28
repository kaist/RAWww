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
from PIL import Image, ImageDraw, ImageFilter


@dataclass(frozen=True)
class RetouchSettings:
    """Параметры этапов ретуши, передаваемые воркеру как простые данные."""

    tone_strength: float = 0.50
    dodge_burn: float = 0.0
    neural_retouch: bool = True
    neural_strength: float = 0.50


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


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    source = rgb.astype(np.float32) / 255.0
    return np.where(source <= .04045, source / 12.92, ((source + .055) / 1.055) ** 2.4)


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    clipped = np.clip(linear, 0.0, 1.0)
    return np.where(clipped <= .0031308, clipped * 12.92, 1.055 * clipped ** (1 / 2.4) - .055)


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
    small_weight = _resize_map(weights, small, Image.Resampling.BOX)
    small_rgb = np.stack([_resize_map(rgb[..., channel], small, Image.Resampling.BOX) for channel in range(3)], axis=-1)
    small_pigments = _pigments(small_rgb)
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
        share = _smooth(_resize_map(face_weight, small, Image.Resampling.BOX), max(2.0, face_scale * small_scale * .25))
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
        result[rows] = np.clip(_linear_to_srgb(corrected) * 255.0 + .5, 0, 255).astype(np.uint8)
    return result


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    matrix = np.array(((.4124564, .3575761, .1804375), (.2126729, .7151522, .0721750), (.0193339, .1191920, .9503041)), dtype=np.float32)
    white = np.array((.95047, 1., 1.08883), dtype=np.float32)
    delta = 6.0 / 29.0
    result = np.empty(rgb.shape, dtype=np.float32)
    for y in range(0, rgb.shape[0], 256):
        source = rgb[y:y + 256].astype(np.float32) / 255.0
        linear = np.where(source <= .04045, source / 12.92, ((source + .055) / 1.055) ** 2.4)
        xyz = (linear @ matrix.T) / white
        f = np.where(xyz > delta ** 3, np.cbrt(xyz), xyz / (3 * delta ** 2) + 4 / 29)
        target = result[y:y + 256]
        target[..., 0] = (116 * f[..., 1] - 16) * 2.55
        target[..., 1] = 500 * (f[..., 0] - f[..., 1]) + 128
        target[..., 2] = 200 * (f[..., 1] - f[..., 2]) + 128
    return result


def _lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    matrix = np.array(((3.2404542, -1.5371385, -.4985314), (-.9692660, 1.8760108, .0415560), (.0556434, -.2040259, 1.0572252)), dtype=np.float32)
    white = np.array((.95047, 1., 1.08883), dtype=np.float32)
    delta = 6.0 / 29.0
    result = np.empty(lab.shape, dtype=np.uint8)
    for y in range(0, lab.shape[0], 256):
        values = lab[y:y + 256]
        fy = (values[..., 0] / 2.55 + 16) / 116
        fx = (values[..., 1] - 128) / 500 + fy
        fz = fy - (values[..., 2] - 128) / 200
        cube = np.stack((fx, fy, fz), axis=-1)
        xyz = np.where(cube > delta, cube ** 3, 3 * delta ** 2 * (cube - 4 / 29)) * white
        linear = xyz @ matrix.T
        encoded = np.where(linear <= .0031308, linear * 12.92, 1.055 * np.maximum(linear, 0) ** (1 / 2.4) - .055)
        result[y:y + 256] = np.clip(encoded * 255, 0, 255).astype(np.uint8)
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
        self._workers = max(1, cpus - max(1, cpus // 4))

    def _skin_masks(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray | None, float]:
        height, width = rgb.shape[:2]
        resized = np.asarray(Image.fromarray(rgb).resize((256, 256), Image.Resampling.BILINEAR), dtype=np.float32) / 255
        logits = self._segmenter.run(None, {self._segmenter_input: resized[None]})[0][0]
        classes = np.argmax(logits, axis=-1)
        selected = np.where((classes == 2) | (classes == 3), 255, 0).astype(np.uint8)
        binary = Image.fromarray(selected).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        binary = binary.resize((width, height), Image.Resampling.NEAREST)
        skin = np.asarray(binary.filter(ImageFilter.GaussianBlur(max(1.4, min(height, width) * .0028))), dtype=np.uint8)
        face_skin, face_coverage, face_area, face_scale = self._facial_masks(rgb)
        if face_skin is not None and face_coverage is not None:
            # Сегментатор человека не различает детали лица. Внутри рамки лица его
            # ответ заменяется семантической маской, иначе губы и белки глаз иногда
            # попадают в ретушь даже при идеальных лендмарках.
            coverage = face_coverage.astype(np.float32) / 255.0
            skin = np.clip(skin.astype(np.float32) * (1.0 - coverage) + face_skin.astype(np.float32), 0, 255).astype(np.uint8)
        if face_scale <= 0:
            # Без найденного лица (кроп 100 %, спина, руки) остаётся оценка по кадру.
            face_scale = min(height, width) * .38
        return skin, face_area, face_scale

    def mask(self, rgb: np.ndarray) -> np.ndarray:
        """Возвращает маску кожи для внешних проверок без деталей лица."""
        return self._skin_masks(rgb)[0]

    def _facial_masks(self, rgb: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, float]:
        """Уточняет кожу лица по классам и сообщает характерный размер лица.

        Лендмарки/детектор используются только для быстрого поиска и кадрирования
        лиц. Принадлежность пикселя коже определяет отдельная модель парсинга:
        геометрические контуры не способны надёжно исключить помаду, глаза и волосы.
        Размер лица нужен выравниванию тона: радиусы фильтров задаются в долях
        лица, иначе на портрете и ростовом кадре сглаживаются разные детали.
        """
        try:
            from .face_analysis import _detect

            boxes, _landmarks, _scores = _detect(Image.fromarray(rgb), threshold=.55)
        except Exception:
            return None, None, None, 0.0
        if not len(boxes):
            return None, None, None, 0.0
        width, height = rgb.shape[1], rgb.shape[0]
        parsed_skin = Image.new("L", (width, height), 0)
        coverage = Image.new("L", (width, height), 0)
        face_area = Image.new("L", (rgb.shape[1], rgb.shape[0]), 0)
        skin_draw = ImageDraw.Draw(parsed_skin)
        coverage_draw = ImageDraw.Draw(coverage)
        face_draw = ImageDraw.Draw(face_area)
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
            restored = Image.fromarray(crop_skin).resize((x1 - x0, y1 - y0), Image.Resampling.NEAREST)
            skin_draw.bitmap((x0, y0), restored, fill=255)
            coverage_draw.rectangle((x0, y0, x1, y1), fill=255)
            face_draw.bitmap((x0, y0), restored, fill=255)
            widths.append(face_width)
        # Мягкий край маски не даёт заметного контура на стыке лица и тела.
        softness = max(2.0, min(rgb.shape[:2]) * .004)
        return (
            np.asarray(parsed_skin.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8),
            np.asarray(coverage.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8),
            np.asarray(face_area.filter(ImageFilter.GaussianBlur(softness)), dtype=np.float32) / 255.0,
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

    def process(self, rgb: np.ndarray, settings: RetouchSettings) -> np.ndarray:
        mask, face_area, face_scale = self._skin_masks(rgb)
        if np.count_nonzero(mask) < 300:
            return rgb.copy()
        result = rgb.copy()
        tone = float(np.clip(settings.tone_strength, 0, 1))
        burn = float(np.clip(settings.dodge_burn, 0, 1))
        if tone or burn:
            # Запас вокруг кожи берётся по самому широкому фильтру выравнивания.
            regions = self._regions(mask, math.ceil(max(face_scale * .42, 16.0) * 3))
            if regions:
                x0, y0, x1, y1 = regions[0]
                weights = mask[y0:y1, x0:x1].astype(np.float32) / 255
                if tone:
                    result[y0:y1, x0:x1] = even_skin_tone(
                        result[y0:y1, x0:x1],
                        weights,
                        tone,
                        face_scale,
                        None if face_area is None else face_area[y0:y1, x0:x1],
                    )
                if burn:
                    lab = _rgb_to_lab(result[y0:y1, x0:x1])
                    light = lab[..., 0]
                    local_weight = _blur(weights * 255, max(3.5, min(lab.shape[:2]) * .006)) / 255
                    local = _blur(light * weights, max(3.5, min(lab.shape[:2]) * .006)) / np.maximum(local_weight, 1e-4)
                    detail = light - local
                    gate = np.clip((np.abs(detail) - 2.7) / 9, 0, 1) * (np.abs(detail) < 18) * (light > 52)
                    light += np.clip(-detail * .62, -7, 7) * gate * weights * burn
                    result[y0:y1, x0:x1] = _lab_to_rgb(lab)
        if settings.neural_retouch:
            cleaned = self._neural(result, mask)
            alpha = _blur(mask.astype(np.float32), 1.3)[:, :, None] / 255 * float(np.clip(settings.neural_strength, 0, 1))
            for y in range(0, result.shape[0], 256):
                source = result[y:y + 256].astype(np.float32)
                result[y:y + 256] = np.clip(source + (cleaned[y:y + 256].astype(np.float32) - source) * alpha[y:y + 256], 0, 255).astype(np.uint8)
        return result
