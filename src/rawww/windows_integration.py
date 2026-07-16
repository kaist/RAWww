## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Интеграция собранного приложения с проводником Windows."""

from __future__ import annotations

import os
from pathlib import Path

from .imaging import JPEG_EXTENSIONS, RAW_EXTENSIONS


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
DEFAULT_APP_PROG_ID = "Kontrolka.Photo"
DEFAULT_APP_CAPABILITIES = r"Software\Kontrolka\Capabilities"
DEFAULT_APP_REGISTRATION = r"Software\RegisteredApplications"
# Ассоциации не дублируют список декодера: новая поддержанная RAW-камера
# автоматически появляется и в выборе программы по умолчанию.
DEFAULT_APP_EXTENSIONS = tuple(sorted(JPEG_EXTENSIONS | RAW_EXTENSIONS))


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


def register_default_app(
    executable: Path, extensions: tuple[str, ...] = DEFAULT_APP_EXTENSIONS
) -> None:
    """Регистрирует Контрольку кандидатом в системном выборе приложений Windows.

    Windows хранит подтверждённое пользователем приложение в защищённом
    ``UserChoice``, поэтому эта функция не меняет текущую программу молча.
    После регистрации пользователь выбирает Контрольку на странице настроек.
    """
    import winreg

    executable = executable.resolve()
    command = f'"{executable}" "%1"'
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, f"{_BASE}\\{DEFAULT_APP_PROG_ID}", 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "Контролька: фотографии RAW и JPG")
        winreg.SetValueEx(key, "FriendlyTypeName", 0, winreg.REG_SZ, "Контролька: фотографии RAW и JPG")
    _set_command(winreg, f"{_BASE}\\{DEFAULT_APP_PROG_ID}\\shell\\open\\command", command)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, DEFAULT_APP_CAPABILITIES, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, "Контролька")
        winreg.SetValueEx(key, "ApplicationDescription", 0, winreg.REG_SZ, "Быстрый просмотр и отбор RAW и JPG")
    for extension in extensions:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            f"{_BASE}\\{extension}\\OpenWithProgids",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, DEFAULT_APP_PROG_ID, 0, winreg.REG_NONE, b"")
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            f"{DEFAULT_APP_CAPABILITIES}\\FileAssociations",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, extension, 0, winreg.REG_SZ, DEFAULT_APP_PROG_ID)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, DEFAULT_APP_REGISTRATION, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "Контролька", 0, winreg.REG_SZ, DEFAULT_APP_CAPABILITIES)


def open_default_apps_settings() -> None:
    """Открывает страницу Windows, на которой пользователь подтверждает ассоциации."""
    os.startfile("ms-settings:defaultapps")


def is_registered() -> bool:
    """Проверяет команду Проводника, создаваемую при регистрации приложения."""
    import winreg

    path = f"{_BASE}\\Directory\\shell\\{VERB}\\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return bool(winreg.QueryValueEx(key, None)[0])
    except FileNotFoundError:
        return False
