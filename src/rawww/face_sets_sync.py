"""Pure helpers for syncing local face sets with the ShotSync server.

The desktop keeps its "наборы лиц" (people) in ``QSettings`` as a JSON list of
``{"id", "name", "embedding", "avatar"}`` entries, where ``avatar`` is a base64
image of the face crop. The server keeps the same people as ``AccountFace``
rows returned by ``GET /api/users/faces/`` as
``{"id", "name", "embedding", "photo_url", "bbox", "auto_mark"}``.

When the user is logged in the server library is authoritative (mirroring the
code-replacements sync), so this module converts server rows into the local
entry shape while preserving avatars already stored locally, and reports which
local-only people still need to be uploaded and which previews still need to be
fetched. All functions here are side-effect free so they can be unit tested
without Qt or the network.
"""

from __future__ import annotations

import json
import math
from hashlib import sha1

# A local-only person is considered "already on the server" (so it is not
# uploaded again) when its embedding matches a server one this closely.
DUPLICATE_SIMILARITY = 0.9


def local_face_id(embedding: list) -> str:
    """Stable 12-char id derived from an embedding (matches app.py)."""
    return sha1(json.dumps(embedding).encode()).hexdigest()[:12]


def face_similarity(left: list, right: list) -> float:
    """Cosine similarity of two embeddings; 0.0 when either is empty."""
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
    """Convert one server ``AccountFace`` payload to a local face-set entry.

    ``previous`` is the matching local entry (if any); its ``avatar`` and ``id``
    are preserved so the on-screen preview and widget identity stay stable.
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
    """Reconcile the local library with the server list.

    Returns ``(merged, to_push, previews)``:

    * ``merged`` — the new local library. Server people come first (authoritative
      when logged in), keeping any avatar already stored locally. Local-only
      people that the server does not have yet are appended so nothing is lost
      before their upload finishes.
    * ``to_push`` — local-only people (no ``server_id`` and not matching any
      server embedding) that should be uploaded to the server.
    * ``previews`` — ``(local_id, photo_url)`` for server people whose avatar is
      not available locally yet and must be downloaded.
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
    """Build the multipart text fields for ``POST /api/users/faces/upload/``."""
    fields = {
        "embedding": json.dumps(entry.get("embedding") or [], separators=(",", ":")),
        "name": str(entry.get("name") or ""),
    }
    bbox = entry.get("bbox")
    if isinstance(bbox, dict) and bbox:
        fields["bbox"] = json.dumps(bbox, separators=(",", ":"))
    return fields
