## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Выбор встроенного JPEG по размеру через LibRaw без распаковки RAW-матрицы.

Каждому запросу принадлежит отдельный libraw_data_t. Нативный мост знает ABI
структур, ctypes вызывает C API с освобождённым GIL. Наружу выходят только
байты и числа; Qt и жизненный цикл фоновых задач остаются у декодера.
"""

from __future__ import annotations

import ctypes as ct
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class PreviewInfo:
    """Описывает один вариант LibRaw; неизвестная геометрия обозначается нулями."""

    index: int
    width: int
    height: int
    flip: int
    byte_size: int
    jpeg: bool
    source_flip: int


@dataclass(frozen=True)
class EmbeddedPreview:
    """Владеет копией JPEG после закрытия LibRaw и метаданными выбранного варианта."""

    data: bytes
    info: PreviewInfo


def choose_preview(previews: list[PreviewInfo], max_size: int) -> PreviewInfo | None:
    """Выбирает минимальный достаточный JPEG; неизвестный размер оставляет fallback.

    Маленькие служебные картинки не растягиваются. При отсутствии достаточного
    варианта старый extract_thumb выберет самый большой, включая варианты,
    разрешение которых LibRaw не смог прочитать из метаданных.
    """
    if max_size <= 0:
        return None
    known = [p for p in previews if p.jpeg and p.width > 0 and p.height > 0 and p.byte_size > 0]
    if not known:
        return None
    reference = max(known, key=lambda p: p.width * p.height)
    aspect = max(reference.width, reference.height) / min(reference.width, reference.height)
    # Служебная 4:3-картинка может содержать поля вокруг кадра 3:2. Небольшое
    # расхождение округления (например Nikon 640x424) сохраняем допустимым.
    candidates = [p for p in known if max(p.width, p.height) >= max_size
                  and abs((max(p.width, p.height) / min(p.width, p.height)) / aspect - 1) <= 0.02]
    return min(candidates, key=lambda p: (p.width * p.height, p.byte_size, p.index), default=None)


def _signature(library, name, result, *arguments):
    """Фиксирует ABI до первого вызова, особенно ширину указателей на Windows."""
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class _Backend:
    """Держит загруженные библиотеки процесса; дескрипторы отдельных RAW не хранит."""

    def __init__(self, bridge, library):
        self.bridge = bridge
        self.library = library
        version = _signature(library, "libraw_versionNumber", ct.c_int)()
        expected = _signature(bridge, "rawww_headers_version", ct.c_int)()
        api = _signature(bridge, "rawww_bridge_version", ct.c_int)()
        if version != expected or api != 1:
            raise RuntimeError("Версия ABI нативного моста не совпадает с LibRaw")
        self.init = _signature(library, "libraw_init", ct.c_void_p, ct.c_uint)
        self.close = _signature(library, "libraw_close", None, ct.c_void_p)
        if sys.platform == "win32":
            self.open = _signature(library, "libraw_open_wfile", ct.c_int, ct.c_void_p, ct.c_wchar_p)
        else:
            self.open = _signature(library, "libraw_open_file", ct.c_int, ct.c_void_p, ct.c_char_p)
        self.info = _signature(bridge, "rawww_preview_info", ct.c_int, ct.c_void_p, ct.c_int, ct.POINTER(ct.c_int64))
        self.unpack = _signature(library, "libraw_unpack_thumb_ex", ct.c_int, ct.c_void_p, ct.c_int)
        self.orient = _signature(bridge, "rawww_preview_orientation", None, ct.c_void_p, ct.c_int)
        self.make = _signature(library, "libraw_dcraw_make_mem_thumb", ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_int))
        self.clear = _signature(library, "libraw_dcraw_clear_mem", None, ct.c_void_p)
        self.jpeg_data = _signature(bridge, "rawww_jpeg_data", ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_uint64))

    def open_path(self, path: Path):
        """Создаёт независимый reader; ошибку открытия закрывает здесь же."""
        handle = self.init(0)
        if not handle:
            raise MemoryError("LibRaw не смог создать reader")
        name = str(path) if sys.platform == "win32" else os.fsencode(path)
        if self.open(handle, name) != 0:
            self.close(handle)
            raise OSError(f"LibRaw не открыл {path}")
        return handle

    def previews(self, handle) -> list[PreviewInfo]:
        """Копирует ограниченный список вариантов, не извлекая их JPEG."""
        result = []
        for index in range(8):
            values = (ct.c_int64 * 6)()
            if not self.info(handle, index, values):
                break
            width, height, flip, size, jpeg, source_flip = values
            result.append(PreviewInfo(index, width, height, flip, size, bool(jpeg), source_flip))
        return result


@lru_cache(maxsize=1)
def _backend() -> _Backend | None:
    """Лениво ищет мост и именно LibRaw установленного rawpy; системную ABI не угадывает."""
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    bridge_path = Path(__file__).parent / "_native" / f"libraw_preview_bridge{suffix}"
    if not bridge_path.is_file():
        return None
    try:
        import rawpy
        import rawpy._rawpy

        root = Path(rawpy.__file__).parent
        candidates = [root / "raw_r.dll"] if sys.platform == "win32" else [
            *root.glob("*raw*.dylib"), *root.glob("*raw*.so*"),
            *(root.parent / "rawpy.libs").glob("*raw*.so*"),
            *(root / ".dylibs").glob("*raw*.dylib"),
            Path(rawpy._rawpy.__file__),
        ]
        bridge = ct.CDLL(str(bridge_path))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                return _Backend(bridge, ct.CDLL(str(candidate)))
            except (OSError, AttributeError, RuntimeError):
                continue
    except (ImportError, OSError, AttributeError):
        pass
    return None


def available() -> bool:
    """Сообщает, совпала ли версия собранного моста с доступной LibRaw."""
    return _backend() is not None


def list_previews(path: Path) -> list[PreviewInfo]:
    """Перечисляет встроенные варианты для диагностики и выбора размера."""
    backend = _backend()
    if backend is None:
        return []
    handle = backend.open_path(path)
    try:
        return backend.previews(handle)
    finally:
        backend.close(handle)


def extract_preview(path: Path, max_size: int) -> EmbeddedPreview | None:
    """Возвращает подходящий JPEG или None, чтобы декодер использовал старый путь."""
    backend = _backend()
    if backend is None or max_size <= 0:
        return None
    handle = backend.open_path(path)
    try:
        chosen = choose_preview(backend.previews(handle), max_size)
        if chosen is None or backend.unpack(handle, chosen.index) != 0:
            return None
        backend.orient(handle, chosen.index)
        error = ct.c_int()
        image = backend.make(handle, ct.byref(error))
        if not image:
            return None
        try:
            if error.value:
                return None
            size = ct.c_uint64()
            pointer = backend.jpeg_data(image, ct.byref(size))
            if not pointer or not 0 < size.value <= 256 * 1024 * 1024:
                return None
            data = ct.string_at(pointer, size.value)
            if not data.startswith(b"\xff\xd8"):
                return None
            return EmbeddedPreview(data, chosen)
        finally:
            backend.clear(image)
    finally:
        backend.close(handle)
