## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Сравнивает пути миниатюр без изменения исходников и рабочего кэша приложения.

Повторные проходы измеряют прогретый файловый кэш ОС. Замеры очереди используют
настоящий QTimer и SQLite, но не включают раскладку и рисование виджетов.
Опциональный libvips запускается с отключённым кэшем операций: повторный вызов
должен декодировать JPEG заново, а не возвращать прошлый результат.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from io import BytesIO
import json
from multiprocessing import get_context
import os
from pathlib import Path
import platform
import random
import statistics
from tempfile import TemporaryDirectory
from time import perf_counter

from PIL import Image, features
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtGui import QImage
import rawpy

from rawww import imaging
from rawww.cache import FolderCache, _decode_jpeg_to_rgba, _encode_jpeg
from rawww.imaging import PixelImage, decode_pixels


def extract(path: Path, direct: bool = False) -> bytes:
    """Извлекает тот же JPEG двумя API rawpy для проверки цены обвязки."""
    with rawpy.RawPy() if direct else rawpy.imread(str(path)) as raw:
        if direct:
            raw.open_file(str(path))
        thumb = raw.extract_thumb()
        if thumb.format != rawpy.ThumbFormat.JPEG:
            raise ValueError("Для этого опыта нужен встроенный JPEG")
        return thumb.data


def pillow_preview(data: bytes, sharpen: bool = True) -> PixelImage:
    """Даёт альтернативу Qt с тем же Pillow-фильтром резкости."""
    return imaging._decode_pillow(Path("preview.jpg"), 256, data=data, sharpen=sharpen)


def vips_preview(data: bytes) -> PixelImage:
    """Материализует libvips-результат до остановки секундомера."""
    import pyvips

    image = pyvips.Image.thumbnail_buffer(data, 256, height=256, size="down")
    if image.bands != 3:
        raise ValueError(f"Неожиданное число каналов: {image.bands}")
    pil = Image.frombytes("RGB", (image.width, image.height), bytes(image.write_to_memory()))
    pil = imaging._sharpen_preview(pil).convert("RGBA")
    return PixelImage(Path("preview.jpg"), pil.tobytes(), pil.width, pil.height)


def direct_cache(data: bytes) -> QImage:
    """Сохраняет RGB888 и собственную память Qt без промежуточных Python bytes."""
    image = QImage.fromData(data, "JPG")
    if image.isNull():
        raise ValueError("JPEG не прочитан")
    return image.convertToFormat(QImage.Format.Format_RGB888)


def worker_decode(path: Path) -> PixelImage:
    """Точка входа spawn без импорта интерфейса приложения."""
    return decode_pixels(path, 256)


def measure(methods: dict, items: list, rounds: int) -> dict:
    """Чередует порядок методов и возвращает медиану полных проходов."""
    samples = {name: [] for name in methods}
    rng = random.Random(42)
    for fn in methods.values():
        fn(items[0])
    for _ in range(rounds):
        names = list(methods)
        rng.shuffle(names)
        for name in names:
            start = perf_counter()
            for item in items:
                methods[name](item)
            samples[name].append((perf_counter() - start) * 1000)
    return {
        name: {"median_ms": statistics.median(values), "ms_per_image": statistics.median(values) / len(items),
               "runs_ms": values, "count": len(items)}
        for name, values in samples.items()
    }


def pool_measure(paths: list[Path], processes: bool, workers: int, rounds: int, decoder=worker_decode) -> dict:
    """Отделяет первый запуск пула от повторных проходов с передачей пикселей."""
    cls = ProcessPoolExecutor if processes else ThreadPoolExecutor
    kwargs = {"mp_context": get_context("spawn")} if processes else {}
    samples = []
    first = []
    first_screen = []
    with cls(max_workers=workers, **kwargs) as pool:
        for _ in range(rounds + 1):
            start = perf_counter()
            futures = [pool.submit(decoder, path) for path in paths]
            count = 0
            for future in as_completed(futures):
                result = future.result()
                if len(result.pixels) != result.width * result.height * 4:
                    raise ValueError("Неполный результат декодирования")
                count += 1
                if count == 1:
                    first.append((perf_counter() - start) * 1000)
                if count == min(24, len(paths)):
                    first_screen.append((perf_counter() - start) * 1000)
            samples.append((perf_counter() - start) * 1000)
    return {"cold_pool_ms": samples[0], "warm_median_ms": statistics.median(samples[1:]),
            "first_image_cold_ms": first[0], "first_24_cold_ms": first_screen[0],
            "first_image_warm_ms": statistics.median(first[1:]),
            "first_24_warm_ms": statistics.median(first_screen[1:]), "runs_ms": samples}


def timed_queue(cache: FolderCache, paths: list[Path], batch: int, interval: int) -> dict:
    """Изолирует ограничение выдачи: один пакет в работе, следующий по QTimer.

    Это модель фоновой подачи, не замер Workspace: сигналы карточек, метаданные,
    видео и отрисовка отсутствуют. Интервал 0 означает пополнение по готовности
    с проверкой результата через event loop.
    """
    loop = QEventLoop()
    timer = QTimer()
    state = {"index": 0, "done": 0, "future": None, "first": None, "screen": None}
    errors = []
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def tick():
            """Принимает готовый пакет и выдаёт следующий без ожидания в UI."""
            try:
                future = state["future"]
                if future is not None:
                    if not future.done():
                        return
                    result = future.result()
                    state["done"] += len(result)
                    elapsed = (perf_counter() - start) * 1000
                    if state["first"] is None:
                        state["first"] = elapsed
                    if state["screen"] is None and state["done"] >= min(24, len(paths)):
                        state["screen"] = elapsed
                    state["future"] = None
                if state["index"] >= len(paths):
                    timer.stop()
                    loop.quit()
                    return
                chunk = paths[state["index"]:state["index"] + batch]
                state["index"] += len(chunk)
                state["future"] = pool.submit(cache.load_batch, chunk, 256)
            except BaseException as exc:
                errors.append(exc)
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start(interval)
        loop.exec()
    if errors:
        raise errors[0]
    assert state["done"] == len(paths)
    return {"total_ms": (perf_counter() - start) * 1000, "first_batch_ms": state["first"],
            "first_24_ms": state["screen"]}


