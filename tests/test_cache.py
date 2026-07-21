## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import closing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from PIL import Image

import rawww.cache as cache_module
from rawww.cache import FolderCache
from rawww.imaging import decode_pixels
from rawww.ai import AiPipeline, prepare_analysis_batch


class CacheTests(unittest.TestCase):
    """Проверяет дисковый кэш папки, его очистку и перенос."""

    def test_ai_model_workers_are_lazy_and_releasable(self) -> None:
        pipeline = AiPipeline()
        self.assertIsNone(pipeline.source_workers)
        self.assertIsNone(pipeline.embedding_workers)
        self.assertIsNone(pipeline.face_workers)
        pipeline._ensure_analysis_workers()
        self.assertIsNotNone(pipeline.source_workers)
        self.assertIsNotNone(pipeline.embedding_workers)
        self.assertIsNotNone(pipeline.face_workers)
        self.assertIsNot(pipeline.embedding_workers, pipeline.face_workers)
        pipeline.release_analysis_workers()
        self.assertIsNone(pipeline.source_workers)
        self.assertIsNone(pipeline.embedding_workers)
        self.assertIsNone(pipeline.face_workers)
        pipeline.shutdown()

    def test_ai_cache_preparation_runs_outside_the_caller_thread(self) -> None:
        pipeline = AiPipeline()
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        worker_thread_ids = []

        class PreparedCache:
            """Изображает уже подготовленный кэш, не трогая настоящий диск."""

            def close(self, *, flush: bool) -> None:
                closed.set()

        def prepare(_job):
            worker_thread_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return PreparedCache(), [], []

        with patch.object(pipeline, "_prepare_job", prepare):
            caller_thread_id = threading.get_ident()
            self.assertTrue(pipeline.scan([Path("/photos/sample.jpg")]))
            self.assertTrue(started.wait(1))
            self.assertNotEqual(worker_thread_ids, [caller_thread_id])
            self.assertEqual(pipeline.progress(Path("/photos")), (0, 0, True))
            release.set()
            deadline = monotonic() + 2
            while pipeline.pending_count() and monotonic() < deadline:
                sleep(0.01)

        self.assertEqual(pipeline.pending_count(), 0)
        self.assertTrue(closed.wait(1))
        pipeline.shutdown()

    def test_analysis_source_is_kept_in_memory_and_limited_to_640px(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.jpg"
            Image.new("RGB", (2400, 1600), (80, 120, 160)).save(path)
            results = prepare_analysis_batch([str(path)])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], str(path))
            with Image.open(__import__("io").BytesIO(results[0][1])) as prepared:
                self.assertLessEqual(max(prepared.size), 640)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_ram_hit_avoids_second_decode_and_disk_uses_jpeg(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (1200, 800), (180, 60, 40)).save(path, quality=90)

            cache_root = folder / "central-cache"
            cache = FolderCache(folder, {"sample.jpg"}, eager_variants={256}, cache_root=cache_root)
            real_decode = cache_module.decode_pixels
            counts = {"decode": 0}

            def counted_decode(*args, **kwargs):
                counts["decode"] += 1
                return real_decode(*args, **kwargs)

            with patch("rawww.cache.decode_pixels", counted_decode):
                first = cache.load_or_decode(path, 256)
                second = cache.load_or_decode(path, 256)
                self.assertEqual(counts["decode"], 1)
                self.assertEqual((first.width, first.height), (second.width, second.height))

            cache.flush()
            cache.close(flush=False)

            db_path = cache.path
            with closing(sqlite3.connect(db_path)) as db:
                fmt = db.execute("SELECT format FROM previews").fetchone()[0]
            self.assertEqual(fmt, "jpeg")

            cache2 = FolderCache(folder, {"sample.jpg"}, eager_variants={256}, cache_root=cache_root)
            with patch("rawww.cache.decode_pixels", counted_decode):
                third = cache2.load_or_decode(path, 256)
                self.assertEqual(counts["decode"], 1)
                self.assertEqual((third.width, third.height), (first.width, first.height))
            cache2.close(flush=False)

    def test_batch_load_returns_only_current_cached_previews(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = folder / "first.jpg"
            second = folder / "second.jpg"
            Image.new("RGB", (800, 600), (180, 60, 40)).save(first)
            Image.new("RGB", (800, 600), (40, 100, 180)).save(second)
            cache = FolderCache(folder, {first.name, second.name}, cache_root=folder / "cache")
            cache.load_or_decode(first, 256)
            cache.load_or_decode(second, 256)

            loaded = cache.load_batch([first, second], 256)

            self.assertEqual(set(loaded), {first, second})
            self.assertTrue(all(max(item.width, item.height) <= 256 for item in loaded.values()))
            cache.close(flush=False)

    def test_selection_keywords_and_xmp_sync_state_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            photo = folder / "photo.NEF"
            photo.write_bytes(b"raw")
            cache = FolderCache(folder, {photo.name}, cache_root=folder / "cache")

            cache.store_xmp_batch(
                [{
                    "name": photo.name, "rating": 4, "color_label": "green",
                    "comment": "Отбор", "keywords": ["портрет", "печать"],
                }],
                [{
                    "sidecar_name": "photo.xmp", "size": 42, "mtime_ns": 10,
                    "digest": "abc", "base_fields": {"rating": 4},
                    "status": "conflict",
                    "conflicts": [{"field": "rating", "local": 4, "external": 2}],
                }],
            )

            self.assertEqual(cache.load_photo_details()[photo.name]["keywords"], ["портрет", "печать"])
            state = cache.load_xmp_states()["photo.xmp"]
            self.assertEqual(state["digest"], "abc")
            self.assertEqual(state["status"], "conflict")
            self.assertEqual(state["conflicts"][0]["external"], 2)
            cache.relocate_xmp_states({"photo.xmp": ("renamed.xmp", "photo.xmp")})
            relocated = cache.load_xmp_states()
            self.assertEqual(relocated["renamed.xmp"]["digest"], "abc")
            self.assertEqual(relocated["photo.xmp"]["digest"], "abc")
            cache.close(flush=False)

    def test_process_worker_decodes_pixels(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.jpg"
            Image.new("RGB", (1600, 900), (20, 80, 140)).save(path, quality=90)

            with ProcessPoolExecutor(max_workers=2) as pool:
                pixel = pool.submit(decode_pixels, path, 320).result(timeout=20)

            self.assertLessEqual(max(pixel.width, pixel.height), 320)
            self.assertEqual(len(pixel.pixels), pixel.width * pixel.height * 4)

    def test_disk_cache_keeps_all_variants(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (1200, 800), (180, 60, 40)).save(path, quality=90)

            cache = FolderCache(folder, {"sample.jpg"}, cache_root=folder / "central-cache")
            cache.load_or_decode(path, 256)
            cache.load_or_decode(path, 1024)
            cache.flush()
            cache.close(flush=False)

            db_path = cache.path
            with closing(sqlite3.connect(db_path)) as db:
                count = db.execute("SELECT COUNT(*) FROM previews").fetchone()[0]
            self.assertEqual(count, 2)

    def test_cache_is_stored_outside_the_photo_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (1200, 800), (180, 60, 40)).save(path, quality=90)

            cache_root = folder / "central-cache"
            cache = FolderCache(folder, {"sample.jpg"}, cache_root=cache_root)
            cache.load_or_decode(path, 256)
            cache.close(flush=False)

            db_path = cache.path
            self.assertEqual(db_path.parent, cache_root)
            self.assertTrue(db_path.exists())
            self.assertFalse((folder / ".rawww").exists())
            with closing(sqlite3.connect(db_path)) as db:
                count = db.execute("SELECT COUNT(*) FROM previews").fetchone()[0]
            self.assertEqual(count, 1)

    def test_cache_uses_high_throughput_sqlite_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cache = FolderCache(folder, set(), cache_root=folder / "cache")
            with closing(sqlite3.connect(cache.path)) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(db.execute("PRAGMA page_size").fetchone()[0], 32 * 1024)
            cache.close(flush=False)

    def test_corrupt_cache_is_rebuilt_from_scratch(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = folder / "cache"
            path = cache_module.cache_path(folder, root)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not a sqlite database")
            cache = FolderCache(folder, set(), cache_root=root)
            with closing(sqlite3.connect(path)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("previews", tables)
            self.assertIn("image_embeddings", tables)
            self.assertIn("face_analysis", tables)
            self.assertIn("photo_metadata", tables)
            cache.close(flush=False)

    def test_ai_results_live_in_folder_cache_and_are_invalidated(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (10, 10)).save(path)
            cache = FolderCache(folder, {path.name}, cache_root=folder / "cache")
            cache.store_image_embeddings([(str(path), b"embedding")])
            cache.store_face_analysis([(str(path), "[]")])
            self.assertEqual(cache.missing_ai_paths([path], "image_embeddings"), [])
            self.assertEqual(cache.missing_ai_paths([path], "face_analysis"), [])
            path.write_bytes(path.read_bytes() + b"changed")
            self.assertEqual(cache.missing_ai_paths([path], "image_embeddings"), [path])
            cache.close(flush=False)

    def test_preview_and_each_ai_result_are_tracked_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (120, 80), (30, 60, 90)).save(path)
            cache = FolderCache(folder, {path.name}, cache_root=folder / "cache")

            cache.load_or_decode(path, 256)
            self.assertEqual(cache.missing_ai_paths([path], "image_embeddings"), [path])
            self.assertEqual(cache.missing_ai_paths([path], "face_analysis"), [path])
            self.assertEqual(cache.missing_metadata_paths([path]), [path])

            cache.store_image_embeddings([(str(path), b"embedding")])
            self.assertEqual(cache.missing_ai_paths([path], "image_embeddings"), [])
            self.assertEqual(cache.missing_ai_paths([path], "face_analysis"), [path])

            cache.store_face_analysis([(str(path), "[]")])
            self.assertEqual(cache.missing_ai_paths([path], "face_analysis"), [])
            cache.store_photo_metadata([(str(path), '{"rating":3}')])
            self.assertEqual(cache.missing_metadata_paths([path]), [])
            cache.close(flush=False)

    def test_photo_details_can_skip_exif_table(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "sample.jpg"
            Image.new("RGB", (32, 32)).save(path)
            cache = FolderCache(folder, {path.name}, cache_root=folder / "cache")
            cache.store_photo_metadata([(str(path), '{"original_datetime":"2026-01-02T03:04:05"}')])

            self.assertIn("original_datetime", cache.load_photo_details()[path.name])
            self.assertNotIn(path.name, cache.load_photo_details(include_metadata=False))
            cache.close(flush=False)

    def test_rename_photo_names_preserves_cache_data_for_a_name_swap(self) -> None:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first, second = folder / "first.jpg", folder / "second.jpg"
            Image.new("RGB", (32, 32), (10, 20, 30)).save(first)
            Image.new("RGB", (32, 32), (40, 50, 60)).save(second)
            cache = FolderCache(folder, {first.name, second.name}, cache_root=folder / "cache")
            cache.store_photo_metadata([(str(first), '{"camera":{"model":"First"}}')])
            cache.store_photo_metadata([(str(second), '{"camera":{"model":"Second"}}')])
            cache.store_photo_selection(first.name, rating=5, color_label="red", comment="keep")

            cache.rename_photo_names({first.name: second.name, second.name: first.name})

            details = cache.load_photo_details()
            self.assertEqual(details[second.name]["camera"]["model"], "First")
            self.assertEqual(details[first.name]["camera"]["model"], "Second")
            self.assertEqual(details[second.name]["comment"], "keep")
            self.assertEqual(cache.live_names, {first.name, second.name})
            cache.close(flush=False)


if __name__ == "__main__":
    unittest.main()
