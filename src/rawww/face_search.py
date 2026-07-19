## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Точный и ограниченный по памяти поиск одного человека по эмбеддингам лиц."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event

import numpy as np
from threadpoolctl import threadpool_limits


FACE_MATCH_THRESHOLD = 0.45
SEARCH_CANDIDATE_BLOCK = 2048
SUBCENTER_COUNT = 2
SUBCENTER_MIN_CORE = 12
SUBCENTER_REFINEMENTS = 3
SUBCENTER_FIT_STEPS = 16
SUBCENTER_CONVERGENCE = 1e-5


@dataclass(frozen=True)
class FaceSearchIndex:
    """Хранит нормализованные лица и их качество для поиска в открытой папке."""

    names: tuple[str, ...]
    embeddings: np.ndarray
    qualities: np.ndarray

    @classmethod
    def from_details(cls, details: Mapping[str, dict], embedding_size: int) -> FaceSearchIndex:
        """Строит индекс один раз, отбрасывая повреждённые векторы кэша."""
        names: list[str] = []
        vectors: list[object] = []
        qualities: list[float] = []
        for name, detail in details.items():
            for face in detail.get("faces") or []:
                if not isinstance(face, dict):
                    continue
                embedding = face.get("embedding")
                if embedding is None or len(embedding) != embedding_size:
                    continue
                names.append(name)
                vectors.append(embedding)
                qualities.append(_face_quality(face))
        if not vectors:
            return cls(
                (),
                np.empty((0, embedding_size), dtype=np.float32),
                np.empty(0, dtype=np.float32),
            )
        try:
            matrix = np.asarray(vectors, dtype=np.float32)
        except (TypeError, ValueError):
            valid_names = []
            valid_vectors = []
            valid_qualities = []
            for name, vector, quality in zip(names, vectors, qualities):
                try:
                    converted = np.asarray(vector, dtype=np.float32)
                except (TypeError, ValueError):
                    continue
                if np.all(np.isfinite(converted)):
                    valid_names.append(name)
                    valid_vectors.append(converted)
                    valid_qualities.append(quality)
            names = valid_names
            qualities = valid_qualities
            matrix = (
                np.stack(valid_vectors)
                if valid_vectors
                else np.empty((0, embedding_size), dtype=np.float32)
            )
        if not matrix.size:
            return cls((), matrix, np.empty(0, dtype=np.float32))
        norms = np.linalg.norm(matrix, axis=1)
        valid = np.isfinite(norms) & (norms > 0)
        matrix = np.ascontiguousarray(matrix[valid])
        matrix /= norms[valid, None]
        valid_names = [name for name, keep in zip(names, valid) if keep]
        quality_array = np.asarray(
            [quality for quality, keep in zip(qualities, valid) if keep],
            dtype=np.float32,
        )
        return cls(tuple(valid_names), matrix, quality_array)

    def matching_names(
        self,
        reference: list[float],
        *,
        threshold: float = FACE_MATCH_THRESHOLD,
        cancelled: Event | None = None,
    ) -> set[str]:
        """Автоматически строит несколько ракурсных центров и возвращает их фото."""
        reference_vector = np.asarray(reference, dtype=np.float32)
        reference_norm = float(np.linalg.norm(reference_vector))
        if (
            not reference_vector.size
            or not reference_norm
            or self.embeddings.shape[1:] != (reference_vector.size,)
        ):
            return set()
        reference_vector /= reference_norm

        # OpenBLAS в поставке NumPy способен занять до 24 потоков. Для этой
        # короткой фоновой задачи два потока быстрее в восприятии и не создают
        # по рабочему буферу на каждое ядро, оставляя Qt ресурсы для анимации.
        with threadpool_limits(limits=2, user_api="blas"):
            scores = self._maximum_scores(reference_vector.reshape(1, -1), cancelled)
            if scores is None:
                return set()
            matched = self._one_face_per_photo(scores, threshold)
            if np.count_nonzero(matched) < SUBCENTER_MIN_CORE:
                return {self.names[index] for index in np.flatnonzero(matched)}

            centers: np.ndarray | None = None
            previous = None
            for _iteration in range(SUBCENTER_REFINEMENTS):
                if cancelled is not None and cancelled.is_set():
                    return set()
                centers = self._fit_subcenters(
                    matched,
                    reference_vector,
                    centers,
                    cancelled,
                )
                if centers is None:
                    return set()
                scores = self._maximum_scores(centers, cancelled)
                if scores is None:
                    return set()
                next_matched = self._one_face_per_photo(scores, threshold)
                signature = next_matched.tobytes()
                matched = next_matched
                if signature == previous:
                    break
                previous = signature
        return {self.names[index] for index in np.flatnonzero(matched)}

    def _maximum_scores(
        self,
        centers: np.ndarray,
        cancelled: Event | None,
    ) -> np.ndarray | None:
        """Считает лучший центр блочно, не создавая матрицу «все лица × все центры»."""
        scores = np.full(len(self.names), -1.0, dtype=np.float32)
        for start in range(0, len(self.names), SEARCH_CANDIDATE_BLOCK):
            if cancelled is not None and cancelled.is_set():
                return None
            stop = min(len(self.names), start + SEARCH_CANDIDATE_BLOCK)
            scores[start:stop] = np.max(
                self.embeddings[start:stop] @ centers.T,
                axis=1,
            )
        return scores

    def _one_face_per_photo(self, scores: np.ndarray, threshold: float) -> np.ndarray:
        """Оставляет в кадре одно лучшее лицо: один человек не может стоять там дважды."""
        best_by_name: dict[str, int] = {}
        for index in np.flatnonzero(scores >= threshold):
            row = int(index)
            previous = best_by_name.get(self.names[row])
            if previous is None or scores[row] > scores[previous]:
                best_by_name[self.names[row]] = row
        matched = np.zeros(len(self.names), dtype=bool)
        if best_by_name:
            matched[np.fromiter(best_by_name.values(), dtype=np.intp)] = True
        return matched

    def _fit_subcenters(
        self,
        matched: np.ndarray,
        reference: np.ndarray,
        previous_centers: np.ndarray | None,
        cancelled: Event | None,
    ) -> np.ndarray | None:
        """Строит два взвешенных сферических центра ракурсов без роста их числа."""
        rows = np.flatnonzero(matched)
        vectors = self.embeddings[rows]
        weights = self.qualities[rows]
        if previous_centers is None:
            distance = 1.0 - vectors @ reference
            second = int(np.argmax(distance * (0.5 + 0.5 * weights)))
            centers = np.vstack((reference, vectors[second])).astype(np.float32)
        else:
            centers = previous_centers.copy()

        for _step in range(SUBCENTER_FIT_STEPS):
            if cancelled is not None and cancelled.is_set():
                return None
            labels = np.argmax(vectors @ centers.T, axis=1)
            updated = []
            for center_index in range(SUBCENTER_COUNT):
                members = vectors[labels == center_index]
                member_weights = weights[labels == center_index]
                if not len(members):
                    updated.append(centers[center_index])
                    continue
                center = np.sum(members * member_weights[:, None], axis=0)
                norm = float(np.linalg.norm(center))
                updated.append(center / norm if norm else centers[center_index])
            next_centers = np.ascontiguousarray(updated, dtype=np.float32)
            if float(np.max(np.abs(next_centers - centers))) < SUBCENTER_CONVERGENCE:
                return next_centers
            centers = next_centers
        return centers


