__all__ = ["main", "mobile_main"]


def main(*args, **kwargs):
    """Lazy entry point.

    Importing the GUI (``.app``) pulls in QtGui/QtWidgets which need a display
    stack (libGL). Deferring the import keeps lightweight submodules such as
    ``shotsync_socket`` importable in headless environments (e.g. tests).

    On the ``mobile`` build profile the full desktop app is never imported;
    the trimmed ShotSync selection shell runs instead.
    """
    from .platform_profile import IS_MOBILE

    if IS_MOBILE:
        return mobile_main(*args, **kwargs)
    from .app import main as _main

    return _main(*args, **kwargs)


def mobile_main(*args, **kwargs):
    """Entry point for the Android/mobile ShotSync selection shell."""
    from .mobile import main as _main

    return _main(*args, **kwargs)
from .version import __version__
