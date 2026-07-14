"""Tests for per-photo AI (embeddings + faces) exchange with ShotSync.

Covers the client side of cases 1 and 2:

* upload attaches cached AI and computes only what is missing (case 1);
* download seeds the folder cache with the server's AI results (case 2).

Both use in-memory cache stand-ins so no QtGui/libGL or real models are needed.
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rawww.shotsync_selection import SelectionDownloader  # noqa: E402
from rawww.shotsync_upload import _AiAttacher  # noqa: E402


class _FakeAiCache:
    def __init__(self, embeddings=None, faces=None):
        self._embeddings = dict(embeddings or {})
        self._faces = dict(faces or {})
        self.stored_embeddings: list = []
        self.stored_faces: list = []

    def load_image_embeddings(self):
        return dict(self._embeddings)

    def load_face_analysis(self):
        return dict(self._faces)

    def store_image_embeddings(self, results):
        self.stored_embeddings.extend(results)

    def store_face_analysis(self, results):
        self.stored_faces.extend(results)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AiAttacherTests(unittest.TestCase):
    def test_uses_cached_values_without_computing(self):
        cache = _FakeAiCache(embeddings={"a.jpg": b"EMB"}, faces={"a.jpg": "[]"})

        def fail(_sources):
            raise AssertionError("should not compute when cached")

        attacher = _AiAttacher(cache, _NoLock(), embed_fn=fail, faces_fn=fail)
        embedding, faces_json = attacher.resolve(Path("/imgs/a.jpg"), b"preview")
        self.assertEqual(embedding, b"EMB")
        self.assertEqual(faces_json, "[]")
        self.assertEqual(cache.stored_embeddings, [])
        self.assertEqual(cache.stored_faces, [])

    def test_computes_missing_and_writes_back_to_cache(self):
        cache = _FakeAiCache()
        faces = [{"bbox": {"x": 0, "y": 0, "width": 1, "height": 1}, "embedding": [0.1], "confidence": 0.9}]

        def embed_fn(sources):
            return [(sources[0][0], b"NEW")]

        def faces_fn(sources):
            return [(sources[0][0], json.dumps(faces))]

        attacher = _AiAttacher(cache, _NoLock(), embed_fn=embed_fn, faces_fn=faces_fn)
        embedding, faces_json = attacher.resolve(Path("/imgs/a.jpg"), b"preview")
        self.assertEqual(embedding, b"NEW")
        self.assertEqual(json.loads(faces_json), faces)
        self.assertEqual(cache.stored_embeddings, [("/imgs/a.jpg", b"NEW")])
        self.assertEqual(cache.stored_faces, [("/imgs/a.jpg", json.dumps(faces))])

    def test_empty_faces_result_is_authoritative(self):
        cache = _FakeAiCache()

        attacher = _AiAttacher(
            cache, _NoLock(),
            embed_fn=lambda sources: [(sources[0][0], b"E")],
            faces_fn=lambda sources: [(sources[0][0], "[]")],
        )
        _embedding, faces_json = attacher.resolve(Path("/imgs/a.jpg"), b"preview")
        self.assertEqual(faces_json, "[]")
        self.assertEqual(cache.stored_faces, [("/imgs/a.jpg", "[]")])

    def test_face_failure_leaves_faces_unknown(self):
        cache = _FakeAiCache(embeddings={"a.jpg": b"E"})

        attacher = _AiAttacher(
            cache, _NoLock(),
            embed_fn=lambda sources: [],
            faces_fn=lambda sources: [],  # nothing returned -> detection failed
        )
        embedding, faces_json = attacher.resolve(Path("/imgs/a.jpg"), b"preview")
        self.assertEqual(embedding, b"E")
        self.assertIsNone(faces_json)
        self.assertEqual(cache.stored_faces, [])


class _RecordingCache:
    def __init__(self):
        self.embeddings: list = []
        self.faces: list = []

    def store_image_embeddings(self, results):
        self.embeddings.extend(results)

    def store_face_analysis(self, results):
        self.faces.extend(results)


class StoreServerAiTests(unittest.TestCase):
    def test_seeds_embedding_and_faces_from_server(self):
        cache = _RecordingCache()
        folder = Path("/local")
        faces = [{"bbox": {}, "embedding": [0.1], "confidence": 0.8}]
        entries = [("a.jpg", base64.b64encode(b"E").decode("ascii"), faces)]
        SelectionDownloader._store_server_ai(cache, folder, entries)
        self.assertEqual(cache.embeddings, [(str(folder / "a.jpg"), b"E")])
        self.assertEqual(cache.faces, [(str(folder / "a.jpg"), json.dumps(faces, separators=(",", ":")))])

    def test_empty_faces_kept_when_embedding_present(self):
        cache = _RecordingCache()
        folder = Path("/local")
        entries = [("a.jpg", base64.b64encode(b"E").decode("ascii"), [])]
        SelectionDownloader._store_server_ai(cache, folder, entries)
        self.assertEqual(cache.faces, [(str(folder / "a.jpg"), "[]")])

    def test_no_ai_data_is_not_stored(self):
        cache = _RecordingCache()
        entries = [("a.jpg", "", [])]
        SelectionDownloader._store_server_ai(cache, Path("/local"), entries)
        self.assertEqual(cache.embeddings, [])
        self.assertEqual(cache.faces, [])


if __name__ == "__main__":
    unittest.main()
