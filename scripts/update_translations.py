## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Извлечение, обновление и сборка каталогов перевода интерфейса.

Команды (все на основе Babel):

- ``extract`` — собрать шаблон ``locale/rawww.pot`` из строк, помеченных ``_()``.
- ``update``  — создать недостающие ``.po`` и слить в существующие новые строки.
- ``compile`` — собрать ``.mo`` из ``.po`` для загрузки приложением.
- ``all``     — выполнить всё перечисленное по порядку (по умолчанию).

Русский — исходный язык, поэтому каталог для него не создаётся: ``msgid`` уже
на русском. Список целевых языков берётся из ``rawww.i18n``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BABEL_CONFIG = ROOT / "babel.cfg"
LOCALE_DIR = ROOT / "src" / "rawww" / "locale"
POT_FILE = LOCALE_DIR / "rawww.pot"
DOMAIN = "rawww"


def _target_languages() -> list[str]:
    """Возвращает языки перевода без исходного русского."""
    sys.path.insert(0, str(ROOT / "src"))
    from rawww.i18n import SUPPORTED_LANGUAGES

    return [code for code, _name in SUPPORTED_LANGUAGES if code != "ru"]


def _run(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "babel.messages.frontend", *args], check=True)


def extract() -> None:
    LOCALE_DIR.mkdir(parents=True, exist_ok=True)
    _run(
        "extract",
        "-F", str(BABEL_CONFIG),
        "-k", "_",
        "--sort-by-file",
        "--no-location",
        "-o", str(POT_FILE),
        str(ROOT / "src"),
    )


def update() -> None:
    for language in _target_languages():
        po_path = LOCALE_DIR / language / "LC_MESSAGES" / f"{DOMAIN}.po"
        if po_path.exists():
            _run("update", "-i", str(POT_FILE), "-d", str(LOCALE_DIR), "-D", DOMAIN, "-l", language)
        else:
            po_path.parent.mkdir(parents=True, exist_ok=True)
            _run("init", "-i", str(POT_FILE), "-d", str(LOCALE_DIR), "-D", DOMAIN, "-l", language)


def compile_catalogs() -> None:
    _run("compile", "-d", str(LOCALE_DIR), "-D", DOMAIN, "--statistics")


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление каталогами перевода.")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=("extract", "update", "compile", "all"),
    )
    command = parser.parse_args().command
    if command in ("extract", "all"):
        extract()
    if command in ("update", "all"):
        update()
    if command in ("compile", "all"):
        compile_catalogs()


if __name__ == "__main__":
    main()
