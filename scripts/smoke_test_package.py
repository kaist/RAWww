## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет собранное приложение и вложенный в него ExifTool без GUI-сервера."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


JPEG_SAMPLE = b"/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDy2iiivZOE/9k="


def _bundled_paths(app_directory: Path) -> tuple[Path, Path]:
    """Находит исполняемый файл приложения и его ExifTool в готовом каталоге."""
    app = app_directory / ("ctrlka.exe" if os.name == "nt" else "ctrlka")
    exiftool = app_directory / "data" / "tools" / ("exiftool.exe" if os.name == "nt" else "exiftool")
    if not app.is_file():
        raise RuntimeError(f"Application executable is missing: {app}")
    if not exiftool.is_file():
        raise RuntimeError(f"Bundled ExifTool is missing: {exiftool}")
    return app, exiftool


def _check_exiftool(executable: Path) -> None:
    """Проверяет запуск sidecar и чтение им корректного JPEG без внешних утилит."""
    version = subprocess.run(
        [str(executable), "-ver"], capture_output=True, check=True, text=True, timeout=20
    ).stdout.strip()
    if not version:
        raise RuntimeError("Bundled ExifTool did not report its version")
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "sample.jpg"
        sample.write_bytes(base64.b64decode(JPEG_SAMPLE))
        subprocess.run(
            [
                str(executable), "-overwrite_original", "-DateTimeOriginal=2024:01:02 03:04:05",
                str(sample),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=20,
        )
        result = subprocess.run(
            [str(executable), "-j", "-n", "-DateTimeOriginal", str(sample)],
            capture_output=True,
            check=True,
            text=True,
            timeout=20,
        )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or not payload or payload[0].get("DateTimeOriginal") != "2024:01:02 03:04:05":
        raise RuntimeError("Bundled ExifTool could not write and read EXIF in the JPEG sample")
    print(f"ExifTool smoke test passed: {version}")


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
    application, exiftool = _bundled_paths(args.app_dir.resolve())
    _check_exiftool(exiftool)
    _check_application(application, args.screenshot.resolve() if args.screenshot else None)


if __name__ == "__main__":
    main()
