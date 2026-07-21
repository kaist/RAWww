## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Экспериментальная сборка Контрольки с помощью Nuitka.

Скрипт повторяет результат ``build_pyinstaller.py`` — переносимый каталог
``dist/ctrlka`` с ``ctrlka.exe``, ресурсами в ``data`` и меткой ``portable.flag`` —
но собирает его компилятором Nuitka. Ресурсы, версия и очистка неиспользуемых
модулей Qt переиспользуются из сборки PyInstaller, чтобы обе поставки были
сопоставимы по размеру и содержимому.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_exiftool import build_exiftool
from build_pyinstaller import (
    BUILD_VERSION_MODULE,
    EXCLUDED_QT_MODULES,
    _bake_build_version,
    _compress_binaries,
    _report_size,
)


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "nuitka_entry.py"
TOOLS_SRC = ROOT / "src" / "rawww" / "tools"
WORK = ROOT / "build" / "nuitka"
NUITKA_DIST = WORK / "nuitka_entry.dist"
DIST = ROOT / "dist" / "ctrlka"
ICON = ROOT / "src" / "rawww" / "assets" / "ctrlka-icon.ico"

# DLL Qt, которые Nuitka копирует по зависимостям, но собранная Контролька не
# использует. Приложению нужны только QtCore, QtGui, QtWidgets, QtMultimedia,
# QtMultimediaWidgets, QtNetwork и QtWebSockets. Список совпадает с очисткой
# PyInstaller, чтобы обе поставки были сопоставимы.
PRUNABLE_QT_DLLS = (
    "Qt6Pdf.dll",
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll",
    "Qt6OpenGL.dll",
    "opengl32sw.dll",
)


def _clean_previous_build() -> None:
    """Удаляет предыдущий дистрибутив и рабочий каталог Nuitka."""
    for directory in (DIST, WORK):
        if directory.exists():
            shutil.rmtree(directory)


def _baked_version() -> str:
    """Читает версию, записанную ``_bake_build_version`` в собираемый пакет."""
    for line in BUILD_VERSION_MODULE.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSION"):
            return line.split('"')[1]
    raise RuntimeError(f"Baked version is missing in {BUILD_VERSION_MODULE}")


def _nuitka_command(console: bool, version: str) -> list[str]:
    """Формирует команду Nuitka для переносимой Windows-сборки.

    ``rawww`` подключается целиком, потому что пользовательские сценарии
    импортируются лениво внутри функций, и статический анализ мог бы их
    пропустить. Ресурсы кладутся в ``data`` рядом с EXE — там их ждёт
    ``runtime_paths.data_path`` в собранной поставке.
    """
    command = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={WORK}",
        "--output-filename=ctrlka.exe",
        "--company-name=Igor Zalomskij",
        "--product-name=Kontrolka",
        f"--file-version={version}",
        f"--product-version={version}",
        "--file-description=Kontrolka",
        "--enable-plugin=pyside6",
        "--include-package=rawww",
        "--include-module=rawww._build_version",
        "--include-module=rawpy",
        "--include-package=onnxruntime",
        "--include-package-data=onnxruntime",
        f"--include-data-dir={ROOT / 'src' / 'rawww' / 'models'}=data/models",
        f"--include-data-dir={ROOT / 'src' / 'rawww' / 'tools'}=data/tools",
        f"--include-data-dir={ROOT / 'src' / 'rawww' / 'assets'}=data/assets",
        "--windows-console-mode=" + ("force" if console else "disable"),
    ]
    if ICON.is_file():
        command.append(f"--windows-icon-from-ico={ICON}")
    for module in EXCLUDED_QT_MODULES:
        command.append(f"--nofollow-import-to={module}")
    command.append(str(ENTRY))
    return command


def _prune_known_unused_qt_files(directory: Path) -> None:
    """Удаляет библиотеки Qt, которые не нужны собранной Контрольке."""
    removed = []
    for name in PRUNABLE_QT_DLLS:
        for path in directory.rglob(name):
            removed.append(path)
            path.unlink()
    if removed:
        print("Pruned unused Qt DLLs: " + ", ".join(path.name for path in removed))


def _restore_bundled_tools() -> None:
    """Восстанавливает бинарники ExifTool, которые Nuitka пропускает в data-каталогах.

    ``--include-data-dir`` намеренно не копирует ``.exe`` и ``.dll``, чтобы не
    подхватить чужие библиотеки. Но вложенный ExifTool — это самодостаточный Perl
    со своими DLL рядом с ``exiftool.exe``; без них он не запустится. Поэтому весь
    каталог инструментов накладывается поверх результата Nuitka как непрозрачные
    данные.
    """
    target = DIST / "data" / "tools"
    shutil.copytree(TOOLS_SRC, target, dirs_exist_ok=True)
    print(f"Restored bundled tool binaries into {target}")


def _move_distribution() -> None:
    """Переносит результат Nuitka в ``dist/ctrlka`` с привычной раскладкой."""
    if not NUITKA_DIST.is_dir():
        raise RuntimeError(f"Nuitka did not produce a standalone build at {NUITKA_DIST}")
    DIST.parent.mkdir(parents=True, exist_ok=True)
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.move(str(NUITKA_DIST), str(DIST))


def main() -> None:
    """Собирает дистрибутив Nuitka, чистит лишнее и печатает сводку размера."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--upx", action="store_true", help="compress DLL/EXE/PYD files with UPX --brute")
    parser.add_argument("--console", action="store_true", help="keep a console for diagnosing startup errors")
    parser.add_argument("--portable", action="store_true", help="mark the frozen build as portable")
    args = parser.parse_args()
    _clean_previous_build()
    _bake_build_version()
    exiftool_runtime = ROOT / "build" / "exiftool-runtime"
    bundled_exiftool: Path | None = None
    try:
        bundled_exiftool = build_exiftool(exiftool_runtime)
        subprocess.run(_nuitka_command(args.console, _baked_version()), cwd=ROOT, check=True)
        _move_distribution()
        _restore_bundled_tools()
        if bundled_exiftool is not None:
            target = DIST / "data" / "tools" / bundled_exiftool.name
            shutil.copy2(bundled_exiftool, target)
            target.chmod(target.stat().st_mode | 0o111)
        _prune_known_unused_qt_files(DIST)
        _prune_qt_translations_in(DIST)
        if args.upx:
            _compress_binaries(DIST)
        if args.portable:
            (DIST / "portable.flag").touch()
            print(f"Portable marker: {DIST / 'portable.flag'}")
        _report_size(DIST)
    finally:
        BUILD_VERSION_MODULE.unlink(missing_ok=True)
        shutil.rmtree(exiftool_runtime, ignore_errors=True)


def _prune_qt_translations_in(directory: Path) -> None:
    """Оставляет только русский перевод Qt, удаляя остальные ``*.qm``."""
    removed = 0
    for translations in directory.rglob("translations"):
        if not translations.is_dir():
            continue
        for path in translations.glob("*.qm"):
            if path.name != "qtbase_ru.qm":
                path.unlink()
                removed += 1
    if removed:
        print(f"Pruned unused Qt translations: {removed} files")


if __name__ == "__main__":
    main()
