## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rawww.cache import FolderCache
from rawww.app import Workspace
from rawww.exif import MetadataPipeline, camera_details, extract_metadata_batch


class ExifTests(unittest.TestCase):
    """Проверяет нормализацию EXIF и формирование сведений о съёмке."""

    def test_extracted_metadata_keeps_xmp_rating_and_capture_settings(self) -> None:
        payload = [{
            "SourceFile": "photo.raw",
            "XMP:Rating": 4,
            "EXIF:ExposureTime": 0.008,
            "EXIF:ISO": 100,
            "EXIF:FNumber": 2.0,
            "EXIF:FocalLength": 85,
        }]
        with patch("rawww.exif._get_client") as get_client:
            get_client.return_value.read_metadata_batch.return_value = payload
            results = extract_metadata_batch(["photo.raw"])

        metadata = json.loads(results[0][1])
        self.assertEqual(metadata["rating"], 4)
        self.assertEqual(metadata["capture_settings"], {
            "exposure_time": 0.008,
            "exposure_display": "1/125",
            "iso": 100,
            "aperture": 2.0,
            "focal_length_mm": 85.0,
        })
        self.assertEqual(metadata["camera"], {})

    def test_camera_identity_prefers_serial_for_filtering(self) -> None:
        camera = camera_details({"EXIF:Model": "Camera X", "MakerNotes:SerialNumber": "SN-42"})
        self.assertEqual(camera, {"model": "Camera X", "serial_number": "SN-42"})
        self.assertEqual(Workspace._camera_filter_key({"camera": camera}), "serial:SN-42")
        self.assertEqual(
            Workspace._camera_filter_key({"camera": {"model": "Camera X"}}),
            "model:Camera X",
        )

    def test_metadata_pipeline_is_independent_and_stores_results(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "photo.jpg"
            path.write_bytes(b"image")
            cache = FolderCache(folder, {path.name}, cache_root=folder / "cache")
            workers = ThreadPoolExecutor(max_workers=1)
            pipeline = MetadataPipeline()
            pipeline.workers = workers
            expected = [(str(path), '{"rating":3}')]

            with patch("rawww.exif.extract_metadata_batch", return_value=expected):
                pipeline.scan([path], cache)
                workers.shutdown(wait=True)
                pipeline.workers = None

            self.assertEqual(cache.load_photo_details()[path.name]["rating"], 3)
            self.assertEqual(pipeline.futures, set())
            pipeline.shutdown()
            cache.close(flush=False)
