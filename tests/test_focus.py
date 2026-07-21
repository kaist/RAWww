## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image, ImageFilter

from rawww.cache import FolderCache
from rawww.focus import FOCUS_BLUR_THRESHOLD, analyze_focus, focus_is_defect


def _natural(width: int = 512, height: int = 384, sigma: float = 0.8) -> np.ndarray:
    """Синтетическая «резкая» текстура: сглаженный белый шум без пиксельного шума."""
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, size=(height, width)).astype(np.uint8)
    return np.asarray(Image.fromarray(noise).filter(ImageFilter.GaussianBlur(sigma)))


def _image(array: np.ndarray) -> Image.Image:
    return Image.fromarray(array).convert("RGB")


def _blur(array: np.ndarray, radius: float) -> np.ndarray:
    return np.array(_image(array).filter(ImageFilter.GaussianBlur(radius)).convert("L"))


def _horizontal_blur(array: np.ndarray, window: int) -> np.ndarray:
    """Смаз только по горизонтали — модель движения камеры вдоль одной оси."""
    base = array.astype(np.float32)
    padded = np.pad(base, ((0, 0), (window // 2, window // 2)), mode="edge")
    cumulative = np.concatenate(
        [np.zeros((base.shape[0], 1)), np.cumsum(padded, axis=1)], axis=1
    )
    length = base.shape[1]
    smeared = (cumulative[:, window:window + length] - cumulative[:, 0:length]) / window
    return smeared.astype(np.uint8)


class FocusAnalysisTests(unittest.TestCase):
    """Проверяет детектор брака по фокусу и смазу на синтетических кадрах."""

    def setUp(self) -> None:
        self.sharp = _natural()

    def test_sharp_image_is_not_flagged(self) -> None:
        result = analyze_focus(_image(self.sharp))
        self.assertFalse(result.is_defect())
        self.assertEqual(result.blur_type, "sharp")
        self.assertLess(result.blur, FOCUS_BLUR_THRESHOLD)

    def test_fully_defocused_image_is_flagged(self) -> None:
        result = analyze_focus(_image(_blur(self.sharp, 5)))
        self.assertTrue(result.is_defect())
        self.assertEqual(result.blur_type, "defocus")

    def test_shallow_depth_of_field_with_sharp_subject_is_not_flagged(self) -> None:
        # Размытый фон, но небольшой резкий участок — как боке с чётким объектом.
        frame = _blur(self.sharp, 5)
        frame[20:120, 20:160] = self.sharp[20:120, 20:160]
        result = analyze_focus(_image(frame))
        self.assertFalse(result.is_defect())
        self.assertGreater(result.sharp_ratio, 1.0)

    def test_motion_blur_is_flagged_and_classified(self) -> None:
        result = analyze_focus(_image(_horizontal_blur(self.sharp, 21)))
        self.assertTrue(result.is_defect())
        self.assertEqual(result.blur_type, "motion")

    def test_defocus_is_isotropic_motion_is_anisotropic(self) -> None:
        defocus = analyze_focus(_image(_blur(self.sharp, 5)))
        motion = analyze_focus(_image(_horizontal_blur(self.sharp, 21)))
        self.assertGreater(motion.anisotropy, defocus.anisotropy)

    def test_subject_sharpness_overrides_sharp_background(self) -> None:
        # Резкий фон, но размытое лицо — фронт/бэк-фокус, который порегионная
        # оценка по всему кадру пропустила бы.
        frame = self.sharp.copy()
        frame[100:200, 200:320] = _blur(self.sharp, 5)[100:200, 200:320]
        faces = [{"bbox": {"x": 200 / 512, "y": 100 / 384, "width": 120 / 512, "height": 100 / 384}}]
        result = analyze_focus(_image(frame), faces=faces)
        self.assertIsNotNone(result.subject)
        self.assertTrue(result.is_defect())

    def test_sharp_subject_on_blurred_background_is_not_flagged(self) -> None:
        frame = _blur(self.sharp, 5)
        frame[100:200, 200:320] = self.sharp[100:200, 200:320]
        faces = [{"bbox": {"x": 200 / 512, "y": 100 / 384, "width": 120 / 512, "height": 100 / 384}}]
        result = analyze_focus(_image(frame), faces=faces)
        self.assertFalse(result.is_defect())

    def test_output_is_deterministic(self) -> None:
        first = analyze_focus(_image(self.sharp))
        second = analyze_focus(_image(self.sharp))
        self.assertEqual(first.to_json(), second.to_json())

    def test_tiny_and_flat_images_are_handled(self) -> None:
        tiny = analyze_focus(Image.new("RGB", (5, 5), (128, 128, 128)))
        flat = analyze_focus(Image.new("RGB", (400, 300), (128, 128, 128)))
        self.assertTrue(tiny.is_defect())
        self.assertTrue(flat.is_defect())

    def test_to_json_round_trips_through_focus_is_defect(self) -> None:
        result = analyze_focus(_image(_blur(self.sharp, 5)))
        detail = {"focus": json.loads(result.to_json())}
        self.assertTrue(focus_is_defect(detail))
        self.assertFalse(focus_is_defect({}))
        self.assertFalse(focus_is_defect({"focus": "broken"}))


class FocusCacheTests(unittest.TestCase):
    """Проверяет хранение результатов фокуса в дисковом кэше папки."""

    def test_store_load_and_missing_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (64, 48), (30, 90, 150)).save(path)
            cache_root = folder / "central-cache"
            cache = FolderCache(folder, {"sample.jpg"}, cache_root=cache_root)
            try:
                self.assertEqual(cache.missing_ai_paths([path], "focus_analysis"), [path])
                cache.store_focus_analysis([(str(path), json.dumps({"blur": 0.9}))])
                self.assertEqual(cache.missing_ai_paths([path], "focus_analysis"), [])
                stored = cache.load_focus_analysis()
                self.assertIn("sample.jpg", stored)
                details = cache.load_photo_details()
                self.assertEqual(details["sample.jpg"]["focus"]["blur"], 0.9)
            finally:
                cache.close(flush=False)

    def test_unknown_table_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cache = FolderCache(folder, set(), cache_root=folder / "central-cache")
            try:
                with self.assertRaises(ValueError):
                    cache.missing_ai_paths([], "not_a_table")
            finally:
                cache.close(flush=False)


if __name__ == "__main__":
    unittest.main()
