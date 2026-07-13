"""Per-user Windows Explorer integration for the packaged application."""

from __future__ import annotations

from pathlib import Path


DEFAULT_EXTENSIONS = (
    ".3fr", ".arw", ".bmp", ".cr2", ".cr3", ".crw", ".dcr", ".dng",
    ".erf", ".fff", ".iiq", ".jpe", ".jpeg", ".jpg", ".kdc", ".mef",
    ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".png", ".raf",
    ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".tif", ".tiff", ".webp",
    ".x3f", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm",
)
VERB = "rawww.open"
LABEL = "Открыть в Контрольке"
_BASE = r"Software\Classes"


def _delete_tree(registry, root, path: str) -> None:
    try:
        with registry.OpenKey(root, path, 0, registry.KEY_READ | registry.KEY_WRITE) as key:
            while True:
                try:
                    child = registry.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(registry, root, f"{path}\\{child}")
        registry.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _set_command(registry, path: str, command: str) -> None:
    with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, path, 0, registry.KEY_WRITE) as key:
        registry.SetValueEx(key, None, 0, registry.REG_SZ, command)


def register(executable: Path, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS) -> None:
    import winreg

    executable = executable.resolve()
    for extension in extensions:
        verb = f"{_BASE}\\SystemFileAssociations\\{extension}\\shell\\{VERB}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, verb, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, LABEL)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, f'"{executable}",0')
        _set_command(winreg, f"{verb}\\command", f'"{executable}" "%1"')
    for kind, argument in (("Directory", "%1"), ("Directory\\Background", "%V")):
        verb = f"{_BASE}\\{kind}\\shell\\{VERB}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, verb, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, LABEL)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, f'"{executable}",0')
        _set_command(winreg, f"{verb}\\command", f'"{executable}" "{argument}"')


def unregister(extensions: tuple[str, ...] = DEFAULT_EXTENSIONS) -> None:
    import winreg

    for extension in extensions:
        _delete_tree(winreg, winreg.HKEY_CURRENT_USER, f"{_BASE}\\SystemFileAssociations\\{extension}\\shell\\{VERB}")
    for kind in ("Directory", "Directory\\Background"):
        _delete_tree(winreg, winreg.HKEY_CURRENT_USER, f"{_BASE}\\{kind}\\shell\\{VERB}")


def is_registered() -> bool:
    """Check the folder command, which is installed with every registration."""
    import winreg

    path = f"{_BASE}\\Directory\\shell\\{VERB}\\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return bool(winreg.QueryValueEx(key, None)[0])
    except FileNotFoundError:
        return False
