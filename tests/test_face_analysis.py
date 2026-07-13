from __future__ import annotations

from io import BytesIO
import json
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from rawww.ai import recognize_face_batch
from rawww.face_analysis import FACE_TEMPLATE, Face, _aligned_face, _nms


class FaceAnalysisTests(unittest.TestCase):
    def test_nms_keeps_separate_detections(self) -> None:
        detections = np.array([
            [0, 0, 10, 10, 0.9],
            [1, 1, 11, 11, 0.8],
            [30, 30, 40, 40, 0.7],
        ], dtype=np.float32)
        self.assertEqual(_nms(detections).tolist(), [0, 2])

    def test_face_alignment_maps_landmarks_to_the_model_template(self) -> None:
        image = Image.new("RGB", (224, 224))
        source = FACE_TEMPLATE * 1.5 + np.array([20, 30], dtype=np.float32)
        aligned = _aligned_face(image, source)
        self.assertEqual(aligned.size, (112, 112))

    def test_face_batch_serializes_embedding_and_confidence(self) -> None:
        source = BytesIO()
        Image.new("RGB", (20, 20)).save(source, "JPEG")
        face = Face(
            bbox=np.array([1, 2, 11, 12], dtype=np.float32),
            landmarks=np.zeros((5, 2), dtype=np.float32),
            confidence=0.9,
            embedding=np.array([0.1234567, -0.5], dtype=np.float32),
        )

        with patch("rawww.ai.recognize", return_value=[face]):
            results = recognize_face_batch([("sample.jpg", source.getvalue())])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "sample.jpg")
        record = json.loads(results[0][1])[0]
        self.assertEqual(record["embedding"], [0.123457, -0.5])
        self.assertEqual(record["confidence"], 0.9)
