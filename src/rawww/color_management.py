## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Управление цветом для полного просмотра.

Модуль отвечает за один шаг, которого раньше не хватало: перевод уже
приведённого к sRGB кадра в ICC-профиль конкретного монитора. Источник кадра
(встроенный профиль снимка) нормализуется в sRGB ещё при декодировании в
``imaging``; здесь мы достраиваем цепочку до дисплея, иначе на калиброванном
широкогамутном мониторе цвета уходят в пересыщение.

Чтение профиля монитора делается через нативные API каждой ОС и обязано быть
безопасным: любая неудача превращается в «профиль неизвестен» и приложение
показывает кадр как есть (как будто дисплей sRGB), а не падает.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageCms
from PySide6.QtGui import QImage, QScreen

#: Идентификаторы rendering intent для настроек и QSettings. Значения совпадают с
#: числовыми кодами LittleCMS, чтобы их можно было хранить как есть.
INTENT_PERCEPTUAL = 0
INTENT_RELATIVE = 1
INTENT_SATURATION = 2
INTENT_ABSOLUTE = 3

_INTENTS = {
    INTENT_PERCEPTUAL: ImageCms.Intent.PERCEPTUAL,
    INTENT_RELATIVE: ImageCms.Intent.RELATIVE_COLORIMETRIC,
    INTENT_SATURATION: ImageCms.Intent.SATURATION,
    INTENT_ABSOLUTE: ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}


@dataclass(frozen=True)
class ColorManagementConfig:
    """Параметры управления цветом, собранные из настроек приложения.

    ``manual_profile_path`` перекрывает автоопределение профиля монитора — это
    страховка на случай, когда ОС профиль не отдаёт (частый случай на Linux).
    """

    enabled: bool = True
    intent: int = INTENT_RELATIVE
    black_point_compensation: bool = True
    manual_profile_path: str = ""


