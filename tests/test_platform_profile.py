import os
import unittest
from unittest import mock

from rawww import platform_profile


class PlatformProfileTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"RAWWW_PROFILE": "mobile"}, clear=True):
            self.assertEqual(platform_profile._detect_profile(), "mobile")
        with mock.patch.dict(os.environ, {"RAWWW_PROFILE": "desktop"}, clear=True):
            self.assertEqual(platform_profile._detect_profile(), "desktop")

    def test_android_env_marks_mobile(self) -> None:
        with mock.patch.dict(os.environ, {"ANDROID_ARGUMENT": "/data/app"}, clear=True):
            self.assertEqual(platform_profile._detect_profile(), "mobile")

    def test_defaults_to_desktop(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(platform_profile.sys, "platform", "linux"):
            if hasattr(platform_profile.sys, "getandroidapilevel"):
                self.skipTest("running on a real Android interpreter")
            self.assertEqual(platform_profile._detect_profile(), "desktop")

    def test_flags_are_consistent(self) -> None:
        # Every desktop-only feature and the process pool track IS_DESKTOP.
        self.assertEqual(platform_profile.FEATURE_AI, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.FEATURE_XMP, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.FEATURE_UTILITIES, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.FEATURE_FILESYSTEM, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.FEATURE_RAW, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.FEATURE_VIDEO, platform_profile.IS_DESKTOP)
        self.assertEqual(platform_profile.DECODE_USE_PROCESSES, platform_profile.IS_DESKTOP)
        self.assertTrue(platform_profile.FEATURE_SHOTSYNC)
        self.assertNotEqual(platform_profile.IS_MOBILE, platform_profile.IS_DESKTOP)


if __name__ == "__main__":
    unittest.main()
