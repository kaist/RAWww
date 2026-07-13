from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage, QImageReader

from .worker_priority import lower_background_priority

if TYPE_CHECKING:
    from PIL import Image

rawpy = None

# Pillow is only needed when a preview is actually decoded/encoded. The mobile
# build never packages it (it decodes previews with QImageReader instead), so it
# is imported lazily to keep ``import rawww.imaging`` free of the dependency.
Image = None  # type: ignore[assignment]
ImageCms = None
ImageFilter = None
ImageOps = None


def _ensure_pillow() -> None:
    """Import Pillow the first time a decode path needs it."""
    global Image, ImageCms, ImageFilter, ImageOps
    if Image is None:
        from PIL import Image as _Image
        from PIL import ImageCms as _ImageCms
        from PIL import ImageFilter as _ImageFilter
        from PIL import ImageOps as _ImageOps

        Image, ImageCms, ImageFilter, ImageOps = _Image, _ImageCms, _ImageFilter, _ImageOps


def _rawpy():
    """Import the native RAW decoder only when a RAW file is actually read."""
    global rawpy
    if rawpy is None:
        try:
            import rawpy as module
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("rawpy is not installed") from exc
        rawpy = module
    return rawpy


JPEG_EXTENSIONS = {".jpg", ".jpeg", ".jpe"}
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".iiq",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".x3f",
}
IMAGE_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS | {".png", ".tif", ".tiff", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
SHARPEN_PREVIEWS = True


@dataclass(frozen=True)
class DecodedImage:
    path: Path
    image: QImage
    width: int
    height: int


@dataclass(frozen=True)
class PixelImage:
    path: Path
    pixels: bytes
    width: int
    height: int


def is_supported_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_supported_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_supported_media(path: Path) -> bool:
    return is_supported_image(path) or is_supported_video(path)


def decode_image(path: Path, max_size: int) -> DecodedImage:
    return pixel_to_decoded(decode_pixels(path, max_size))


def decode_pixels(path: Path, max_size: int) -> PixelImage:
    _ensure_pillow()
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _decode_raw_preview(path, max_size)
    return _decode_pillow(path, max_size)


def decode_thumbnail_pixels(path: Path, max_size: int) -> PixelImage:
    """Thumbnail worker entry point; never compete with the foreground view."""
    lower_background_priority()
    return decode_pixels(path, max_size)


def decode_original_pixels(path: Path) -> PixelImage:
    """Decode the complete source for the 100% viewer.

    This deliberately shares the colour-management and orientation path used
    by previews, but skips JPEG ``draft`` downsampling. RAW files keep using
    their embedded preview; only files without one fall back to raw decoding.
    """
    _ensure_pillow()
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _decode_raw_preview(path, None)
    return _decode_pillow(path, None, use_draft=False, sharpen=False)


def pixel_to_decoded(pixel: PixelImage) -> DecodedImage:
    rgba = QImage(
        pixel.pixels,
        pixel.width,
        pixel.height,
        pixel.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
    # Photographic previews never need alpha. RGB888 keeps the in-RAM
    # thumbnail cache at three bytes per pixel instead of four.
    qimage = rgba.convertToFormat(QImage.Format.Format_RGB888)
    return DecodedImage(path=pixel.path, image=qimage, width=pixel.width, height=pixel.height)


def _decode_raw_preview(path: Path, max_size: int | None) -> PixelImage:
    decoder = _rawpy()

    with decoder.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
        except decoder.LibRawNoThumbnailError:
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
            image = Image.fromarray(rgb)
            return _pillow_to_pixels(path, image, max_size, sharpen=max_size is not None)

    if thumb.format == decoder.ThumbFormat.JPEG:
        if max_size is None:
            return _decode_pillow(path, None, data=thumb.data, use_draft=False, sharpen=False)
        return _decode_qt_jpeg_bytes(path, thumb.data, max_size)
    if thumb.format == decoder.ThumbFormat.BITMAP:
        return _pillow_to_pixels(path, Image.fromarray(thumb.data), max_size, sharpen=max_size is not None)
    raise RuntimeError(f"Unsupported RAW thumbnail format: {thumb.format}")


def _decode_pillow(
    path: Path, max_size: int | None, data: bytes | None = None, *, use_draft: bool = True,
    sharpen: bool = True,
) -> PixelImage:
    source = BytesIO(data) if data is not None else path
    with Image.open(source) as image:
        if use_draft and max_size is not None and image.format == "JPEG":
            image.draft("RGB", (max_size, max_size))
        image = ImageOps.exif_transpose(image)
        return _pillow_to_pixels(path, image, max_size, sharpen=sharpen)


def _decode_qt_jpeg_bytes(path: Path, data: bytes, max_size: int) -> PixelImage:
    byte_array = QByteArray(data)
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"JPG")
    reader.setAutoTransform(True)

    size = reader.size()
    if max_size <= 512:
        intermediate_size = max_size
    else:
        intermediate_size = round(max_size * 1.25)
    if size.isValid() and max(size.width(), size.height()) > intermediate_size:
        if size.width() >= size.height():
            scaled = QSize(intermediate_size, max(1, round(size.height() * intermediate_size / size.width())))
        else:
            scaled = QSize(max(1, round(size.width() * intermediate_size / size.height())), intermediate_size)
        reader.setScaledSize(scaled)

    image = reader.read()
    if image.isNull():
        raise RuntimeError(reader.errorString())
    if max_size <= 512:
        image = _qimage_sharpen(image)
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        width = image.width()
        height = image.height()
        return PixelImage(path=path, pixels=bytes(image.bits()), width=width, height=height)
    return _qimage_to_pillow_pixels(path, image, max_size)


def _pillow_to_pixels(path: Path, image: Image.Image, max_size: int | None, *, sharpen: bool = True) -> PixelImage:
    image = _convert_to_srgb(image)
    if max_size is not None:
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    if sharpen:
        image = _sharpen_preview(image)
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    width, height = image.size
    return PixelImage(path=path, pixels=image.tobytes("raw", "RGBA"), width=width, height=height)


def _convert_to_srgb(image: Image.Image) -> Image.Image:
    _ensure_pillow()
    icc = image.info.get("icc_profile")
    if not icc:
        return image.convert("RGB") if image.mode not in {"RGB", "RGBA"} else image

    try:
        source = ImageCms.ImageCmsProfile(BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        converted = ImageCms.profileToProfile(image, source, target, outputMode="RGB")
        converted.info.pop("icc_profile", None)
        return converted
    except Exception:
        return image.convert("RGB")


def _sharpen_preview(image: Image.Image) -> Image.Image:
    if not SHARPEN_PREVIEWS:
        return image
    return image.filter(ImageFilter.UnsharpMask(radius=0.7, percent=110, threshold=3))


def _qimage_sharpen(image: QImage) -> QImage:
    if not SHARPEN_PREVIEWS:
        return image
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = rgba.width()
    height = rgba.height()
    pil = Image.frombytes("RGBA", (width, height), bytes(rgba.bits()), "raw", "RGBA")
    pil = _sharpen_preview(pil)
    data = pil.tobytes("raw", "RGBA")
    return QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def _qimage_to_pillow_pixels(path: Path, image: QImage, max_size: int) -> PixelImage:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = rgba.width()
    height = rgba.height()
    pil = Image.frombytes("RGBA", (width, height), bytes(rgba.bits()), "raw", "RGBA")
    pil.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    pil = _sharpen_preview(pil)
    out_width, out_height = pil.size
    return PixelImage(path=path, pixels=pil.tobytes("raw", "RGBA"), width=out_width, height=out_height)
