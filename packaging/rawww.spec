# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for RAWww — tuned for a *minimal* Windows onedir build.

Run from the project root on Windows:

    pyinstaller packaging/rawww.spec --noconfirm --clean

Output: dist/RAWww/RAWww.exe  (onedir; ship the whole dist/RAWww folder)

Size strategy (see packaging/BUILD.md for the full methodology):
  1. Exclude entire Python packages we never import (matplotlib, tkinter, ...).
  2. Exclude Qt modules we do not use (QML/Quick/WebEngine/Charts/3D/...).
  3. Strip symbols + (optionally) UPX-compress the binaries.
  4. Run packaging/prune_build.py afterwards to delete leftover Qt plugins,
     translations and duplicate DLLs the hooks pull in defensively.

Anything you are unsure about should be *kept* here and removed later with
prune_build.py, because that step is reversible without a full rebuild.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------------------
# Paths. SPECPATH is provided by PyInstaller and points at this file's dir.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(SPECPATH).resolve().parent
SRC = PROJECT_ROOT / "src"
PKG = SRC / "rawww"

# ---------------------------------------------------------------------------
# Toggle UPX from the environment: `set RAWWW_UPX=1` before building.
# UPX shrinks DLLs a lot but occasionally trips antivirus and slows startup,
# so it is OFF by default. Point RAWWW_UPX_DIR at the folder containing upx.exe.
# ---------------------------------------------------------------------------
USE_UPX = os.environ.get("RAWWW_UPX", "0") == "1"
UPX_DIR = os.environ.get("RAWWW_UPX_DIR") or None

# ---------------------------------------------------------------------------
# Runtime data files. These MUST mirror the package layout ("rawww/<sub>")
# because the app resolves them via Path(__file__).with_name(...), and under
# PyInstaller __file__ for rawww modules lives at <bundle>/rawww/...
# ---------------------------------------------------------------------------
datas = [
    (str(PKG / "assets"), "rawww/assets"),
    (str(PKG / "models"), "rawww/models"),
    (str(PKG / "tools"), "rawww/tools"),
]

# insightface ships small package data + many submodules loaded lazily.
datas += collect_data_files("insightface")
hiddenimports = collect_submodules("insightface")

# ---------------------------------------------------------------------------
# Packages we never use. Excluding them keeps their (often huge) trees out.
# NOTE: scipy / scikit-image / sklearn are intentionally NOT excluded — parts
# of insightface import them lazily. If your run proves they are unused you can
# add them here for a big additional saving (test face detection afterwards!).
# ---------------------------------------------------------------------------
excludes = [
    # GUI toolkits we don't use
    "tkinter", "turtle", "turtledemo",
    "PyQt5", "PyQt6", "PySide2", "shiboken2",
    # Plotting / data science we don't ship
    "matplotlib", "pandas", "seaborn", "sympy",
    # Notebook / dev tooling
    "IPython", "jupyter", "jupyter_core", "notebook", "ipykernel",
    "pytest", "_pytest", "nose", "setuptools", "pip", "wheel",
    "pydoc_data", "lib2to3", "distutils",
    # Misc stdlib bits pulled in transitively but unused by the GUI
    "curses", "sqlite3.test",
    # ---- Qt modules we don't touch (QtCore/Gui/Widgets/Multimedia only) ----
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtNfc", "PySide6.QtBluetooth", "PySide6.QtRemoteObjects",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer", "PySide6.QtDBus", "PySide6.QtNetworkAuth",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    # Qt tooling shipped inside PySide6 (designer/assistant/qml runtimes etc.)
    "PySide6.scripts", "PySide6.support",
]

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "rawww_launcher.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,  # -OO: strip asserts + docstrings from bundled bytecode
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RAWww",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,          # strip is unreliable on Windows; prune instead
    upx=USE_UPX,
    upx_exclude=[
        # Never UPX these — Windows/Qt runtime DLLs that break when compressed.
        "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
        "python3.dll", "python312.dll",
        "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
    ],
    console=False,        # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PKG / "assets" / "app.ico") if (PKG / "assets" / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=USE_UPX,
    upx_dir=UPX_DIR,
    upx_exclude=[
        "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
        "python3.dll", "python312.dll",
        "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
    ],
    name="RAWww",
)
