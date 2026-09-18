## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверка настоящего окна и модели на синтетических файлах в work.

Запуск: uv run python scripts/check_inpaint_gui.py. В Windows использует
родной графический сеанс; не открывает и не меняет пользовательские снимки.
"""

from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms, ImageDraw
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from rawww.i18n import activate
from rawww.inpaint_dialog import InpaintDialog
from rawww.inpaint_pipeline import inpaint_model_path
from rawww.theme import apply_theme


def main() -> None:
    """Проходит кисть → инференс → запись → навигацию → освобождение процесса."""
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    activate("ru")
    apply_theme(app)
    root = Path(__file__).resolve().parents[1] / "work"
    root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="inpaint-gui-", dir=root))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    paths = []
    for index in range(5):
        # JPEG draft реально декодируется уменьшенным: проверяем именно путь,
        # который важен при быстром листании больших камерных кадров.
        pixels = np.empty((2400,3600,3), dtype=np.uint8)
        for y in range(2400):
            pixels[y,:] = (min(255, 130+y//10), min(255, 155+y//15), min(255, 180+y//20))
        image = Image.fromarray(pixels)
        draw = ImageDraw.Draw(image)
        for x in range(-3600,7200,360):
            draw.line((1800,240,x,2400), fill=(65,75,90), width=6)
        draw.rectangle((1665,990,1935,1410), fill=(220,35,40))
        draw.text((30,30), f"Perspective test {index+1}", fill="white")
        exif = Image.Exif()
        exif[271] = "Inpaint GUI test"
        path = folder / f"test-{index}.jpg"
        image.save(path, quality=98, exif=exif, icc_profile=profile)
        paths.append(path)
    errors = []
    def warning(*args, **kwargs):
        errors.append(str(args[2]))
        return QMessageBox.StandardButton.Cancel
    def wait_until(predicate, timeout=120):
        end = time.monotonic()+timeout
        while not predicate():
            app.processEvents()
            if errors:
                raise AssertionError(errors)
            if time.monotonic() > end:
                raise TimeoutError(dialog.status.text())
            QTest.qWait(10)
    from PySide6.QtCore import QSettings
    settings = QSettings(str(folder / "inpaint-test.ini"), QSettings.Format.IniFormat)
    dialog = InpaintDialog(paths, paths[0], settings)
    dialog.show()
    started = time.monotonic()
    try:
        with patch.object(QMessageBox, "warning", side_effect=warning):
            wait_until(lambda: not dialog.view.image.isNull())
            wait_until(lambda: any(state in {"loading", "downloading"} for state in dialog._models.values()))
            # Один загрузчик инициализирует модели последовательно. Проверяем,
            # что чтение следующего кадра не ждёт эту тяжёлую работу.
            for target in (1, 2, 3, 4, 3, 2, 1):
                dialog.index = target
                dialog._open()
            wait_until(lambda: dialog.path == str(paths[1]) and dialog._displayed_path == str(paths[1]))
            assert not dialog.view.image.isNull()
            dialog.navigate(-1)
            wait_until(lambda: dialog.path == str(paths[0]) and dialog._displayed_path == str(paths[0]))
            print("Navigation during sequential model loading: OK", flush=True)
            wait_until(lambda: dialog.view.editable)
            assert dialog.sd_quality_button.isChecked()
            if inpaint_model_path("hd").is_file():
                dialog.hd_quality_button.click()
                wait_until(lambda: dialog._models["inpaint"] == "ready")
                assert dialog.hd_quality_button.isChecked()
                print("SD -> HD model switch: OK", flush=True)
            assert max(dialog.view.image.width(), dialog.view.image.height()) <= 1920
            pid = dialog.process.processId()
            print(f"Model ready: {time.monotonic()-started:.2f}s; worker PID {pid}", flush=True)
            point = dialog.view.mapFromScene(dialog.view.image.width() / 2, dialog.view.image.height() / 2)
            dialog.view.diameter = 175 * dialog.view.transform().m11()
            QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
            dialog.grab().save(str(folder / "01-mask.png"))
            started = time.monotonic()
            QTest.keyClick(dialog.view, Qt.Key.Key_Return)
            wait_until(lambda: dialog.revisions.get(str(paths[0]),0) == 1)
            dialog.navigate(1)
            wait_until(lambda: dialog.path == str(paths[1]) and dialog.view.editable)
            wait_until(lambda: not any(dialog.pending.values()))
            print(f"Inpaint + background save + next frame: {time.monotonic()-started:.2f}s", flush=True)
            dialog.navigate(-1)
            wait_until(lambda: dialog.view.editable)
            dialog.grab().save(str(folder / "02-result.png"))
            with Image.open(paths[0]) as saved:
                assert saved.getexif()[271] == "Inpaint GUI test"
                assert saved.info["icc_profile"] == profile
                assert saved.getpixel((1800,1200)) != (220,35,40)
            if dialog.hd_quality_button.isChecked():
                dialog.sd_quality_button.click()
                wait_until(lambda: dialog._models["inpaint"] == "ready")
                assert dialog.sd_quality_button.isChecked()
                print("HD -> SD model switch: OK", flush=True)
            for delta in (1,1,1,-1,-1,-1):
                dialog.navigate(delta)
            wait_until(lambda: dialog.view.editable)
            assert dialog.path == str(paths[0])
            dialog.close()
            wait_until(lambda: dialog._closed)
            assert dialog.process.processId() == 0
            print(f"Closed, worker released. Screenshots: {folder}", flush=True)
            # Закрытие во время загрузки тоже не оставляет модель в памяти.
            dialog = InpaintDialog(paths, paths[0], settings)
            dialog.show()
            app.processEvents()
            dialog.close()
            wait_until(lambda: dialog._closed)
            print("Close during model startup: OK", flush=True)
    finally:
        if not dialog._closed:
            dialog.pending.clear()
            dialog._closing = True
            dialog._stop_process()
            wait_until(lambda: dialog._closed, timeout=10)


if __name__ == "__main__":
    main()
