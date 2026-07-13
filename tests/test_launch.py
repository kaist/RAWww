from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rawww.launch import target_from_argv


class LaunchTargetTests(unittest.TestCase):
    def test_returns_existing_file_or_folder(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            image = folder / "image.jpg"
            image.touch()

            self.assertEqual(target_from_argv([str(image)]), image.resolve())
            self.assertEqual(target_from_argv([str(folder)]), folder.resolve())

    def test_ignores_options_and_missing_paths(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(target_from_argv(["--platform", str(folder)]), folder.resolve())
            self.assertIsNone(target_from_argv([str(folder / "missing")]))

