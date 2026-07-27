## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Чтение кадров Canon RAW Burst без загрузки всего roll-файла в память.

CR3 является контейнером ISO Base Media. В RAW Burst Canon хранит кадры как
samples нескольких дорожек: JPEG-превью, RAW и служебные метаданные. Модуль
сначала читает только ``moov`` и таблицы samples, а затем обращается к одному
нужному диапазону исходного файла. Это особенно важно для roll на сотни МБ.
"""

from __future__ import annotations

import errno
import os
import struct
import sys
from dataclasses import dataclass
from time import monotonic, sleep
from uuid import uuid4
from pathlib import Path
from typing import Any


MAX_MOOV_BYTES = 64 * 1024 * 1024
WINDOWS_FILE_RETRY_SECONDS = 5.0


class CanonBurstError(RuntimeError):
    """Контейнер CR3 не соответствует ожидаемой структуре RAW Burst."""


@dataclass(frozen=True, order=True)
class BurstFrame:
    """Виртуальный кадр внутри физического Canon RAW Burst roll."""

    source: Path
    index: int
    count: int

    @property
    def name(self) -> str:
        return f"{self.source.stem} [{self.index + 1:03d}].CR3"

    @property
    def suffix(self) -> str:
        return self.source.suffix

    @property
    def parent(self) -> Path:
        return self.source.parent

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    def with_suffix(self, suffix: str) -> Path:
        """Строит только логический sidecar-путь; сам виртуальный кадр на диск не пишет."""
        return self.parent / Path(self.name).with_suffix(suffix)

    def is_file(self) -> bool:
        return self.source.is_file()

    def is_dir(self) -> bool:
        return False

    def stat(self) -> Any:
        return self.source.stat()

    def __str__(self) -> str:
        return f"{self.source}#rawww-burst-frame={self.index}"

    @property
    def cache_name(self) -> str:
        return f"{self.source.name}#burst-{self.index:04d}"


@dataclass(frozen=True)
class FileRange:
    """Диапазон байтов в исходном roll-файле."""

    offset: int
    size: int


@dataclass(frozen=True)
class BurstIndex:
    """Компактный индекс кадров roll, пригодный для передачи в worker-процесс.

    Индекс не владеет открытым файлом и не содержит RAW-данные. При изменении
    размера или времени исходника вызывающая сторона обязана построить его
    заново, иначе смещения могут указывать уже совсем не на фотографию.
    """

    path: Path
    file_size: int
    mtime_ns: int
    preview_samples: tuple[FileRange, ...]
    track_samples: tuple[tuple[FileRange, ...], ...]

    @property
    def frame_count(self) -> int:
        """Возвращает число кадров, совпадающее у всех пригодных дорожек."""
        return len(self.preview_samples)

    def frame(self, index: int) -> FileRange:
        """Возвращает JPEG-превью одного виртуального кадра."""
        try:
            return self.preview_samples[index]
        except IndexError as exc:
            raise CanonBurstError(f"Кадр RAW Burst вне диапазона: {index}") from exc

    def samples(self, index: int) -> tuple[FileRange, ...]:
        """Возвращает все samples кадра для сборки самостоятельного CR3."""
        if index < 0 or index >= self.frame_count:
            raise CanonBurstError(f"Кадр RAW Burst вне диапазона: {index}")
        return tuple(track[index] for track in self.track_samples)

    def is_current(self) -> bool:
        """Проверяет, что индекс всё ещё относится к неизменённому roll."""
        try:
            stamp = self.path.stat()
        except OSError:
            return False
        return stamp.st_size == self.file_size and stamp.st_mtime_ns == self.mtime_ns


def _u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _u64(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from(">Q", data, offset)[0]


def _box_header(read_at, offset: int, end: int) -> tuple[int, bytes, int]:
    """Читает заголовок ISOBMFF-box, не перенося содержимое в память."""
    if offset + 8 > end:
        raise CanonBurstError("Обрезанный заголовок CR3-box")
    header = read_at(offset, min(16, end - offset))
    size = _u32(header)
    kind = header[4:8]
    header_size = 8
    if size == 1:
        if len(header) < 16:
            raise CanonBurstError("Обрезанный расширенный заголовок CR3-box")
        size = _u64(header, 8)
        header_size = 16
    elif size == 0:
        size = end - offset
    if size < header_size or offset + size > end:
        raise CanonBurstError("Некорректный размер CR3-box")
    return size, kind, header_size


def _children(data: bytes, start: int, end: int):
    """Итерирует вложенные box из уже небольшого блока ``moov``."""
    position = start
    while position + 8 <= end:
        size = _u32(data, position)
        kind = data[position + 4:position + 8]
        header_size = 8
        if size == 1:
            if position + 16 > end:
                raise CanonBurstError("Обрезанный расширенный вложенный CR3-box")
            size = _u64(data, position + 8)
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise CanonBurstError("Некорректный вложенный CR3-box")
        yield position, size, kind, header_size
        position += size


def _find_children(data: bytes, start: int, end: int, wanted: bytes):
    for offset, size, kind, header_size in _children(data, start, end):
        if kind == wanted:
            yield offset, size, header_size


def _parse_track(data: bytes, offset: int, size: int) -> tuple[str, tuple[FileRange, ...]] | None:
    """Извлекает handler и таблицу samples одной дорожки ``trak``."""
    handler = ""
    sample_sizes: list[int] | None = None
    sample_offsets: list[int] | None = None

    def walk(start: int, end: int) -> None:
        nonlocal handler, sample_sizes, sample_offsets
        for position, box_size, kind, header_size in _children(data, start, end):
            payload = position + header_size
            if kind in {b"mdia", b"minf", b"stbl"}:
                walk(payload, position + box_size)
            elif kind == b"hdlr" and box_size >= header_size + 12:
                handler = data[payload + 8:payload + 12].decode("ascii", "replace")
            elif kind == b"stsz" and box_size >= header_size + 12:
                fixed_size = _u32(data, payload + 4)
                count = _u32(data, payload + 8)
                if fixed_size:
                    sample_sizes = [fixed_size] * count
                elif payload + 12 + count * 4 <= position + box_size:
                    sample_sizes = [_u32(data, payload + 12 + item * 4) for item in range(count)]
            elif kind in {b"co64", b"stco"} and box_size >= header_size + 8:
                count = _u32(data, payload + 4)
                width = 8 if kind == b"co64" else 4
                if payload + 8 + count * width <= position + box_size:
                    sample_offsets = [
                        (_u64(data, payload + 8 + item * width) if width == 8 else _u32(data, payload + 8 + item * width))
                        for item in range(count)
                    ]

    walk(offset + 8, offset + size)
    if not sample_sizes or not sample_offsets or len(sample_sizes) != len(sample_offsets):
        return None
    return handler, tuple(FileRange(item_offset, item_size) for item_offset, item_size in zip(sample_offsets, sample_sizes))


def read_burst_index(path: Path) -> BurstIndex | None:
    """Строит индекс Canon RAW Burst или возвращает ``None`` для обычного CR3.

    Во время индексации читаются заголовки верхнего уровня и блок ``moov``.
    Большой ``mdat`` с изображениями остаётся на диске до запроса конкретного
    превью или кадра.
    """
    try:
        stamp = path.stat()
        with path.open("rb") as source:
            def read_at(offset: int, size: int) -> bytes:
                source.seek(offset)
                value = source.read(size)
                if len(value) != size:
                    raise CanonBurstError("Не удалось дочитать CR3")
                return value

            moov: bytes | None = None
            position = 0
            while position + 8 <= stamp.st_size:
                size, kind, header_size = _box_header(read_at, position, stamp.st_size)
                if kind == b"moov":
                    if size > MAX_MOOV_BYTES:
                        raise CanonBurstError("Таблица кадров RAW Burst слишком велика")
                    moov = read_at(position, size)
                    break
                position += size
    except (OSError, struct.error, ValueError) as exc:
        raise CanonBurstError(f"Не удалось прочитать CR3: {path.name}") from exc
    if moov is None:
        return None

    tracks: list[tuple[str, tuple[FileRange, ...]]] = []
    try:
        for offset, size, kind, _header_size in _children(moov, 8, len(moov)):
            if kind == b"trak":
                track = _parse_track(moov, offset, size)
                if track is not None:
                    tracks.append(track)
    except (struct.error, ValueError) as exc:
        raise CanonBurstError(f"Не удалось разобрать таблицы CR3: {path.name}") from exc
    if not tracks:
        return None

    frame_count = max(len(samples) for _handler, samples in tracks)
    if frame_count < 2:
        return None
    preview_samples: tuple[FileRange, ...] = ()
    with path.open("rb") as source:
        for handler, samples in tracks:
            if handler != "vide" or len(samples) != frame_count or not samples:
                continue
            source.seek(samples[0].offset)
            if source.read(2) == b"\xff\xd8":
                preview_samples = samples
                break
    if not preview_samples:
        return None
    return BurstIndex(
        path=path,
        file_size=stamp.st_size,
        mtime_ns=stamp.st_mtime_ns,
        preview_samples=preview_samples,
        track_samples=tuple(samples for _handler, samples in tracks if len(samples) == frame_count),
    )


def read_frame_preview(index: BurstIndex, frame_index: int) -> bytes:
    """Возвращает JPEG-превью одного кадра, читая ровно его диапазон байтов."""
    if not index.is_current():
        raise CanonBurstError("RAW Burst изменился, индекс устарел")
    sample = index.frame(frame_index)
    try:
        with index.path.open("rb") as source:
            source.seek(sample.offset)
            jpeg = source.read(sample.size)
    except OSError as exc:
        raise CanonBurstError(f"Не удалось прочитать кадр RAW Burst: {index.path.name}") from exc
    if len(jpeg) != sample.size or not jpeg.startswith(b"\xff\xd8"):
        raise CanonBurstError("В RAW Burst не найдено JPEG-превью кадра")
    return jpeg


def read_frame_samples(index: BurstIndex, frame_index: int) -> tuple[bytes, ...]:
    """Читает данные одного кадра из всех дорожек roll.

    Функция нужна только полному просмотру и явной материализации. Даже там
    объём ограничен одним кадром, а не размером всего burst-файла.
    """
    if not index.is_current():
        raise CanonBurstError("RAW Burst изменился, индекс устарел")
    samples = index.samples(frame_index)
    try:
        with index.path.open("rb") as source:
            result = []
            for sample in samples:
                source.seek(sample.offset)
                value = source.read(sample.size)
                if len(value) != sample.size:
                    raise CanonBurstError("Не удалось дочитать sample RAW Burst")
                result.append(value)
    except OSError as exc:
        raise CanonBurstError(f"Не удалось прочитать кадр RAW Burst: {index.path.name}") from exc
    return tuple(result)


def materialized_path(frame: BurstFrame) -> Path:
    """Возвращает стабильное имя физического CR3 рядом с исходным roll."""
    return frame.source.with_name(
        f"{frame.source.stem}_{frame.index + 1:03d}{frame.source.suffix}"
    )


def _retry_windows_file_operation(operation) -> None:
    """Повторяет файловую операцию, пока Windows освобождает файл после записи.

    Антивирус и индексатор могут на короткое время открыть только что созданный
    CR3 без разрешения на переименование. На других платформах и для остальных
    ошибок повтор скрывал бы реальную проблему, поэтому там исключение
    возвращается сразу.
    """
    deadline = monotonic() + WINDOWS_FILE_RETRY_SECONDS
    while True:
        try:
            operation()
            return
        except OSError as exc:
            sharing_error = (
                getattr(exc, "winerror", None) in {5, 32, 33}
                or exc.errno in {errno.EACCES, errno.EBUSY, errno.EPERM}
            )
            if sys.platform != "win32" or not sharing_error or monotonic() >= deadline:
                raise
            sleep(0.1)


def _discard_temporary(path: Path) -> None:
    """Удаляет незавершённый файл, не подменяя им исходную ошибку извлечения."""
    try:
        _retry_windows_file_operation(lambda: path.unlink(missing_ok=True))
    except OSError:
        # Диагностика сборки важнее вторичной ошибки уборки; имя с UUID не
        # столкнётся со следующей попыткой, даже если Windows удерживает файл.
        pass


def materialize_frame(frame: BurstFrame) -> Path:
    """Атомарно извлекает виртуальный кадр в самостоятельный CR3.

    Сборщик использует memory map исходника: адресное пространство отображает
    roll целиком, но Python не читает его в ``bytes`` и не держит сотни МБ в
    своей куче. На диск публикуется только полностью собранный файл.
    """
    target = materialized_path(frame)
    if target.is_file():
        raise CanonBurstError(
            f"Файл {target.name} уже существует; существующий CR3 не будет перезаписан"
        )
    temporary = target.with_name(f".{target.name}.rawww-burst-{uuid4().hex}.tmp")
    from ._vendor_canon_burst_extract import CR3BurstFile

    try:
        burst = CR3BurstFile(str(frame.source))
        try:
            if burst.num_images != frame.count:
                raise CanonBurstError("Число кадров RAW Burst изменилось")
            burst.extract_image(frame.index, str(temporary))
        finally:
            # Публикуем результат лишь после освобождения всех дескрипторов
            # сборщика: порядок особенно важен для Windows.
            burst.close()
        _retry_windows_file_operation(lambda: os.replace(temporary, target))
    except Exception:
        _discard_temporary(temporary)
        raise
    return target
