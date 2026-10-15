## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки выбора превью, fallback и владения нативной памятью LibRaw."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from rawww import imaging, raw_previews
from rawww.raw_previews import EmbeddedPreview, PreviewInfo, choose_preview


def preview(index, width, height, *, jpeg=True, size=1000):
    """Создаёт вариант без зависимости unit-тестов от компилятора."""
    return PreviewInfo(index, width, height, 65535, size, jpeg, 5)


class PreviewSelectionTests(unittest.TestCase):
    """Проверяет достаточность разрешения и корректный отказ от неподходящих вариантов."""

    def test_smallest_sufficient_jpeg(self):
        choices = [preview(0, 160, 120), preview(1, 1620, 1080), preview(2, 6000, 4000)]
        self.assertEqual(choose_preview(choices, 256).index, 1)
        self.assertEqual(choose_preview(choices, 1620).index, 1)
        self.assertEqual(choose_preview(choices, 1621).index, 2)
        self.assertEqual(choose_preview([preview(0, 1080, 1620)], 256).index, 0)

    def test_unknown_small_empty_and_non_jpeg_use_fallback(self):
        choices = [preview(0, 160, 120), preview(1, 0, 0), preview(2, 640, 480, jpeg=False),
                   preview(3, 640, 480, size=0)]
        self.assertIsNone(choose_preview(choices, 256))
        self.assertIsNone(choose_preview([], 256))

    def test_equal_resolution_prefers_smaller_payload(self):
        choices = [preview(0, 640, 480, size=50000), preview(1, 640, 480, size=10000)]
        self.assertEqual(choose_preview(choices, 256).index, 1)

    def test_service_thumbnail_with_different_aspect_is_not_selected(self):
        choices = [preview(0, 160, 120), preview(1, 1620, 1080)]
        self.assertEqual(choose_preview(choices, 128).index, 1)
        nikon = [preview(0, 640, 424), preview(1, 6048, 4032)]
        self.assertEqual(choose_preview(nikon, 256).index, 0)

    def test_invalid_target_uses_fallback(self):
        self.assertIsNone(choose_preview([preview(0, 640, 480)], 0))

    def test_abi_mismatch_rejected_before_reading_native_memory(self):
        from unittest.mock import Mock

        functions = [Mock(return_value=0x1601), Mock(return_value=0x1504), Mock(return_value=1)]
        with patch.object(raw_previews, "_signature", side_effect=functions) as signature:
            with self.assertRaisesRegex(RuntimeError, "ABI"):
                raw_previews._Backend(object(), object())
        self.assertEqual(signature.call_count, 3)

    def test_unavailable_backend_returns_none(self):
        with patch.object(raw_previews, "_backend", return_value=None):
            self.assertIsNone(raw_previews.extract_preview(Path("missing.nef"), 256))
            self.assertEqual(raw_previews.list_previews(Path("missing.nef")), [])

    def test_decoder_uses_jpeg_orientation_once(self):
        source = Image.new("RGB", (600, 400), "red")
        exif = Image.Exif()
        exif[274] = 8
        output = BytesIO()
        source.save(output, "JPEG", exif=exif)
        embedded = EmbeddedPreview(output.getvalue(), preview(1, 600, 400))
        with patch.object(raw_previews, "extract_preview", return_value=embedded), patch.object(imaging, "_rawpy") as old:
            result = imaging.decode_pixels(Path("sample.nef"), 256)
        old.assert_not_called()
        self.assertEqual((result.width, result.height), (171, 256))

    def test_fit_reserves_resampling_margin(self):
        with patch.object(raw_previews, "extract_preview", return_value=None) as extract, patch.object(imaging, "_rawpy", side_effect=RuntimeError("fallback")):
            with self.assertRaisesRegex(RuntimeError, "fallback"):
                imaging.decode_pixels(Path("sample.nef"), 1024)
        extract.assert_called_once_with(Path("sample.nef"), 1280)

    def test_broken_preview_falls_back(self):
        for value in (None, EmbeddedPreview(b"invalid jpeg", preview(1, 640, 480))):
            with self.subTest(value=value), patch.object(raw_previews, "extract_preview", return_value=value), patch.object(imaging, "_rawpy", side_effect=RuntimeError("fallback")) as old:
                with self.assertRaisesRegex(RuntimeError, "fallback"):
                    imaging.decode_pixels(Path("sample.nef"), 256)
                old.assert_called_once()

    def test_original_does_not_select_smaller_preview(self):
        with patch.object(raw_previews, "extract_preview") as extract, patch.object(imaging, "_rawpy", side_effect=RuntimeError("original")):
            with self.assertRaisesRegex(RuntimeError, "original"):
                imaging.decode_original_pixels(Path("sample.nef"))
        extract.assert_not_called()


@unittest.skipUnless(raw_previews.available(), "Нативный мост LibRaw не собран")
class NativePreviewTests(unittest.TestCase):
    """Проверяет настоящую LibRaw на CR3-фикстуре, включая Unicode и параллельность."""

    source = Path(__file__).parent / "fixtures" / "metadata" / "canon_eos_r" / "IMG_0639.CR3"

    def test_enumeration_and_owned_jpeg(self):
        candidates = raw_previews.list_previews(self.source)
        self.assertGreaterEqual(len(candidates), 2)
        chosen = raw_previews.extract_preview(self.source, 256)
        self.assertIsNotNone(chosen)
        # Reader уже закрыт; байты должны продолжать жить независимо от LibRaw.
        for _ in range(3):
            raw_previews.extract_preview(self.source, 256)
        with Image.open(BytesIO(chosen.data)) as image:
            image.load()
            self.assertEqual(image.size, (chosen.info.width, chosen.info.height))
            self.assertGreaterEqual(max(image.size), 256)

    def test_unicode_path_and_parallel_readers(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Снимок Nikon и Canon.CR3"
            path.write_bytes(self.source.read_bytes())
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: raw_previews.extract_preview(path, 256), range(12)))
            self.assertTrue(all(result.data == results[0].data for result in results))

    def test_missing_and_invalid_files_fail_without_crash(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.nef"
            with self.assertRaises(OSError):
                raw_previews.list_previews(path)
            path.write_bytes(b"not a raw file")
            with self.assertRaises(OSError):
                raw_previews.list_previews(path)

    def test_orientation_and_fallback_resolution(self):
        with patch.object(raw_previews, "extract_preview", return_value=None):
            old = imaging.decode_pixels(self.source, 256)
        new = imaging.decode_pixels(self.source, 256)
        self.assertEqual((old.width, old.height), (new.width, new.height))
        self.assertIsNone(raw_previews.extract_preview(self.source, 100000))


if __name__ == "__main__":
    unittest.main()
