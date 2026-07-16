## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Сборка Контрольки с помощью PyInstaller и отчёт о наиболее крупных файлах в дистрибутиве."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_exiftool import build_exiftool


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "ctrlka"
PORTABLE_MARKER = DIST / "portable.flag"
CONTENTS = DIST / "bin"
BUILD_VERSION_MODULE = ROOT / "src" / "rawww" / "_build_version.py"
EXCLUDED_QT_MODULES = (
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvg",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebView",
    "PySide6.QtXml",
)
PRUNABLE_QT_FILES = (
    "Qt6Pdf.dll",
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll",
    "Qt6OpenGL.dll",
    "opengl32sw.dll",
)


def _report_size(directory: Path) -> None:
    files = [path for path in directory.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    print(f"\nBuild: {directory}")
    print(f"Total: {total / 1024 / 1024:.1f} MiB across {len(files)} files")
    print("Largest files:")
    for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True)[:25]:
        relative = path.relative_to(directory)
        print(f"  {path.stat().st_size / 1024 / 1024:7.1f} MiB  {relative}")


def _add_portable_marker() -> None:
    PORTABLE_MARKER.touch()
    print(f"Portable marker: {PORTABLE_MARKER}")


def _prune_known_unused_qt_files(directory: Path) -> None:
    """Удаляет библиотеки Qt, которые не используются собранной Контролькой.

    В приложение входят только QtCore, QtGui, QtWidgets, QtMultimedia,
    QtMultimediaWidgets, QtNetwork и QtWebSockets. Проверка ``pyi-bindepend``
    подтверждает, что они не зависят от удаляемой группы QML, Quick и PDF.
    Список оставлен явным: при добавлении нового импорта PySide соответствующую
    DLL нужно убрать из списка и заново проверить сборку.
    """
    qt_dir = CONTENTS / "PySide6"
    removed = []
    for name in PRUNABLE_QT_FILES:
        path = qt_dir / name
        if path.is_file():
            removed.append(path)
            path.unlink()
    if removed:
        print("Pruned unused Qt DLLs: " + ", ".join(path.name for path in removed))


def _move_application_data(directory: Path) -> None:
    """Переносит изменяемые ресурсы рядом с EXE, а не внутрь служебной папки."""
    source = CONTENTS / "data"
    target = directory / "data"
    if not source.is_dir():
        raise RuntimeError(f"PyInstaller did not collect application data at {source}")
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))


def _prune_qt_translations(directory: Path) -> None:
    translations = CONTENTS / "PySide6" / "translations"
    if not translations.is_dir():
        return
    removed = []
    for path in translations.glob("*.qm"):
        if path.name != "qtbase_ru.qm":
            removed.append(path)
            path.unlink()
    if removed:
        print(f"Pruned unused Qt translations: {len(removed)} files")


def _compress_binaries(directory: Path) -> None:
    upx = shutil.which("upx")
    if upx is None:
        raise RuntimeError("UPX was requested but is not available on PATH")
    binaries = [
        path for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".dll", ".exe", ".pyd"}
    ]
    before = sum(path.stat().st_size for path in binaries)
    packed = 0
    for path in binaries:
        result = subprocess.run([upx, "--brute", "--quiet", str(path)], check=False)
        packed += result.returncode == 0
    after = sum(path.stat().st_size for path in binaries)
    print(f"UPX --brute packed {packed}/{len(binaries)} binaries: "
          f"{before / 1024 / 1024:.1f} -> {after / 1024 / 1024:.1f} MiB")


def _clean_previous_build() -> None:
    """Удаляет предыдущий дистрибутив и рабочий каталог перед новой сборкой."""
    for directory in (DIST, ROOT / "build" / "pyinstaller"):
        if directory.exists():
            shutil.rmtree(directory)


def _bake_build_version() -> None:
    """Записывает вычисленную из Git версию внутрь собираемого пакета.

    Готовое приложение не должно запускать Git: на машине пользователя может не
    быть ни Git, ни репозитория, а в Windows ещё и мелькнёт консоль. Версия
    вычисляется здесь, после чего ``rawww.version`` читает уже готовое значение.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        revision = int(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        revision = 0
    version = f"1.0.{max(revision, 0)}"
    BUILD_VERSION_MODULE.write_text(
        f'"""Generated at build time. Do not edit or commit."""\n\nVERSION = "{version}"\n',
        encoding="utf-8",
    )
    print(f"Baked build version: {version}")


def _linux_shared_library(soname: str) -> Path:
    """Возвращает путь к системной библиотеке Linux для явного включения в AppImage.

    PyInstaller намеренно не всегда собирает системные графические библиотеки.
    Для Qt это опасно: без EGL приложение не сможет даже создать offscreen-окно
    на минимальной Linux-системе.
    """
    result = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        if line.lstrip().startswith(f"{soname} ") and " => " in line:
            path = Path(line.rsplit(" => ", 1)[1])
            if path.is_file():
                return path
    raise RuntimeError(f"Linux runtime library is missing: {soname}")


def main() -> None:
    """Собирает дистрибутив, чистит лишние файлы и печатает сводку размера."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--upx", action="store_true", help="compress DLL/EXE/PYD files with UPX --brute")
    parser.add_argument("--console", action="store_true", help="keep a console for diagnosing startup errors")
    parser.add_argument("--portable", action="store_true", help="mark the frozen build as portable")
    args = parser.parse_args()
    _clean_previous_build()
    _bake_build_version()
    exiftool_runtime = ROOT / "build" / "exiftool-runtime"
    try:
        bundled_exiftool = build_exiftool(exiftool_runtime)
        command = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--name", "ctrlka",
            "--icon", str(ROOT / "src" / "rawww" / "assets" / "ctrlka-icon.ico"),
            "--contents-directory", "bin",
            "--paths", str(ROOT / "src"),
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / "build" / "pyinstaller"),
            "--specpath", str(ROOT / "build" / "pyinstaller"),
            "--add-data", f"{ROOT / 'src' / 'rawww' / 'models'}{os.pathsep}data/models",
            "--add-data", f"{ROOT / 'src' / 'rawww' / 'tools'}{os.pathsep}data/tools",
            "--add-data", f"{ROOT / 'src' / 'rawww' / 'assets'}{os.pathsep}data/assets",
            "--collect-binaries", "onnxruntime",
            "--hidden-import", "rawpy",
            "--hidden-import", "rawww._build_version",
        ]
        if bundled_exiftool is not None:
            command.extend(("--add-data", f"{bundled_exiftool}{os.pathsep}data/tools"))
        if sys.platform.startswith("linux"):
            egl_library = _linux_shared_library("libEGL.so.1")
            command.extend(("--add-binary", f"{egl_library}{os.pathsep}PySide6"))
        command.append("--console" if args.console else "--windowed")
        for module in EXCLUDED_QT_MODULES:
            command.extend(("--exclude-module", module))
        if args.upx:
            command.append("--noupx")
        command.append(str(ROOT / "scripts" / "pyinstaller_entry.py"))
        subprocess.run(command, cwd=ROOT, check=True)
    finally:
        BUILD_VERSION_MODULE.unlink(missing_ok=True)
        shutil.rmtree(exiftool_runtime, ignore_errors=True)
    _move_application_data(DIST)
    _prune_known_unused_qt_files(DIST)
    _prune_qt_translations(DIST)
    if args.upx:
        _compress_binaries(DIST)
    if args.portable:
        _add_portable_marker()
    _report_size(DIST)


if __name__ == "__main__":
    main()
