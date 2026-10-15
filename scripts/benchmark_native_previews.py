## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Сравнивает нативный выбор превью со старым largest-JPEG на тех же RAW."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics

import numpy as np

from benchmark_previews import extract, measure, pool_measure
from rawww import imaging, raw_previews


def legacy_decode(path: Path) -> imaging.PixelImage:
    """Фиксирует старый путь независимо от доступности нативного моста."""
    return imaging._decode_qt_jpeg_bytes(path, extract(path), 256)


def main() -> None:
    """Проверяет геометрию каждого кадра и измеряет уже подключённый декодер."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--pools", action="store_true")
    args = parser.parse_args()
    if not raw_previews.available():
        parser.error("Нативный мост не доступен")
    paths = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() in imaging.RAW_EXTENSIONS)
    if not paths or args.rounds < 1:
        parser.error("Нужны RAW и положительное число повторов")
    report = {"images": len(paths), "rounds": args.rounds, "notes": "Warm OS cache; decode only, no UI/metadata/cache writes"}

    def publish(key, value):
        """Сохраняет промежуточные измерения без имён пользовательских снимков."""
        report[key] = value
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(key, json.dumps(value), flush=True)

    selection = {}
    old_bytes = new_bytes = 0
    for path in paths:
        chosen = raw_previews.extract_preview(path, 256)
        key = f"{chosen.info.width}x{chosen.info.height}" if chosen else "fallback"
        selection[key] = selection.get(key, 0) + 1
        old = extract(path)
        old_bytes += len(old)
        new_bytes += len(chosen.data) if chosen else len(old)
    publish("selection", {"sizes": selection, "old_bytes": old_bytes, "new_bytes": new_bytes})
    publish("serial", measure({"legacy": legacy_decode, "native": lambda p: imaging.decode_pixels(p, 256)}, paths, args.rounds))
    if args.pools:
        for processes, workers in ((False, 2), (True, 2), (True, 5)):
            for name, decoder in (("legacy", legacy_decode), ("native", imaging.decode_pixels)):
                # imaging.decode_pixels требует max_size, поэтому используем штатную обёртку.
                from benchmark_previews import worker_decode
                publish(f"{'process' if processes else 'thread'}_{workers}_{name}",
                        pool_measure(paths, processes, workers, args.rounds,
                                     legacy_decode if name == "legacy" else worker_decode))
    differences = []
    equal_size = 0
    for path in paths:
        before = legacy_decode(path)
        after = imaging.decode_pixels(path, 256)
        if (before.width, before.height) != (after.width, after.height):
            continue
        equal_size += 1
        a = np.frombuffer(before.pixels, np.uint8).reshape(-1, 4)[:, :3].astype(float)
        b = np.frombuffer(after.pixels, np.uint8).reshape(-1, 4)[:, :3].astype(float)
        differences.append(float(np.abs(a - b).mean()))
    publish("quality", {"equal_dimensions": equal_size, "mae_median": statistics.median(differences) if differences else None,
                        "mae_max": max(differences) if differences else None})
    if len(paths) <= 10:
        publish("preview_lists", [[asdict(p) for p in raw_previews.list_previews(path)] for path in paths])


if __name__ == "__main__":
    main()
