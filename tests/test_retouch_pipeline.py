## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки чистых функций пакетной ретуши: выравнивание тона кожи.

ONNX-модели здесь не нужны: `even_skin_tone` работает с уже готовой маской.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from rawww import retouch_pipeline
from rawww.retouch_pipeline import (
    _lightness, _linear_to_srgb, _pigments, _smooth, _srgb_to_linear,
    adjust_colour, apply_lut, dodge_burn, even_skin_tone, load_cube_lut, matte_skin,
)


def _skin(size: int = 320, blotches: bool = True, zone: bool = False) -> np.ndarray:
    """Синтетическая кожа: светотень, поры и пятна гемоглобина.

    Изображение строится прямо из карт пигментов, поэтому у теста есть
    достоверная «правда» о том, что должно исчезнуть, а что остаться.
    """
    from rawww.retouch_pipeline import _PIGMENT_BASIS

    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    melanin = np.full((size, size), .30, np.float32)
    hemoglobin = np.full((size, size), .45, np.float32)
    if blotches:
        for center in ((.3, .3), (.7, .35), (.45, .65), (.75, .75)):
            cx, cy = center[0] * size, center[1] * size
            hemoglobin += .28 * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (size * .06) ** 2)))
    if zone:
        # Красный нос: одна большая зона целиком в низких частотах.
        hemoglobin += .33 * np.exp(
            -(((xx - size * .5) ** 2 + (yy - size * .5) ** 2) / (2 * (size * .17) ** 2))
        )
    shading = .35 - .25 * np.exp(-(((xx - size * .4) ** 2 + (yy - size * .5) ** 2) / (2 * (size * .5) ** 2)))
    density = np.stack((melanin, hemoglobin, shading), axis=-1) @ _PIGMENT_BASIS.T
    density += rng.normal(0, .012, (size, size, 1)).astype(np.float32)
    return np.clip(_linear_to_srgb(np.exp(-density)) * 255 + .5, 0, 255).astype(np.uint8)


