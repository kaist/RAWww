## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Получение и нормализация метаданных через долгоживущий ExifTool.

Один процесс ExifTool обслуживает пакет запросов: запускать его на каждый
кадр было бы надёжно, но слишком похоже на наказание за любовь к RAW.
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Sequence

from concurrent.futures import Future, ProcessPoolExecutor

from .cache import FolderCache
from .runtime_paths import data_path
from .subprocess_utils import no_window_kwargs

from .worker_priority import lower_background_priority


EXIFTOOL_TAGS = [
    "-DateTimeOriginal", "-SubSecDateTimeOriginal", "-CreateDate", "-OffsetTimeOriginal",
    "-Orientation", "-Rating", "-XMP:Rating", "-EXIF:Rating", "-ExposureTime",
    "-ShutterSpeedValue", "-ISO", "-FNumber", "-ApertureValue", "-FocalLength",
    "-Model", "-SerialNumber", "-InternalSerialNumber",
]
BUNDLED_WINDOWS_EXIFTOOL = data_path("tools") / "exiftool.exe"
BUNDLED_UNIX_EXIFTOOL = data_path("tools") / "exiftool"
BUNDLED_EXIFTOOL_SCRIPT = data_path("tools") / "exiftool_files" / "exiftool.pl"
METADATA_BATCH_SIZE = 32


class ExifToolError(RuntimeError):
    """Ошибка протокола или ответа фонового процесса ExifTool."""

    pass


def bundled_exiftool_command() -> list[str]:
    """Возвращает команду ExifTool, которая поставляется вместе с приложением.

    В Windows используем готовый EXE. В собранных macOS- и Linux-версиях это
    sidecar с упакованным Perl runtime. Запуск из исходников сохраняет fallback
    на Perl разработчика, чтобы не требовать локальную сборку для каждого теста.
    """
    executable = BUNDLED_WINDOWS_EXIFTOOL if sys.platform == "win32" else BUNDLED_UNIX_EXIFTOOL
    if executable.is_file():
        return [str(executable)]
    if getattr(sys, "frozen", False):
        raise ExifToolError(f"Bundled ExifTool is missing: {executable}")

    if not BUNDLED_EXIFTOOL_SCRIPT.is_file():
        raise ExifToolError(f"Bundled ExifTool is missing: {BUNDLED_EXIFTOOL_SCRIPT}")
    perl = shutil.which("perl")
    if perl:
        return [perl, str(BUNDLED_EXIFTOOL_SCRIPT)]
    raise ExifToolError("ExifTool source requires Perl when the application runs from sources")


class ExifToolClient:
    """Держит один долгоживущий процесс ExifTool для пакетных запросов.

    Протокол ``-stay_open`` экономит запуск отдельного процесса на каждый
    снимок. Доступ защищён блокировкой: перемешать ответы двух пакетов было бы
    быстро, эффектно и совершенно бесполезно.
    """

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.command = list(command) if command is not None else bundled_exiftool_command()
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def read_metadata(self, path: str) -> dict:
        payload = self.read_metadata_batch([path])
        return payload[0] if payload else {}

    def read_metadata_batch(self, paths: list[str]) -> list[dict]:
        with self.lock:
            self._ensure_process()
            assert self.process and self.process.stdin and self.process.stdout
            for argument in [*EXIFTOOL_TAGS, *paths]:
                self.process.stdin.write(f"{argument}\n")
            self.process.stdin.write("-execute\n")
            self.process.stdin.flush()
            lines = []
            while True:
                line = self.process.stdout.readline()
                if line == "":
                    self.close()
                    raise ExifToolError("ExifTool stopped before response was complete")
                if line.strip() == "{ready}":
                    break
                lines.append(line)
        try:
            payload = json.loads("".join(lines) or "[]")
        except json.JSONDecodeError as exc:
            self.close()
            raise ExifToolError("ExifTool returned invalid JSON") from exc
        return payload if isinstance(payload, list) else []

    def _ensure_process(self) -> None:
        if self.process and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen(
                [*self.command, "-stay_open", "True", "-@", "-", "-common_args", "-json", "-n", "-G1", "-fast2"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8",
                **no_window_kwargs(),
            )
        except OSError as exc:
            raise ExifToolError(f"Cannot start ExifTool: {exc}") from exc

    def close(self) -> None:
        process, self.process = self.process, None
        if not process or process.poll() is not None:
            return
        try:
            assert process.stdin
            process.stdin.write("-stay_open\nFalse\n")
            process.stdin.flush()
            process.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            process.kill()


_client: ExifToolClient | None = None


def _get_client() -> ExifToolClient:
    global _client
    if _client is None:
        _client = ExifToolClient()
    return _client


def extract_metadata_batch(paths: list[str]) -> list[tuple[str, str]]:
    lower_background_priority()
    results = []
    try:
        payloads = _get_client().read_metadata_batch(paths)
    except (ExifToolError, OSError):
        return results
    by_path = {str(item.get("SourceFile", "")): item for item in payloads if isinstance(item, dict)}
    for index, path in enumerate(paths):
        try:
            raw = by_path.get(path, payloads[index] if index < len(payloads) else {})
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


def _close_client() -> None:
    if _client is not None:
        _client.close()


atexit.register(_close_client)


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
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            futures = tuple(self.futures)
            self.futures.clear()
            workers, self.workers = self.workers, None
        for future in futures:
            future.cancel()
        if workers is not None:
            workers.shutdown(wait=False, cancel_futures=True)
