## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

from .cache import FolderCache
from .exif import extract_metadata_batch
from .imaging import PixelImage, decode_pixels, decode_thumbnail_pixels, is_supported_image


def main() -> None:
    """Измеряет скорость декодирования превью и извлечения EXIF."""
    parser = argparse.ArgumentParser(description="Benchmark Контролька decode and cache performance.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--full-limit", type=int, default=8)
    parser.add_argument("--full-size", type=int, default=2560)
    args = parser.parse_args()

    paths = sorted(
        [path for path in args.folder.iterdir() if path.is_file() and is_supported_image(path)],
        key=lambda item: item.name.lower(),
    )[: args.limit]
    if not paths:
        print("No supported images found.")
        return

    print(f"Folder: {args.folder}")
    print(f"Images: {len(paths)}")
    print(f"Workers: {os.cpu_count() or 1}")

    cache = FolderCache(args.folder, {path.name for path in paths}, eager_variants={256})
    thumb_pixels = _decode_many(paths, 256)
    _benchmark_thumbnail_creation_with_exif(paths, 256)
    for pixel in thumb_pixels:
        cache.store_pixels(pixel, 256)

    full_paths = paths[: args.full_limit]
    full_pixels = _decode_many(full_paths, args.full_size)
    for pixel in full_pixels:
        cache.store_pixels(pixel, args.full_size)

    start = perf_counter()
    cache.flush()
    flush_seconds = perf_counter() - start

    cache_path = cache.path
    cache.close(flush=False)

    print(f"Flush in {flush_seconds:.3f}s")
    print(f"Disk cache: {_cache_size(cache_path) / 1024 / 1024:.1f} MB (unlimited)")

    start = perf_counter()
    warm_cache = FolderCache(args.folder, {path.name for path in paths}, eager_variants={256})
    eager_seconds = perf_counter() - start

    start = perf_counter()
    thumb_hits = [warm_cache.load(path, 256) for path in paths]
    thumb_hit_seconds = perf_counter() - start

    start = perf_counter()
    full_hits = [warm_cache.load(path, args.full_size) for path in full_paths]
    full_hit_seconds = perf_counter() - start
    warm_cache.close(flush=False)

    print(f"Eager thumb cache load: {eager_seconds:.3f}s")
    print(
        f"Thumb SQLite hits (no EXIF read): "
        f"{len([item for item in thumb_hits if item])}/{len(paths)} in {thumb_hit_seconds:.3f}s"
    )
    print(f"Full RAM-DB JPEG hits: {len([item for item in full_hits if item])}/{len(full_paths)} in {full_hit_seconds:.3f}s")


def _decode_many(paths: list[Path], size: int) -> list[PixelImage]:
    start = perf_counter()
    results: list[PixelImage] = []
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        futures = [pool.submit(decode_pixels, path, size) for path in paths]
        for future in as_completed(futures):
            results.append(future.result())
    seconds = perf_counter() - start
    rate = len(paths) / seconds if seconds > 0 else 0
    print(f"Decode {size}px: {len(paths)} files in {seconds:.3f}s ({rate:.1f}/s)")
    return results


def _decode_thumbnail_and_exif(path: Path, size: int) -> tuple[PixelImage, list[tuple[str, str]]]:
    """Приблизительно воспроизводит старый путь миниатюры, блокировавшийся на EXIF."""
    return decode_thumbnail_pixels(path, size), extract_metadata_batch([str(path)])


def _benchmark_thumbnail_creation_with_exif(paths: list[Path], size: int) -> None:
    start = perf_counter()
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        futures = [pool.submit(_decode_thumbnail_and_exif, path, size) for path in paths]
        for future in as_completed(futures):
            future.result()
    seconds = perf_counter() - start
    rate = len(paths) / seconds if seconds > 0 else 0
    print(f"Decode {size}px + blocking EXIF: {len(paths)} files in {seconds:.3f}s ({rate:.1f}/s)")


def _cache_size(cache_path: Path) -> int:
    if not cache_path.exists():
        return 0
    return cache_path.stat().st_size


if __name__ == "__main__":
    main()
