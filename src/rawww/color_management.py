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

    enabled: bool = False
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


#: Константы Windows Color System / DisplayConfig для чтения профиля дисплея.
_WIN_QDC_ONLY_ACTIVE_PATHS = 2
_WIN_INFO_GET_SOURCE_NAME = 1
_WIN_INFO_GET_ADVANCED_COLOR = 9
_WIN_CPT_ICC = 0
_WIN_CPST_NONE = 4
_WIN_CPST_STANDARD_DISPLAY = 7  # SDR-профиль дисплея (для advanced color)
_WIN_SCOPE_SYSTEM_WIDE = 0
_WIN_SCOPE_CURRENT_USER = 1


def _windows_color_directory() -> str:
    """Каталог Windows с .icm-профилями (WCS-API отдаёт лишь имя файла)."""
    import ctypes
    from ctypes import wintypes

    try:
        mscms = ctypes.WinDLL("mscms")
        size = wintypes.DWORD(260 * 2)
        buffer = ctypes.create_unicode_buffer(260)
        if mscms.GetColorDirectoryW(None, buffer, ctypes.byref(size)):
            return buffer.value
    except Exception:
        pass
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(windir, "System32", "spool", "drivers", "color")


def _windows_profile_by_name(name: str) -> bytes | None:
    """Достраивает путь до профиля (если API вернул только имя) и читает байты."""
    if not name:
        return None
    path = name if os.path.isabs(name) else os.path.join(_windows_color_directory(), name)
    return _read_file_bytes(path)