@lru_cache(maxsize=1)
def _srgb_profile_bytes() -> bytes:
    """Сериализует sRGB-профиль LittleCMS в ICC-байты для сравнения и transform."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _read_file_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        return data or None
    except OSError:
        return None


def _windows_display_profile(screen: QScreen | None) -> bytes | None:
    """Достаёт ICC-профиль монитора через GDI (``GetICMProfileW``)."""
    import ctypes
    from ctypes import wintypes

    name = screen.name() if screen is not None else ""
    gdi32 = ctypes.WinDLL("gdi32")
    # Имя экрана в Qt на Windows совпадает с device name вида "\\.\DISPLAY1".
    hdc = gdi32.CreateDCW("DISPLAY", name or None, None, None)
    if not hdc:
        hdc = gdi32.CreateDCW(None, None, None, None)
    if not hdc:
        return None
    try:
        # GetICMProfileW заполняет путь к файлу профиля; берём буфер с запасом,
        # чтобы не зависеть от предварительного запроса длины.
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not gdi32.GetICMProfileW(hdc, ctypes.byref(size), buffer):
            return None
        return _read_file_bytes(buffer.value)
    finally:
        gdi32.DeleteDC(hdc)


def _macos_display_profile(screen: QScreen | None) -> bytes | None:
    """Берёт ICC-данные экрана через ``NSScreen.colorSpace``."""
    try:
        from AppKit import NSScreen
    except ImportError:
        return None

    screens = list(NSScreen.screens() or [])
    if not screens:
        return None
    target = None
    if screen is not None:
        geometry = screen.geometry()
        for candidate in screens:
            frame = candidate.frame()
            # Совпадение по верхнему левому углу и ширине надёжнее сравнения имён.
            if int(frame.origin.x) == geometry.left() and int(frame.size.width) == geometry.width():
                target = candidate
                break
    if target is None:
        target = NSScreen.mainScreen() or screens[0]
    color_space = target.colorSpace()
    if color_space is None:
        return None
    data = color_space.ICCProfileData()
    if data is None:
        return None
    return bytes(data)


def _linux_display_profile(screen: QScreen | None) -> bytes | None:
    """Читает атом ``_ICC_PROFILE`` с корневого окна X11, если он выставлен."""
    try:
        import ctypes
    except ImportError:
        return None
    try:
        xlib = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None

    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XInternAtom.restype = ctypes.c_ulong
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        atom = xlib.XInternAtom(ctypes.c_void_p(display), b"_ICC_PROFILE", True)
        if not atom:
            return None
        root = xlib.XDefaultRootWindow(ctypes.c_void_p(display))
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = xlib.XGetWindowProperty(
            ctypes.c_void_p(display),
            ctypes.c_ulong(root),
            ctypes.c_ulong(atom),
            0,
            0x7FFFFFFF,
            False,
            ctypes.c_ulong(4),  # AnyPropertyType
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        if status != 0 or not data or nitems.value == 0:
            return None
        try:
            return bytes(bytearray(data[: nitems.value]))
        finally:
            xlib.XFree(data)
    finally:
        xlib.XCloseDisplay(ctypes.c_void_p(display))


def display_profile_bytes(screen: QScreen | None, config: ColorManagementConfig) -> bytes | None:
    """Возвращает ICC-профиль монитора: ручной файл или системный, иначе ``None``.

    Функция никогда не бросает исключений — при любой ошибке возвращается
    ``None``, и вызывающий код показывает кадр без коррекции под дисплей.
    """
    if config.manual_profile_path:
        manual = _read_file_bytes(config.manual_profile_path)
        if manual is not None:
            return manual
    try:
        if sys.platform == "win32":
            return _windows_display_profile(screen)
        if sys.platform == "darwin":
            return _macos_display_profile(screen)
        return _linux_display_profile(screen)
    except Exception:
        return None


_transform_cache: dict[tuple, object | None] = {}


def srgb_to_display_transform(display_icc: bytes | None, config: ColorManagementConfig):
    """Строит (с кэшем) transform sRGB → профиль дисплея или ``None``, если он не нужен.

    ``None`` означает «коррекция не требуется»: CMS выключен, профиль неизвестен
    или он практически совпадает с sRGB. В этом случае кадр рисуется как есть.
    """
    if not config.enabled or not display_icc:
        return None
    srgb = _srgb_profile_bytes()
    if display_icc == srgb:
        return None
    key = (display_icc, config.intent, config.black_point_compensation)
    if key in _transform_cache:
        return _transform_cache[key]
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(srgb))
        target = ImageCms.ImageCmsProfile(BytesIO(display_icc))
        # NOCACHE делает один transform потокобезопасным для cmsDoTransform, что
        # позволяет параллелить коррекцию полосами без отдельного объекта на поток.
        flags = ImageCms.Flags.NOCACHE
        if config.black_point_compensation:
            flags |= ImageCms.Flags.BLACKPOINTCOMPENSATION
        transform = ImageCms.buildTransform(
            source,
            target,
            "RGB",
            "RGB",
            renderingIntent=_INTENTS.get(config.intent, ImageCms.Intent.RELATIVE_COLORIMETRIC),
            flags=flags,
        )
    except Exception:
        transform = None
    _transform_cache[key] = transform
    return transform


#: Ниже этого числа строк параллелить незачем — накладные расходы на пул съедят выигрыш.
_MIN_ROWS_PER_STRIP = 256

_pool: ThreadPoolExecutor | None = None


def _thread_pool() -> ThreadPoolExecutor:
    """Общий пул для полос: LittleCMS отпускает GIL, поэтому потоки дают реальный прирост."""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(
            max_workers=max(1, os.cpu_count() or 1), thread_name_prefix="cms"
        )
    return _pool


def _transform_strip(buffer: bytes, width: int, height: int, stride: int, transform) -> bytes:
    pil = Image.frombuffer("RGB", (width, height), buffer, "raw", "RGB", stride, 1)
    return ImageCms.applyTransform(pil, transform).tobytes("raw", "RGB")


def apply_transform_to_qimage(image: QImage, transform) -> QImage:
    """Применяет готовый CMS-transform к ``QImage`` и возвращает новый ``QImage``.

    Изображение приводится к ``RGB888`` и корректируется горизонтальными полосами
    в общем пуле потоков (transform собран с ``NOCACHE`` и потокобезопасен), чтобы
    коррекция масштабировалась по ядрам. Мелкие кадры обрабатываются одним потоком.
    Исходный ``devicePixelRatio`` сохраняется.
    """
    if transform is None or image.isNull():
        return image
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    width = rgb.width()
    height = rgb.height()
    stride = rgb.bytesPerLine()
    buffer = bytes(rgb.constBits())

    workers = max(1, min(os.cpu_count() or 1, height // _MIN_ROWS_PER_STRIP))
    if workers <= 1:
        data = _transform_strip(buffer, width, height, stride, transform)
    else:
        bounds = [round(index * height / workers) for index in range(workers + 1)]
        parts = _thread_pool().map(
            lambda span: _transform_strip(
                buffer[span[0] * stride : span[1] * stride], width, span[1] - span[0], stride, transform
            ),
            [(bounds[index], bounds[index + 1]) for index in range(workers)],
        )
        data = b"".join(parts)

    result = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
    result.setDevicePixelRatio(image.devicePixelRatio())
    return result
