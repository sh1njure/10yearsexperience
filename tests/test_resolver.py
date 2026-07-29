"""Tests for name-resolution helpers and association XML (pure logic)."""
from xml.etree import ElementTree as ET

from app import resolver, xml_builder


def test_parse_features_basic():
    specs = resolver.parse_features("Finish:Black:2:0,Max wattage:60 W:6:0")
    assert len(specs) == 2
    assert specs[0].name == "Finish" and specs[0].value == "Black"
    assert specs[0].position == "2"
    assert specs[1].value == "60 W"


def test_parse_features_value_with_inner_colon():
    specs = resolver.parse_features("Insulation:Class II - double insulated:13:0")
    assert specs[0].value == "Class II - double insulated"


def test_parse_features_two_part_fallback():
    specs = resolver.parse_features("Colour:Black")
    assert specs[0].name == "Colour" and specs[0].value == "Black"
    assert specs[0].position == "0"


def test_split_list():
    assert resolver.split_list("Home, Lighting , ,Outdoor") == ["Home", "Lighting", "Outdoor"]
    assert resolver.split_list("") == []


BLANK = ("<?xml version='1.0'?><prestashop><product><reference/>"
         "<associations><categories><category><id/></category></categories>"
         "</associations></product></prestashop>")


def _find_all(root, tag):
    return [e for e in root.iter() if e.tag.split('}')[-1] == tag]


def test_associations_categories_and_features():
    xml = xml_builder.build_create_xml(
        BLANK, {"reference": "X1"},
        associations={"categories": [2, 5], "tags": [10],
                      "product_features": [(1, 4), (2, 8)]})
    root = ET.fromstring(xml)
    cat_ids = [e.text for e in _find_all(root, "category") for c in e if c.tag == "id"]
    assert [c.text for c in root.iter() if c.tag == "id" and c.text in ("2", "5")]
    # features rendered as product_feature with id + id_feature_value
    pf = _find_all(root, "product_feature")
    assert len(pf) == 2
    assert _find_all(root, "id_feature_value")[0].text == "4"


def test_associations_absent_when_empty():
    xml = xml_builder.build_create_xml(BLANK, {"reference": "X1"}, associations={})
    assert "<associations" not in xml or "<category><id>" not in xml
