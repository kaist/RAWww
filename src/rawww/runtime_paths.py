"""Locations of resources in source checkouts and frozen distributions."""

from __future__ import annotations

import sys
from pathlib import Path


def data_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data" / name
    return Path(__file__).with_name(name)
