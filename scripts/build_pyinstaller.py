"""Build Контролька with PyInstaller and report the largest bundled files."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "ctrlka"
CONTENTS = DIST / "bin"
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


def _prune_known_unused_qt_files(directory: Path) -> None:
    """Remove leaf Qt libraries not used by Контролька's bundled bindings.

    The application bundles QtCore, QtGui, QtWidgets, QtMultimedia,
    QtMultimediaWidgets, QtNetwork, and QtWebSockets only. ``pyi-bindepend``
    confirms none of those libraries links to this QML/Quick/PDF group.
    Keep this list explicit: adding a corresponding PySide import requires
    removing its DLL from this list and testing the build again.
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
    """Put editable application resources beside the executable, not in bin."""
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
    """Remove the previous target and work directory before collecting files."""
    for directory in (DIST, ROOT / "build" / "pyinstaller"):
        if directory.exists():
            shutil.rmtree(directory)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upx", action="store_true", help="compress DLL/EXE/PYD files with UPX --brute")
    parser.add_argument("--console", action="store_true", help="keep a console for diagnosing startup errors")
    args = parser.parse_args()
    _clean_previous_build()
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
    ]
    command.append("--console" if args.console else "--windowed")
    for module in EXCLUDED_QT_MODULES:
        command.extend(("--exclude-module", module))
    if args.upx:
        # PyInstaller otherwise auto-detects UPX and applies its own LZMA
        # settings before this script gets a chance to run ``--brute``.
        command.append("--noupx")
    command.append(str(ROOT / "scripts" / "pyinstaller_entry.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    _move_application_data(DIST)
    _prune_known_unused_qt_files(DIST)
    _prune_qt_translations(DIST)
    if args.upx:
        _compress_binaries(DIST)
    _report_size(DIST)


if __name__ == "__main__":
    main()
