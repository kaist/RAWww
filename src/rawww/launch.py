"""Resolve a filesystem target passed to the desktop application."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


def target_from_argv(argv: Sequence[str] | None = None) -> Path | None:
    """Return the first existing path supplied when the application starts.

    File managers pass a path as the first positional argument when opening a
    file association or a folder context-menu command.  Options are ignored so
    the normal application startup remains tolerant of platform launch flags;
    a path beginning with ``-`` can still be supplied after ``--``.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    positional = False
    for argument in arguments:
        if argument == "--":
            positional = True
            continue
        if not positional and argument.startswith("-"):
            continue
        path = Path(argument).expanduser()
        try:
            return path.resolve(strict=True)
        except OSError:
            return None
    return None
