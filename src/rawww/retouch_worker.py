## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Процесс предпросмотра и пакетной ретуши.

Пакет запускается одноразовым процессом и завершается сам. Предпросмотр же
живёт вместе с окном: процесс читает задачи построчно и держит распакованный
кадр с масками кожи, поэтому движение ползунка пересчитывает только цветовые
этапы, а не декодирование, сегментацию и разбор лица заново.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import replace
import json
import os
import queue
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .imaging import RAW_EXTENSIONS, decode_original_pixels, decode_pixels
from .retouch_pipeline import RetouchSettings, SkinMasks, SkinRetoucher
from .runtime_paths import data_path


def _event(kind: str, **values) -> None:
    """Отправляет протокол всегда в UTF-8, независимо от кодовой страницы Windows."""
    sys.stdout.buffer.write(json.dumps({"event": kind, **values}, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _read(path: Path, max_side: int | None, region: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, tuple[int, int], tuple[int, int]]:
    if region is not None and path.suffix.lower() not in RAW_EXTENSIONS:
        # JPEG обычно распаковывается лениво: вырезаем только область, которая
        # нужна экрану, плюс заранее переданный контекст для маски и размытия.
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            full_size = image.size
            x, y, width, height = region
            left, top = max(0, x), max(0, y)
            right, bottom = min(full_size[0], x + width), min(full_size[1], y + height)
            return np.asarray(image.crop((left, top, right, bottom)), dtype=np.uint8), (left, top), full_size
    # Для вписанного preview JPEG декодируется сразу в нужном черновом размере:
    # ``decode_pixels`` использует Pillow.draft, не распаковывая оригинал целиком.
    pixel = decode_pixels(path, max_side) if max_side else decode_original_pixels(path)
    image = Image.frombytes("RGBA", (pixel.width, pixel.height), pixel.pixels).convert("RGB")
    if path.suffix.lower() not in RAW_EXTENSIONS:
        with Image.open(path) as opened:
            native = ImageOps.exif_transpose(opened).size
    else:
        native = image.size
    return np.asarray(image, dtype=np.uint8), (0, 0), native


def _write(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    image = Image.fromarray(rgb, "RGB")
    image.save(temporary, format="JPEG", quality=95, subsampling=0)
    os.replace(temporary, path)


def _jpeg_bytes(rgb: np.ndarray, quality: int = 95) -> bytes:
    """Кодирует preview в памяти, чтобы не создавать временный файл на диске."""
    from io import BytesIO

    buffer = BytesIO()
    Image.fromarray(rgb, "RGB").save(buffer, format="JPEG", quality=quality, subsampling=0)
    return buffer.getvalue()


class _Frame:
    """Распакованный кадр предпросмотра вместе с масками кожи.

    Кэш живёт до смены снимка или области 100 %: маски зависят только от
    пикселей, а ползунки на них не влияют.
    """

    def __init__(self, key: tuple, rgb: np.ndarray, origin: tuple[int, int], full_size: tuple[int, int]) -> None:
        self.key = key
        self.rgb = rgb
        self.origin = origin
        self.full_size = full_size
        self.masks: SkinMasks | None = None
        self.sent_before = False


class _Preview:
    """Обрабатывает поток задач предпросмотра одним процессом с кэшем кадра."""

    def __init__(self, retoucher: SkinRetoucher) -> None:
        self._retoucher = retoucher
        self._frame: _Frame | None = None

    def run(self, task: dict, settings: RetouchSettings, *, stale) -> None:
        source = Path(task["source"])
        region = tuple(task["region"]) if task.get("region") else None
        key = (str(source), task.get("max_side"), region)
        frame = self._frame
        if frame is None or frame.key != key:
            rgb, origin, full_size = _read(source, task.get("max_side"), region)
            frame = _Frame(key, rgb, origin, full_size)
            self._frame = frame
        if frame.masks is None:
            frame.masks = self._retoucher.skin_masks(frame.rgb)
        if stale():
            return
        base = self._retoucher.retouch_skin(frame.rgb, replace(settings, neural_retouch=False), frame.masks)
        # Цветовой результат уходит на экран сразу: нейроретушь на кадре превью
        # стоит в разы больше, а ждать её, чтобы увидеть тон, незачем.
        self._send(frame, self._retoucher.finish(base, settings), exact=not settings.neural_retouch)
        if not settings.neural_retouch or stale():
            return
        # Нейроретушь ложится на кадр до цвета и таблицы, иначе точный
        # предпросмотр отличался бы от результата пакета порядком этапов.
        exact = self._retoucher.neural_retouch(base, frame.masks.skin, settings.neural_strength)
        self._send(frame, self._retoucher.finish(exact, settings), exact=True)

    def _send(self, frame: _Frame, result: np.ndarray, *, exact: bool) -> None:
        values = {
            "jpeg": base64.b64encode(_jpeg_bytes(result, 92)).decode("ascii"),
            "origin": frame.origin,
            "full_size": frame.full_size,
            "exact": exact,
        }
        if not frame.sent_before:
            # Оригинал для сравнения передаётся один раз на кадр: он не зависит
            # от настроек, а лишний JPEG на каждое движение ползунка — это
            # кодирование и пересылка нескольких мегабайт впустую.
            values["before"] = base64.b64encode(_jpeg_bytes(frame.rgb, 92)).decode("ascii")
            frame.sent_before = True
        _event("preview", **values)


def _batch(retoucher: SkinRetoucher, tasks: list[dict], settings: RetouchSettings) -> None:
    for index, task in enumerate(tasks, 1):
        source = Path(task["source"])
        original, _origin, _full_size = _read(source, task.get("max_side"))
        target = Path(task["target"])
        _write(target, retoucher.process(original, settings))
        _event("progress", done=index, total=len(tasks), source=str(source), target=str(target))


def _jobs() -> queue.Queue:
    """Читает задачи из stdin отдельным потоком: select на трубе не работает в Windows."""
    incoming: queue.Queue = queue.Queue()

    def reader() -> None:
        # QProcess передаёт JSON в UTF-8, а ``sys.stdin`` на Windows может быть
        # обёрнут в cp1251/cp866 и испортить путь с кириллицей.
        for line in sys.stdin.buffer:
            if line.strip():
                incoming.put(line)
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()
    return incoming


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", nargs="?", type=Path)
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        retoucher: SkinRetoucher | None = None
        preview: _Preview | None = None
        if args.stdin:
            incoming = _jobs()
            closed = False
            while True:
                line = incoming.get()
                if line is None:
                    return 0
                while not incoming.empty():
                    # Пока считался старый предпросмотр, ползунок уехал дальше:
                    # промежуточные задачи никому не нужны. Конец потока при
                    # этом только запоминается: задачу в руках надо досчитать,
                    # иначе пакет из одной строки с сразу закрытым каналом
                    # молча пропал бы.
                    newer = incoming.get()
                    if newer is None:
                        closed = True
                        break
                    line = newer
                job = json.loads(line.decode("utf-8"))
                if retoucher is None:
                    retoucher = SkinRetoucher(data_path("models") / "retouch")
                settings = RetouchSettings(**job["settings"])
                if job.get("preview"):
                    if preview is None:
                        preview = _Preview(retoucher)
                    try:
                        preview.run(job["tasks"][0], settings, stale=lambda: not incoming.empty())
                    except Exception as exc:
                        # Битая таблица или недоступный файл не должны уносить
                        # живой процесс превью: модели грузились секунды.
                        _event("error", message=str(exc))
                    _event("finished")
                    if closed:
                        return 0
                    continue
                _batch(retoucher, job["tasks"], settings)
                _event("finished")
                return 0
        if args.job is None:
            raise RuntimeError("Не передана задача ретуши")
        job = json.loads(args.job.read_text(encoding="utf-8"))
        retoucher = SkinRetoucher(data_path("models") / "retouch")
        _batch(retoucher, job["tasks"], RetouchSettings(**job["settings"]))
        _event("finished")
        return 0
    except Exception as exc:
        _event("error", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
