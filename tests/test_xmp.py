## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

import tempfile
import unittest
from pathlib import Path

from rawww.xmp import (
    XmpChangedError,
    XmpFields,
    XmpParseError,
    build_xmp,
    merge_xmp_fields,
    read_sidecar,
    sidecar_path,
    update_sidecar,
    write_sidecar,
)


class XmpTests(unittest.TestCase):
    """Проверяет построение и атомарную запись XMP-файлов."""

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

    def test_round_trip_preserves_unknown_lightroom_metadata(self) -> None:
        source = """<?xpacket begin=""?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      xmp:Rating="2" crs:Exposure2012="0.75" />
  </rdf:RDF>
</x:xmpmeta><?xpacket end="w"?>"""
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "image.CR3"
            target = sidecar_path(photo)
            target.write_text(source, encoding="utf-8")
            before = read_sidecar(photo)

            update_sidecar(
                photo,
                XmpFields(5, "green", "Отбор", ("портрет",)),
                expected_digest=before.digest,
            )

            payload = target.read_text(encoding="utf-8")
            after = read_sidecar(photo)
            self.assertIn("Exposure2012", payload)
            self.assertIn("0.75", payload)
            self.assertEqual(after.fields, XmpFields(5, "green", "Отбор", ("портрет",)))

    def test_three_way_merge_combines_independent_changes(self) -> None:
        base = XmpFields(3, "red", "", ())
        local = XmpFields(5, "red", "", ())
        external = XmpFields(3, "green", "", ())

        result = merge_xmp_fields(base, local, external)

        self.assertEqual(result.fields, XmpFields(5, "green", "", ()))
        self.assertEqual(result.conflicts, ())

    def test_three_way_merge_reports_same_field_conflict(self) -> None:
        result = merge_xmp_fields(
            XmpFields(comment="Было"),
            XmpFields(comment="Локально"),
            XmpFields(comment="Снаружи"),
        )

        self.assertEqual(result.fields.comment, "Локально")
        self.assertEqual(result.conflicts[0].field, "comment")

    def test_conditional_write_rejects_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "image.NEF"
            target = sidecar_path(photo)
            target.write_text(build_xmp({"rating": 2}, [], {}), encoding="utf-8")
            snapshot = read_sidecar(photo)
            target.write_text(build_xmp({"rating": 4}, [], {}), encoding="utf-8")

            with self.assertRaises(XmpChangedError):
                update_sidecar(photo, XmpFields(rating=5), expected_digest=snapshot.digest)

            self.assertEqual(read_sidecar(photo).fields.rating, 4)

    def test_malformed_xmp_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "image.NEF"
            target = sidecar_path(photo)
            target.write_text("<broken", encoding="utf-8")

            with self.assertRaises(XmpParseError):
                update_sidecar(photo, XmpFields(rating=5))

            self.assertEqual(target.read_text(encoding="utf-8"), "<broken")

    def test_raw_and_jpeg_resolve_to_one_sidecar(self) -> None:
        folder = Path("shoot")
        self.assertEqual(sidecar_path(folder / "IMG_1.CR3"), sidecar_path(folder / "IMG_1.JPG"))


if __name__ == "__main__":
    unittest.main()
