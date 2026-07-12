"""Frozen entry point for RAWww.

This is the script PyInstaller freezes. It exists (instead of pointing
PyInstaller straight at the package) so that ``multiprocessing.freeze_support``
runs *before* anything else in every spawned worker process.

On Windows, ``ProcessPoolExecutor`` uses the "spawn" start method: each worker
re-launches the very same .exe. ``freeze_support()`` detects that it is running
as a worker, executes the pickled task, and exits. Without it, each worker would
fall through and start the GUI again -> an infinite cascade of windows.
"""

from __future__ import annotations

import multiprocessing


def _run() -> None:
    from rawww import main

    main()


if __name__ == "__main__":
    # Must be the first thing that happens in a (re-launched) worker process.
    multiprocessing.freeze_support()
    _run()