def _windows_display_target(screen: QScreen | None):
    """Ищет монитор в DisplayConfig и возвращает ``(adapter_id, source_id, hdr)``.

    ``adapter_id`` — структура ``LUID`` (нужна современному WCS-API), ``source_id``
    — номер источника, ``hdr`` — включён ли расширенный цвет/HDR. При любой
    неудаче возвращает ``(None, None, False)``: тогда работают только легаси-пути.
    """
    import ctypes
    from ctypes import wintypes

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _HEADER(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD), ("size", wintypes.DWORD),
            ("adapterId", _LUID), ("id", wintypes.DWORD),
        ]

    class _SOURCE(ctypes.Structure):
        _fields_ = [("adapterId", _LUID), ("id", wintypes.DWORD), ("modeInfoIdx", wintypes.DWORD), ("statusFlags", wintypes.DWORD)]

    class _TARGET(ctypes.Structure):
        _fields_ = [
            ("adapterId", _LUID), ("id", wintypes.DWORD), ("modeInfoIdx", wintypes.DWORD),
            ("outputTechnology", wintypes.DWORD), ("rotation", wintypes.DWORD), ("scaling", wintypes.DWORD),
            ("rr_num", wintypes.DWORD), ("rr_den", wintypes.DWORD),
            ("scanLineOrdering", wintypes.DWORD), ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.DWORD),
        ]

    class _PATH(ctypes.Structure):
        _fields_ = [("sourceInfo", _SOURCE), ("targetInfo", _TARGET), ("flags", wintypes.DWORD)]

    class _MODE(ctypes.Structure):
        _fields_ = [("_pad", ctypes.c_byte * 64)]

    class _SOURCE_NAME(ctypes.Structure):
        _fields_ = [("header", _HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]

    class _ADV_COLOR(ctypes.Structure):
        _fields_ = [("header", _HEADER), ("value", wintypes.DWORD), ("colorEncoding", wintypes.DWORD), ("bits", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32")
    n_path = wintypes.UINT(0)
    n_mode = wintypes.UINT(0)
    if user32.GetDisplayConfigBufferSizes(_WIN_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_path), ctypes.byref(n_mode)) != 0:
        return None, None, False
    paths = (_PATH * n_path.value)()
    modes = (_MODE * n_mode.value)()
    if user32.QueryDisplayConfig(_WIN_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_path), paths, ctypes.byref(n_mode), modes, None) != 0:
        return None, None, False

    wanted = screen.name() if screen is not None else ""
    for index in range(n_path.value):
        path = paths[index]
        adapter = path.sourceInfo.adapterId
        source_id = path.sourceInfo.id

        source_name = _SOURCE_NAME()
        source_name.header.type = _WIN_INFO_GET_SOURCE_NAME
        source_name.header.size = ctypes.sizeof(source_name)
        source_name.header.adapterId = adapter
        source_name.header.id = source_id
        gdi_name = source_name.viewGdiDeviceName if user32.DisplayConfigGetDeviceInfo(ctypes.byref(source_name)) == 0 else ""

        # Берём совпавший по имени экран, а без совпадения — первый активный путь.
        if wanted and gdi_name and gdi_name != wanted:
            continue

        adv = _ADV_COLOR()
        adv.header.type = _WIN_INFO_GET_ADVANCED_COLOR
        adv.header.size = ctypes.sizeof(adv)
        adv.header.adapterId = adapter
        adv.header.id = path.targetInfo.id
        hdr = bool(adv.value & 0x2) if user32.DisplayConfigGetDeviceInfo(ctypes.byref(adv)) == 0 else False
        return adapter, source_id, hdr
    return None, None, False


def _windows_color_profile_default(adapter, source_id: int, scope: int, subtype: int) -> bytes | None:
    """Современный ``ColorProfileGetDisplayDefault`` (Win10 21H2+), иначе ``None``."""
    import ctypes
    from ctypes import wintypes

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    try:
        mscms = ctypes.WinDLL("mscms")
        mscms.ColorProfileGetDisplayDefault.argtypes = [
            wintypes.DWORD, _LUID, wintypes.UINT, wintypes.INT, wintypes.INT,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        mscms.ColorProfileGetDisplayDefault.restype = wintypes.BOOL
    except (OSError, AttributeError):
        return None
    out = wintypes.LPWSTR()
    try:
        ok = mscms.ColorProfileGetDisplayDefault(scope, adapter, source_id, _WIN_CPT_ICC, subtype, ctypes.byref(out))
    except Exception:
        return None
    if not ok or not out.value:
        if out:
            _windows_local_free(out)
        return None
    name = out.value
    _windows_local_free(out)
    return _windows_profile_by_name(name)


def _windows_local_free(pointer) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.LocalFree.restype = wintypes.LPVOID
    kernel32.LocalFree(ctypes.cast(pointer, wintypes.LPVOID))


def _windows_wcs_default(name: str, scope: int) -> bytes | None:
    """WCS ``WcsGetDefaultColorProfile`` для указанного scope, иначе ``None``."""
    import ctypes
    from ctypes import wintypes

    try:
        mscms = ctypes.WinDLL("mscms")
        mscms.WcsGetDefaultColorProfileSize.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.INT, wintypes.INT,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        mscms.WcsGetDefaultColorProfileSize.restype = wintypes.BOOL
        mscms.WcsGetDefaultColorProfile.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.INT, wintypes.INT,
            wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR,
        ]
        mscms.WcsGetDefaultColorProfile.restype = wintypes.BOOL
    except (OSError, AttributeError):
        return None
    need = wintypes.DWORD(0)
    if not mscms.WcsGetDefaultColorProfileSize(scope, name, _WIN_CPT_ICC, _WIN_CPST_NONE, 0, ctypes.byref(need)) or need.value == 0:
        return None
    buffer = ctypes.create_unicode_buffer(need.value // 2 + 1)
    if not mscms.WcsGetDefaultColorProfile(scope, name, _WIN_CPT_ICC, _WIN_CPST_NONE, 0, need, buffer):
        return None
    return _windows_profile_by_name(buffer.value)


def _windows_geticmprofile(name: str) -> bytes | None:
    """Легаси-путь GDI ``GetICMProfileW`` — последний фолбэк на старых системах."""
    import ctypes
    from ctypes import wintypes

    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.CreateDCW.restype = wintypes.HDC
    gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetICMProfileW.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR]
    gdi32.GetICMProfileW.restype = wintypes.BOOL
    # Имя экрана в Qt на Windows совпадает с device name вида "\\.\DISPLAY1".
    hdc = gdi32.CreateDCW("DISPLAY", name or None, None, None)
    if not hdc:
        hdc = gdi32.CreateDCW(None, None, None, None)
    if not hdc:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not gdi32.GetICMProfileW(hdc, ctypes.byref(size), buffer):
            return None
        return _read_file_bytes(buffer.value)
    finally:
        gdi32.DeleteDC(hdc)


def _windows_display_profile(screen: QScreen | None) -> bytes | None:
    """Определяет ICC-профиль монитора «по-взрослому»: WCS → GDI.

    Порядок: современный ``ColorProfileGetDisplayDefault`` (учитывает per-user и
    новые дисплеи) → ``WcsGetDefaultColorProfile`` → легаси ``GetICMProfileW``.
    В режиме расширенного цвета/HDR все они отдают sRGB — но в этом случае цветом
    управляет сама Windows, и коррекцию делать не нужно (см. ``os_manages_display_color``).
    """
    name = screen.name() if screen is not None else ""
    adapter, source_id, _hdr = _windows_display_target(screen)
    if adapter is not None:
        for scope in (_WIN_SCOPE_CURRENT_USER, _WIN_SCOPE_SYSTEM_WIDE):
            for subtype in (_WIN_CPST_STANDARD_DISPLAY, _WIN_CPST_NONE):
                icc = _windows_color_profile_default(adapter, source_id, scope, subtype)
                if icc is not None:
                    return icc
    for scope in (_WIN_SCOPE_CURRENT_USER, _WIN_SCOPE_SYSTEM_WIDE):
        icc = _windows_wcs_default(name, scope)
        if icc is not None:
            return icc
    return _windows_geticmprofile(name)


def _windows_advanced_color_active(screen: QScreen | None) -> bool:
    """Включён ли на мониторе расширенный цвет/HDR (тогда цветом управляет ОС)."""
    try:
        return bool(_windows_display_target(screen)[2])
    except Exception:
        return False


def os_manages_display_color(screen: QScreen | None) -> bool:
    """``True``, если ОС сама доводит цвет до монитора и наш CMS применять нельзя.

    Актуально для Windows в режиме расширенного цвета/HDR: там композитор DWM
    применяет профиль монитора сам, и повторная коррекция в приложении дала бы
    двойное управление цветом. На остальных ОС всегда ``False``.
    """
    if sys.platform != "win32":
        return False
    return _windows_advanced_color_active(screen)


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
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XInternAtom.restype = ctypes.c_ulong
    xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    # long_offset и long_length — 64-битные long: без явных argtypes ctypes
    # передаёт их как 32-битный int, и сервер возвращает 0 элементов.
    xlib.XGetWindowProperty.restype = ctypes.c_int
    xlib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    xlib.XFree.argtypes = [ctypes.c_void_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        atom = xlib.XInternAtom(display, b"_ICC_PROFILE", True)
        if not atom:
            return None
        root = xlib.XDefaultRootWindow(display)
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = xlib.XGetWindowProperty(
            display,
            root,
            atom,
            0,
            0x1FFFFFFF,
            False,
            0,  # AnyPropertyType
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


def describe_profile(icc: bytes | None) -> str:
    """Возвращает человекочитаемое название ICC-профиля или пустую строку.

    Используется настройками, чтобы показать пользователю, какой профиль монитора
    сейчас применяется. При любой ошибке разбора возвращается пустая строка.
    """
    if not icc:
        return ""
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(icc))
        return (ImageCms.getProfileDescription(profile) or "").strip()
    except Exception:
        return ""


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
