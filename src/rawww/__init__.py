__all__ = ["main"]


def main(*args, **kwargs):
    """Lazy entry point.

    Importing the GUI (``.app``) pulls in QtGui/QtWidgets which need a display
    stack (libGL). Deferring the import keeps lightweight submodules such as
    ``shotsync_socket`` importable in headless environments (e.g. tests).
    """
    from .app import main as _main

    return _main(*args, **kwargs)
from .version import __version__
