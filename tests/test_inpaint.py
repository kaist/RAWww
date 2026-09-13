## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет сохранение оригиналов и гонки кэша редактора удаления объектов."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from rawww.inpaint_pipeline import (
    ImageConflictError, LamaInpainter, ensure_inpaint_model, fingerprint, inpaint_model_path,
    load_image, make_mask, save_image, scale_strokes,
)
from rawww.inpaint_worker import ImageStore


class InpaintFileTests(unittest.TestCase):
    """Атомарная запись обязана сохранять метаданные и не трогать чужую версию."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_formats_keep_exif_icc_and_orientation(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for suffix in ("jpg", "png", "webp", "tiff"):
            with self.subTest(suffix=suffix):
                path = self.root / f"portrait.{suffix}"
                exif = Image.Exif()
                exif[271], exif[272], exif[274] = "Test camera", "Model 1", 6
                exif[36867] = "2026:09:13 12:34:56"
                Image.new("RGB", (60,40), "red").save(path, exif=exif, icc_profile=profile)
                frame = load_image(path)
                self.assertEqual(frame.image.size, (40,60))
                save_image(frame, frame.image, frame.signature)
                with Image.open(path) as saved:
                    self.assertEqual(saved.size, (40,60))
                    self.assertEqual(saved.getexif()[271], "Test camera")
                    self.assertEqual(saved.getexif()[36867], "2026:09:13 12:34:56")
                    self.assertIn(saved.getexif().get(274, 1), (1,))
                    self.assertEqual(saved.info["icc_profile"], profile)

    def test_external_change_is_never_overwritten(self):
        path = self.root / "photo.png"
        Image.new("RGB", (30,30), "red").save(path)
        frame = load_image(path)
        Image.new("RGB", (31,30), "blue").save(path)
        external = path.read_bytes()
        with self.assertRaises(ImageConflictError):
            save_image(frame, frame.image, frame.signature)
        self.assertEqual(path.read_bytes(), external)

    def test_change_during_encoding_is_detected_and_temp_is_removed(self):
        path = self.root / "photo.png"
        Image.new("RGB", (30,30), "red").save(path)
        frame = load_image(path)
        original_save = Image.Image.save
        def race(image, *args, **kwargs):
            original_save(image, *args, **kwargs)
            original_save(Image.new("RGB", (40,40), "blue"), path)
        with patch.object(Image.Image, "save", race):
            with self.assertRaises(ImageConflictError):
                save_image(frame, frame.image, frame.signature)
        self.assertEqual(list(self.root.iterdir()), [path])
        with Image.open(path) as image:
            self.assertEqual(image.size, (40,40))

    def test_rejects_raw_multiframe_and_high_bit_depth(self):
        path = self.root / "high.tiff"
        Image.fromarray(np.zeros((20,20), dtype=np.uint16)).save(path)
        with self.assertRaises(ValueError):
            load_image(path)
        Image.new("RGB", (20,20)).save(path, save_all=True, append_images=[Image.new("RGB", (20,20))])
        with self.assertRaises(ValueError):
            load_image(path)
        with self.assertRaises(ValueError):
            load_image(self.root / "image.cr3")

    def test_apply_changes_only_mask_neighborhood_and_keeps_alpha(self):
        path = self.root / "transparent.png"
        Image.new("RGBA", (700,400), (45,60,80,130)).save(path)
        frame = load_image(path)
        mask = make_mask(frame.image.size, [{"diameter": 40, "points": [[350,200]]}])
        class Session:
            def run(self, _, inputs):
                self.inputs = inputs
                return [np.full((1,3,512,512), 200, dtype=np.float32)]
        model = LamaInpainter.__new__(LamaInpainter)
        model.session = Session()
        result = model.apply(frame, mask)
        self.assertEqual(result.getpixel((350,200)), (200,200,200,130))
        self.assertEqual(result.getpixel((50,50)), (45,60,80,130))
        self.assertEqual(result.getchannel("A").tobytes(), frame.image.getchannel("A").tobytes())
        self.assertEqual(model.session.inputs["image"].shape, (1,3,512,512))
        self.assertEqual(model.session.inputs["mask"].shape, (1,1,512,512))
        self.assertLessEqual(model.session.inputs["image"].max(), 1)

    def test_draft_is_smaller_but_keeps_orientation_and_scale(self):
        path = self.root / "large.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (4000, 3000), "red").save(path, exif=exif)
        draft = load_image(path, 1000)
        full = load_image(path)
        self.assertTrue(draft.draft)
        self.assertEqual(full.image.size, (3000, 4000))
        self.assertLessEqual(max(draft.image.size), 1000)
        strokes = scale_strokes([{"diameter": 10, "points": [[draft.image.width/2, draft.image.height/2]]}],
                               full.image.width/draft.image.width, full.image.height/draft.image.height)
        self.assertAlmostEqual(strokes[0]["points"][0][0], 1500)
        self.assertAlmostEqual(strokes[0]["points"][0][1], 2000)


class InpaintModelTests(unittest.TestCase):
    """Модель появляется только после полной загрузки и проверки контрольной суммы."""

    class _Response(BytesIO):
        def __init__(self, content: bytes):
            super().__init__(content)
            self.headers = {"Content-Length": str(len(content))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.model = Path(self.directory.name) / "models" / "lama_fp32.onnx"

    def tearDown(self):
        self.directory.cleanup()

    def test_download_reports_progress_and_publishes_verified_model(self):
        content = b"lama" * 400_000
        import hashlib
        progress = []
        with patch("rawww.inpaint_pipeline.inpaint_model_path", return_value=self.model), \
             patch("rawww.inpaint_pipeline.MODEL_SHA256", hashlib.sha256(content).hexdigest()), \
             patch("rawww.inpaint_pipeline.urlopen", return_value=self._Response(content)):
            self.assertEqual(ensure_inpaint_model(lambda done, total: progress.append((done, total))), self.model)
        self.assertEqual(self.model.read_bytes(), content)
        self.assertEqual(progress[0], (0, len(content)))
        self.assertEqual(progress[-1], (len(content), len(content)))
        self.assertFalse(list(self.model.parent.glob("*.download")))

    def test_bad_download_never_replaces_existing_model(self):
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"old")
        with patch("rawww.inpaint_pipeline.inpaint_model_path", return_value=self.model), \
             patch("rawww.inpaint_pipeline.MODEL_SHA256", "expected"), \
             patch("rawww.inpaint_pipeline.urlopen", return_value=self._Response(b"wrong")):
            with self.assertRaisesRegex(RuntimeError, "model_checksum_mismatch"):
                ensure_inpaint_model()
        self.assertEqual(self.model.read_bytes(), b"old")
        self.assertFalse(list(self.model.parent.glob("*.download")))

    def test_portable_model_lives_next_to_other_models(self):
        portable_root = Path(self.directory.name) / "data" / "models"
        with patch("rawww.inpaint_pipeline.PORTABLE", True), \
             patch("rawww.inpaint_pipeline.data_path", return_value=portable_root):
            self.assertEqual(inpaint_model_path(), portable_root / "inpaint" / "lama_fp32.onnx")


class InpaintCacheTests(unittest.TestCase):
    """Сохранение и предзагрузка не могут подменить редактируемую ревизию."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "photo.png"
        Image.new("RGB", (30,30), "red").save(self.path)
        self.store = ImageStore()
        self.store.current = str(self.path)

    def tearDown(self):
        self.store.shutdown()
        self.directory.cleanup()

    def test_two_readers_share_one_frame_owner(self):
        barrier = threading.Barrier(2)
        def read(path, draft_side=None):
            frame = load_image(path)
            barrier.wait(timeout=3)
            return frame
        with patch("rawww.inpaint_worker.load_image", side_effect=read), ThreadPoolExecutor(2) as pool:
            a = pool.submit(self.store.get, str(self.path))
            b = pool.submit(self.store.get, str(self.path))
            self.assertIs(a.result(5), b.result(5))

    def test_write_does_not_block_next_revision_or_navigation(self):
        frame = self.store.get(str(self.path))
        frame.image = Image.new("RGB", frame.image.size, "blue")
        frame.revision = 1
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        events = []
        def delayed(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("timeout")
            return save_image(*args)
        def notify(kind, **kwargs):
            events.append(kind)
            if len(events) == 2:
                done.set()
        try:
            with patch("rawww.inpaint_worker.save_image", side_effect=delayed):
                self.store.save(str(self.path), 1, notify)
                self.assertTrue(entered.wait(3))
                self.assertIs(self.store.get(str(self.path)), frame)
                frame.image = Image.new("RGB", frame.image.size, "green")
                frame.revision = 2
                self.store.save(str(self.path), 2, notify)
                release.set()
                self.assertTrue(done.wait(5))
        finally:
            release.set()
        self.assertEqual(events, ["saved", "saved"])
        self.assertEqual(frame.saved_revision, 2)
        self.assertEqual(frame.signature, fingerprint(self.path))
        with Image.open(self.path) as saved:
            self.assertEqual(saved.getpixel((0,0)), (0,128,0))

    def test_clean_cached_frame_refreshes_after_external_edit(self):
        old = self.store.get(str(self.path))
        Image.new("RGB", (31,30), "blue").save(self.path)
        new = self.store.get(str(self.path))
        self.assertIsNot(old, new)
        self.assertEqual(new.image.size, (31,30))

    def test_revision_numbers_survive_pixel_eviction(self):
        frame = self.store.get(str(self.path))
        self.store.replace_pixels(frame, Image.new("RGB", frame.image.size, "blue"))
        done = threading.Event()
        self.store.save(str(self.path), frame.revision, lambda *args, **kwargs: done.set())
        self.assertTrue(done.wait(5))
        self.store.current = "other.png"
        self.store.budget = 0
        with self.store.lock:
            self.store._trim()
        self.assertNotIn(str(self.path), self.store.frames)
        self.store.current = str(self.path)
        loaded = self.store.get(str(self.path))
        self.assertEqual(loaded.revision, 1)
        self.assertEqual(loaded.saved_revision, 1)
        self.store.replace_pixels(loaded, Image.new("RGB", loaded.image.size, "green"))
        self.assertEqual(loaded.revision, 2)
        self.assertNotEqual(loaded.revision, loaded.saved_revision)

    def test_failed_save_keeps_dirty_pixels(self):
        frame = self.store.get(str(self.path))
        frame.revision = 1
        frame.image = Image.new("RGB", frame.image.size, "blue")
        Image.new("RGB", (31,30), "yellow").save(self.path)
        done = threading.Event()
        events = []
        def notify(kind, **kwargs):
            events.append((kind, kwargs))
            done.set()
        self.store.save(str(self.path), 1, notify)
        self.assertTrue(done.wait(5))
        self.assertEqual(events[0][0], "save_error")
        self.assertEqual(events[0][1]["error"], "external_change")
        self.assertEqual(frame.saved_revision, 0)
        self.assertIs(self.store.get(str(self.path)), frame)

    def test_prepare_full_does_not_delay_open_draft_and_apply_gets_full(self):
        Image.new("RGB", (3200, 2400), "red").save(self.path)
        draft = self.store.get(str(self.path))
        self.assertTrue(draft.draft)
        self.store.prepare_full(str(self.path))
        full, sx, sy = self.store.full_for_apply(str(self.path), draft.image.size)
        self.assertFalse(full.draft)
        self.assertGreater(sx, 1)
        self.assertGreater(sy, 1)


if __name__ == "__main__":
    unittest.main()
