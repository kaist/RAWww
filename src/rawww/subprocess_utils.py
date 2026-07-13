"""Helpers for launching child processes without flashing console windows."""

from __future__ import annotations

import subprocess
import sys


def no_window_kwargs() -> dict:
    """Return ``subprocess`` keyword arguments that hide the child's console.

    ExifTool and Git are console executables. When they are launched from the
    windowed (``--windowed``) frozen Windows build they briefly pop a console
    window each time, which flashes on screen during startup and folder scans.
    ``CREATE_NO_WINDOW`` together with a hidden ``STARTUPINFO`` suppresses it.
    The flags only exist on Windows, so other platforms get an empty mapping.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }
