## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Создание XMP-файлов с метками и описанием фотографии."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Iterable


_CODE_RE = re.compile(r"(?:\{([^}]+)\}|\\([^\\]+)\\|=([^=]+)=|@(\w+))")
_TAG_RE = re.compile(r"#(\w+)")


def sidecar_path(photo_path: Path) -> Path:
    """Возвращает стандартный путь XMP: ``photo.NEF`` → ``photo.xmp``."""
    return photo_path.with_suffix(".xmp")


def extract_tags(text: str) -> list[str]:
    return list(dict.fromkeys(_TAG_RE.findall(text or "")))


def expand_comment(text: str, replacements: dict[str, str]) -> str:
    """Подставляет коды ShotSync и убирает хэштеги из текста описания."""
    expanded = _CODE_RE.sub(
        lambda match: replacements.get(next(value for value in match.groups() if value is not None), match.group(0)),
        text or "",
    )
    return re.sub(r"#\w+\s*", "", expanded).strip()


def named_face_regions(detail: dict, face_sets: Iterable[dict], *, threshold: float = 0.42) -> list[dict]:
    """Сопоставляет найденные лица с людьми и переводит рамки к центру XMP."""
    people = [person for person in face_sets if str(person.get("name") or "").strip()]
    regions: list[dict] = []
    for face in detail.get("faces") or []:
        if not isinstance(face, dict):
            continue
        embedding = face.get("embedding")
        bbox = face.get("bbox") or {}
        if not isinstance(embedding, list) or not isinstance(bbox, dict):
            continue
        person = next((item for item in people if _similarity(embedding, item.get("embedding")) >= threshold), None)
        if person is None:
            continue
        try:
            x, y = float(bbox["x"]), float(bbox["y"])
            width, height = float(bbox["width"]), float(bbox["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        regions.append({"name": str(person["name"]).strip(), "x": x + width / 2, "y": y + height / 2, "width": width, "height": height})
    return regions


def build_xmp(detail: dict, face_sets: Iterable[dict], replacements: dict[str, str]) -> str | None:
    """Строит совместимый XMP-пакет или возвращает ``None`` для пустых данных."""
    rating = detail.get("rating")
    color_label = str(detail.get("color_label") or "").strip()
    raw_comment = str(detail.get("comment") or "").strip()
    comment = expand_comment(raw_comment, replacements)
    tags = extract_tags(raw_comment)
    regions = named_face_regions(detail, face_sets)
    names = list(dict.fromkeys(region["name"] for region in regions))
    subjects = list(dict.fromkeys([*names, *tags]))
    try:
        normalized_rating = int(rating) if rating is not None else None
    except (TypeError, ValueError):
        normalized_rating = None
    rating_block = f"<xmp:Rating>{normalized_rating}</xmp:Rating>" if normalized_rating and normalized_rating > 0 else ""
    label_block = f"<xmp:Label>{escape(color_label)}</xmp:Label>" if color_label else ""
    description_block = f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(comment)}</rdf:li></rdf:Alt></dc:description>' if comment else ""
    subject_block = "<dc:subject><rdf:Bag>" + "".join(f"<rdf:li>{escape(subject)}</rdf:li>" for subject in subjects) + "</rdf:Bag></dc:subject>" if subjects else ""
    regions_block = "<mwg-rs:Regions><rdf:Bag>" + "".join(
        '<rdf:li rdf:parseType="Resource">'
        f'<mwg-rs:Name>{escape(region["name"])}</mwg-rs:Name><mwg-rs:Type>Face</mwg-rs:Type>'
        f'<mwg-rs:Area stArea:x="{region["x"]:.6f}" stArea:y="{region["y"]:.6f}" stArea:w="{region["width"]:.6f}" stArea:h="{region["height"]:.6f}" stArea:unit="normalized"/>'
        "</rdf:li>" for region in regions
    ) + "</rdf:Bag></mwg-rs:Regions>" if regions else ""
    blocks = (rating_block, label_block, description_block, subject_block, regions_block)
    if not any(blocks):
        return None
    return "\n".join(line for line in (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>',
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
        '    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/" xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#" rdf:about="">',
        *(f"      {block}" for block in blocks if block),
        "    </rdf:Description>", "  </rdf:RDF>", "</x:xmpmeta>", '<?xpacket end="w"?>',
    ) if line.strip())


def write_sidecar(photo_path: Path, xmp: str | None) -> Path | None:
    """Атомарно обновляет sidecar-файл или удаляет его при пустых метаданных."""
    target = sidecar_path(photo_path)
    if xmp is None:
        target.unlink(missing_ok=True)
        return None
    temporary = target.with_suffix(".xmp.tmp")
    temporary.write_text(xmp, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _similarity(left: object, right: object) -> float:
    if not isinstance(left, list) or not isinstance(right, list) or not left or len(left) != len(right):
        return -1.0
    try:
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = sum(float(a) ** 2 for a in left) ** 0.5
        right_norm = sum(float(b) ** 2 for b in right) ** 0.5
    except (TypeError, ValueError):
        return -1.0
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0
