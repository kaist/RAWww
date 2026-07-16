## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Собирает автономный ExifTool для macOS и Linux.

PAR::Packer упаковывает Perl и все модули ExifTool в один исполняемый файл.
Это устраняет зависимость готового приложения от системных Perl и ExifTool.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXIFTOOL_ROOT = ROOT / "src" / "rawww" / "tools" / "exiftool_files"
EXIFTOOL_SCRIPT = EXIFTOOL_ROOT / "exiftool.pl"


def build_exiftool(output_directory: Path) -> Path | None:
    """Создаёт sidecar-файл ExifTool для текущей Unix-платформы и проверяет его."""
    if sys.platform == "win32":
        return None
    if not EXIFTOOL_SCRIPT.is_file():
        raise RuntimeError(f"ExifTool source is missing: {EXIFTOOL_SCRIPT}")
    packer = shutil.which("pp")
    if packer is None:
        raise RuntimeError("PAR::Packer is required to build the bundled ExifTool")

    output_directory.mkdir(parents=True, exist_ok=True)
    executable = output_directory / "exiftool"
    command = [
        packer,
        "--output", str(executable),
        "--lib", str(EXIFTOOL_ROOT / "lib"),
        "--addfile", f"{EXIFTOOL_ROOT / 'lib'};lib",
        str(EXIFTOOL_SCRIPT),
    ]
    subprocess.run(command, cwd=EXIFTOOL_ROOT, check=True)
    executable.chmod(executable.stat().st_mode | 0o111)
    version = subprocess.run(
        [str(executable), "-ver"], capture_output=True, check=True, text=True, timeout=20
    ).stdout.strip()
    if not version:
        raise RuntimeError("The bundled ExifTool did not report its version")
    print(f"Bundled ExifTool: {version}")
    return executable
