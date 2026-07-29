"""Tests for schema parsing and XML payload building."""
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app import xml_builder
from app.api_client import PrestaShopClient

FIXTURES = Path(__file__).parent / "fixtures"
BLANK_SCHEMA = (FIXTURES / "product_schema_blank.xml").read_text()
API_ROOT = (FIXTURES / "api_root.xml").read_text()


def _find(root, tag):
    for el in root.iter():
        if el.tag.split("}")[-1] == tag:
            return el
    return None


# ------------------------------ schema -------------------------------- #
def test_parse_schema_lists_fields():
    schema = PrestaShopClient.parse_schema("products", BLANK_SCHEMA)
    names = schema.field_names()
    assert "reference" in names
    assert "name" in names
    # Multilingual detection
    name_field = next(f for f in schema.fields if f.name == "name")
    assert name_field.multilingual is True
    # Read-only detection
    id_field = next(f for f in schema.fields if f.name == "id")
    assert id_field.read_only is True


def test_writable_fields_exclude_readonly():
    schema = PrestaShopClient.parse_schema("products", BLANK_SCHEMA)
    writable = {f.name for f in schema.writable_fields}
    assert "id" not in writable
    assert "manufacturer_name" not in writable
    assert "quantity" not in writable
    assert "reference" in writable


def test_test_connection_parsing():
    # extract resources from the root api document (parsing logic reuse).
    root = ET.fromstring(API_ROOT)
    api = _find(root, "api")
    resources = sorted({c.tag.split("}")[-1] for c in api})
    assert "products" in resources
    assert "stock_availables" in resources


# ---------------------------- build create ---------------------------- #
def test_build_create_strips_readonly_and_fills_values():
    values = {"reference": "ABC-123", "price": "9.99", "id": "999",
              "manufacturer_name": "should be stripped"}
    xml = xml_builder.build_create_xml(BLANK_SCHEMA, values, lang_id=1)
    root = ET.fromstring(xml)

    assert _find(root, "reference").text == "ABC-123"
    assert _find(root, "price").text == "9.99"
    # read-only fields removed entirely
    assert _find(root, "id") is None
    assert _find(root, "manufacturer_name") is None


def test_build_create_wraps_multilingual():
    values = {"name": "Blue Widget"}
    xml = xml_builder.build_create_xml(BLANK_SCHEMA, values, lang_id=1)
    root = ET.fromstring(xml)
    name_el = _find(root, "name")
    langs = [c for c in name_el if c.tag.split("}")[-1] == "language"]
    assert len(langs) == 1
    assert langs[0].get("id") == "1"
    assert langs[0].text == "Blue Widget"


def test_build_create_respects_configurable_lang_id():
    xml = xml_builder.build_create_xml(BLANK_SCHEMA, {"name": "X"}, lang_id=3)
    root = ET.fromstring(xml)
    lang = _find(root, "language")
    assert lang.get("id") == "3"


def test_build_update_preserves_unmapped_fields():
    existing = """<?xml version="1.0"?>
    <prestashop><product>
      <id>42</id>
      <reference>OLD-REF</reference>
      <price>1.00</price>
      <weight>2.5</weight>
    </product></prestashop>"""
    xml = xml_builder.build_update_xml(existing, {"price": "3.50"}, lang_id=1)
    root = ET.fromstring(xml)
    # mapped field updated
    assert _find(root, "price").text == "3.50"
    # unmapped field preserved
    assert _find(root, "weight").text == "2.5"
    # id must be PRESERVED on PUT (PrestaShop requires it to modify a resource)
    assert _find(root, "id").text == "42"


# ------------------------------ slugify ------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("Blue Widget", "blue-widget"),
    ("  Größe & Co / Ltd.  ", "grosse-and-co-ltd"),
    ("Über Cool!!!", "uber-cool"),
    ("", "product"),
])
def test_slugify(raw, expected):
    assert xml_builder.slugify(raw) == expected


def test_build_update_stock_available_keeps_id_and_quantity():
    """stock_availables PUT must keep <id> and be able to set quantity
    (quantity is read-only on products but writable here)."""
    existing = ("<?xml version='1.0'?><prestashop><stock_available>"
                "<id>124</id><id_product>55</id_product>"
                "<id_product_attribute>3</id_product_attribute>"
                "<id_shop>1</id_shop><quantity>0</quantity>"
                "</stock_available></prestashop>")
    xml = xml_builder.build_update_xml(existing, {"quantity": "10"})
    root = ET.fromstring(xml)
    assert _find(root, "id").text == "124"
    assert _find(root, "quantity").text == "10"
    assert _find(root, "id_product").text == "55"
