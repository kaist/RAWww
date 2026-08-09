## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверяет границы зависимостей модулей, импортируемых spawn-воркерами."""

from __future__ import annotations

import subprocess
import sys
import unittest


class WorkerImportTests(unittest.TestCase):
    def _assert_clean_import(self, module: str, forbidden: tuple[str, ...]) -> None:
        checks = ", ".join(repr(name) for name in forbidden)
        code = (
            f"import sys; import {module}; "
            f"forbidden = [{checks}]; "
            "loaded = [name for name in forbidden if name in sys.modules]; "
            "assert not loaded, loaded"
        )
        subprocess.run([sys.executable, "-c", code], check=True)

    def test_decode_worker_module_does_not_import_qt(self) -> None:
        self._assert_clean_import("rawww.imaging", ("PySide6", "PySide6.QtGui"))

    def test_decode_worker_accepts_images_above_pillow_pixel_limit(self) -> None:
        code = "from PIL import Image; import rawww.imaging; assert Image.MAX_IMAGE_PIXELS is None"
        subprocess.run([sys.executable, "-c", code], check=True)

    def test_exif_worker_module_does_not_import_ui_cache(self) -> None:
        self._assert_clean_import("rawww.exif", ("rawww.cache", "PySide6"))

    def test_ai_worker_module_loads_only_shared_image_dependencies(self) -> None:
        self._assert_clean_import(
            "rawww.ai",
            ("rawww.cache", "rawww.face_analysis", "numpy", "PySide6"),
        )

    def test_batch_image_workers_do_not_import_app_or_qt(self) -> None:
        self._assert_clean_import(
            "rawww.batch_image_workers",
            ("rawww.app", "PySide6"),
        )


if __name__ == "__main__":
    unittest.main()
