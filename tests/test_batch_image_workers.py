## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from rawww.batch_image_workers import recompress_jpeg_worker, resize_export_worker


class BatchImageWorkerTests(unittest.TestCase):
    def test_resize_worker_writes_requested_bounded_jpeg(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            target = root / "target.jpg"
            Image.new("RGB", (100, 50), "red").save(source)

            result = resize_export_worker(
                (str(source), str(target), 40, False, 100, False, 1.0, 100, 0, False, 1)
            )

            self.assertEqual(result, (str(source), str(target), None))
            with Image.open(target) as image:
                self.assertEqual(image.size, (40, 20))

    def test_recompress_worker_keeps_a_valid_jpeg(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.jpg"
            Image.new("RGB", (80, 60), "blue").save(source, quality=100)

            path, original_size, new_size, error = recompress_jpeg_worker(
                (str(source), 70, False)
            )

            self.assertEqual(path, str(source))
            self.assertIsNone(error)
            self.assertGreater(original_size, 0)
            self.assertGreater(new_size, 0)
            with Image.open(source) as image:
                self.assertEqual(image.size, (80, 60))


if __name__ == "__main__":
    unittest.main()
