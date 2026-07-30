"""Tests for the combination-descriptions import payload building.

Pure-function style (matches the other tests): build a write payload from a
blank ``combination_descriptions`` schema and assert the shape the module's
Webservice resource expects — read-only fields stripped, multilingual
description fields wrapped in <language> nodes.
"""
from xml.etree import ElementTree as ET

from app import xml_builder
from app.routers.mapping import COMBINATION_DESCRIPTION_TARGETS

# The ?schema=blank skeleton the module returns for the resource: id is
# read-only, description / description_short are multilingual.
BLANK = (
    "<?xml version='1.0'?><prestashop><combination_description>"
    "<id readOnly='true'/>"
    "<id_product_attribute required='true'/>"
    "<id_product/>"
    "<id_shop/>"
    "<description><language id='1'></language></description>"
    "<description_short><language id='1'></language></description_short>"
    "</combination_description></prestashop>"
)


def _find(root, tag):
    for el in root.iter():
        if el.tag.split("}")[-1] == tag:
            return el
    return None


def _langs(el):
    return [c for c in el if c.tag.split("}")[-1] == "language"]


def test_build_create_sets_ids_and_wraps_multilingual():
    values = {
        "id_product_attribute": "12",
        "id_product": "45",
        "id_shop": "1",
        "description": "<p>Slim-fit red T-shirt.</p>",
        "description_short": "<p>Red / M</p>",
    }
    xml = xml_builder.build_create_xml(BLANK, values, lang_id=1)
    root = ET.fromstring(xml)

    assert _find(root, "id_product_attribute").text == "12"
    assert _find(root, "id_product").text == "45"
    assert _find(root, "id_shop").text == "1"

    # read-only id stripped from the write payload
    assert _find(root, "id") is None

    desc = _find(root, "description")
    langs = _langs(desc)
    assert len(langs) == 1
    assert langs[0].get("id") == "1"
    assert langs[0].text == "<p>Slim-fit red T-shirt.</p>"

    short = _find(root, "description_short")
    assert _langs(short)[0].text == "<p>Red / M</p>"


def test_build_create_respects_lang_id():
    xml = xml_builder.build_create_xml(
        BLANK, {"id_product_attribute": "9", "description": "Rot"}, lang_id=3)
    root = ET.fromstring(xml)
    assert _langs(_find(root, "description"))[0].get("id") == "3"


def test_build_update_preserves_id_and_overwrites_text():
    existing = (
        "<?xml version='1.0'?><prestashop><combination_description>"
        "<id>7</id><id_product_attribute>12</id_product_attribute>"
        "<id_product>45</id_product><id_shop>1</id_shop>"
        "<description><language id='1'>old</language></description>"
        "<description_short><language id='1'>old</language></description_short>"
        "</combination_description></prestashop>"
    )
    xml = xml_builder.build_update_xml(
        existing, {"description": "new text"}, lang_id=1)
    root = ET.fromstring(xml)
    # id required on PUT — must be kept
    assert _find(root, "id").text == "7"
    assert _langs(_find(root, "description"))[0].text == "new text"
    # unmapped field preserved
    assert _find(root, "id_product_attribute").text == "12"


def test_mapping_targets_expose_reference_and_text_fields():
    for expected in ("reference", "id_product_attribute",
                     "description", "description_short"):
        assert expected in COMBINATION_DESCRIPTION_TARGETS
