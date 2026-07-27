## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Получение и нормализация метаданных через pyexiv2.

Библиотека работает в процессе Python без отдельного вспомогательного процесса.
Здесь ключи Exiv2 приводятся к прежнему внутреннему контракту, чтобы кэш и
остальные потребители метаданных не зависели от конкретной библиотеки.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import Future, ProcessPoolExecutor

import pyexiv2

from .cache import FolderCache
from .error_log import log_exception
from .task_lifecycle import retire_executor

from .worker_priority import lower_background_priority


METADATA_BATCH_SIZE = 32
def read_metadata(path: str) -> dict:
    """Читает EXIF и XMP одного файла, всегда освобождая нативный дескриптор."""
    image = pyexiv2.Image(path)
    try:
        exif = image.read_exif()
        xmp = image.read_xmp()
    finally:
        image.close()
    return _normalize_pyexiv2_metadata(exif, xmp)


def read_metadata_batch(paths: list[str]) -> list[dict]:
    """Читает пакет без общего нативного состояния, сохраняя порядок входных путей."""
    results = []
    for path in paths:
        try:
            results.append(read_metadata(path))
        except (OSError, RuntimeError, ValueError):
            results.append({})
    return results


def _normalize_pyexiv2_metadata(exif: dict, xmp: dict) -> dict:
    """Дополняет ключи Exiv2 совместимыми именами и численными значениями.

    Exiv2 хранит рационали строками и не создаёт composite-время. Нормализация
    здесь не даёт этим различиям протечь в SQLite-кэш и фильтры приложения.
    """
    result = {**exif, **xmp}
    aliases = {
        "Exif.Image.Orientation": "EXIF:Orientation",
        "Xmp.xmp.Rating": "XMP:Rating",
        "Exif.Photo.ExposureTime": "EXIF:ExposureTime",
        "Exif.Photo.ShutterSpeedValue": "EXIF:ShutterSpeedValue",
        "Exif.Photo.ISOSpeedRatings": "EXIF:ISO",
        "Exif.Photo.FNumber": "EXIF:FNumber",
        "Exif.Photo.ApertureValue": "EXIF:ApertureValue",
        "Exif.Photo.FocalLength": "EXIF:FocalLength",
        "Exif.Image.Model": "EXIF:Model",
        "Exif.Photo.BodySerialNumber": "EXIF:SerialNumber",
        "Exif.Photo.DateTimeOriginal": "EXIF:DateTimeOriginal",
        "Exif.Photo.OffsetTimeOriginal": "EXIF:OffsetTimeOriginal",
        "Exif.Photo.DateTimeDigitized": "EXIF:CreateDate",
        "Exif.Image.DateTime": "EXIF:CreateDate",
    }
    numeric = {
        "Exif.Image.Orientation", "Xmp.xmp.Rating", "Exif.Photo.ExposureTime",
        "Exif.Photo.ShutterSpeedValue", "Exif.Photo.ISOSpeedRatings", "Exif.Photo.FNumber",
        "Exif.Photo.ApertureValue", "Exif.Photo.FocalLength",
    }
    for source, target in aliases.items():
        value = result.get(source)
        if value not in (None, ""):
            result[target] = _rational_to_float(value) if source in numeric else value
    for key, value in exif.items():
        if key.endswith(".InternalSerialNumber") and value not in (None, ""):
            result["MakerNotes:InternalSerialNumber"] = value
    datetime_original = result.get("Exif.Photo.DateTimeOriginal")
    subseconds = result.get("Exif.Photo.SubSecTimeOriginal")
    offset = result.get("Exif.Photo.OffsetTimeOriginal", "")
    if datetime_original:
        fraction = f".{subseconds}" if subseconds else ""
        result["Composite:SubSecDateTimeOriginal"] = f"{datetime_original}{fraction}{offset}"
    return result


def _rational_to_float(value):
    """Преобразует рациональ Exiv2 ``числитель/знаменатель`` в число."""
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            return int(numerator) / int(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def extract_metadata_batch(paths: list[str]) -> list[tuple[str, str]]:
    lower_background_priority()
    results = []
    try:
        payloads = read_metadata_batch(paths)
    except OSError:
        return results
    for index, path in enumerate(paths):
        try:
            raw = payloads[index] if index < len(payloads) else {}
            exif = sanitize_exif(raw)
            metadata = {
                "exif": exif,
                "orientation": normalize_orientation(first_tag(exif, "EXIF:Orientation", "Orientation")),
                "rating": normalize_rating(first_tag(exif, "XMP:Rating", "EXIF:Rating", "Rating")),
                "capture_settings": capture_settings(exif),
                "camera": camera_details(exif),
                "original_datetime": original_datetime(exif),
            }
            results.append((path, json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))))
        except (TypeError, ValueError):
            continue
    return results


def sanitize_exif(exif: object) -> dict:
    if not isinstance(exif, dict):
        return {}
    types = (str, int, float, bool, list, dict, type(None))
    return {key: value for key, value in exif.items() if isinstance(key, str) and key != "SourceFile" and isinstance(value, types)}


