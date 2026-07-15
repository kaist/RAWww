## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from PIL import Image, ImageOps
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

from .cache import FolderCache
from .imaging import RAW_EXTENSIONS, PixelImage, _convert_to_srgb, decode_pixels, is_supported_image

try:
    import rawpy
except ImportError:  # pragma: no cover
    rawpy = None


@dataclass
class StageTimes:
    """Замеры отдельных стадий декодирования одного файла."""

    raw_thumb_ms: float
    pillow_ms: float
    qimage_ms: float
    cache_store_ms: float
    cache_hit_ms: float
    pixmap_scale_ms: float


def main() -> None:
    """Профилирует стадии конвейера на файлах переданной папки."""
    parser = argparse.ArgumentParser(description="Profile Контролька pipeline stages.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--size", type=int, default=2560)
    args = parser.parse_args()

    app = QGuiApplication.instance() or QGuiApplication([])
    paths = sorted(
        [path for path in args.folder.iterdir() if path.is_file() and is_supported_image(path)],
        key=lambda item: item.name.lower(),
    )[: args.limit]
    if not paths:
        print("No supported images found.")
        return

    cache = FolderCache(args.folder, {path.name for path in paths})
    samples = [_profile_path(cache, path, args.size) for path in paths]
    cache.close(flush=False)

    print(f"Folder: {args.folder}")
    print(f"Files: {len(samples)}")
    _print_stage("RAW thumb", [sample.raw_thumb_ms for sample in samples])
    _print_stage("Pillow decode+resize+ICC", [sample.pillow_ms for sample in samples])
    _print_stage("QImage from RGBA", [sample.qimage_ms for sample in samples])
    _print_stage("Cache store JPEG", [sample.cache_store_ms for sample in samples])
    _print_stage("Cache hit JPEG->QImage", [sample.cache_hit_ms for sample in samples])
    _print_stage("QPixmap full scale", [sample.pixmap_scale_ms for sample in samples])


def _profile_path(cache: FolderCache, path: Path, size: int) -> StageTimes:
    """Замеряет декодирование, кодирование и запись кэша для одного файла."""
    raw_thumb_ms = 0.0
    pillow_ms = 0.0

    if path.suffix.lower() in RAW_EXTENSIONS and rawpy is not None:
        start = perf_counter()
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
        raw_thumb_ms = _elapsed_ms(start)

        if thumb.format == rawpy.ThumbFormat.JPEG:
            start = perf_counter()
            pixel = _decode_jpeg_bytes(path, thumb.data, size)
            pillow_ms = _elapsed_ms(start)
        else:
            start = perf_counter()
            pixel = decode_pixels(path, size)
            pillow_ms = _elapsed_ms(start)
    else:
        start = perf_counter()
        pixel = decode_pixels(path, size)
        pillow_ms = _elapsed_ms(start)

    start = perf_counter()
    qimage = QImage(
        pixel.pixels,
        pixel.width,
        pixel.height,
        pixel.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
    qimage_ms = _elapsed_ms(start)

    start = perf_counter()
    cache.store_pixels(pixel, size)
    cache_store_ms = _elapsed_ms(start)

    start = perf_counter()
    decoded = cache.load(path, size)
    cache_hit_ms = _elapsed_ms(start)

    if decoded is None:
        pixmap_scale_ms = 0.0
    else:
        start = perf_counter()
        pixmap = QPixmap.fromImage(decoded.image)
        pixmap.scaled(2560, 1440)
        pixmap_scale_ms = _elapsed_ms(start)

    return StageTimes(
        raw_thumb_ms=raw_thumb_ms,
        pillow_ms=pillow_ms,
        qimage_ms=qimage_ms,
        cache_store_ms=cache_store_ms,
        cache_hit_ms=cache_hit_ms,
        pixmap_scale_ms=pixmap_scale_ms,
    )


def _decode_jpeg_bytes(path: Path, data: bytes, size: int) -> PixelImage:
    with Image.open(BytesIO(data)) as image:
        if image.format == "JPEG":
            image.draft("RGB", (size, size))
        image = ImageOps.exif_transpose(image)
        image = _convert_to_srgb(image)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        width, height = image.size
        return PixelImage(path=path, pixels=image.tobytes("raw", "RGBA"), width=width, height=height)


def _print_stage(name: str, values: list[float]) -> None:
    active = [value for value in values if value > 0]
    if not active:
        return
    print(
        f"{name}: avg={mean(active):.1f}ms med={median(active):.1f}ms "
        f"min={min(active):.1f}ms max={max(active):.1f}ms"
    )


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    main()
