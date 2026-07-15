## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Синхронизация локальных наборов лиц с данными ShotSync."""

from __future__ import annotations

import json
import math
from hashlib import sha1

DUPLICATE_SIMILARITY = 0.9


def local_face_id(embedding: list) -> str:
    """Строит стабильный 12-символьный идентификатор из эмбеддинга лица."""
    return sha1(json.dumps(embedding).encode()).hexdigest()[:12]


def face_similarity(left: list, right: list) -> float:
    """Возвращает косинусное сходство эмбеддингов или 0 для пустого вектора."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def clean_auto_mark(value: object) -> dict:
    if not isinstance(value, dict):
        return {"kind": "", "value": ""}
    return {
        "kind": str(value.get("kind") or ""),
        "value": str(value.get("value") or ""),
    }


def server_face_to_local(face: dict, previous: dict | None = None) -> dict:
    """Преобразует серверную запись ``AccountFace`` в локальный набор лица.

    Если передан ``previous``, его локальные ID и аватар сохраняются. Благодаря
    этому после синхронизации виджет не меняет личность и не мигает превью.
    """
    embedding = [float(value) for value in (face.get("embedding") or [])]
    previous = previous or {}
    return {
        "id": str(previous.get("id") or local_face_id(embedding)),
        "server_id": int(face.get("id")),
        "name": (str(face.get("name") or "").strip() or "Без имени"),
        "embedding": embedding,
        "avatar": str(previous.get("avatar") or ""),
        "auto_mark": clean_auto_mark(face.get("auto_mark")),
    }


def merge_server_faces(
    local_sets: list[dict], server_faces: list[dict]
) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Объединяет локальную библиотеку лиц с актуальным списком сервера.

    Возвращает ``(merged, to_push, previews)``. В ``merged`` серверные записи
    считаются основными, но уже загруженные локальные аватары сохраняются.
    ``to_push`` содержит локальные наборы, которых ещё нет на сервере, а
    ``previews`` — пары ``(local_id, photo_url)`` для недостающих аватаров.
    Локальные данные не исчезают до успешной отправки: синхронизация должна
    объединять библиотеку, а не играть в рулетку с лицами пользователей.
    """
    previous_by_server_id = {
        int(entry["server_id"]): entry
        for entry in local_sets
        if isinstance(entry, dict) and entry.get("server_id")
    }

    merged: list[dict] = []
    previews: list[tuple[str, str]] = []
    for face in server_faces:
        if not isinstance(face, dict) or face.get("id") is None:
            continue
        previous = previous_by_server_id.get(int(face["id"]))
        entry = server_face_to_local(face, previous)
        merged.append(entry)
        if not entry["avatar"] and face.get("photo_url"):
            previews.append((entry["id"], str(face["photo_url"])))

    server_embeddings = [entry["embedding"] for entry in merged]
    to_push: list[dict] = []
    for entry in local_sets:
        if not isinstance(entry, dict) or entry.get("server_id"):
            continue
        embedding = entry.get("embedding") or []
        if any(
            face_similarity(embedding, other) >= DUPLICATE_SIMILARITY
            for other in server_embeddings
        ):
            continue
        merged.append(entry)
        to_push.append(entry)
    return merged, to_push, previews


def upload_fields_for_entry(entry: dict) -> dict[str, str]:
    """Готовит текстовые поля для ``POST /api/users/faces/upload/``."""
    fields = {
        "embedding": json.dumps(entry.get("embedding") or [], separators=(",", ":")),
        "name": str(entry.get("name") or ""),
    }
    bbox = entry.get("bbox")
    if isinstance(bbox, dict) and bbox:
        fields["bbox"] = json.dumps(bbox, separators=(",", ":"))
    return fields
