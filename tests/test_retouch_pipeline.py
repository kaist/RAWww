## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки чистых функций пакетной ретуши: выравнивание тона кожи.

ONNX-модели здесь не нужны: `even_skin_tone` работает с уже готовой маской.
"""

from __future__ import annotations

import unittest

import numpy as np

from rawww.retouch_pipeline import _pigments, _smooth, even_skin_tone


def _skin(size: int = 320, blotches: bool = True) -> np.ndarray:
    """Синтетическая кожа: светотень, поры и пятна гемоглобина.

    Изображение строится прямо из карт пигментов, поэтому у теста есть
    достоверная «правда» о том, что должно исчезнуть, а что остаться.
    """
    from rawww.retouch_pipeline import _PIGMENT_BASIS, _linear_to_srgb

    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    melanin = np.full((size, size), .30, np.float32)
    hemoglobin = np.full((size, size), .45, np.float32)
    if blotches:
        for center in ((.3, .3), (.7, .35), (.45, .65), (.75, .75)):
            cx, cy = center[0] * size, center[1] * size
            hemoglobin += .28 * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (size * .06) ** 2)))
    shading = .35 - .25 * np.exp(-(((xx - size * .4) ** 2 + (yy - size * .5) ** 2) / (2 * (size * .5) ** 2)))
    density = np.stack((melanin, hemoglobin, shading), axis=-1) @ _PIGMENT_BASIS.T
    density += rng.normal(0, .012, (size, size, 1)).astype(np.float32)
    return np.clip(_linear_to_srgb(np.exp(-density)) * 255 + .5, 0, 255).astype(np.uint8)


class SmoothTest(unittest.TestCase):
    def test_constant_survives(self) -> None:
        values = np.full((64, 64), 2.5, np.float32)
        self.assertTrue(np.allclose(_smooth(values, 6.0), 2.5, atol=1e-4))

    def test_impulse_spreads_and_keeps_energy(self) -> None:
        values = np.zeros((129, 129), np.float32)
        values[64, 64] = 1.0
        blurred = _smooth(values, 5.0)
        self.assertAlmostEqual(float(blurred.sum()), 1.0, places=3)
        self.assertLess(float(blurred[64, 64]), .02)
        self.assertGreater(float(blurred[64, 64]), float(blurred[64, 75]))


class PigmentTest(unittest.TestCase):
    def test_decomposition_is_reversible(self) -> None:
        from rawww.retouch_pipeline import _PIGMENT_BASIS, _linear_to_srgb

        rgb = _skin(96)
        restored = np.exp(-(_pigments(rgb) @ _PIGMENT_BASIS.T))
        restored = np.clip(_linear_to_srgb(restored) * 255 + .5, 0, 255).astype(np.uint8)
        self.assertLessEqual(int(np.abs(restored.astype(int) - rgb.astype(int)).max()), 1)


class EvenSkinToneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rgb = _skin()
        self.weights = np.ones(self.rgb.shape[:2], np.float32)
        self.face_scale = 220.0

    def _hemoglobin(self, rgb: np.ndarray) -> np.ndarray:
        return _pigments(rgb)[..., 1]

    def test_zero_strength_keeps_pixels(self) -> None:
        result = even_skin_tone(self.rgb, self.weights, 0.0, self.face_scale)
        self.assertTrue(np.array_equal(result, self.rgb))

    def test_blotches_fade_and_grow_with_strength(self) -> None:
        band = lambda rgb: _smooth(self._hemoglobin(rgb), 5) - _smooth(self._hemoglobin(rgb), 90)
        source = float(band(self.rgb).std())
        half = float(band(even_skin_tone(self.rgb, self.weights, .5, self.face_scale)).std())
        full = float(band(even_skin_tone(self.rgb, self.weights, 1.0, self.face_scale)).std())
        self.assertLess(half, source * .8)
        self.assertLess(full, half * .8)

    def test_texture_and_shading_survive(self) -> None:
        result = even_skin_tone(self.rgb, self.weights, 1.0, self.face_scale)
        source, corrected = self._hemoglobin(self.rgb), self._hemoglobin(result)
        texture = lambda values: float((values - _smooth(values, 2)).std())
        self.assertAlmostEqual(texture(corrected), texture(source), delta=texture(source) * .5)
        light = lambda rgb: _smooth(_pigments(rgb)[..., 2], 40)
        self.assertLess(float(np.abs(light(result) - light(self.rgb)).max()), .02)

    def test_mask_limits_correction(self) -> None:
        weights = self.weights.copy()
        weights[:, :100] = 0.0
        result = even_skin_tone(self.rgb, weights, 1.0, self.face_scale)
        self.assertTrue(np.array_equal(result[:, :100], self.rgb[:, :100]))
        self.assertFalse(np.array_equal(result[:, 160:], self.rgb[:, 160:]))

    def test_face_weight_softens_body(self) -> None:
        body = even_skin_tone(self.rgb, self.weights, 1.0, self.face_scale, np.zeros(self.rgb.shape[:2], np.float32))
        face = even_skin_tone(self.rgb, self.weights, 1.0, self.face_scale, np.ones(self.rgb.shape[:2], np.float32))
        difference = lambda rgb: float(np.abs(rgb.astype(np.float32) - self.rgb).mean())
        self.assertLess(difference(body), difference(face))

    def test_black_pixels_are_left_alone(self) -> None:
        rgb = self.rgb.copy()
        rgb[:40] = 0
        result = even_skin_tone(rgb, self.weights, 1.0, self.face_scale)
        self.assertTrue(np.array_equal(result[:30], rgb[:30]))


if __name__ == "__main__":
    unittest.main()
