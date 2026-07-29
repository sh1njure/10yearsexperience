"""Build PrestaShop write payloads by filling a ``?schema=blank`` skeleton.

The brief is strict: never hand-build XML. Every payload here starts from the
blank schema returned by the shop and only fills in the mapped values, so the
element structure always matches what the shop expects.

Pure functions, no I/O — unit-tested against fixture schemas.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .api_client import READ_ONLY_FIELDS, _localname


def slugify(value: str) -> str:
    """Turn a string into a valid ``link_rewrite`` slug.

    PrestaShop silently fails product validation if ``link_rewrite`` is not a
    clean slug, so we normalise aggressively: lowercase, ASCII-ish, hyphens.
    """
    value = (value or "").strip().lower()
    # Common transliterations before dropping non-ascii.
    replacements = {
        "&": " and ", "@": " at ", "/": "-", "\\": "-",
        "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
        "é": "e", "è": "e", "ê": "e", "à": "a", "â": "a", "ç": "c",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "product"


def _set_multilingual(element: ET.Element, value: str, lang_id: int) -> None:
    """Fill a multilingual field element with a single language value."""
    # Reuse an existing <language> skeleton child if present, else create one.
    lang_el = None
    for child in list(element):
        if _localname(child.tag) == "language":
            lang_el = child
            # Drop any extra language skeletons we are not filling.
            for extra in list(element)[1:]:
                element.remove(extra)
            break
    if lang_el is None:
        lang_el = ET.SubElement(element, "language")
    lang_el.attrib.clear()
    lang_el.set("id", str(lang_id))
    lang_el.text = value


def _apply_values(resource_el: ET.Element, values: dict[str, object],
                  multilingual_fields: set[str], lang_id: int) -> None:
    """Set element text from ``values`` and strip read-only / empty fields."""
    for el in list(resource_el):
        name = _localname(el.tag)

        if name in READ_ONLY_FIELDS or el.attrib.get("readOnly") == "true":
            resource_el.remove(el)
            continue

        # Clear schema-only attributes that must not be sent back.
        for attr in ("readOnly", "required", "maxSize", "format"):
            el.attrib.pop(attr, None)

        if name not in values or values[name] is None or values[name] == "":
            # Leave unset writable fields as empty elements from the skeleton;
            # remove multilingual placeholders that would otherwise be invalid.
            if name in multilingual_fields:
                for child in list(el):
                    el.remove(child)
            continue

        value = values[name]
        if name in multilingual_fields:
            _set_multilingual(el, str(value), lang_id)
        else:
            # Non-multilingual: drop any stray children, set text.
            for child in list(el):
                el.remove(child)
            el.text = str(value)


def build_create_xml(blank_schema_xml: str, values: dict[str, object], *,
                     multilingual_fields: set[str] | None = None,
                     lang_id: int = 1,
                     associations: dict[str, object] | None = None) -> str:
    """Return XML for a POST, built from the blank schema and mapped values.

    ``associations`` optionally carries resolved relations to inject:
        {"categories": [2, 5],
         "tags": [10, 11],
         "product_features": [(feat_id, value_id), ...]}
    """
    root = ET.fromstring(blank_schema_xml)
    resource_el = next(iter(root), None)
    if resource_el is None:
        raise ValueError("Blank schema has no resource element")

    multilingual = multilingual_fields or _detect_multilingual(resource_el)
    _apply_values(resource_el, values, multilingual, lang_id)
    if associations:
        _set_associations(resource_el, associations)
    return _serialize(root)


def _set_associations(resource_el: ET.Element, associations: dict[str, object]) -> None:
    """Replace/add the <associations> block with resolved relations."""
    # Remove any skeleton associations element and rebuild cleanly.
    for existing in [el for el in resource_el if _localname(el.tag) == "associations"]:
        resource_el.remove(existing)
    assoc_el = ET.SubElement(resource_el, "associations")

    categories = associations.get("categories") or []
    if categories:
        cats = ET.SubElement(assoc_el, "categories")
        for cid in categories:
            c = ET.SubElement(cats, "category")
            ET.SubElement(c, "id").text = str(cid)

    tags = associations.get("tags") or []
    if tags:
        tags_el = ET.SubElement(assoc_el, "tags")
        for tid in tags:
            t = ET.SubElement(tags_el, "tag")
            ET.SubElement(t, "id").text = str(tid)

    features = associations.get("product_features") or []
    if features:
        feats = ET.SubElement(assoc_el, "product_features")
        for feat_id, value_id in features:
            pf = ET.SubElement(feats, "product_feature")
            ET.SubElement(pf, "id").text = str(feat_id)
            ET.SubElement(pf, "id_feature_value").text = str(value_id)

    # Combination -> attribute value links.
    option_values = associations.get("product_option_values") or []
    if option_values:
        povs = ET.SubElement(assoc_el, "product_option_values")
        for vid in option_values:
            pov = ET.SubElement(povs, "product_option_value")
            ET.SubElement(pov, "id").text = str(vid)

    # If nothing was added, drop the empty element again.
    if len(assoc_el) == 0:
        resource_el.remove(assoc_el)


def build_update_xml(existing_resource_xml: str, values: dict[str, object], *,
                     multilingual_fields: set[str] | None = None,
                     lang_id: int = 1) -> str:
    """Return XML for a PUT.

    Starts from the *full existing resource* (fetched via GET) so unmapped
    fields are preserved — partial PUTs wipe fields. Only mapped fields are
    overwritten; read-only fields are stripped.
    """
    root = ET.fromstring(existing_resource_xml)
    resource_el = next(iter(root), None)
    if resource_el is None:
        raise ValueError("Existing resource XML has no resource element")

    multilingual = multilingual_fields or _detect_multilingual(resource_el)
    # For updates we only overwrite provided values; keep the rest as-is, but
    # still strip read-only fields the API rejects.
    for el in list(resource_el):
        name = _localname(el.tag)
        if name in READ_ONLY_FIELDS or el.attrib.get("readOnly") == "true":
            resource_el.remove(el)
            continue
        for attr in ("readOnly", "required", "maxSize", "format"):
            el.attrib.pop(attr, None)
        if name in values and values[name] not in (None, ""):
            if name in multilingual:
                _set_multilingual(el, str(values[name]), lang_id)
            else:
                for child in list(el):
                    el.remove(child)
                el.text = str(values[name])
    return _serialize(root)


def _detect_multilingual(resource_el: ET.Element) -> set[str]:
    fields = set()
    for el in resource_el:
        if any(_localname(c.tag) == "language" for c in el):
            fields.add(_localname(el.tag))
    return fields


def _serialize(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
