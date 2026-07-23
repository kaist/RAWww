## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет минимальную версию macOS у Mach-O внутри собранного .app.

Готовые колёса Qt/onnxruntime и т.п. несут в LC_BUILD_VERSION/LC_VERSION_MIN_MACOSX
минимальную версию macOS, под которую они собраны. Если эта версия выше, чем у
пользователя, приложение падает при загрузке нужной библиотеки (например
`Symbol not found … Expected in: /usr/lib/libc++.1.dylib`). Раннеры CI работают на
свежих macOS и такую регрессию не ловят, поэтому сверяем «пол» здесь.

По умолчанию завершаемся с ошибкой, если хоть один Mach-O требует macOS выше
целевой. Пути из списка исключений (например onnxruntime) только предупреждают:
эти зависимости не грузятся при старте и ограничивают лишь отдельные функции.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


VERSION_LINE = re.compile(r"^\s*(minos|version)\s+(\d+)\.(\d+)(?:\.(\d+))?\s*$")


def _iter_macho(bundle: Path):
    """Отдаёт файлы бандла, которые являются Mach-O (по сигнатуре ``file``)."""
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        probe = subprocess.run(
            ["file", "-b", str(path)], capture_output=True, text=True
        )
        if "Mach-O" in probe.stdout:
            yield path


def _min_macos(path: Path) -> tuple[int, int] | None:
    """Возвращает минимальную версию macOS (major, minor) из load-команд Mach-O."""
    dump = subprocess.run(
        ["otool", "-l", str(path)], capture_output=True, text=True
    ).stdout
    best: tuple[int, int] | None = None
    for line in dump.splitlines():
        match = VERSION_LINE.match(line)
        if match:
            found = (int(match.group(2)), int(match.group(3)))
            # Для файла берём наибольшее требование среди всех архитектур/команд.
            if best is None or found > best:
                best = found
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="путь к .app или каталогу сборки")
    parser.add_argument(
        "--max-version",
        default="11.0",
        help="максимально допустимая минимальная версия macOS (major.minor)",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="подстрока пути, для которой превышение только предупреждает",
    )
    args = parser.parse_args()

    ceiling = tuple(int(part) for part in args.max_version.split("."))[:2]
    if len(ceiling) == 1:
        ceiling = (ceiling[0], 0)

    hard_violations: list[tuple[Path, tuple[int, int]]] = []
    soft_violations: list[tuple[Path, tuple[int, int]]] = []
    highest: tuple[int, int] | None = None
    for path in _iter_macho(args.bundle):
        version = _min_macos(path)
        if version is None:
            continue
        if highest is None or version > highest:
            highest = version
        if version > ceiling:
            ignored = any(token in str(path) for token in args.ignore)
            (soft_violations if ignored else hard_violations).append((path, version))

    def _fmt(items: list[tuple[Path, tuple[int, int]]]) -> str:
        return "\n".join(
            f"  {major}.{minor}\t{path.relative_to(args.bundle)}"
            for path, (major, minor) in sorted(items, key=lambda item: item[1], reverse=True)
        )

    print(f"Максимальная минимальная версия macOS среди Mach-O: {highest}")
    if soft_violations:
        print(
            f"ПРЕДУПРЕЖДЕНИЕ: библиотеки требуют macOS выше {args.max_version} "
            f"(нагружают только отдельные функции):\n{_fmt(soft_violations)}"
        )
    if hard_violations:
        print(
            f"ОШИБКА: библиотеки требуют macOS выше {args.max_version} и нужны для "
            f"запуска:\n{_fmt(hard_violations)}"
        )
        return 1
    print(f"OK: все критичные Mach-O запускаются на macOS {args.max_version}+")
    return 0


if __name__ == "__main__":
    sys.exit(main())
