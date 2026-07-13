"""Locations of resources in source checkouts and frozen distributions."""

from __future__ import annotations

import sys
from pathlib import Path


# Set to True for a self-contained distribution.  The normal installed build
# keeps using platform-native data locations.
PORTABLE = False


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
