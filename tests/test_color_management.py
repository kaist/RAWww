## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки управления цветом полного просмотра."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PIL import ImageCms  # noqa: E402
from PySide6.QtGui import QColorSpace, QImage, qRgb  # noqa: E402

from rawww import color_management as cm  # noqa: E402


def _adobe_rgb_profile() -> bytes:
    """Реальный широкогамутный ICC (Adobe RGB) из Qt — для проверок с монитором."""
    return bytes(QColorSpace(QColorSpace.NamedColorSpace.AdobeRgb).iccProfile())


def _identity_transform():
    """Валидный потокобезопасный RGB→RGB transform для проверки путей кода."""
    profile = ImageCms.createProfile("sRGB")
    return ImageCms.buildTransform(
        profile,
        profile,
        "RGB",
        "RGB",
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
        flags=ImageCms.Flags.NOCACHE | ImageCms.Flags.BLACKPOINTCOMPENSATION,
    )


def _random_qimage(width: int, height: int) -> QImage:
    data = (np.random.rand(height, width, 3) * 255).astype("uint8")
    return QImage(data.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888).copy()


class ColorManagementConfigTest(unittest.TestCase):
    def test_disabled_returns_no_transform(self) -> None:
        config = cm.ColorManagementConfig(enabled=False)
        self.assertIsNone(cm.srgb_to_display_transform(b"anything", config))

    def test_missing_profile_returns_no_transform(self) -> None:
        config = cm.ColorManagementConfig(enabled=True)
        self.assertIsNone(cm.srgb_to_display_transform(None, config))

    def test_srgb_display_is_identity_and_skipped(self) -> None:
        # Профиль монитора совпадает с sRGB — коррекция не нужна.
        config = cm.ColorManagementConfig(enabled=True)
        srgb = cm._srgb_profile_bytes()
        self.assertIsNone(cm.srgb_to_display_transform(srgb, config))

    def test_wide_gamut_profile_builds_transform_and_changes_pixels(self) -> None:
        # Реальный профиль монитора (Adobe RGB) даёт непустой transform, и
        # насыщенный цвет заметно меняется при пересчёте sRGB → монитор.
        config = cm.ColorManagementConfig(enabled=True)
        transform = cm.srgb_to_display_transform(_adobe_rgb_profile(), config)
        self.assertIsNotNone(transform)
        image = QImage(16, 16, QImage.Format.Format_RGB888)
        image.fill(qRgb(220, 30, 30))
        result = cm.apply_transform_to_qimage(image, transform)
        self.assertNotEqual(
            image.pixelColor(0, 0).getRgb(), result.pixelColor(0, 0).getRgb()
        )


class DescribeProfileTest(unittest.TestCase):
    def test_returns_empty_for_missing(self) -> None:
        self.assertEqual(cm.describe_profile(None), "")
        self.assertEqual(cm.describe_profile(b""), "")

    def test_reads_profile_description(self) -> None:
        self.assertIn("RGB", cm.describe_profile(_adobe_rgb_profile()))


class ApplyTransformTest(unittest.TestCase):
    def test_none_transform_returns_same_image(self) -> None:
        image = _random_qimage(8, 8)
        self.assertIs(cm.apply_transform_to_qimage(image, None), image)

    def test_preserves_size_and_dpr(self) -> None:
        image = _random_qimage(64, 48)
        image.setDevicePixelRatio(2.0)
        result = cm.apply_transform_to_qimage(image, _identity_transform())
        self.assertEqual((result.width(), result.height()), (64, 48))
        self.assertEqual(result.devicePixelRatio(), 2.0)

    def test_multithreaded_matches_single_strip(self) -> None:
        # Изображение выше порога полос: результат обязан совпасть с однопоточным.
        transform = _identity_transform()
        image = _random_qimage(200, cm._MIN_ROWS_PER_STRIP * 3)
        rgb = image.convertToFormat(QImage.Format.Format_RGB888)
        single = cm._transform_strip(
            bytes(rgb.constBits()), rgb.width(), rgb.height(), rgb.bytesPerLine(), transform
        )
        threaded = cm.apply_transform_to_qimage(image, transform)
        self.assertEqual(bytes(threaded.constBits()), single)


if __name__ == "__main__":
    unittest.main()
