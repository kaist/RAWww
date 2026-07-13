"""Bounded in-memory LRU caches for decoded frames and thumbnails.

This is pure bookkeeping logic with no Qt widget dependencies, split out of
``Workspace`` so it can be reasoned about and unit-tested on its own.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtGui import QImage

from .imaging import DecodedImage


class DecodeCache:
    """RAM LRU for full/preview frames plus a byte-bounded thumbnail LRU."""

    def __init__(
        self,
        *,
        ram_limit: int,
        full_limit: int,
        thumbnail_bytes_limit: int,
        original_size: int,
        thumb_size: int,
    ) -> None:
        self.memory: OrderedDict[tuple[Path, int], DecodedImage] = OrderedDict()
        self.thumbnails: OrderedDict[Path, QImage] = OrderedDict()
        self.thumbnail_bytes = 0
        self._ram_limit = ram_limit
        self._full_limit = full_limit
        self._thumbnail_bytes_limit = thumbnail_bytes_limit
        self._original_size = original_size
        self._thumb_size = thumb_size

    def get(self, key: tuple[Path, int]) -> DecodedImage | None:
        decoded = self.memory.get(key)
        if decoded is None:
            return None
        self.memory.move_to_end(key)
        return decoded

    def put(self, key: tuple[Path, int], decoded: DecodedImage) -> None:
        self.memory[key] = decoded
        self.memory.move_to_end(key)
        self._trim_memory()

    def _trim_memory(self) -> None:
        # Original frames are much larger than display previews. Keeping the
        # active one is enough for a repeated Z press without risking a large
        # accumulation while browsing.
        original_keys = [key for key in self.memory if key[1] == self._original_size]
        while len(original_keys) > 1:
            self.memory.pop(original_keys.pop(0), None)
        full_keys = [key for key in self.memory if key[1] > self._thumb_size]
        while len(full_keys) > self._full_limit:
            self.memory.pop(full_keys.pop(0), None)
        while len(self.memory) > self._ram_limit:
            self.memory.popitem(last=False)

    def thumbnail_get(self, path: Path) -> QImage | None:
        image = self.thumbnails.get(path)
        if image is not None:
            self.thumbnails.move_to_end(path)
        return image

    def thumbnail_put(self, path: Path, image: QImage) -> None:
        previous = self.thumbnails.pop(path, None)
        if previous is not None:
            self.thumbnail_bytes -= previous.sizeInBytes()
        self.thumbnails[path] = image
        self.thumbnail_bytes += image.sizeInBytes()
        while self.thumbnails and self.thumbnail_bytes > self._thumbnail_bytes_limit:
            _path, expired = self.thumbnails.popitem(last=False)
            self.thumbnail_bytes -= expired.sizeInBytes()

    def clear(self) -> None:
        self.memory.clear()
        self.thumbnails.clear()
        self.thumbnail_bytes = 0