def first_tag(exif: dict, *names: str):
    for name in names:
        value = tag_value(exif, name)
        if value not in (None, ""):
            return value
    return None


def tag_value(exif: dict, name: str):
    if name in exif:
        return exif[name]
    group, _, bare = name.partition(":")
    for key, value in exif.items():
        key_group, _, key_name = key.partition(":")
        if bare and key_name == bare and key_group.upper().startswith(group.upper()):
            return value
    if bare:
        return None
    return next((value for key, value in exif.items() if key == group or key.endswith(f":{group}")), None)


def _bounded_integer(value, low: int, high: int):
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result if low <= result <= high else None


def normalize_rating(value):
    return _bounded_integer(value, 0, 5)


def normalize_orientation(value):
    return _bounded_integer(value, 1, 8)


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capture_settings(exif: dict) -> dict:
    exposure = _numeric(first_tag(exif, "EXIF:ExposureTime", "ExposureTime"))
    aperture = _numeric(first_tag(exif, "EXIF:FNumber", "FNumber", "Composite:Aperture", "ApertureValue"))
    iso = _numeric(first_tag(exif, "EXIF:ISO", "ISO"))
    focal = _numeric(first_tag(exif, "EXIF:FocalLength", "FocalLength"))
    result = {}
    if exposure is not None:
        result.update(exposure_time=exposure, exposure_display=_exposure_display(exposure))
    if iso is not None:
        result["iso"] = int(iso)
    if aperture is not None:
        result["aperture"] = aperture
    if focal is not None:
        result["focal_length_mm"] = focal
    return result


def camera_details(exif: dict) -> dict:
    """Возвращает сведения о камере; серийный номер хранится, но не показывается."""
    result = {}
    model = first_tag(exif, "EXIF:Model", "Model", "UniqueCameraModel")
    serial = first_tag(exif, "EXIF:SerialNumber", "SerialNumber", "InternalSerialNumber")
    if model not in (None, ""):
        result["model"] = str(model).strip()
    if serial not in (None, ""):
        result["serial_number"] = str(serial).strip()
    return result


def _exposure_display(value: float) -> str:
    if value <= 0:
        return str(value)
    if value < 1:
        return f"1/{round(1 / value)}"
    return str(int(value)) if value.is_integer() else f"{value:g}"


def original_datetime(exif: dict) -> str | None:
    value = first_tag(exif, "Composite:SubSecDateTimeOriginal", "SubSecDateTimeOriginal", "EXIF:DateTimeOriginal", "DateTimeOriginal", "CreateDate")
    if not value:
        return None
    offset = first_tag(exif, "EXIF:OffsetTimeOriginal", "OffsetTimeOriginal") or ""
    text = str(value).strip()
    if offset and text[-6:] not in (str(offset), str(offset).replace(":", "")):
        text += str(offset)
    for fmt in ("%Y:%m:%d %H:%M:%S.%f%z", "%Y:%m:%d %H:%M:%S%z", "%Y:%m:%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    return None


class MetadataPipeline:
    """Фоновая очередь EXIF, намеренно независимая от прогресса ИИ."""

    def __init__(self) -> None:
        self.workers: ProcessPoolExecutor | None = None
        self.futures: set[Future] = set()
        self._lock = threading.Lock()
        self._shutting_down = False

    def scan(self, paths: list[Path], cache: FolderCache, on_complete=None) -> None:
        """Ставит отсутствующие EXIF-записи в отдельную фоновую очередь."""
        missing = cache.missing_metadata_paths(paths)
        if not missing:
            return
        with self._lock:
            if self._shutting_down:
                return
            if self.workers is None:
                self.workers = ProcessPoolExecutor(max_workers=1)
            workers = self.workers
        for start in range(0, len(missing), METADATA_BATCH_SIZE):
            batch = [str(path) for path in missing[start:start + METADATA_BATCH_SIZE]]
            try:
                future = workers.submit(extract_metadata_batch, batch)
            except RuntimeError:
                break
            with self._lock:
                if self._shutting_down:
                    future.cancel()
                    break
                self.futures.add(future)
            future.add_done_callback(
                lambda done, target=cache, callback=on_complete: self._finished(
                    done, target, callback
                )
            )

    def _finished(self, future: Future, cache: FolderCache, on_complete) -> None:
        with self._lock:
            self.futures.discard(future)
            if self._shutting_down:
                return
        if future.cancelled():
            return
        try:
            results = future.result()
            cache.store_photo_metadata(results)
            if on_complete is not None:
                on_complete(results)
        except Exception as exc:
            log_exception("Не удалось обработать результаты pyexiv2", exc)

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            futures = tuple(self.futures)
            self.futures.clear()
            workers, self.workers = self.workers, None
        for future in futures:
            future.cancel()
        if workers is not None:
            retire_executor(workers)

    def pending_futures(self) -> tuple[Future, ...]:
        """Возвращает снимок пакетов метаданных перед файловой операцией."""
        with self._lock:
            return tuple(self.futures)
