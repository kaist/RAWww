## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Подготовка импорта с карты памяти без привязки к Qt-интерфейсу."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .exif import ExifToolClient, original_datetime
from .i18n import gettext as _
from .imaging import IMAGE_EXTENSIONS
from .transfer_queue import TransferEntry


# Это именно расширения служебных записей, а не имена каталогов: реальные
# видео у разных камер живут и в PRIVATE, и в M4ROOT, и в других деревьях.
SERVICE_EXTENSIONS = frozenset({
    ".bdm", ".bnp", ".cpi", ".ctg", ".dat", ".dsc", ".inp", ".int",
    ".lrv", ".mpl", ".mrk", ".tdt", ".thm", ".tid", ".tmp",
})
FINGERPRINT_SIZE = 128 * 1024


@dataclass(frozen=True)
class CardImportScan:
    """Содержит принятые файлы и дату, выбранную для каталога съёмки."""

    root: Path
    files: tuple[Path, ...]
    capture_date: date
    source_roots: tuple[Path, ...] = ()


def is_importable_file(path: Path) -> bool:
    """Отсеивает только известные служебные расширения без риска потерять формат камеры."""
    return path.suffix.lower() not in SERVICE_EXTENSIONS


def scan_card(root: Path) -> CardImportScan:
    """Рекурсивно собирает файлы карты и определяет дату по первому снимку с EXIF.

    Обход намеренно не знает имён каталогов производителей. Порядок стабилен,
    поэтому повторный импорт получает тот же каталог даты и тот же порядок имён.
    """
    root = Path(root)
    files: list[Path] = []
    for directory, dir_names, file_names in os.walk(root):
        dir_names.sort(key=str.casefold)
        for name in sorted(file_names, key=str.casefold):
            path = Path(directory) / name
            try:
                if path.is_file() and is_importable_file(path):
                    files.append(path)
            except OSError:
                # Карта может быть извлечена в середине обхода; очередь позже
                # покажет понятную ошибку для оставшихся доступных файлов.
                continue
    if not files:
        return CardImportScan(root, (), date.today(), (root,))

    capture = _first_capture_date(files)
    if capture is None:
        try:
            capture = datetime.fromtimestamp(files[0].stat().st_mtime).date()
        except OSError:
            capture = date.today()
    return CardImportScan(root, tuple(files), capture, (root,))


def merge_scans(scans: list[CardImportScan]) -> CardImportScan:
    """Объединяет выбранные карты в один импорт, сохраняя корень каждого файла."""
    non_empty = [scan for scan in scans if scan.files]
    if not non_empty:
        root = scans[0].root if scans else Path.cwd()
        return CardImportScan(root, (), date.today(), tuple(scan.root for scan in scans))
    return CardImportScan(
        non_empty[0].root,
        tuple(path for scan in non_empty for path in scan.files),
        non_empty[0].capture_date,
        tuple(root for scan in non_empty for root in (scan.source_roots or (scan.root,))),
    )


def _first_capture_date(files: list[Path]) -> date | None:
    """Читает EXIF только первого фото, чтобы подготовка большой карты не тормозила."""
    photo = next((path for path in files if path.suffix.lower() in IMAGE_EXTENSIONS), None)
    if photo is None:
        return None
    client: ExifToolClient | None = None
    try:
        client = ExifToolClient()
        value = original_datetime(client.read_metadata(str(photo)))
        return datetime.fromisoformat(value).date() if value else None
    except (OSError, ValueError):
        return None
    finally:
        if client is not None:
            client.close()


def build_import_entries(
    scan: CardImportScan,
    destination: Path,
    *,
    flatten: bool,
    reserved: Callable[[Path], bool] | None = None,
) -> list[TransferEntry]:
    """Строит цели импорта с предсказуемым автоматическим переименованием."""
    destination = Path(destination)
    occupied: dict[Path, Path] = {}
    entries: list[TransferEntry] = []
    for source in scan.files:
        relative = Path(source.name) if flatten else source.relative_to(_source_root(scan, source))
        target = _available_target(source, destination / relative, occupied, reserved)
        if target is None:
            continue
        occupied[target] = source
        entries.append(TransferEntry(source, target))
    return entries


def build_backup_entries(
    entries: list[TransferEntry],
    import_root: Path,
    backup_root: Path,
    *,
    flatten: bool,
    reserved: Callable[[Path], bool] | None = None,
) -> list[TransferEntry]:
    """Повторяет фактически импортированные имена в резервной копии."""
    occupied: dict[Path, Path] = {}
    result: list[TransferEntry] = []
    for entry in entries:
        relative = Path(entry.target.name) if flatten else entry.target.relative_to(import_root)
        target = _available_target(entry.target, Path(backup_root) / relative, occupied, reserved)
        if target is None:
            continue
        occupied[target] = entry.target
        result.append(TransferEntry(entry.target, target))
    return result


def _available_target(
    source: Path,
    target: Path,
    occupied: dict[Path, Path],
    reserved: Callable[[Path], bool] | None,
) -> Path | None:
    """Ищет свободное имя либо пропускает уже импортированный идентичный файл."""
    if _same_target_content(source, target, occupied):
        return None
    if not target.exists() and target not in occupied and not (reserved and reserved(target)):
        return target
    for number in range(2, 10_000):
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        if _same_target_content(source, candidate, occupied):
            return None
        if not candidate.exists() and candidate not in occupied and not (reserved and reserved(candidate)):
            return candidate
    raise OSError(_("Не удалось подобрать свободное имя для {name}").format(name=target.name))


def _same_target_content(source: Path, target: Path, occupied: dict[Path, Path]) -> bool:
    """Быстро сверяет размер и края файла, не читая целый RAW ради конфликта имён."""
    other = occupied.get(target, target if target.is_file() else None)
    if other is None:
        return False
    try:
        if source.stat().st_size != other.stat().st_size:
            return False
        if _fingerprint(source) != _fingerprint(other):
            return False
        return True
    except OSError:
        return False


def _fingerprint(path: Path) -> tuple[bytes, bytes]:
    """Читает края файла до полного сравнения: обычно этого уже достаточно для различия."""
    size = path.stat().st_size
    with path.open("rb", buffering=0) as stream:
        start = stream.read(FINGERPRINT_SIZE)
        if size > FINGERPRINT_SIZE:
            stream.seek(max(0, size - FINGERPRINT_SIZE))
        end = stream.read(FINGERPRINT_SIZE)
    return start, end




def _source_root(scan: CardImportScan, source: Path) -> Path:
    """Находит карту-владельца файла в объединённом импорте нескольких носителей."""
    for root in scan.source_roots or (scan.root,):
        try:
            source.relative_to(root)
            return root
        except ValueError:
            continue
    return scan.root
