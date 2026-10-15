## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет чтение среднего PRVW вместо полного JPEG в обычных Canon CR3.

Экспериментальный reader ограничен UUID Canon и не предназначен для подмены
общего декодера RAW. Для ориентации пока используется LibRaw: это сохраняет
измерение честным, не выдавая неповёрнутую картинку за готовый результат.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import statistics

from PIL import Image
import numpy as np
import rawpy

from benchmark_previews import extract, measure
from rawww import imaging


def read_prvw(path: Path) -> bytes:
    """Читает ограниченный PRVW внутри UUID по заголовкам ISO BMFF, без mdat."""
    file_size = path.stat().st_size
    with path.open("rb") as source:
        position = 0
        while position + 8 <= file_size:
            source.seek(position)
            header = source.read(8)
            size = int.from_bytes(header[:4], "big")
            kind = header[4:]
            header_size = 8
            if size == 1:
                size = int.from_bytes(source.read(8), "big")
                header_size = 16
            if size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                raise ValueError("Повреждённый размер CR3 box")
            if kind == b"uuid":
                prefix = source.read(min(64, size - header_size))
                # UUID контейнера Canon PRVW, за ним 8 байтов до дочернего box.
                if prefix[:16] == bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16"):
                    child = position + header_size + 24
                    source.seek(child)
                    prvw = source.read(24)
                    if len(prvw) != 24 or prvw[4:8] != b"PRVW":
                        raise ValueError("Неожиданный заголовок PRVW")
                    child_size = int.from_bytes(prvw[:4], "big")
                    jpeg_size = int.from_bytes(prvw[20:24], "big")
                    if jpeg_size <= 0 or jpeg_size > child_size - 24 or child + child_size > position + size:
                        raise ValueError("PRVW выходит за границы контейнера")
                    data = source.read(jpeg_size)
                    if len(data) != jpeg_size or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                        raise ValueError("Неполный JPEG PRVW")
                    return data
            position += size
    raise ValueError("PRVW не найден")


def medium_preview(path: Path) -> imaging.PixelImage:
    """Возвращает PRVW 256 px с ориентацией из LibRaw и штатной резкостью."""
    data = read_prvw(path)
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
    pixel = imaging._decode_qt_jpeg_bytes(path, data, 256)
    image = Image.frombytes("RGBA", (pixel.width, pixel.height), pixel.pixels)
    operation = {0: None, 3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90,
                 6: Image.Transpose.ROTATE_270}.get(flip)
    if flip not in (0, 3, 5, 6):
        raise ValueError(f"Непроверенный поворот LibRaw: {flip}")
    if operation is not None:
        image = image.transpose(operation)
    return imaging.PixelImage(path, image.tobytes(), image.width, image.height)


def main() -> None:
    """Сравнивает байты, геометрию, пиксели и длительность на всей папке."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    paths = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() == ".cr3")
    if not paths or args.rounds < 1:
        parser.error("Нужны CR3 и положительное число повторов")
    blobs = [read_prvw(p) for p in paths]
    with Image.open(BytesIO(blobs[0])) as im:
        dimensions = im.size
    report = {"images": len(paths), "prvw_dimensions": dimensions,
              "prvw_bytes_total": sum(map(len, blobs)), "prvw_bytes_median": statistics.median(map(len, blobs))}
    print(json.dumps(report), flush=True)
    report["extract"] = measure({"largest": extract, "prvw": read_prvw}, paths, args.rounds)
    print("extract", json.dumps(report["extract"]), flush=True)
    report["decode"] = measure({"current": lambda p: imaging.decode_pixels(p, 256), "prvw_with_orientation": medium_preview}, paths, args.rounds)
    print("decode", json.dumps(report["decode"]), flush=True)
    errors = []
    sizes_equal = 0
    for path in paths:
        before = imaging.decode_pixels(path, 256)
        after = medium_preview(path)
        if (before.width, before.height) != (after.width, after.height):
            continue
        sizes_equal += 1
        a = np.frombuffer(before.pixels, dtype=np.uint8).reshape(-1, 4)[:, :3].astype(float)
        b = np.frombuffer(after.pixels, dtype=np.uint8).reshape(-1, 4)[:, :3].astype(float)
        errors.append(float(np.abs(a - b).mean()))
    report["quality"] = {"equal_dimensions": sizes_equal, "rgb_mae_median": statistics.median(errors) if errors else None,
                         "rgb_mae_max": max(errors) if errors else None,
                         "note": "Different camera JPEG/resampling; MAE is diagnostic, not perceptual approval"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("quality", json.dumps(report["quality"]), flush=True)


if __name__ == "__main__":
    main()
