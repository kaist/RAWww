from __future__ import annotations

import unittest

import numpy as np
from PIL import Image

from rawww.face_analysis import FACE_TEMPLATE, _aligned_face, _nms


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