def main() -> None:
    """Запускает независимые опыты; JSON содержит агрегаты без имён фотографий."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--vips", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1 or args.limit < 0:
        parser.error("rounds >= 1, limit >= 0")
    paths = sorted(path for path in args.folder.iterdir() if path.suffix.lower() in imaging.RAW_EXTENSIONS and path.is_file())
    if args.limit:
        paths = paths[:args.limit]
    if not paths:
        parser.error("В папке нет RAW")
    app = QCoreApplication.instance() or QCoreApplication([])
    report = {"environment": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
              "pillow_turbo": features.version_feature("libjpeg_turbo"), "rawpy": rawpy.__version__},
              "images": len(paths), "rounds": args.rounds,
              "notes": "Warm OS cache; isolated temporary SQLite; queue model without widgets; RAW only"}

    def publish(name: str, value: object):
        """Пишет промежуточный отчёт после каждого законченного опыта."""
        report[name] = value
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    # Первый проход заодно прогревает чтение всех источников для сравнения API.
    start = perf_counter()
    blobs = [extract(path) for path in paths]
    publish("initial_extraction_ms", (perf_counter() - start) * 1000)
    with Image.open(BytesIO(blobs[0])) as header:
        publish("first_embedded_jpeg", {"size": header.size, "orientation": header.getexif().get(274),
                                      "icc": bool(header.info.get("icc_profile"))})
    publish("embedded_jpeg_bytes", {"min": min(map(len, blobs)), "max": max(map(len, blobs)), "total": sum(map(len, blobs))})
    publish("extract", measure({"imread": extract, "open_file": lambda p: extract(p, True)}, paths, args.rounds))
    publish("extract_bytes_equal", all(extract(p, True) == blob for p, blob in zip(paths, blobs)))
    methods = {"qt_current": lambda data: imaging._decode_qt_jpeg_bytes(Path("preview.jpg"), data, 256),
               "pillow": pillow_preview, "pillow_no_sharpen": lambda data: pillow_preview(data, False)}
    if args.vips:
        import pyvips
        pyvips.cache_set_max(0)
        report["environment"]["libvips"] = ".".join(str(pyvips.version(i)) for i in range(3))
        methods["vips_pillow_sharpen"] = vips_preview
    publish("jpeg_memory", measure(methods, blobs, args.rounds))
    publish("serial_full_raw", measure({"current": worker_decode}, paths, args.rounds))
    for processes in (False, True):
        for workers in (1, 2, 5):
            publish(f"pool_{'process' if processes else 'thread'}_{workers}", pool_measure(paths, processes, workers, args.rounds))

    pixels = [PixelImage(path, pixel.pixels, pixel.width, pixel.height) for path, pixel in
              zip(paths, (methods["qt_current"](blob) for blob in blobs))]
    encoded = [_encode_jpeg(p.pixels, p.width, p.height) for p in pixels]
    # Размер хранится в SQLite; разбор заголовка намеренно вынесен из замера.
    def old_cached(item):
        """Точная цепочка текущего кэша с заранее известной геометрией."""
        data, pixel = item
        rgba = _decode_jpeg_to_rgba(data)
        return QImage(rgba, pixel.width, pixel.height, pixel.width * 4, QImage.Format.Format_RGBA8888).copy().convertToFormat(QImage.Format.Format_RGB888)
    cached_items = list(zip(encoded, pixels))
    publish("cache_jpeg_memory", measure({"current": old_cached, "direct_rgb888": lambda item: direct_cache(item[0])}, cached_items * 5, args.rounds))
    publish("cache_pixels_equal", all(old_cached(item) == direct_cache(item[0]) for item in cached_items))
    publish("cache_encode", measure({"qt": lambda p: _encode_jpeg(p.pixels, p.width, p.height)}, pixels, args.rounds))
    with TemporaryDirectory(prefix="rawww-preview-bench-") as temporary:
        cache = FolderCache(args.folder, {p.name for p in paths}, cache_root=Path(temporary))
        try:
            start = perf_counter()
            cache.store_pixels_batch([(p, 256) for p in pixels])
            publish("cache_store_total_ms", (perf_counter() - start) * 1000)
            readers = {"single": lambda p: cache.load(p, 256)}
            publish("cache_load_single", measure(readers, paths, args.rounds))
            for batch in (8, 32, 64):
                chunks = [paths[i:i + batch] for i in range(0, len(paths), batch)]
                publish(f"cache_load_batch_{batch}", measure({"load": lambda chunk: cache.load_batch(chunk, 256)}, chunks, args.rounds))
            for batch, interval in ((1, 16), (8, 16), (32, 16), (8, 1), (32, 1)):
                runs = [timed_queue(cache, paths, batch, interval) for _ in range(args.rounds)]
                publish(f"queue_batch{batch}_interval{interval}", {"runs": runs, "median_total_ms": statistics.median(r["total_ms"] for r in runs)})
        finally:
            cache.close(flush=False)


if __name__ == "__main__":
    main()
