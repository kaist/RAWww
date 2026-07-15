## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Регистрация команд Контрольки в проводнике Windows для собранного приложения."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from rawww.windows_integration import DEFAULT_EXTENSIONS, register, unregister


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Explorer commands for Контролька")
    parser.add_argument("--executable", type=Path, default=Path("dist/ctrlka/ctrlka.exe"))
    parser.add_argument("--extension", dest="extensions", action="append", metavar="EXT")
    parser.add_argument("--unregister", action="store_true")
    args = parser.parse_args()
    extensions = tuple(extension.lower() if extension.startswith(".") else f".{extension.lower()}"
                       for extension in (args.extensions or DEFAULT_EXTENSIONS))
    if os.name != "nt":
        parser.error("Explorer registration is available only on Windows")
    if args.unregister:
        unregister(extensions)
        print("Explorer commands removed.")
        return
    executable = args.executable.expanduser().resolve()
    if not executable.is_file():
        parser.error(f"executable not found: {executable}")
    register(executable, extensions)
    print(f"Explorer commands registered for {len(extensions)} formats.")


if __name__ == "__main__":
    main()
