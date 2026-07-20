## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from io import BytesIO
import json
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from rawww.ai import recognize_face_batch
from rawww.face_analysis import FACE_TEMPLATE, Face, _aligned_face, _classify_eyes, _nms, recognize


class FaceAnalysisTests(unittest.TestCase):
    """Проверяет подготовку изображения, детектор и эмбеддинги лиц."""

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

    def test_face_batch_serializes_eyes_open_when_available(self) -> None:
        source = BytesIO()
        Image.new("RGB", (20, 20)).save(source, "JPEG")
        face = Face(
            bbox=np.array([1, 2, 11, 12], dtype=np.float32),
            landmarks=np.zeros((5, 2), dtype=np.float32),
            confidence=0.9,
            embedding=np.array([0.1, -0.5], dtype=np.float32),
            eyes_open=0.123456,
        )

        with patch("rawww.ai.recognize", return_value=[face]):
            results = recognize_face_batch([("sample.jpg", source.getvalue())])

        record = json.loads(results[0][1])[0]
        self.assertEqual(record["eyes_open"], 0.1235)

    def test_classify_eyes_returns_probability_per_face_from_face_crops(self) -> None:
        image = Image.new("RGB", (224, 224))
        boxes = np.array([[1, 2, 60, 62], [70, 71, 130, 131]], dtype=np.float32)

        class Session:
            """Заглушка модели глаз: закрыт=[5,0], открыт=[0,5] по softmax."""

            def get_inputs(self):
                return [type("Input", (), {"name": "pixel_values"})()]

            def run(self, _, feeds):
                # Один кроп лица на каждое лицо: первое открыто, второе закрыто.
                logits = np.array([[0.0, 5.0], [5.0, 0.0]], dtype=np.float32)
                assert feeds["pixel_values"].shape == (2, 3, 224, 224)
                return [logits]

        with patch("rawww.face_analysis._eye_state", return_value=Session()):
            states = _classify_eyes(image, boxes)

        self.assertEqual(len(states), 2)
        self.assertGreater(states[0], 0.5)  # лицо с открытыми глазами
        self.assertLess(states[1], 0.5)  # лицо с закрытыми глазами

    def test_classify_eyes_returns_none_without_a_model(self) -> None:
        image = Image.new("RGB", (224, 224))
        boxes = np.array([[1, 2, 60, 62], [70, 71, 130, 131]], dtype=np.float32)

        with patch("rawww.face_analysis._eye_state", side_effect=RuntimeError):
            states = _classify_eyes(image, boxes)

        self.assertEqual(states, [None, None])

    def test_recognition_runs_each_face_with_the_model_batch_size(self) -> None:
        image = Image.new("RGB", (224, 224))
        boxes = np.array([[1, 2, 11, 12], [20, 21, 30, 31]], dtype=np.float32)
        landmarks = np.repeat(FACE_TEMPLATE[None], 2, axis=0)
        scores = np.array([0.9, 0.8], dtype=np.float32)

        class Session:
            """Предсказуемая заглушка ONNX без тяжёлой нейросети внутри."""

            def __init__(self) -> None:
                self.batches = []

            def get_inputs(self):
                return [type("Input", (), {"name": "input"})()]

            def run(self, _, feeds):
                batch = feeds["input"]
                self.batches.append(batch.shape)
                return [np.full((1, 512), len(self.batches), dtype=np.float32)]

        session = Session()
        with patch("rawww.face_analysis._detect", return_value=(boxes, landmarks, scores)), patch(
            "rawww.face_analysis._recognition", return_value=session
        ):
            faces = recognize(image)

        self.assertEqual(session.batches, [(1, 3, 112, 112), (1, 3, 112, 112)])
        self.assertEqual([face.embedding[0] for face in faces], [1.0, 2.0])
