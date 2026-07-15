## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки синхронизации локальных наборов лиц с ShotSync."""

from __future__ import annotations

import json
import unittest

from rawww.face_sets_sync import (
    merge_server_faces,
    server_face_to_local,
    upload_fields_for_entry,
)


def _server_face(face_id, name, embedding, photo_url="", auto_mark=None):
    return {
        "id": face_id,
        "name": name,
        "embedding": embedding,
        "photo_url": photo_url,
        "bbox": {},
        "auto_mark": auto_mark or {"kind": "", "value": ""},
    }


class ServerFaceToLocalTests(unittest.TestCase):
    """Проверяет преобразование серверного лица в локальный набор."""

    def test_converts_and_keeps_previous_avatar_and_id(self):
        previous = {"id": "keepid", "avatar": "AVATAR", "embedding": [0.1, 0.2]}
        entry = server_face_to_local(
            _server_face(7, "Иван", [0.1, 0.2], auto_mark={"kind": "rating", "value": "5"}),
            previous,
        )
        self.assertEqual(entry["id"], "keepid")
        self.assertEqual(entry["server_id"], 7)
        self.assertEqual(entry["name"], "Иван")
        self.assertEqual(entry["embedding"], [0.1, 0.2])
        self.assertEqual(entry["avatar"], "AVATAR")
        self.assertEqual(entry["auto_mark"], {"kind": "rating", "value": "5"})

    def test_blank_name_defaults(self):
        entry = server_face_to_local(_server_face(1, "  ", [0.5]))
        self.assertEqual(entry["name"], "Без имени")


class MergeServerFacesTests(unittest.TestCase):
    """Проверяет объединение серверной и локальной библиотек лиц."""

    def test_server_faces_replace_local_and_keep_local_avatar(self):
        local = [{"id": "abc", "server_id": 7, "name": "Old", "embedding": [0.1], "avatar": "AV"}]
        server = [_server_face(7, "New", [0.1], photo_url="https://x/a.jpg")]
        merged, to_push, previews = merge_server_faces(local, server)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["server_id"], 7)
        self.assertEqual(merged[0]["name"], "New")
        self.assertEqual(merged[0]["avatar"], "AV")  # локальный аватар сохранился
        self.assertEqual(to_push, [])
        self.assertEqual(previews, [])  # существующий аватар не скачиваем повторно

    def test_missing_avatar_is_queued_for_preview(self):
        local = [{"id": "abc", "server_id": 7, "name": "N", "embedding": [0.1], "avatar": ""}]
        server = [_server_face(7, "N", [0.1], photo_url="https://x/a.jpg")]
        _merged, _to_push, previews = merge_server_faces(local, server)
        self.assertEqual(previews, [("abc", "https://x/a.jpg")])

    def test_local_only_face_is_kept_and_pushed(self):
        local = [{"id": "loc", "name": "Local", "embedding": [1.0, 0.0], "avatar": "AV"}]
        server = [_server_face(7, "Other", [0.0, 1.0])]
        merged, to_push, _previews = merge_server_faces(local, server)
        ids = {entry.get("id") for entry in merged}
        self.assertEqual(ids, {"loc", server_face_to_local(server[0])["id"]})
        self.assertEqual(len(to_push), 1)
        self.assertEqual(to_push[0]["id"], "loc")

    def test_local_duplicate_of_server_face_is_not_pushed(self):
        local = [{"id": "loc", "name": "Local", "embedding": [1.0, 0.0], "avatar": ""}]
        server = [_server_face(7, "Server", [1.0, 0.0])]
        merged, to_push, _previews = merge_server_faces(local, server)
        self.assertEqual(to_push, [])
        self.assertEqual(len(merged), 1)  # после объединения остаётся серверная запись
        self.assertEqual(merged[0]["server_id"], 7)


class UploadFieldsTests(unittest.TestCase):
    """Проверяет поля формы при отправке набора лиц."""

    def test_serializes_embedding_name_and_optional_bbox(self):
        fields = upload_fields_for_entry(
            {"name": "Иван", "embedding": [0.1, 0.2], "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}}
        )
        self.assertEqual(json.loads(fields["embedding"]), [0.1, 0.2])
        self.assertEqual(fields["name"], "Иван")
        self.assertEqual(json.loads(fields["bbox"]), {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4})

    def test_bbox_omitted_when_absent(self):
        fields = upload_fields_for_entry({"name": "A", "embedding": [0.1]})
        self.assertNotIn("bbox", fields)


if __name__ == "__main__":
    unittest.main()
