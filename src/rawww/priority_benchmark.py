from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from PySide6.QtCore import QSettings

from .ai import AiPipeline
from .cache import FolderCache
from .imaging import decode_pixels, is_supported_image


def _decode_visible(paths: list[Path], *, with_ai: bool) -> tuple[float, list[float]]:
    with TemporaryDirectory(prefix="rawww-priority-") as temporary:
        cache = FolderCache(paths[0].parent, {path.name for path in paths}, cache_root=Path(temporary))
        visible = ProcessPoolExecutor(max_workers=1)
        background = ProcessPoolExecutor(max_workers=3)
        pipeline = AiPipeline() if with_ai else None
        if pipeline:
            pipeline.scan(paths, cache, background)
        # Exclude process spawn/import time from the visible latency comparison.
        visible.submit(decode_pixels, paths[0], 256).result()
        latencies = []
        started_all = perf_counter()
        for path in paths[1:11]:
            started = perf_counter()
            visible.submit(decode_pixels, path, 256).result()
            latencies.append(perf_counter() - started)
        elapsed = perf_counter() - started_all
        if pipeline:
            pipeline.shutdown()
        visible.shutdown(wait=True, cancel_futures=True)
        background.shutdown(wait=False, cancel_futures=True)
        cache.close(flush=False)
        return elapsed, latencies


def main() -> None:
    folder = Path(str(QSettings("Контролька", "Контролька").value("last_directory", Path.cwd())))
    paths = sorted(path for path in folder.iterdir() if path.is_file() and is_supported_image(path))[:32]
    baseline, baseline_items = _decode_visible(paths, with_ai=False)
    loaded, loaded_items = _decode_visible(paths, with_ai=True)
    print(f"Folder: {folder}")
    print(f"Visible previews measured: {len(baseline_items)}")
    print(f"Without AI: {baseline:.3f}s, {baseline / len(baseline_items) * 1000:.2f} ms/image")
    print(f"With AI:    {loaded:.3f}s, {loaded / len(loaded_items) * 1000:.2f} ms/image")
    print(f"Slowdown:   {loaded / baseline:.2f}x ({(loaded / baseline - 1) * 100:.1f}%)")
    print(f"Worst visible latency: {max(loaded_items) * 1000:.2f} ms")


if __name__ == "__main__":
    main()
