## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки устойчивого поиска фотографий одного человека."""

import unittest
import math
from threading import Event

import numpy as np

from rawww.face_search import FaceSearchIndex, matching_face_names


class FaceSearchTests(unittest.TestCase):
    """Проверяет независимость выдачи от выбранного кадра одной группы."""

    def test_connected_prototypes_give_same_complete_result(self) -> None:
        angles = (0, 25, 50, 72)
        details = {}
        for angle in angles:
            radians = math.radians(angle)
            embedding = [math.cos(radians), math.sin(radians), 0.0]
            for index in range(15):
                details[f"person-{angle}-{index}.jpg"] = {
                    "faces": [{"embedding": embedding}]
                }
        details["other.jpg"] = {"faces": [{"embedding": [0.0, 0.0, 1.0]}]}

        from_first = matching_face_names(details, [1.0, 0.0, 0.0])
        last = [math.cos(math.radians(72)), math.sin(math.radians(72)), 0.0]
        from_last = matching_face_names(details, last)

        self.assertEqual(len(from_first), 60)
        self.assertNotIn("other.jpg", from_first)
        self.assertEqual(from_last, from_first)

    def test_transitive_bridge_cannot_drift_away_from_reference(self) -> None:
        details = {
            "source.jpg": {"faces": [{"embedding": [1.0, 0.0]}]},
            "bridge.jpg": {"faces": [{"embedding": [0.8, 0.6]}]},
            "stranger.jpg": {"faces": [{"embedding": [0.28, 0.96]}]},
        }

        matches = matching_face_names(details, [1.0, 0.0])

        self.assertEqual(matches, {"source.jpg", "bridge.jpg"})

    def test_subcenter_refinement_is_bounded_on_a_long_dense_chain(self) -> None:
        details = {}
        for angle in range(0, 181, 20):
            radians = math.radians(angle)
            embedding = [math.cos(radians), math.sin(radians)]
            for index in range(15):
                details[f"chain-{angle}-{index}.jpg"] = {
                    "faces": [{"embedding": embedding}]
                }

        matches = matching_face_names(details, [1.0, 0.0])

        self.assertIn("chain-0-0.jpg", matches)
        self.assertNotIn("chain-180-0.jpg", matches)

    def test_cancelled_search_publishes_no_partial_result(self) -> None:
        details = {
            f"person-{index}.jpg": {"faces": [{"embedding": [1.0, index / 1000]}]}
            for index in range(100)
        }
        index = FaceSearchIndex.from_details(details, 2)
        cancelled = Event()
        cancelled.set()

        self.assertEqual(index.matching_names([1.0, 0.0], cancelled=cancelled), set())

    def test_any_matching_face_in_photo_includes_it_once(self) -> None:
        details = {
            "group.jpg": {"faces": [
                {"embedding": [0.0, 1.0]},
                {"embedding": [1.0, 0.0]},
            ]},
        }
        self.assertEqual(matching_face_names(details, [1.0, 0.0]), {"group.jpg"})

    def test_wide_transitive_level_is_processed_as_one_group(self) -> None:
        details = {
            f"person-{index}.jpg": {"faces": [{"embedding": [1.0, index / 10_000]}]}
            for index in range(300)
        }
        details["other.jpg"] = {"faces": [{"embedding": [0.0, 1.0]}]}

        matches = matching_face_names(details, [1.0, 0.0])

        self.assertEqual(len(matches), 300)
        self.assertNotIn("other.jpg", matches)

    def test_index_does_not_change_detail_embeddings(self) -> None:
        details = {"photo.jpg": {"faces": [{"embedding": [1.0, 0.0]}]}}
        original = details["photo.jpg"]["faces"][0]["embedding"]

        index = FaceSearchIndex.from_details(details, 2)

        self.assertIs(details["photo.jpg"]["faces"][0]["embedding"], original)
        self.assertIsInstance(original, list)
        self.assertEqual(index.embeddings.dtype, np.float32)
        self.assertEqual(index.qualities.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
