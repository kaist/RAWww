## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Лёгкие точки входа process-воркеров пакетной обработки изображений."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .imaging import RAW_EXTENSIONS


def resize_export_worker(
    job: tuple[str, str, int, bool, int, bool, float, int, int, bool, int],
) -> tuple[str, str, str | None]:
    """Экспортирует JPEG в отдельном процессе."""
    (
        source_text,
        output_text,
        max_side,
        sharpen,
        sharpen_amount,
        unsharp,
        unsharp_radius,
        unsharp_amount,
        unsharp_threshold,
        keep_exif,
        raw_orientation,
    ) = job
    source, output = Path(source_text), Path(output_text)
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp")
    try:
        is_raw = source.suffix.lower() in RAW_EXTENSIONS
        if is_raw:
            import rawpy

            with rawpy.imread(str(source)) as raw:
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        image = Image.open(BytesIO(thumb.data))
                    else:
                        image = Image.fromarray(thumb.data)
                except rawpy.LibRawNoThumbnailError:
                    image = Image.fromarray(
                        raw.postprocess(
                            use_camera_wb=True,
                            no_auto_bright=True,
                            output_bps=8,
                        )
                    )
        else:
            image = Image.open(source)
        embedded_orientation = image.getexif().get(274)
        image = ImageOps.exif_transpose(image)
        if is_raw and not embedded_orientation:
            transforms = {
                2: Image.Transpose.FLIP_LEFT_RIGHT,
                3: Image.Transpose.ROTATE_180,
                4: Image.Transpose.FLIP_TOP_BOTTOM,
                5: Image.Transpose.TRANSPOSE,
                6: Image.Transpose.ROTATE_270,
                7: Image.Transpose.TRANSVERSE,
                8: Image.Transpose.ROTATE_90,
            }
            transform = transforms.get(int(raw_orientation or 1))
            if transform is not None:
                image = image.transpose(transform)
        exif = image.info.get("exif") or image.getexif().tobytes() if keep_exif else b""
        icc_profile = image.info.get("icc_profile")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if sharpen:
            image = ImageEnhance.Sharpness(image).enhance(sharpen_amount / 100)
        if unsharp:
            image = image.filter(
                ImageFilter.UnsharpMask(
                    radius=unsharp_radius,
                    percent=unsharp_amount,
                    threshold=unsharp_threshold,
                )
            )
        image = image.convert("RGB")
        options = {"format": "JPEG", "quality": 95, "subsampling": 0}
        if exif:
            options["exif"] = exif
        if icc_profile:
            options["icc_profile"] = icc_profile
        image.save(temporary, **options)
        os.replace(temporary, output)
        return source_text, output_text, None
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return source_text, output_text, str(exc)


def recompress_jpeg_worker(job: tuple[str, int, bool]) -> tuple[str, int, int, str | None]:
    """Пересохраняет JPEG с меньшим качеством, сохраняя профиль цвета и EXIF."""
    source_text, quality, keep_exif = job
    source = Path(source_text)
    temporary = source.with_name(f".{source.stem}.{uuid4().hex}.tmp")
    try:
        original_size = source.stat().st_size
        with Image.open(source) as opened:
            opened.load()
            exif = opened.info.get("exif") if keep_exif else None
            icc_profile = opened.info.get("icc_profile")
            image = opened if opened.mode in ("RGB", "L", "CMYK") else opened.convert("RGB")
            options = {"format": "JPEG", "quality": int(quality), "subsampling": "keep"}
            if exif:
                options["exif"] = exif
            if icc_profile:
                options["icc_profile"] = icc_profile
            try:
                image.save(temporary, **options)
            except (ValueError, OSError):
                options.pop("subsampling", None)
                image.save(temporary, **options)
        new_size = temporary.stat().st_size
        os.replace(temporary, source)
        return source_text, original_size, new_size, None
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return source_text, 0, 0, str(exc)
