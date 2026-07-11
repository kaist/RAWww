from __future__ import annotations

import os


_lowered = False


def lower_background_priority() -> None:
    """Lower the current worker process priority once, leaving UI workers alone."""
    global _lowered
    if _lowered:
        return
    try:
        if os.name == "nt":
            import ctypes

            below_normal_priority_class = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), below_normal_priority_class
            )
        else:
            os.nice(5)
        _lowered = True
    except (AttributeError, OSError):
        pass
