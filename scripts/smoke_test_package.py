## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет запуск собранного приложения без GUI-сервера."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import time
from pathlib import Path


MACOS_DISPLAY_NAMES = {
    "en": "Controlka",
    "de": "Controlka",
    "ru": "Контролька",
    "zh-Hans": "Controlka",
}


def _check_macos_bundle_names(app_directory: Path) -> None:
    """Проверяет стабильное имя .app и локализованные системные названия."""
    if app_directory.name != "ctrlka.app":
        raise RuntimeError(f"macOS bundle must be named ctrlka.app: {app_directory}")
    contents = app_directory / "Contents"
    with (contents / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleDisplayName") != "Controlka":
        raise RuntimeError("macOS bundle fallback display name is not Controlka")
    for language, expected in MACOS_DISPLAY_NAMES.items():
        path = contents / "Resources" / f"{language}.lproj" / "InfoPlist.strings"
        with path.open("rb") as source:
            localized = plistlib.load(source)
        if localized.get("CFBundleDisplayName") != expected:
            raise RuntimeError(f"Invalid macOS display name for {language}: {localized!r}")
    print("macOS localized bundle names passed")


def _application_path(app_directory: Path) -> Path:
    """Находит исполняемый файл приложения в onedir-каталоге или .app."""
    if app_directory.suffix == ".app":
        app = app_directory / "Contents" / "MacOS" / "ctrlka"
    else:
        app = app_directory / ("ctrlka.exe" if os.name == "nt" else "ctrlka")
    if not app.is_file():
        raise RuntimeError(f"Application executable is missing: {app}")
    return app


def _check_application(executable: Path, screenshot_path: Path | None = None) -> None:
    """Запускает собранный Qt-клиент на offscreen-платформе и ловит раннее падение."""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    if screenshot_path is not None:
        environment["RAWWW_CAPTURE_SCREENSHOT"] = str(screenshot_path)
    process = subprocess.Popen(
        [str(executable)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment
    )
    try:
        time.sleep(8)
        if screenshot_path is not None:
            try:
                output = process.communicate(timeout=8)[0]
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Application did not create its startup screenshot") from error
            if process.returncode != 0:
                raise RuntimeError(f"Application screenshot failed with code {process.returncode}: {output[-2000:]}")
            if not screenshot_path.is_file() or screenshot_path.stat().st_size == 0:
                raise RuntimeError("Application did not save its startup screenshot")
            print(f"Application screenshot smoke test passed: {screenshot_path}")
            return
        if process.poll() is not None:
            output = process.communicate(timeout=1)[0]
            raise RuntimeError(f"Application stopped with code {process.returncode}: {output[-2000:]}")
        print("Application smoke test passed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    """Запускает проверки для каталога, созданного PyInstaller до упаковки артефакта."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if args.app_dir.suffix == ".app":
        _check_macos_bundle_names(args.app_dir.resolve())
    _check_application(
        _application_path(args.app_dir.resolve()),
        args.screenshot.resolve() if args.screenshot else None,
    )


if __name__ == "__main__":
    main()
