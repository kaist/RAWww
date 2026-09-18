## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет сохранение оригиналов и гонки кэша редактора удаления объектов."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from rawww.inpaint_pipeline import (
    DeepOad, EditableImage, ImageConflictError, LamaInpainter, crop_image, crop_with_inpaint, ensure_horizon_model, ensure_inpaint_model,
    fingerprint, horizon_model_path, inpaint_model_path, load_image, make_mask, save_image,
    scale_strokes, straighten_image,
)
from rawww.inpaint_worker import ImageStore


class InpaintFileTests(unittest.TestCase):
    """Атомарная запись обязана сохранять метаданные и не трогать чужую версию."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_straighten_crops_empty_corners_and_preserves_alpha(self):
        frame = load_image(self.root / "transparent.png") if (self.root / "transparent.png").exists() else None
        if frame is None:
            Image.new("RGBA", (300, 200), (20, 40, 60, 120)).save(self.root / "transparent.png")
            frame = load_image(self.root / "transparent.png")
        result = straighten_image(frame, 8)
        self.assertLess(result.width, frame.image.width)
        self.assertLess(result.height, frame.image.height)
        self.assertAlmostEqual(result.width / result.height, frame.image.width / frame.image.height, places=2)
        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(result.getpixel((result.width//2, result.height//2))[3], 120)

    def test_crop_clamps_to_photo_and_keeps_requested_inner_area(self):
        path = self.root / "crop.png"
        Image.new("RGB", (100, 80), "red").save(path)
        frame = load_image(path)
        result = crop_image(frame, (-20, 10, 70, 90))
        self.assertEqual(result.size, (70, 70))

    def test_crop_beyond_edge_masks_only_added_canvas_for_inpaint(self):
        path = self.root / "extend.png"
        Image.new("RGB", (100, 80), "red").save(path)
        frame = load_image(path)

        class Inpainter:
            def apply(self, expanded, mask):
                self.size = expanded.image.size
                self.mask = mask
                return Image.new("RGB", expanded.image.size, "blue")

        model = Inpainter()
        result = crop_with_inpaint(frame, (-10, 0, 100, 80), model)
        self.assertEqual(model.size, (110, 80))
        self.assertEqual(model.mask.getpixel((5, 40)), 255)
        self.assertEqual(model.mask.getpixel((15, 40)), 0)
        self.assertEqual(result.size, (110, 80))
        self.assertEqual(result.getpixel((5, 40)), (0, 0, 255))

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
        model.side = 512
        result = model.apply(frame, mask)
        self.assertEqual(result.getpixel((350,200)), (200,200,200,130))
        self.assertEqual(result.getpixel((50,50)), (45,60,80,130))
        self.assertEqual(result.getchannel("A").tobytes(), frame.image.getchannel("A").tobytes())
        self.assertEqual(model.session.inputs["image"].shape, (1,3,512,512))
        self.assertEqual(model.session.inputs["mask"].shape, (1,1,512,512))
        self.assertLessEqual(model.session.inputs["image"].max(), 1)

    def test_hd_uses_1024_native_pixels_of_context(self):
        image = Image.new("RGB", (1400, 1200), "black")
        image.paste("red", (300, 0, 320, 1200))
        frame = EditableImage(
            self.root / "context.png", image, (0, 0, 0, 0), "PNG", b"", None, None, None,
        )
        mask = make_mask(image.size, [{"diameter": 40, "points": [[700, 600]]}])

        class Session:
            def run(self, _, inputs):
                self.inputs = inputs
                return [np.zeros((1, 3, 1024, 1024), dtype=np.float32)]

        model = LamaInpainter.__new__(LamaInpainter)
        model.session = Session()
        model.side = 1024
        model.apply(frame, mask)

        # Полоса находится за пределами прежнего 512-кропа, но входит в окно HD.
        self.assertEqual(model.session.inputs["image"][0, 0].max(), 1)
        self.assertEqual(model.session.inputs["image"].shape, (1, 3, 1024, 1024))

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
            self.assertEqual(inpaint_model_path("hd"), portable_root / "inpaint" / "lama_fp32_1024.onnx")

    def test_hd_model_is_downloaded_only_when_explicitly_requested(self):
        content = b"lama-hd" * 300_000
        hd_model = Path(self.directory.name) / "models" / "lama_fp32_1024.onnx"
        with patch("rawww.inpaint_pipeline.inpaint_model_path", return_value=hd_model), \
             patch("rawww.inpaint_pipeline.HD_MODEL_URL", "https://example.test/lama-hd.onnx"), \
             patch("rawww.inpaint_pipeline.HD_MODEL_SHA256", hashlib.sha256(content).hexdigest()), \
             patch("rawww.inpaint_pipeline.urlopen", return_value=self._Response(content)) as download:
            self.assertEqual(ensure_inpaint_model(quality="hd"), hd_model)
        download.assert_called_once()
        self.assertEqual(hd_model.read_bytes(), content)

    def test_missing_hd_url_does_not_touch_the_network(self):
        hd_model = Path(self.directory.name) / "models" / "lama_fp32_1024.onnx"
        with patch("rawww.inpaint_pipeline.inpaint_model_path", return_value=hd_model), \
             patch("rawww.inpaint_pipeline.HD_MODEL_URL", ""), \
             patch("rawww.inpaint_pipeline.urlopen") as download:
            with self.assertRaisesRegex(RuntimeError, "hd_model_url_missing"):
                ensure_inpaint_model(quality="hd")
        download.assert_not_called()

    def test_horizon_download_reports_progress_and_publishes_verified_model(self):
        content = b"deep-oad" * 400_000
        import hashlib
        progress = []
        with patch("rawww.inpaint_pipeline.horizon_model_path", return_value=self.model), \
             patch("rawww.inpaint_pipeline.HORIZON_MODEL_SHA256", hashlib.sha256(content).hexdigest()), \
             patch("rawww.inpaint_pipeline.urlopen", return_value=self._Response(content)):
            self.assertEqual(ensure_horizon_model(lambda done, total: progress.append((done, total))), self.model)
        self.assertEqual(progress[-1], (len(content), len(content)))
        self.assertEqual(self.model.read_bytes(), content)

    def test_horizon_model_lives_in_separate_directory(self):
        portable_root = Path(self.directory.name) / "data" / "models"
        with patch("rawww.inpaint_pipeline.PORTABLE", True), \
             patch("rawww.inpaint_pipeline.data_path", return_value=portable_root):
            self.assertEqual(horizon_model_path(), portable_root / "orientation" / "deep_oad.onnx")


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

    def test_apply_uses_small_photo_without_missing_full_loader_request(self):
        Image.new("RGB", (1920, 1280), "red").save(self.path)
        frame = self.store.get(str(self.path))
        self.assertFalse(frame.draft)
        full, sx, sy = self.store.full_for_apply(str(self.path), frame.image.size)
        self.assertIs(full, frame)
        self.assertEqual((sx, sy), (1, 1))

    def test_manual_rotation_reuses_one_base_for_opposite_directions(self):
        frame = self.store.get(str(self.path))
        bases = []
        def rotate(source, degrees):
            bases.append(source.image)
            return Image.new("RGB", source.image.size, "blue" if degrees < 0 else "green")
        with patch("rawww.inpaint_worker.straighten_image", side_effect=rotate):
            self.store.rotate_from_base(frame, .5)
            self.store.rotate_from_base(frame, 0)
        self.assertIs(bases[0], bases[1])
        self.assertEqual(self.store.rotation_angles[str(self.path)], 0)

    def test_reset_restores_pixels_before_an_autosave(self):
        frame = self.store.get(str(self.path))
        full, _, _ = self.store.full_for_apply(str(self.path), frame.image.size)
        self.store.replace_pixels(full, Image.new("RGB", full.image.size, "blue"))
        restored = self.store.reset(str(self.path))
        self.assertEqual(restored.image.getpixel((0, 0)), (255, 0, 0))

    def test_history_keeps_ten_steps_and_supports_redo(self):
        frame = self.store.get(str(self.path))
        for value in range(11):
            self.store.replace_pixels(frame, Image.new("RGB", frame.image.size, (value, 0, 0)))
        self.assertEqual(len(self.store.history[str(self.path)]), 10)
        previous = self.store.undo(str(self.path))
        self.assertEqual(previous.image.getpixel((0, 0)), (9, 0, 0))
        repeated = self.store.redo(str(self.path))
        self.assertEqual(repeated.image.getpixel((0, 0)), (10, 0, 0))


if __name__ == "__main__":
    unittest.main()
