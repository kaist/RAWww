import tempfile
import unittest
from pathlib import Path

from rawww.xmp import build_xmp, sidecar_path, write_sidecar


class XmpTests(unittest.TestCase):
    def test_build_xmp_expands_codes_tags_and_named_faces(self) -> None:
        detail = {"rating": 5, "color_label": "red", "comment": "Hello {name} #hero", "faces": [{"embedding": [1.0, 0.0], "bbox": {"x": .1, "y": .2, "width": .3, "height": .4}}]}
        xmp = build_xmp(detail, [{"name": "Anna", "embedding": [1.0, 0.0]}], {"name": "World"})
        self.assertIn("<xmp:Rating>5</xmp:Rating>", xmp)
        self.assertIn("Hello World", xmp)
        self.assertNotIn("#hero</rdf:li></rdf:Alt>", xmp)
        self.assertIn("<rdf:li>hero</rdf:li>", xmp)
        self.assertIn("<rdf:li>Anna</rdf:li>", xmp)
        self.assertIn('stArea:x="0.250000"', xmp)

    def test_empty_metadata_does_not_leave_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "image.NEF"
            target = sidecar_path(photo)
            target.write_text("old", encoding="utf-8")
            self.assertIsNone(write_sidecar(photo, build_xmp({}, [], {})))
            self.assertFalse(target.exists())

    def test_zero_rating_does_not_create_xmp(self) -> None:
        self.assertIsNone(build_xmp({"rating": 0}, [], {}))


if __name__ == "__main__":
    unittest.main()
