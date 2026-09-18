## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""LaMa и файловые операции удаления объектов; модуль не зависит от Qt."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from io import BytesIO
import math
import os
from pathlib import Path
import tempfile
from collections.abc import Callable
from urllib.request import urlopen

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageOps

from .runtime_paths import PORTABLE, application_cache_path, data_path


INPAINT_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"})
MODEL_URL = "https://shotsync.ru/media/ctrlka/models/lama_fp32.onnx"
MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
# URL заполняется после публикации подготовленного 1024-графа на сервере моделей.
HD_MODEL_URL = "https://shotsync.ru/media/ctrlka/models/lama_fp32_1024.onnx"
HD_MODEL_SHA256 = "49a400fa4e2e8198cc2011753820b9a5dd8bfdee4d26b19e583e37ef2690d1c4"
HORIZON_MODEL_URL = "https://shotsync.ru/media/ctrlka/models/deep-oad.onnx"
HORIZON_MODEL_SHA256 = "fed21a8aeacc49e362fb66bca1d67d333a4965087d1fc706c961221e334947b9"


def inpaint_model_path(quality: str = "sd") -> Path:
    """Выбирает место модели, в которое текущая сборка вправе записывать.

    Portable-версия хранит её среди поставляемых моделей. Обычная сборка не
    пишет в Program Files и использует пользовательский кэш приложения.
    """
    name = "lama_fp32_1024.onnx" if quality == "hd" else "lama_fp32.onnx"
    if PORTABLE:
        return data_path("models") / "inpaint" / name
    return application_cache_path() / "models" / "inpaint" / name


def horizon_model_path() -> Path:
    """Возвращает путь Deep-OAD, не смешивая его с моделью дорисовки."""
    if PORTABLE:
        return data_path("models") / "orientation" / "deep_oad.onnx"
    return application_cache_path() / "models" / "orientation" / "deep_oad.onnx"


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def ensure_inpaint_model(
    progress: Callable[[int, int | None], None] | None = None, quality: str = "sd",
) -> Path:
    """Скачивает LaMa атомарно и возвращает только модель с ожидаемым хешем.

    Промежуточный файл никогда не попадает в ONNX Runtime: закрытие окна или
    обрыв сети оставляют прежнюю проверенную модель нетронутой.
    """
    if quality not in {"sd", "hd"}:
        raise ValueError("unknown_inpaint_quality")
    url = HD_MODEL_URL if quality == "hd" else MODEL_URL
    expected_digest = HD_MODEL_SHA256 if quality == "hd" else MODEL_SHA256
    model = inpaint_model_path(quality)
    if model.is_file() and _file_digest(model) == expected_digest:
        return model
    if not url:
        raise RuntimeError("hd_model_url_missing")
    model.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".lama-", suffix=".download", dir=model.parent)
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as output, urlopen(url, timeout=60) as source:
            length = source.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            if progress:
                progress(0, total)
            downloaded = 0
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_digest:
            raise RuntimeError("model_checksum_mismatch")
        os.replace(temporary, model)
        return model
    finally:
        temporary.unlink(missing_ok=True)


def ensure_horizon_model(progress: Callable[[int, int | None], None] | None = None) -> Path:
    """Скачивает Deep-OAD атомарно; непроверенный файл не идёт в инференс."""
    model = horizon_model_path()
    if model.is_file() and _file_digest(model) == HORIZON_MODEL_SHA256:
        return model
    model.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".deep-oad-", suffix=".download", dir=model.parent)
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as output, urlopen(HORIZON_MODEL_URL, timeout=60) as source:
            length = source.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            if progress:
                progress(0, total)
            downloaded = 0
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != HORIZON_MODEL_SHA256:
            raise RuntimeError("horizon_model_checksum_mismatch")
        os.replace(temporary, model)
        return model
    finally:
        temporary.unlink(missing_ok=True)


class ImageConflictError(RuntimeError):
    """Исходник изменился вне редактора; заменять чужую версию нельзя."""