def _shiny(size: int = 320, spread: float = .12, amount: float = .32) -> np.ndarray:
    """Кожа с жирным блеском: нейтральный свет поверх матовой кожи.

    Блик добавляется в линейном свете ровно так, как его описывает двухцветная
    модель, поэтому у теста есть достоверная «правда»: сколько света лишнее,
    какой под ним цвет кожи и какая текстура должна остаться.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    linear = _srgb_to_linear(_skin(size, blotches=False))
    blob = np.exp(-(((xx - size * .5) ** 2 + (yy - size * .42) ** 2) / (2 * (size * spread) ** 2)))
    return np.clip(_linear_to_srgb(linear + (blob * amount)[..., None]) * 255 + .5, 0, 255).astype(np.uint8)


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
        # Светотень проверяется по видимой светлоте, а не по каналу
        # освещённости: именно он берёт на себя возврат яркости после правки
        # пигментов, чтобы в кадре светлота осталась прежней.
        light = lambda rgb: _smooth(rgb.astype(np.float32) @ retouch_pipeline._LUMA, 40)
        self.assertLess(float(np.abs(light(result) - light(self.rgb)).max()), 2.0)

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

    def test_zonal_redness_is_removed(self) -> None:
        """Краснота размером с нос лежит ниже полосы пятен и требует зонального шага."""
        rgb = _skin(zone=True)
        size = rgb.shape[0]
        centre = (slice(int(size * .42), int(size * .58)),) * 2
        edge = (slice(0, int(size * .12)),) * 2
        excess = lambda image: float(self._hemoglobin(image)[centre].mean() - self._hemoglobin(image)[edge].mean())
        result = even_skin_tone(rgb, self.weights, 1.0, self.face_scale)
        self.assertLess(excess(result), excess(rgb) * .5)

    def test_lightness_is_almost_untouched(self) -> None:
        """Тон отвечает за цвет: светлота обязана остаться почти исходной."""
        rgb = _skin(zone=True)
        luma = lambda image: image.astype(np.float32) @ retouch_pipeline._LUMA
        shift = lambda image: float(np.abs(luma(image) - luma(rgb)).mean())
        kept = shift(even_skin_tone(rgb, self.weights, 1.0, self.face_scale))
        with mock.patch.object(retouch_pipeline, "_LUMA_SHARE", np.float32(1.0)):
            free = shift(even_skin_tone(rgb, self.weights, 1.0, self.face_scale))
        self.assertLess(kept, free * .4)

    def test_black_pixels_are_left_alone(self) -> None:
        rgb = self.rgb.copy()
        rgb[:40] = 0
        result = even_skin_tone(rgb, self.weights, 1.0, self.face_scale)
        self.assertTrue(np.array_equal(result[:30], rgb[:30]))


class MatteSkinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rgb = _shiny()
        self.weights = np.ones(self.rgb.shape[:2], np.float32)
        self.face_scale = 220.0
        size = self.rgb.shape[0]
        self.shine = (slice(int(size * .34), int(size * .50)), slice(int(size * .42), int(size * .58)))
        self.matte = (slice(int(size * .78), size), slice(int(size * .10), int(size * .40)))

    def _luma(self, rgb: np.ndarray) -> np.ndarray:
        return rgb.astype(np.float32) @ retouch_pipeline._LUMA

    def _saturation(self, rgb: np.ndarray) -> float:
        values = rgb.astype(np.float32)
        return float((1.0 - values.min(-1) / np.maximum(values.max(-1), 1.0)).mean())

    def test_zero_strength_keeps_pixels(self) -> None:
        self.assertTrue(np.array_equal(matte_skin(self.rgb, self.weights, 0.0, self.face_scale), self.rgb))

    def test_shine_fades_and_grows_with_strength(self) -> None:
        excess = lambda rgb: float(self._luma(rgb)[self.shine].mean() - self._luma(rgb)[self.matte].mean())
        half = matte_skin(self.rgb, self.weights, .5, self.face_scale)
        full = matte_skin(self.rgb, self.weights, 1.0, self.face_scale)
        self.assertLess(excess(half), excess(self.rgb) * .8)
        self.assertLess(excess(full), excess(half) * .8)

    def test_skin_colour_returns_under_shine(self) -> None:
        """Одно затемнение оставляет серое пятно: цвет обязан вернуться."""
        result = matte_skin(self.rgb, self.weights, 1.0, self.face_scale)
        target = self._saturation(self.rgb[self.matte])
        self.assertLess(self._saturation(self.rgb[self.shine]), target * .8)
        self.assertGreater(self._saturation(result[self.shine]), self._saturation(self.rgb[self.shine]) * 1.2)
        self.assertLess(self._saturation(result[self.shine]), target * 1.2)

    def test_texture_survives_under_shine(self) -> None:
        result = matte_skin(self.rgb, self.weights, 1.0, self.face_scale)
        texture = lambda rgb: float((self._luma(rgb) - _smooth(self._luma(rgb), 2))[self.shine].std())
        self.assertGreater(texture(result), texture(self.rgb) * .7)

    def test_even_sheen_is_left_alone(self) -> None:
        """Ровный общий подсвет — это освещение кадра, а не жирный блеск."""
        flat = _shiny(spread=8.0)
        peak = lambda rgb, out: float(np.abs(out.astype(np.float32) - rgb).max())
        even = peak(flat, matte_skin(flat, self.weights, 1.0, self.face_scale))
        spot = peak(self.rgb, matte_skin(self.rgb, self.weights, 1.0, self.face_scale))
        self.assertLess(even, spot * .35)

    def test_mask_limits_correction(self) -> None:
        weights = self.weights.copy()
        weights[:, :100] = 0.0
        result = matte_skin(self.rgb, weights, 1.0, self.face_scale)
        self.assertTrue(np.array_equal(result[:, :100], self.rgb[:, :100]))
        self.assertFalse(np.array_equal(result[self.shine], self.rgb[self.shine]))


class DodgeBurnTest(unittest.TestCase):
    """Dodge & Burn правит только светлоту и только мелкие перепады."""

    def setUp(self) -> None:
        # Кожа со светотенью и парой локальных темных пятен — ровно тот
        # масштаб неровности, за которым охотится dodge и burn.
        size = 320
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        shading = 1.0 - .18 * (xx / size)
        spots = np.zeros((size, size), np.float32)
        for cx, cy in ((110, 120), (210, 200)):
            spots += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.5 ** 2))
        base = np.array((208.0, 168.0, 150.0), np.float32)
        image = base * (shading * (1.0 - .07 * spots))[..., None]
        self.rgb = np.clip(image, 0, 255).astype(np.uint8)
        self.spot = (spots > .6)
        self.weights = np.ones(self.rgb.shape[:2], np.float32)

    def test_zero_strength_keeps_pixels(self) -> None:
        self.assertTrue(np.array_equal(dodge_burn(self.rgb, self.weights, 0.0), self.rgb))

    def test_local_spot_fades_and_shading_survives(self) -> None:
        light = lambda rgb: _lightness(_srgb_to_linear(rgb) @ retouch_pipeline._LUMA)
        source, result = light(self.rgb), light(dodge_burn(self.rgb, self.weights, 1.0))
        around = ~self.spot
        depth = lambda values: float(values[around].mean() - values[self.spot].mean())
        self.assertLess(depth(result), depth(source) * .95)
        # Объём лица — низкая частота и остаётся нетронутым.
        gradient = lambda values: float(values[10:310, 10:60].mean() - values[10:310, 260:310].mean())
        self.assertAlmostEqual(gradient(result), gradient(source), delta=.3)

    def test_hue_survives(self) -> None:
        """Меняется яркость пикселя, а не его цвет: правка идёт множителем."""
        result = dodge_burn(self.rgb, self.weights, 1.0).astype(np.float32) + 1.0
        source = self.rgb.astype(np.float32) + 1.0
        shift = np.abs(result[..., 0] / result[..., 2] - source[..., 0] / source[..., 2])
        self.assertLess(float(shift.max()), .04)

    def test_mask_limits_correction(self) -> None:
        weights = self.weights.copy()
        weights[:, :100] = 0.0
        result = dodge_burn(self.rgb, weights, 1.0)
        self.assertTrue(np.array_equal(result[:, :100], self.rgb[:, :100]))
        self.assertFalse(np.array_equal(result[self.spot], self.rgb[self.spot]))


class AdjustColourTest(unittest.TestCase):
    """Яркость, контраст и насыщенность после ретуши кожи."""

    def setUp(self) -> None:
        rng = np.random.default_rng(7)
        self.rgb = rng.integers(40, 210, (32, 32, 3), dtype=np.uint8)

    def test_zero_keeps_frame(self) -> None:
        self.assertTrue(np.array_equal(adjust_colour(self.rgb, 0.0, 0.0, 0.0), self.rgb))

    def test_brightness_moves_mean(self) -> None:
        brighter = adjust_colour(self.rgb, .3, 0.0, 0.0).astype(np.float32)
        darker = adjust_colour(self.rgb, -.3, 0.0, 0.0).astype(np.float32)
        self.assertGreater(brighter.mean(), self.rgb.mean() + 10)
        self.assertLess(darker.mean(), self.rgb.mean() - 10)

    def test_saturation_zero_gives_grey(self) -> None:
        grey = adjust_colour(self.rgb, 0.0, 0.0, -1.0)
        self.assertLess(float(np.abs(grey[..., 0].astype(np.float32) - grey[..., 1]).max()), 2)

    def test_contrast_stretches_spread(self) -> None:
        harder = adjust_colour(self.rgb, 0.0, .5, 0.0).astype(np.float32)
        self.assertGreater(harder.std(), self.rgb.astype(np.float32).std())


def _write_cube(path, size: int = 2, invert: bool = True) -> None:
    lines = [f"LUT_3D_SIZE {size}"]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                values = [component / (size - 1) for component in (red, green, blue)]
                if invert:
                    values = [1.0 - value for value in values]
                lines.append(" ".join(f"{value:.6f}" for value in values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class CubeLutTest(unittest.TestCase):
    """Чтение .cube и наложение таблицы последним этапом."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "invert.cube"
        _write_cube(self.path)
        self.lut = load_cube_lut(self.path)
        rng = np.random.default_rng(3)
        self.rgb = rng.integers(0, 256, (16, 16, 3), dtype=np.uint8)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_reads_size_and_table(self) -> None:
        self.assertEqual(self.lut.size, 2)
        self.assertEqual(len(self.lut.table), 2 ** 3 * 3)

    def test_rejects_one_dimensional_table(self) -> None:
        path = Path(self.directory.name) / "curve.cube"
        path.write_text("LUT_1D_SIZE 4\n0 0 0\n1 1 1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_cube_lut(path)

    def test_full_strength_inverts_frame(self) -> None:
        result = apply_lut(self.rgb, self.lut, 1.0).astype(np.float32)
        self.assertLess(float(np.abs(result - (255 - self.rgb.astype(np.float32))).max()), 3)

    def test_zero_strength_keeps_frame(self) -> None:
        self.assertTrue(np.array_equal(apply_lut(self.rgb, self.lut, 0.0), self.rgb))

    def test_half_strength_lands_between(self) -> None:
        result = apply_lut(self.rgb, self.lut, .5).astype(np.float32)
        expected = (self.rgb.astype(np.float32) + (255 - self.rgb.astype(np.float32))) / 2
        self.assertLess(float(np.abs(result - expected).max()), 3)


class PipelineOrderTest(unittest.TestCase):
    """Порядок этапов: кожа, затем цвет, затем таблица."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "invert.cube"
        _write_cube(self.path)
        self.retoucher = retouch_pipeline.SkinRetoucher.__new__(retouch_pipeline.SkinRetoucher)
        self.retoucher._lut_cache = None
        self.rgb = np.full((8, 8, 3), 100, dtype=np.uint8)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_skin_stages_run_in_agreed_order(self) -> None:
        """Матирование обязано идти до тона: по блику пигменты считаются неверно."""
        calls: list[str] = []
        rgb = _skin(96)
        masks = retouch_pipeline.SkinMasks(np.full(rgb.shape[:2], 255, dtype=np.uint8), None, 90.0)
        self.retoucher._regions = lambda mask, pad: [(0, 0, rgb.shape[1], rgb.shape[0])]
        settings = retouch_pipeline.RetouchSettings(
            tone_strength=.5,
            matte_strength=.5,
            dodge_burn=.5,
            neural_retouch=True,
            neural_strength=.5,
        )
        with mock.patch.object(retouch_pipeline, "matte_skin", side_effect=lambda frame, *a: calls.append("matte") or frame), \
             mock.patch.object(retouch_pipeline, "even_skin_tone", side_effect=lambda frame, *a: calls.append("tone") or frame), \
             mock.patch.object(retouch_pipeline, "dodge_burn", side_effect=lambda frame, *a: calls.append("burn") or frame):
            self.retoucher.neural_retouch = lambda frame, *a: calls.append("neural") or frame
            self.retoucher.retouch_skin(rgb, settings, masks)

        self.assertEqual(calls, ["matte", "tone", "burn", "neural"])

    def test_lut_lands_after_colour(self) -> None:
        settings = retouch_pipeline.RetouchSettings(
            brightness=.5,
            lut_path=str(self.path),
            lut_strength=1.0,
        )
        result = self.retoucher.finish(self.rgb, settings)
        brightened = adjust_colour(self.rgb, .5, 0.0, 0.0)
        expected = apply_lut(brightened, load_cube_lut(self.path), 1.0)
        self.assertTrue(np.array_equal(result, expected))

    def test_table_is_parsed_once(self) -> None:
        with mock.patch.object(retouch_pipeline, "load_cube_lut", wraps=load_cube_lut) as parse:
            for _ in range(3):
                self.retoucher.lut(str(self.path))
        self.assertEqual(parse.call_count, 1)

    def test_edited_table_is_reread(self) -> None:
        first = self.retoucher.lut(str(self.path))
        _write_cube(self.path, size=3)
        os.utime(self.path, (0, 0))
        second = self.retoucher.lut(str(self.path))
        self.assertIsNot(first, second)
        self.assertEqual(second.size, 3)


if __name__ == "__main__":
    unittest.main()