def _face_quality(face: dict) -> float:
    """Оценивает пригодность лица для центра по детектору и размеру рамки."""
    try:
        confidence = float(face.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.05, min(1.0, confidence))
    bbox = face.get("bbox") or {}
    if not isinstance(bbox, dict):
        return confidence
    try:
        area = max(0.0, float(bbox.get("width", 0.0))) * max(
            0.0, float(bbox.get("height", 0.0))
        )
    except (TypeError, ValueError):
        area = 0.0
    if not area:
        return confidence
    size_quality = max(0.25, min(1.0, area**0.5 / 0.2))
    return confidence * size_quality


def indexed_face_matches(
    details: Mapping[str, dict] | None,
    reference: list[float],
    index: FaceSearchIndex | None = None,
    cancelled: Event | None = None,
) -> tuple[FaceSearchIndex, set[str]]:
    """Строит индекс при первом запросе и возвращает его вместе с совпадениями."""
    search_index = index or FaceSearchIndex.from_details(details or {}, len(reference))
    return search_index, search_index.matching_names(reference, cancelled=cancelled)


def matching_face_names(
    details: Mapping[str, dict],
    reference: list[float],
    *,
    threshold: float = FACE_MATCH_THRESHOLD,
) -> set[str]:
    """Совместимый функциональный интерфейс для тестов и пакетных сценариев."""
    index = FaceSearchIndex.from_details(details, len(reference))
    return index.matching_names(reference, threshold=threshold)
