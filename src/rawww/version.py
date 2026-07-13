"""Version metadata shared by the desktop application and its updater."""

from __future__ import annotations

import subprocess
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def _git_revision() -> int | None:
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None

    try:
        revision = int(result.stdout.strip())
    except ValueError:
        return None
    return revision if revision >= 0 else None


__version__ = f"1.0.{_git_revision() or 0}"
