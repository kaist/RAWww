import unittest

from rawww.updater import is_newer, version_key


class VersionComparisonTests(unittest.TestCase):
    def test_compares_dotted_versions_with_missing_parts(self):
        self.assertTrue(is_newer("1.2.1", "1.2"))
        self.assertFalse(is_newer("1.2", "1.2.0"))
        self.assertEqual(version_key("v2.10.3"), (2, 10, 3))

    def test_rejects_non_numeric_versions(self):
        with self.assertRaises(ValueError):
            version_key("1.2-beta")
