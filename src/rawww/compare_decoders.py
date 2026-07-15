## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from PIL import Image, ImageOps
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QGuiApplication, QImageReader

from .imaging import RAW_EXTENSIONS, _convert_to_srgb

try:
    import rawpy
except ImportError:  # pragma: no cover
    rawpy = None


def main() -> None:
    """Сравнивает доступные декодеры на одном изображении и сохраняет результаты."""
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--size", type=int, default=2560)
    args = parser.parse_args()

    QGuiApplication.instance() or QGuiApplication([])
    paths = sorted(
        [path for path in args.folder.iterdir() if path.is_file() and path.suffix.lower() in RAW_EXTENSIONS],
        key=lambda path: path.name.lower(),
    )[: args.limit]

    stages = {name: [] for name in ["raw", "pil_load", "pil_icc", "pil_resize", "pil_rgba", "qt_scaled", "qt_full"]}
    for path in paths:
        start = perf_counter()
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
        stages["raw"].append(_elapsed_ms(start))
        data = thumb.data

        start = perf_counter()
        image = Image.open(BytesIO(data))
        image.draft("RGB", (args.size, args.size))
        image = ImageOps.exif_transpose(image)
        image.load()
        stages["pil_load"].append(_elapsed_ms(start))

        start = perf_counter()
        image = _convert_to_srgb(image)
        stages["pil_icc"].append(_elapsed_ms(start))

        start = perf_counter()
        image.thumbnail((args.size, args.size), Image.Resampling.LANCZOS)
        stages["pil_resize"].append(_elapsed_ms(start))

        start = perf_counter()
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        image.tobytes("raw", "RGBA")
        stages["pil_rgba"].append(_elapsed_ms(start))

        stages["qt_scaled"].append(_read_qt(data, args.size, scaled=True))
        stages["qt_full"].append(_read_qt(data, args.size, scaled=False))

    for name, values in stages.items():
        print(
            f"{name}: avg={mean(values):.1f}ms med={median(values):.1f}ms "
            f"min={min(values):.1f}ms max={max(values):.1f}ms"
        )


def _read_qt(data: bytes, size: int, *, scaled: bool) -> float:
    byte_array = QByteArray(data)
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"JPG")
    reader.setAutoTransform(True)
    if scaled:
        reader.setScaledSize(QSize(size, size))
    start = perf_counter()
    image = reader.read()
    if image.isNull():
        raise RuntimeError(reader.errorString())
    return _elapsed_ms(start)


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000


if __name__ == "__main__":
    main()