def fingerprint(path: Path) -> tuple[int, int, int, int]:
    """Отличает и запись на месте, и атомарную замену файла."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


@dataclass
class EditableImage:
    """Владеет ориентированными пикселями в исходном ICC и метаданными файла.

    Опубликованный image не меняется на месте: сохранение может одновременно
    писать предыдущую ревизию, пока обработчик уже готовит следующую.
    """

    path: Path
    image: Image.Image
    signature: tuple[int, int, int, int]
    format: str
    exif: bytes
    icc: bytes | None
    xmp: bytes | None
    dpi: tuple | None
    draft: bool = False
    revision: int = 0
    saved_revision: int = 0
    pending: int = 0


def load_image(path: Path, draft_side: int | None = None) -> EditableImage:
    """Читает кадр либо быстрый draft; метаданные всегда берутся из оригинала.

    JPEG умеет уменьшать DCT-коэффициенты до распаковки. У PNG и TIFF такого
    пути нет, но единый контракт draft всё равно не даёт держать их полный кадр
    в кэше, когда пользователь только листает список.
    """
    if path.suffix.lower() not in INPAINT_EXTENSIONS:
        raise ValueError("unsupported_image")
    signature = fingerprint(path)
    with Image.open(path) as source:
        bits = getattr(source, "tag_v2", {}).get(258, (8,))
        if isinstance(bits, int):
            bits = (bits,)
        if (getattr(source, "n_frames", 1) != 1 or max(bits) > 8
                or source.mode not in {"RGB", "RGBA", "L", "LA", "P"}):
            raise ValueError("unsupported_image")
        fmt = source.format
        info = source.info.copy()
        if draft_side and max(source.size) > draft_side:
            source.draft(source.mode, (draft_side, draft_side))
        # TIFF-метаданные живут в tag_v2 и теряются у копии после transpose.
        # Забираем их до декодирования, пока исходный файловый объект открыт.
        original_exif = source.getexif().tobytes()
        oriented = ImageOps.exif_transpose(source)
        exif = Image.Exif()
        exif.load(original_exif)
        # Пиксели уже повёрнуты; старые геометрические теги здесь опаснее их отсутствия.
        for key in (274, 256, 257):
            if key in exif:
                del exif[key]
        image = oriented.convert("RGBA" if "A" in oriented.mode or "transparency" in info else "RGB")
        # Серый ICC нельзя прикреплять к RGB: такие файлы сохраняем в исходном режиме.
        if source.mode in {"L", "LA"}:
            image = oriented.copy()
        is_draft = bool(draft_side and max(image.size) > draft_side)
        if is_draft:
            image.thumbnail((draft_side, draft_side), Image.Resampling.LANCZOS)
        frame = EditableImage(path, image, signature, fmt, exif.tobytes() if exif else b"",
                              info.get("icc_profile"), oriented.info.get("xmp", oriented.info.get("XML:com.adobe.xmp")),
                              info.get("dpi"), draft=is_draft)
    if fingerprint(path) != signature:
        raise ImageConflictError(str(path))
    return frame


def scale_strokes(strokes: list[dict], scale_x: float, scale_y: float) -> list[dict]:
    """Переводит нарисованную на draft маску в координаты полного оригинала."""
    return [
        {
            "diameter": stroke["diameter"] * (scale_x + scale_y) / 2,
            "points": [[x * scale_x, y * scale_y] for x, y in stroke["points"]],
        }
        for stroke in strokes
    ]


def to_srgb(image: Image.Image, icc: bytes | None) -> Image.Image:
    """Даёт модели и предпросмотру sRGB, не меняя профиль сохраняемого кадра."""
    if not icc:
        return image.convert("RGB")
    profile = ImageCms.ImageCmsProfile(BytesIO(icc))
    colour = image.convert("L" if image.mode in {"L", "LA"} else "RGB")
    return ImageCms.profileToProfile(colour, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")


def from_srgb(image: Image.Image, frame: EditableImage) -> Image.Image:
    """Возвращает только дорисованный фрагмент в цветовое пространство исходника."""
    mode = "L" if frame.image.mode in {"L", "LA"} else "RGB"
    if not frame.icc:
        return image.convert(mode)
    return ImageCms.profileToProfile(image, ImageCms.createProfile("sRGB"),
                                     ImageCms.ImageCmsProfile(BytesIO(frame.icc)), outputMode=mode)


def save_image(frame: EditableImage, image: Image.Image, expected: tuple) -> tuple:
    """Атомарно заменяет файл, перенося EXIF, ICC, XMP и DPI.

    Проверка перед публикацией защищает от внешнего редактора. Координатор
    дополнительно сериализует свои записи: проверка версии не заменяет очередь.
    """
    path = frame.path
    if fingerprint(path) != expected:
        raise ImageConflictError(str(path))
    options = {"format": frame.format}
    if frame.exif:
        options["exif"] = frame.exif
    if frame.icc:
        options["icc_profile"] = frame.icc
    if frame.dpi:
        options["dpi"] = frame.dpi
    if frame.xmp and frame.format in {"JPEG", "WEBP"}:
        options["xmp"] = frame.xmp
    if frame.format == "PNG" and frame.xmp:
        from PIL.PngImagePlugin import PngInfo
        pnginfo = PngInfo()
        text = frame.xmp.decode("utf-8") if isinstance(frame.xmp, bytes) else frame.xmp
        pnginfo.add_itxt("XML:com.adobe.xmp", text)
        options["pnginfo"] = pnginfo
    if frame.format == "JPEG":
        options.update(quality=98, subsampling=0)
    elif frame.format == "WEBP":
        options["lossless"] = True
    elif frame.format == "TIFF":
        options["compression"] = "tiff_deflate"
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}-inpaint-", suffix=path.suffix, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w+b") as output:
            image.save(output, **options)
            output.flush()
            os.fsync(output.fileno())
        if fingerprint(path) != expected:
            raise ImageConflictError(str(path))
        os.replace(temporary, path)
        return fingerprint(path)
    finally:
        temporary.unlink(missing_ok=True)


def make_mask(size: tuple[int, int], strokes: list[dict]) -> Image.Image:
    """Растеризует штрихи в координатах оригинала, включая одиночный клик."""
    mask = Image.new("L", size)
    draw = ImageDraw.Draw(mask)
    for stroke in strokes:
        points = [(float(x), float(y)) for x, y in stroke["points"]]
        radius = max(.5, min(float(stroke["diameter"]), max(size)) / 2)
        if len(points) > 1:
            draw.line(points, fill=255, width=max(1, round(radius * 2)), joint="curve")
        for x, y in points:
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=255)
    return mask


class LamaInpainter:
    """Одна ONNX-сессия процесса; получает маску, возвращает новые пиксели.

    Вся маска обрабатывается одним окном с контекстом: независимые тайлы
    теряют общую перспективу. Большой объект потребует уменьшения этого окна.
    """

    def __init__(self, model: Path | None = None) -> None:
        import onnxruntime as ort

        model = model or inpaint_model_path()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 2) - 1))
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        image_input = next((item for item in self.session.get_inputs() if item.name == "image"), None)
        mask_input = next((item for item in self.session.get_inputs() if item.name == "mask"), None)
        image_shape = image_input.shape if image_input is not None else ()
        mask_shape = mask_input.shape if mask_input is not None else ()
        if (len(image_shape) != 4 or len(mask_shape) != 4 or not isinstance(image_shape[2], int)
                or image_shape[2] != image_shape[3] or mask_shape[2:] != image_shape[2:]):
            raise RuntimeError("unsupported_inpaint_model_shape")
        self.side = image_shape[2]

    def apply(self, frame: EditableImage, mask: Image.Image) -> Image.Image:
        """Дорисовывает расширенную маску и сохраняет остальные пиксели точно."""
        bounds = mask.getbbox()
        if bounds is None:
            return frame.image
        w, h = frame.image.size
        left, top, right, bottom = bounds
        # HD должен видеть больше исходного кадра, а не растянутый SD-кроп:
        # иначе детали крупнее обучающего масштаба, а дополнительного контекста нет.
        crop_side = max(self.side, round(max(right-left, bottom-top) * 2))
        crop_side = min(crop_side, max(w, h))
        x = max(0, min(w-crop_side, (left+right-crop_side)//2))
        y = max(0, min(h-crop_side, (top+bottom-crop_side)//2))
        box = (x, y, min(w, x+crop_side), min(h, y+crop_side))
        native = frame.image.crop(box)
        rgb = to_srgb(native, frame.icc)
        local_mask = mask.crop(box)
        # Запас маски убирает цветной ореол объекта; мягкий край остаётся снаружи выделения.
        local_mask = local_mask.filter(ImageFilter.MaxFilter(7))
        alpha = local_mask.filter(ImageFilter.GaussianBlur(1))
        input_side = self.side
        scale = input_side / max(rgb.size)
        size = (max(1, round(rgb.width*scale)), max(1, round(rgb.height*scale)))
        small = rgb.resize(size, Image.Resampling.LANCZOS)
        small_mask = local_mask.resize(size, Image.Resampling.NEAREST)
        pixels = np.asarray(small, dtype=np.float32) / 255
        pixels = np.pad(pixels, ((0, input_side-size[1]), (0, input_side-size[0]), (0,0)), mode="edge")
        holes = np.pad(np.asarray(small_mask) > 0, ((0, input_side-size[1]), (0, input_side-size[0])))
        output = self.session.run(None, {"image": pixels.transpose(2,0,1)[None],
                                         "mask": holes.astype(np.float32)[None,None]})[0]
        generated = Image.fromarray(np.clip(output[0].transpose(1,2,0), 0, 255).astype(np.uint8))
        generated = generated.crop((0,0,*size)).resize(rgb.size, Image.Resampling.LANCZOS)
        restored = from_srgb(generated, frame)
        if "A" in frame.image.mode:
            restored.putalpha(native.getchannel("A"))
        result = frame.image.copy()
        result.paste(Image.composite(restored, native, alpha), box[:2])
        return result


class DeepOad:
    """Владеет ONNX-сессией оценки наклона и не знает о виджетах Qt.

    Deep-OAD обучен на синтетически повёрнутых снимках. Он не отличает
    авторский наклон от ошибки камеры, поэтому вызывающий код ограничивает
    автоматическую коррекцию малым углом.
    """

    def __init__(self, model: Path | None = None) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 2) - 1))
        self.session = ort.InferenceSession(
            str(model or horizon_model_path()), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def predict_angle(self, frame: EditableImage) -> float:
        """Возвращает угол наклона снимка в диапазоне от -180 до 180 градусов."""
        source = to_srgb(frame.image, frame.icc).resize((224, 224), Image.Resampling.BICUBIC)
        pixels = np.asarray(source, dtype=np.float32) / 255.0
        # ViTImageProcessor для google/vit-base-patch16-224 нормализует RGB
        # относительно 0.5; размерности NCHW зафиксированы опубликованным ONNX.
        pixels = ((pixels - 0.5) / 0.5).transpose(2, 0, 1)[None]
        predicted = float(np.asarray(self.session.run(None, {self.input_name: pixels})[0]).reshape(-1)[0])
        return (predicted + 180.0) % 360.0 - 180.0


def _largest_rotated_rectangle(width: int, height: int, degrees: float) -> tuple[int, int]:
    """Находит центральный прямоугольник без пустых углов после малого поворота."""
    angle = abs(math.radians(degrees)) % math.pi
    if angle > math.pi / 2:
        angle = math.pi - angle
    if angle < 1e-7:
        return width, height
    sin_a, cos_a = abs(math.sin(angle)), abs(math.cos(angle))
    if width <= 2 * sin_a * cos_a * height or height <= 2 * sin_a * cos_a * width:
        x = 0.5 * min(width, height)
        if width < height:
            result_w, result_h = x / sin_a, x / cos_a
        else:
            result_w, result_h = x / cos_a, x / sin_a
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        result_w = (width * cos_a - height * sin_a) / cos_2a
        result_h = (height * cos_a - width * sin_a) / cos_2a
    return max(1, round(result_w)), max(1, round(result_h))


def straighten_image(frame: EditableImage, angle: float) -> Image.Image:
    """Компенсирует оценённый наклон и кадрирует пустые углы без чёрной рамки."""
    correction = -angle
    source = frame.image
    rotated = source.rotate(correction, resample=Image.Resampling.BICUBIC, expand=True)
    safe_w, safe_h = _largest_rotated_rectangle(source.width, source.height, correction)
    # Максимальный прямоугольник после поворота может иметь другие пропорции.
    # Вписываем в него кадр с форматом оригинала, чтобы горизонт не менял его.
    scale = min(safe_w / source.width, safe_h / source.height)
    target_w = max(1, round(source.width * scale))
    target_h = max(1, round(source.height * scale))
    left = max(0, (rotated.width - target_w) // 2)
    top = max(0, (rotated.height - target_h) // 2)
    return rotated.crop((left, top, left + target_w, top + target_h))


def crop_image(frame: EditableImage, box: tuple[float, float, float, float]) -> Image.Image:
    """Обрезает полный кадр по проверенной рамке интерфейса без изменения профиля."""
    left, top, right, bottom = box
    left = max(0, min(frame.image.width - 1, round(left)))
    top = max(0, min(frame.image.height - 1, round(top)))
    right = max(left + 1, min(frame.image.width, round(right)))
    bottom = max(top + 1, min(frame.image.height, round(bottom)))
    return frame.image.crop((left, top, right, bottom))


def crop_with_inpaint(frame: EditableImage, box: tuple[float, float, float, float], inpainter: LamaInpainter) -> Image.Image:
    """Расширяет кадр LaMa только за его границами и возвращает выбранный прямоугольник."""
    left, top, right, bottom = (round(value) for value in box)
    left, top = min(left, right - 1), min(top, bottom - 1)
    right, bottom = max(right, left + 1), max(bottom, top + 1)
    if left >= 0 and top >= 0 and right <= frame.image.width and bottom <= frame.image.height:
        return crop_image(frame, (left, top, right, bottom))
    canvas_left, canvas_top = min(0, left), min(0, top)
    canvas_right, canvas_bottom = max(frame.image.width, right), max(frame.image.height, bottom)
    size = (canvas_right - canvas_left, canvas_bottom - canvas_top)
    fill = (0, 0, 0, 255) if "A" in frame.image.mode else (0, 0, 0)
    canvas = Image.new(frame.image.mode, size, fill)
    source_at = (-canvas_left, -canvas_top)
    canvas.paste(frame.image, source_at)
    mask = Image.new("L", size, 255)
    mask.paste(0, (*source_at, source_at[0] + frame.image.width, source_at[1] + frame.image.height))
    generated = inpainter.apply(replace(frame, image=canvas), mask)
    return generated.crop((left - canvas_left, top - canvas_top, right - canvas_left, bottom - canvas_top))
