"""Version metadata shared by the desktop application and its updater.

The version is ``1.0.<Git commit count>``. The commit count is resolved from
Git only in a source checkout; the number is baked into ``_build_version.py`` at
build time (see ``scripts/build_pyinstaller.py``) so the frozen, shipped
application never shells out to Git — that would flash a console window and is
meaningless on a client machine that has neither Git nor this repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .subprocess_utils import no_window_kwargs


_ROOT = Path(__file__).resolve().parents[2]


def _git_revision() -> int | None:
    if getattr(sys, "frozen", False):
        return None

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None

    try:
        revision = int(result.stdout.strip())
    except ValueError:
        return None
    return revision if revision >= 0 else None


def _resolve_version() -> str:
    try:
        from ._build_version import VERSION
    except ImportError:
        return f"1.0.{_git_revision() or 0}"
    return VERSION


__version__ = _resolve_version()
