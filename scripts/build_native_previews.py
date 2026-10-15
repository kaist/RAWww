## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Собирает маленький C-мост к заголовкам той же версии LibRaw, что у rawpy.

Мост не содержит второй LibRaw: при работе используется библиотека rawpy.
Windows: uv run --with ziglang==0.15.2 python scripts/build_native_previews.py.
Linux/macOS: достаточно системного cc. Скачанный SDK остаётся в build/.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.22.1"
SHA256 = "a789dc4e2409e2901d93793a4e0b80c7b49d0d97cf6ad71c850eb7616acfd786"


def build(headers: Path | None = None) -> Path:
    """Проверяет SDK и публикует собранный мост только после успешной компиляции."""
    import rawpy

    actual = ".".join(map(str, rawpy.libraw_version))
    work = ROOT / "build" / "native-previews"
    work.mkdir(parents=True, exist_ok=True)
    if headers is None:
        if actual != VERSION:
            raise RuntimeError(f"LibRaw {actual}: передайте совпадающий SDK через --headers")
        archive = work / f"LibRaw-{VERSION}.tar.gz"
        if not archive.exists():
            temporary = archive.with_suffix(".download")
            urllib.request.urlretrieve(f"https://www.libraw.org/data/LibRaw-{VERSION}.tar.gz", temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
                raise RuntimeError("SHA256 SDK LibRaw не совпадает")
            temporary.replace(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("SHA256 SDK LibRaw не совпадает")
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
        headers = work / f"LibRaw-{VERSION}"
    if not (headers / "libraw" / "libraw_types.h").is_file():
        raise RuntimeError("Не найден libraw/libraw_types.h")
    if sys.platform == "win32":
        if importlib.util.find_spec("ziglang") is None:
            raise RuntimeError("Запустите: uv run --with ziglang==0.15.2 python scripts/build_native_previews.py")
        compiler = [sys.executable, "-m", "ziglang", "cc"]
        suffix = ".dll"
    else:
        compiler = [os.environ.get("CC") or shutil.which("cc") or "cc"]
        suffix = ".dylib" if sys.platform == "darwin" else ".so"
    output = ROOT / "src" / "rawww" / "_native" / f"libraw_preview_bridge{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = work / output.name
    command = [*compiler, "-O2", "-std=c11", "-shared", "-I", str(headers),
               str(ROOT / "native" / "libraw_preview_bridge.c"), "-o", str(temporary_output)]
    if sys.platform != "win32":
        command.insert(len(compiler), "-fPIC")
    if sys.platform == "darwin" and os.environ.get("MACOSX_DEPLOYMENT_TARGET"):
        command.insert(len(compiler), f"-mmacosx-version-min={os.environ['MACOSX_DEPLOYMENT_TARGET']}")
    subprocess.run(command, check=True)
    # Не заменяем библиотеку, загруженную текущим процессом: сборка идёт отдельно.
    shutil.copy2(temporary_output, output)
    print(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headers", type=Path)
    build(parser.parse_args().headers)
