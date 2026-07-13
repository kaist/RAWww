"""Unit tests for the in-memory decode/thumbnail LRU caches.

These exercise the eviction and byte-accounting logic in isolation, which used
to live inline in the ``Workspace`` god-object and was impossible to test
without a full Qt application and folder cache.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402

from rawww.decode_cache import DecodeCache  # noqa: E402
from rawww.imaging import DecodedImage  # noqa: E402

ORIGINAL_SIZE = 0
THUMB_SIZE = 256


def _decoded(name: str, size: int) -> DecodedImage:
    image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    return DecodedImage(path=Path(name), image=image, width=4, height=4)


def _make_cache(*, ram_limit=96, full_limit=5, thumbnail_bytes_limit=10_000) -> DecodeCache:
    return DecodeCache(
        ram_limit=ram_limit,
        full_limit=full_limit,
        thumbnail_bytes_limit=thumbnail_bytes_limit,
        original_size=ORIGINAL_SIZE,
        thumb_size=THUMB_SIZE,
    )


class DecodeCacheMemoryTests(unittest.TestCase):
    def test_get_returns_none_for_missing_key(self) -> None:
        cache = _make_cache()
        self.assertIsNone(cache.get((Path("a.jpg"), THUMB_SIZE)))

    def test_put_then_get_roundtrips(self) -> None:
        cache = _make_cache()
        key = (Path("a.jpg"), THUMB_SIZE)
        decoded = _decoded("a.jpg", THUMB_SIZE)
        cache.put(key, decoded)
        self.assertIs(cache.get(key), decoded)

    def test_get_marks_entry_most_recently_used(self) -> None:
        cache = _make_cache(ram_limit=2)
        a, b, c = (Path("a"), THUMB_SIZE), (Path("b"), THUMB_SIZE), (Path("c"), THUMB_SIZE)
        cache.put(a, _decoded("a", THUMB_SIZE))
        cache.put(b, _decoded("b", THUMB_SIZE))
        cache.get(a)  # touch 'a' so 'b' becomes the eviction victim
        cache.put(c, _decoded("c", THUMB_SIZE))
        self.assertIsNotNone(cache.get(a))
        self.assertIsNone(cache.get(b))
        self.assertIsNotNone(cache.get(c))

    def test_ram_limit_evicts_oldest(self) -> None:
        cache = _make_cache(ram_limit=3)
        for index in range(5):
            cache.put((Path(str(index)), THUMB_SIZE), _decoded(str(index), THUMB_SIZE))
        self.assertEqual(len(cache.memory), 3)
        self.assertIsNone(cache.get((Path("0"), THUMB_SIZE)))
        self.assertIsNotNone(cache.get((Path("4"), THUMB_SIZE)))

    def test_only_one_original_frame_is_kept(self) -> None:
        cache = _make_cache()
        cache.put((Path("a"), ORIGINAL_SIZE), _decoded("a", ORIGINAL_SIZE))
        cache.put((Path("b"), ORIGINAL_SIZE), _decoded("b", ORIGINAL_SIZE))
        originals = [key for key in cache.memory if key[1] == ORIGINAL_SIZE]
        self.assertEqual(originals, [(Path("b"), ORIGINAL_SIZE)])

    def test_full_frames_limited_to_full_limit(self) -> None:
        cache = _make_cache(full_limit=2)
        for index in range(4):
            cache.put((Path(str(index)), 1024), _decoded(str(index), 1024))
        full_keys = [key for key in cache.memory if key[1] > THUMB_SIZE]
        self.assertEqual(len(full_keys), 2)
        self.assertNotIn((Path("0"), 1024), cache.memory)
        self.assertIn((Path("3"), 1024), cache.memory)


class DecodeCacheThumbnailTests(unittest.TestCase):
    def test_thumbnail_roundtrip_and_byte_accounting(self) -> None:
        cache = _make_cache()
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        cache.thumbnail_put(Path("a"), image)
        self.assertIs(cache.thumbnail_get(Path("a")), image)
        self.assertEqual(cache.thumbnail_bytes, image.sizeInBytes())

    def test_replacing_thumbnail_updates_byte_total(self) -> None:
        cache = _make_cache()
        first = QImage(8, 8, QImage.Format.Format_RGBA8888)
        second = QImage(16, 16, QImage.Format.Format_RGBA8888)
        cache.thumbnail_put(Path("a"), first)
        cache.thumbnail_put(Path("a"), second)
        self.assertEqual(len(cache.thumbnails), 1)
        self.assertEqual(cache.thumbnail_bytes, second.sizeInBytes())

    def test_thumbnail_byte_limit_evicts_oldest(self) -> None:
        image = QImage(16, 16, QImage.Format.Format_RGBA8888)
        per_image = image.sizeInBytes()
        cache = _make_cache(thumbnail_bytes_limit=per_image * 2)
        for name in ("a", "b", "c"):
            cache.thumbnail_put(Path(name), QImage(16, 16, QImage.Format.Format_RGBA8888))
        self.assertLessEqual(cache.thumbnail_bytes, per_image * 2)
        self.assertIsNone(cache.thumbnail_get(Path("a")))
        self.assertIsNotNone(cache.thumbnail_get(Path("c")))

    def test_clear_resets_everything(self) -> None:
        cache = _make_cache()
        cache.put((Path("a"), THUMB_SIZE), _decoded("a", THUMB_SIZE))
        cache.thumbnail_put(Path("a"), QImage(8, 8, QImage.Format.Format_RGBA8888))
        cache.clear()
        self.assertEqual(len(cache.memory), 0)
        self.assertEqual(len(cache.thumbnails), 0)
        self.assertEqual(cache.thumbnail_bytes, 0)


if __name__ == "__main__":
    unittest.main()
