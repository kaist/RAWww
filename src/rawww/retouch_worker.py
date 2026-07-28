## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Одноразовый процесс для предпросмотра и пакетной ретуши."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .imaging import RAW_EXTENSIONS, decode_original_pixels, decode_pixels
from .retouch_pipeline import RetouchSettings, SkinRetoucher
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


def _jpeg_bytes(rgb: np.ndarray) -> bytes:
    """Кодирует preview в памяти, чтобы не создавать временный файл на диске."""
    from io import BytesIO

    buffer = BytesIO()
    Image.fromarray(rgb, "RGB").save(buffer, format="JPEG", quality=95, subsampling=0)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", nargs="?", type=Path)
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.stdin:
            # QProcess передаёт JSON в UTF-8, а ``sys.stdin`` на Windows может
            # быть обёрнут в cp1251/cp866 и испортить путь с кириллицей.
            job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        elif args.job is not None:
            job = json.loads(args.job.read_text(encoding="utf-8"))
        else:
            raise RuntimeError("Не передана задача ретуши")
        settings = RetouchSettings(**job["settings"])
        retoucher = SkinRetoucher(data_path("models") / "retouch")
        tasks = job["tasks"]
        for index, task in enumerate(tasks, 1):
            source = Path(task["source"])
            original, origin, full_size = _read(source, task.get("max_side"), tuple(task["region"]) if task.get("region") else None)
            result = retoucher.process(original, settings)
            if job.get("preview"):
                _event(
                    "preview",
                    jpeg=base64.b64encode(_jpeg_bytes(result)).decode("ascii"),
                    before=base64.b64encode(_jpeg_bytes(original)).decode("ascii"),
                    origin=origin,
                    full_size=full_size,
                )
            else:
                target = Path(task["target"])
                _write(target, result)
                _event("progress", done=index, total=len(tasks), source=str(source), target=str(target))
        _event("finished")
        return 0
    except Exception as exc:
        _event("error", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
