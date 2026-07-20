## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Безопасное чтение, слияние и атомарное обновление XMP sidecar-файлов."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Iterable


_CODE_RE = re.compile(r"(?:\{([^}]+)\}|\\([^\\]+)\\|=([^=]+)=|@(\w+))")
_TAG_RE = re.compile(r"#(\w+)")

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
    "rawww": "https://shotsync.ru/ns/ctrlka/1.0/",
}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
CONTROLLED_FIELDS = ("rating", "color_label", "comment", "keywords")
_UNSET = object()

for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


class XmpError(RuntimeError):
    """Базовая ошибка XMP, которую можно безопасно показать пользователю."""


class XmpParseError(XmpError):
    """Sidecar содержит некорректный XML и не должен быть перезаписан."""


class XmpChangedError(XmpError):
    """Sidecar изменился между чтением и записью."""


@dataclass(frozen=True)
class XmpFields:
    """Поля отбора, которыми Контролька обменивается с другими программами."""

    rating: int | None = None
    color_label: str = ""
    comment: str = ""
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> "XmpFields":
        data = value if isinstance(value, dict) else {}
        rating = data.get("rating")
        try:
            rating = int(rating) if rating not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        if rating == 0:
            rating = None
        if rating is not None and not 1 <= rating <= 5:
            rating = None
        raw_keywords = data.get("keywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        elif not isinstance(raw_keywords, (list, tuple, set)):
            raw_keywords = []
        keywords = tuple(dict.fromkeys(
            str(item).strip() for item in raw_keywords if str(item).strip()
        ))
        color_label = str(data.get("color_label") or "").strip()
        if color_label.casefold() in {"red", "yellow", "green", "blue", "purple"}:
            color_label = color_label.casefold()
        return cls(
            rating=rating,
            color_label=color_label,
            comment=str(data.get("comment") or "").strip(),
            keywords=keywords,
        )

    @classmethod
    def from_detail(cls, detail: dict, replacements: dict[str, str] | None = None) -> "XmpFields":
        raw_comment = str(detail.get("comment") or "").strip()
        explicit = detail.get("keywords") or []
        if isinstance(explicit, str):
            explicit = [explicit]
        elif not isinstance(explicit, (list, tuple, set)):
            explicit = []
        keywords = tuple(dict.fromkeys([
            *(str(item).strip() for item in explicit if str(item).strip()),
            *extract_tags(raw_comment),
        ]))
        return cls.from_dict({
            "rating": detail.get("rating"),
            "color_label": detail.get("color_label"),
            "comment": expand_comment(raw_comment, replacements or {}),
            "keywords": keywords,
        })

    def to_dict(self) -> dict[str, object]:
        return {
            "rating": self.rating,
            "color_label": self.color_label,
            "comment": self.comment,
            "keywords": list(self.keywords),
        }

    def value(self, name: str) -> object:
        return getattr(self, name)


@dataclass(frozen=True)
class XmpConflict:
    """Одно поле, независимо изменённое локально и во внешнем редакторе."""

    field: str
    base: object
    local: object
    external: object


@dataclass(frozen=True)
class XmpMergeResult:
    """Результат трёхстороннего слияния и обнаруженные конфликты."""

    fields: XmpFields
    conflicts: tuple[XmpConflict, ...] = ()
    external_changed: tuple[str, ...] = ()
    local_changed: tuple[str, ...] = ()


@dataclass
class XmpDocument:
    """XML-дерево XMP и признак того, что sidecar создала Контролька."""

    root: ET.Element
    created_by_rawww: bool = False


@dataclass(frozen=True)
class XmpReadResult:
    """Снимок sidecar, пригодный для сравнения и условной записи."""

    path: Path
    fields: XmpFields
    digest: str | None
    size: int
    mtime_ns: int
    exists: bool


@dataclass(frozen=True)
class XmpWriteResult:
    """Итог атомарной записи sidecar."""

    path: Path
    fields: XmpFields
    digest: str | None
    size: int
    mtime_ns: int


def sidecar_path(photo_path: Path) -> Path:
    """Возвращает общий sidecar: ``photo.NEF`` и ``photo.JPG`` → ``photo.xmp``."""
    return photo_path.with_suffix(".xmp")


def extract_tags(text: str) -> list[str]:
    return list(dict.fromkeys(_TAG_RE.findall(text or "")))


def expand_comment(text: str, replacements: dict[str, str]) -> str:
    """Подставляет коды ShotSync и убирает хэштеги из описания."""
    expanded = _CODE_RE.sub(
        lambda match: replacements.get(next(value for value in match.groups() if value is not None), match.group(0)),
        text or "",
    )
    return re.sub(r"#\w+\s*", "", expanded).strip()


def merge_xmp_fields(base: XmpFields, local: XmpFields, external: XmpFields) -> XmpMergeResult:
    """Сливает поля по правилу base/local/external, не скрывая конфликтов."""
    merged: dict[str, object] = {}
    conflicts: list[XmpConflict] = []
    external_changed: list[str] = []
    local_changed: list[str] = []
    for name in CONTROLLED_FIELDS:
        base_value = base.value(name)
        local_value = local.value(name)
        external_value = external.value(name)
        local_differs = local_value != base_value
        external_differs = external_value != base_value
        if local_differs:
            local_changed.append(name)
        if external_differs:
            external_changed.append(name)
        if local_differs and external_differs and local_value != external_value:
            conflicts.append(XmpConflict(name, base_value, local_value, external_value))
            merged[name] = local_value
        elif external_differs:
            merged[name] = external_value
        else:
            merged[name] = local_value
    return XmpMergeResult(
        XmpFields.from_dict(merged), tuple(conflicts), tuple(external_changed), tuple(local_changed)
    )


def read_sidecar(photo_or_sidecar: Path) -> XmpReadResult:
    """Читает управляемые поля и отпечаток, не изменяя файл."""
    target = photo_or_sidecar if photo_or_sidecar.suffix.casefold() == ".xmp" else sidecar_path(photo_or_sidecar)
    try:
        data = target.read_bytes()
        stat = target.stat()
    except FileNotFoundError:
        return XmpReadResult(target, XmpFields(), None, 0, 0, False)
    document = parse_xmp(data)
    return XmpReadResult(
        target, extract_xmp_fields(document), hashlib.sha256(data).hexdigest(),
        len(data), stat.st_mtime_ns, True,
    )


def parse_xmp(data: bytes | str) -> XmpDocument:
    """Разбирает XMP с комментариями; внешние сущности ElementTree не загружает."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    _register_document_namespaces(payload)
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(payload, parser=parser)
    except (ET.ParseError, ValueError) as exc:
        raise XmpParseError(f"Некорректный XMP: {exc}") from exc
    marker = _q("rawww", "Creator")
    return XmpDocument(root, any(element.get(marker) == "Контролька" for element in root.iter()))


def extract_xmp_fields(document: XmpDocument) -> XmpFields:
    descriptions = _descriptions(document)
    rating = _first_property(descriptions, "xmp", "Rating")
    label = _first_property(descriptions, "xmp", "Label") or ""
    comment = ""
    keywords: list[str] = []
    for description in descriptions:
        container = description.find(_q("dc", "description"))
        if container is not None:
            alternatives = list(container.iter(_q("rdf", "li")))
            preferred = next((item for item in alternatives if item.get(XML_LANG) == "x-default"), None)
            selected = preferred if preferred is not None else (alternatives[0] if alternatives else None)
            if selected is not None:
                comment = "".join(selected.itertext()).strip()
                break
    for description in descriptions:
        container = description.find(_q("dc", "subject"))
        if container is not None:
            keywords.extend("".join(item.itertext()).strip() for item in container.iter(_q("rdf", "li")))
    return XmpFields.from_dict({
        "rating": rating,
        "color_label": label,
        "comment": comment,
        "keywords": keywords,
    })


def update_xmp_document(
    document: XmpDocument | None,
    fields: XmpFields,
    regions: Iterable[dict] = (),
) -> XmpDocument | None:
    """Обновляет только принадлежащие Контрольке поля, сохраняя остальное дерево."""
    regions = list(regions)
    if document is None and not _has_fields(fields) and not regions:
        return None
    document = document or _new_document()
    for existing in _descriptions(document):
        _remove_property(existing, "xmp", "Rating")
        _remove_property(existing, "xmp", "Label")
        _remove_property(existing, "dc", "description")
        _remove_property(existing, "dc", "subject")
    description = _ensure_description(document)
    description.set(_q("rawww", "Creator"), "Контролька")
    _replace_simple_property(description, "xmp", "Rating", fields.rating)
    _replace_simple_property(description, "xmp", "Label", fields.color_label)
    _replace_alt_property(description, "dc", "description", fields.comment)
    _replace_bag_property(description, "dc", "subject", fields.keywords)
    _merge_face_regions(description, regions)
    if not _document_has_metadata(document):
        # Чужой пустой sidecar не удаляем: оставляем минимальный пакет с маркером.
        # Созданный Контролькой файл можно убрать полностью.
        return None if document.created_by_rawww else document
    return document


def serialize_xmp(document: XmpDocument) -> bytes:
    """Сериализует дерево в UTF-8; порядок и форматирование могут измениться, данные — нет."""
    body = ET.tostring(document.root, encoding="utf-8", xml_declaration=False, short_empty_elements=True)
    return (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        + body
        + b'\n<?xpacket end="w"?>\n'
    )


def update_sidecar(
    photo_or_sidecar: Path,
    fields: XmpFields,
    regions: Iterable[dict] = (),
    *,
    expected_digest: str | None | object = _UNSET,
) -> XmpWriteResult:
    """Условно и атомарно обновляет sidecar, не перетирая внезапное внешнее изменение."""
    target = photo_or_sidecar if photo_or_sidecar.suffix.casefold() == ".xmp" else sidecar_path(photo_or_sidecar)
    existing: bytes | None
    try:
        existing = target.read_bytes()
    except FileNotFoundError:
        existing = None
    digest = hashlib.sha256(existing).hexdigest() if existing is not None else None
    if expected_digest is not _UNSET and expected_digest != digest:
        raise XmpChangedError("XMP изменился после чтения; запись остановлена")
    document = parse_xmp(existing) if existing is not None else None
    updated = update_xmp_document(document, fields, regions)
    if updated is None:
        if existing is not None and document is not None and document.created_by_rawww:
            target.unlink(missing_ok=True)
        return XmpWriteResult(target, fields, None, 0, 0)
    payload = serialize_xmp(updated)
    temporary = target.with_name(f".{target.name}.rawww-tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    stat = target.stat()
    return XmpWriteResult(
        target, fields, hashlib.sha256(payload).hexdigest(), len(payload), stat.st_mtime_ns
    )


def named_face_regions(detail: dict, face_sets: Iterable[dict], *, threshold: float = 0.42) -> list[dict]:
    """Сопоставляет найденные лица с людьми и переводит рамки к центру XMP."""
    people = [person for person in face_sets if str(person.get("name") or "").strip()]
    regions: list[dict] = []
    for face in detail.get("faces") or []:
        if not isinstance(face, dict):
            continue
        embedding = face.get("embedding")
        bbox = face.get("bbox") or {}
        if embedding is None or not isinstance(bbox, dict):
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
    """Совместимый адаптер для создания нового XMP без существующего документа."""
    fields = XmpFields.from_detail(detail, replacements)
    regions = named_face_regions(detail, face_sets)
    fields = XmpFields(
        fields.rating,
        fields.color_label,
        fields.comment,
        tuple(dict.fromkeys([*fields.keywords, *(region["name"] for region in regions)])),
    )
    document = update_xmp_document(None, fields, regions)
    return serialize_xmp(document).decode("utf-8") if document is not None else None


def write_sidecar(photo_path: Path, xmp: str | None) -> Path | None:
    """Атомарно записывает готовый пакет; оставлен для совместимости старого API."""
    target = sidecar_path(photo_path)
    if xmp is None:
        target.unlink(missing_ok=True)
        return None
    temporary = target.with_name(f".{target.name}.rawww-tmp")
    try:
        temporary.write_text(xmp, encoding="utf-8", newline="\n")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _new_document() -> XmpDocument:
    root = ET.Element(_q("x", "xmpmeta"))
    ET.SubElement(root, _q("rdf", "RDF"))
    return XmpDocument(root, True)


def _descriptions(document: XmpDocument) -> list[ET.Element]:
    return list(document.root.iter(_q("rdf", "Description")))


def _ensure_description(document: XmpDocument) -> ET.Element:
    descriptions = _descriptions(document)
    if descriptions:
        return descriptions[0]
    rdf = document.root.find(f".//{_q('rdf', 'RDF')}")
    if rdf is None:
        rdf = ET.SubElement(document.root, _q("rdf", "RDF"))
    return ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})


def _first_property(descriptions: Iterable[ET.Element], prefix: str, name: str) -> str | None:
    qname = _q(prefix, name)
    for description in descriptions:
        if qname in description.attrib:
            return str(description.attrib[qname]).strip()
        child = description.find(qname)
        if child is not None and child.text is not None:
            return child.text.strip()
    return None


def _remove_property(description: ET.Element, prefix: str, name: str) -> None:
    qname = _q(prefix, name)
    description.attrib.pop(qname, None)
    for child in list(description):
        if child.tag == qname:
            description.remove(child)


def _replace_simple_property(description: ET.Element, prefix: str, name: str, value: object) -> None:
    _remove_property(description, prefix, name)
    if value not in (None, ""):
        ET.SubElement(description, _q(prefix, name)).text = str(value)


def _replace_alt_property(description: ET.Element, prefix: str, name: str, value: str) -> None:
    _remove_property(description, prefix, name)
    if not value:
        return
    container = ET.SubElement(description, _q(prefix, name))
    alt = ET.SubElement(container, _q("rdf", "Alt"))
    ET.SubElement(alt, _q("rdf", "li"), {XML_LANG: "x-default"}).text = value


def _replace_bag_property(description: ET.Element, prefix: str, name: str, values: Iterable[str]) -> None:
    _remove_property(description, prefix, name)
    values = list(dict.fromkeys(value for value in values if value))
    if not values:
        return
    container = ET.SubElement(description, _q(prefix, name))
    bag = ET.SubElement(container, _q("rdf", "Bag"))
    for value in values:
        ET.SubElement(bag, _q("rdf", "li")).text = value


def _merge_face_regions(description: ET.Element, regions: list[dict]) -> None:
    """Добавляет отсутствующие области, не удаляя лица из внешнего редактора."""
    container = description.find(_q("mwg-rs", "Regions"))
    if container is None:
        if not regions:
            return
        container = ET.SubElement(description, _q("mwg-rs", "Regions"))
        bag = ET.SubElement(container, _q("rdf", "Bag"))
    else:
        bag = container.find(_q("rdf", "Bag"))
        if bag is None:
            bag = ET.SubElement(container, _q("rdf", "Bag"))
    for item in list(bag):
        if item.get(_q("rawww", "Managed")) == "true":
            bag.remove(item)
    if not regions and not len(bag):
        description.remove(container)
        return
    existing = {
        "".join(name.itertext()).strip().casefold()
        for name in container.iter(_q("mwg-rs", "Name"))
        if "".join(name.itertext()).strip()
    }
    for region in regions:
        name = str(region.get("name") or "").strip()
        if not name or name.casefold() in existing:
            continue
        item = ET.SubElement(bag, _q("rdf", "li"), {
            _q("rdf", "parseType"): "Resource", _q("rawww", "Managed"): "true",
        })
        ET.SubElement(item, _q("mwg-rs", "Name")).text = name
        ET.SubElement(item, _q("mwg-rs", "Type")).text = "Face"
        ET.SubElement(item, _q("mwg-rs", "Area"), {
            _q("stArea", "x"): f"{float(region['x']):.6f}",
            _q("stArea", "y"): f"{float(region['y']):.6f}",
            _q("stArea", "w"): f"{float(region['width']):.6f}",
            _q("stArea", "h"): f"{float(region['height']):.6f}",
            _q("stArea", "unit"): "normalized",
        })
        existing.add(name.casefold())


def _document_has_metadata(document: XmpDocument) -> bool:
    marker = _q("rawww", "Creator")
    for description in _descriptions(document):
        if any(name not in {_q("rdf", "about"), marker} for name in description.attrib):
            return True
        if len(description):
            return True
    return False


def _has_fields(fields: XmpFields) -> bool:
    return fields.rating is not None or bool(fields.color_label or fields.comment or fields.keywords)


def _register_document_namespaces(data: bytes) -> None:
    try:
        for _event, namespace in ET.iterparse(BytesIO(data), events=("start-ns",)):
            prefix, uri = namespace
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                ET.register_namespace(prefix, uri)
    except (ET.ParseError, ValueError):
        pass


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def _similarity(left: object, right: object) -> float:
    if left is None or right is None:
        return -1.0
    try:
        if len(left) == 0 or len(left) != len(right):
            return -1.0
    except TypeError:
        return -1.0
    try:
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = sum(float(a) ** 2 for a in left) ** 0.5
        right_norm = sum(float(b) ** 2 for b in right) ** 0.5
    except (TypeError, ValueError):
        return -1.0
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0
