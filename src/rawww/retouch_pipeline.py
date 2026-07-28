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

    def _skin_masks(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
        height, width = rgb.shape[:2]
        resized = np.asarray(Image.fromarray(rgb).resize((256, 256), Image.Resampling.BILINEAR), dtype=np.float32) / 255
        logits = self._segmenter.run(None, {self._segmenter_input: resized[None]})[0][0]
        classes = np.argmax(logits, axis=-1)
        selected = np.where((classes == 2) | (classes == 3), 255, 0).astype(np.uint8)
        binary = Image.fromarray(selected).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        binary = binary.resize((width, height), Image.Resampling.NEAREST)
        skin = np.asarray(binary.filter(ImageFilter.GaussianBlur(max(1.4, min(height, width) * .0028))), dtype=np.uint8)
        face_skin, face_coverage, face_reference, face_area = self._facial_masks(rgb)
        if face_skin is not None and face_coverage is not None:
            # Сегментатор человека не различает детали лица. Внутри рамки лица его
            # ответ заменяется семантической маской, иначе губы и белки глаз иногда
            # попадают в ретушь даже при идеальных лендмарках.
            coverage = face_coverage.astype(np.float32) / 255.0
            skin = np.clip(skin.astype(np.float32) * (1.0 - coverage) + face_skin.astype(np.float32), 0, 255).astype(np.uint8)
        return skin, face_reference, face_area

    def mask(self, rgb: np.ndarray) -> np.ndarray:
        """Возвращает маску кожи для внешних проверок без деталей лица."""
        return self._skin_masks(rgb)[0]

    def _facial_masks(self, rgb: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, np.ndarray | None]:
        """Уточняет кожу лица по классам и строит зоны для оценки её цвета.

        Лендмарки/детектор используются только для быстрого поиска и кадрирования
        лиц. Принадлежность пикселя коже определяет отдельная модель парсинга:
        геометрические контуры не способны надёжно исключить помаду, глаза и волосы.
        """
        try:
            from .face_analysis import _detect

            boxes, _landmarks, _scores = _detect(Image.fromarray(rgb), threshold=.55)
        except Exception:
            return None, None, None, None
        if not len(boxes):
            return None, None, None, None
        width, height = rgb.shape[1], rgb.shape[0]
        parsed_skin = Image.new("L", (width, height), 0)
        coverage = Image.new("L", (width, height), 0)
        reference = Image.new("L", (rgb.shape[1], rgb.shape[0]), 0)
        face_area = Image.new("L", (rgb.shape[1], rgb.shape[0]), 0)
        skin_draw = ImageDraw.Draw(parsed_skin)
        coverage_draw = ImageDraw.Draw(coverage)
        reference_draw = ImageDraw.Draw(reference)
        face_draw = ImageDraw.Draw(face_area)
        image = Image.fromarray(rgb, "RGB")
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
            # 1 — кожа, 14 и 15 — шея. Все остальные классы (включая губы,
            # глаза, брови, волосы и зубы) исключаются до смешивания результата.
            crop_skin = np.where(np.isin(labels, (1, 14, 15)), 255, 0).astype(np.uint8)
            restored = Image.fromarray(crop_skin).resize((x1 - x0, y1 - y0), Image.Resampling.NEAREST)
            skin_draw.bitmap((x0, y0), restored, fill=255)
            coverage_draw.rectangle((x0, y0, x1, y1), fill=255)
            face_draw.bitmap((x0, y0), restored, fill=255)

            # Щёки и верх лба являются лишь фильтром выбора образца. Их всё равно
            # пересекаем с семантической кожей, чтобы эталон не включал волосы/глаза.
            center_x = (left + right) / 2.0
            for sign in (-1.0, 1.0):
                cx = center_x + sign * face_width * .22
                cy = top + face_height * .58
                reference_draw.ellipse((cx - face_width * .17, cy - face_height * .17, cx + face_width * .17, cy + face_height * .17), fill=255)
            reference_draw.ellipse((center_x - face_width * .24, top + face_height * .08, center_x + face_width * .24, top + face_height * .30), fill=255)
        semantic_skin = np.asarray(parsed_skin, dtype=np.uint8)
        raw_reference = np.asarray(reference, dtype=np.uint8)
        reference = Image.fromarray(np.minimum(semantic_skin, raw_reference), "L")
        # Мягкий край маски не даёт заметного контура на стыке лица и тела.
        softness = max(2.0, min(rgb.shape[:2]) * .004)
        return (
            np.asarray(parsed_skin.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8),
            np.asarray(coverage.filter(ImageFilter.GaussianBlur(softness)), dtype=np.uint8),
            np.asarray(reference.filter(ImageFilter.GaussianBlur(softness)), dtype=np.float32) / 255.0,
            np.asarray(face_area.filter(ImageFilter.GaussianBlur(softness)), dtype=np.float32) / 255.0,
        )

    @staticmethod
    def _regions(mask: np.ndarray, margin: int) -> list[tuple[int, int, int, int]]:
        """Ограничивает дорогие Lab-операции расширенными областями кожи."""
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

    @staticmethod
    def _weighted_mean(channel: np.ndarray, weight: np.ndarray, radius: float) -> np.ndarray:
        """Возвращает цвет кожи без влияния фона за границей маски."""
        soft = _blur(weight * 255, radius) / 255
        return _blur(channel * weight, radius) / np.maximum(soft, 1e-4)

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
        mask, face_reference, face_area = self._skin_masks(rgb)
        if np.count_nonzero(mask) < 300:
            return rgb.copy()
        result = rgb.copy()
        tone = float(np.clip(settings.tone_strength, 0, 1))
        burn = float(np.clip(settings.dodge_burn, 0, 1))
        if tone or burn:
            radius = max(16., min(rgb.shape[:2]) * .032)
            zone_radius = max(radius * 3.2, min(rgb.shape[:2]) * .15)
            regions = self._regions(mask, math.ceil(zone_radius * 3))
            if regions:
                x0, y0, x1, y1 = regions[0]
                weights = mask[y0:y1, x0:x1].astype(np.float32) / 255
                lab = _rgb_to_lab(result[y0:y1, x0:x1])
                valid = weights > .35
                if tone and np.any(valid):
                    # Корректируем вектор цветности Lab целиком, а не a и b по
                    # отдельности. Прямое притягивание двух каналов к среднему
                    # превращает здоровую тёплую кожу в грязный серо-зелёный цвет.
                    # Здесь сохраняется исходная насыщенность и меняется лишь
                    # аномальное направление цвета; красный нос дополнительно
                    # теряет только избыток насыщенности относительно окружения.
                    zonal_mix = .72
                    channels = lab[..., 1:3] - 128.0
                    local = np.stack((
                        self._weighted_mean(channels[..., 0], weights, radius),
                        self._weighted_mean(channels[..., 1], weights, radius),
                    ), axis=-1)
                    zonal = np.stack((
                        self._weighted_mean(channels[..., 0], weights, zone_radius),
                        self._weighted_mean(channels[..., 1], weights, zone_radius),
                    ), axis=-1)
                    reference = local * (1.0 - zonal_mix) + zonal * zonal_mix
                    if face_reference is not None and face_area is not None:
                        stable = (
                            (face_reference > .55)
                            & (weights > .35)
                            & (lab[..., 0] > 88)
                            & (lab[..., 0] < 220)
                        )
                        if np.count_nonzero(stable) > 80:
                            face_tone = np.median(channels[stable], axis=0)
                            # Лицо получает более надёжный эталон щёк/лба.
                            # Руки и шея сохраняют свой локальный свет: для них
                            # направление лица лишь очень слабая подсказка.
                            influence = face_area[..., None] * .65 + (1.0 - face_area[..., None]) * .10
                            reference = reference * (1.0 - influence) + face_tone * influence
                    chroma = np.linalg.norm(channels, axis=-1)
                    reference_chroma = np.linalg.norm(reference, axis=-1)
                    unit = reference / np.maximum(reference_chroma[..., None], 1e-4)
                    cosine = np.sum(channels * unit, axis=-1) / np.maximum(chroma, 1e-4)
                    hue_outlier = np.clip((1.0 - cosine - .018) / .18, 0.0, 1.0)
                    excess_chroma = np.clip((chroma - reference_chroma - 4.0) / 16.0, 0.0, 1.0)
                    target_chroma = chroma - np.maximum(chroma - reference_chroma - 4.0, 0.0) * .90
                    target = unit * target_chroma[..., None]
                    shadow_gate = np.clip((lab[..., 0] - 72.0) / 62.0, 0.0, 1.0)
                    shadow_gate = shadow_gate * shadow_gate * (3.0 - 2.0 * shadow_gate)
                    # Нелинейная шкала оставляет середину управляемой, но даёт
                    # честный рабочий максимум: 100 % заметно сильнее 50 %.
                    tone_gain = tone * (.50 + 1.50 * tone)
                    alpha = np.clip(weights * shadow_gate * tone_gain * np.maximum(hue_outlier, excess_chroma), 0.0, 1.0)
                    channels += (target - channels) * alpha[..., None]
                    lab[..., 1:3] = channels + 128.0
                if burn:
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
