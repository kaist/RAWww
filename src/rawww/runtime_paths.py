"""Locations of resources in source checkouts and frozen distributions."""

from __future__ import annotations

import sys
from pathlib import Path


# A portable PyInstaller distribution carries this marker beside its executable.
# The regular installer intentionally does not, because its application
# directory is normally under Program Files and is not writable by the user.
PORTABLE = bool(
    getattr(sys, "frozen", False)
    and (Path(sys.executable).resolve().parent / "portable.flag").is_file()
)


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def work_path() -> Path:
    return application_directory() / "work"


def data_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data" / name
    return Path(__file__).with_name(name)
