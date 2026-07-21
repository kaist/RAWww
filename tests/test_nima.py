## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image

from rawww import nima
from rawww.cache import FolderCache


def _image(width: int = 320, height: int = 240) -> Image.Image:
    """Синтетический кадр с текстурой, чтобы модели было что оценивать."""
    rng = np.random.default_rng(0)
    pixels = rng.integers(0, 256, size=(height, width, 3)).astype(np.uint8)
    return Image.fromarray(pixels, "RGB")


class QualityScoresTests(unittest.TestCase):
    """Проверяет разбор сохранённого JSON оценок качества для UI-фильтра."""

    def test_reads_both_axes(self) -> None:
        quality, aesthetic = nima.quality_scores({"quality": {"quality": 4.5, "aesthetic": 6.0}})
        self.assertAlmostEqual(quality, 4.5)
        self.assertAlmostEqual(aesthetic, 6.0)

    def test_missing_data_yields_none(self) -> None:
        self.assertEqual(nima.quality_scores({}), (None, None))
        self.assertEqual(nima.quality_scores({"quality": "broken"}), (None, None))
        self.assertEqual(nima.quality_scores({"quality": {"quality": "x"}}), (None, None))

    def test_json_round_trips(self) -> None:
        payload = json.loads(nima.quality_json(4.7123, 4.2345))
        self.assertEqual(payload, {"quality": 4.712, "aesthetic": 4.234})


class QualityCacheTests(unittest.TestCase):
    """Проверяет хранение оценок качества в дисковом кэше папки."""

    def test_store_load_and_missing_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (64, 48), (30, 90, 150)).save(path)
            cache = FolderCache(folder, {"sample.jpg"}, cache_root=folder / "central-cache")
            try:
                self.assertEqual(cache.missing_ai_paths([path], "quality_analysis"), [path])
                cache.store_quality_analysis(
                    [(str(path), nima.quality_json(4.7, 5.3))]
                )
                self.assertEqual(cache.missing_ai_paths([path], "quality_analysis"), [])
                stored = cache.load_quality_analysis()
                self.assertIn("sample.jpg", stored)
                details = cache.load_photo_details()
                self.assertEqual(details["sample.jpg"]["quality"]["quality"], 4.7)
                self.assertEqual(details["sample.jpg"]["quality"]["aesthetic"], 5.3)
            finally:
                cache.close(flush=False)

    def test_stored_record_invalidated_by_mtime(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (64, 48), (30, 90, 150)).save(path)
            cache = FolderCache(folder, {"sample.jpg"}, cache_root=folder / "central-cache")
            try:
                cache.store_quality_analysis([(str(path), nima.quality_json(4.7, 5.3))])
                self.assertEqual(cache.missing_ai_paths([path], "quality_analysis"), [])
                # Перезапись файла меняет размер/mtime — запись обязана устареть.
                Image.new("RGB", (80, 60), (10, 10, 10)).save(path)
                self.assertEqual(cache.missing_ai_paths([path], "quality_analysis"), [path])
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


class NimaInferenceTests(unittest.TestCase):
    """Проверяет, что ONNX-модели NIMA грузятся и дают баллы в диапазоне 1..10."""

    def test_score_images_returns_scores_in_range(self) -> None:
        if not (nima.TECHNICAL_MODEL.exists() and nima.AESTHETIC_MODEL.exists()):
            self.skipTest("NIMA ONNX models are not available")
        scores = nima.score_images([_image(), _image(200, 300)])
        self.assertEqual(len(scores), 2)
        for technical, aesthetic in scores:
            self.assertGreaterEqual(technical, 1.0)
            self.assertLessEqual(technical, 10.0)
            self.assertGreaterEqual(aesthetic, 1.0)
            self.assertLessEqual(aesthetic, 10.0)


if __name__ == "__main__":
    unittest.main()
