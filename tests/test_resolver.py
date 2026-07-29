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


def test_price_includes_tax_conversion(tmp_path):
    """20.5 incl 23% tax -> ~16.67 stored (tax-excluded)."""
    import asyncio, httpx
    from app.api_client import PrestaShopClient
    from app.importer import Importer, ImportConfig, Mode
    import re

    blank = ("<?xml version='1.0'?><prestashop><product><reference/><price/>"
             "<name><language id=\"1\"/></name>"
             "<link_rewrite><language id=\"1\"/></link_rewrite></product></prestashop>")

    def handler(req):
        if req.url.path.endswith("/api/products") and dict(req.url.params).get("schema") == "blank":
            return httpx.Response(200, text=blank)
        return httpx.Response(200, json={})

    async def run():
        c = PrestaShopClient("http://s", "K")
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        imp = Importer(c, ImportConfig(dry_run=True, mode=Mode.CREATE_ONLY,
                                       price_includes_tax=True, tax_rate=23))
        r = (await imp.run([{"reference": "X", "name": "Anna", "price": "20.5"}]))[0]
        await c.aclose()
        return re.search(r"<price>([^<]+)</price>", r.payload).group(1)

    price = float(asyncio.run(run()))
    assert abs(price - 20.5 / 1.23) < 0.001


def test_parse_attribute_pairs():
    pairs = resolver.parse_attribute_pairs(
        "Colour:color:0, Configuration:select:1", "Black:0, Lantern head only:0")
    assert len(pairs) == 2
    assert pairs[0].group_name == "Colour" and pairs[0].group_type == "color"
    assert pairs[0].value_name == "Black"
    assert pairs[1].group_name == "Configuration" and pairs[1].group_type == "select"
    assert pairs[1].value_name == "Lantern head only"


def test_parse_attribute_pairs_value_with_colon():
    pairs = resolver.parse_attribute_pairs("Size:select:0", "10:20 cm:3")
    assert pairs[0].value_name == "10:20 cm"
    assert pairs[0].value_position == "3"


def test_combination_associations_xml():
    blank = ("<?xml version='1.0'?><prestashop><combination><id_product/><reference/>"
             "<associations><product_option_values><product_option_value><id/>"
             "</product_option_value></product_option_values></associations>"
             "</combination></prestashop>")
    xml = xml_builder.build_create_xml(
        blank, {"id_product": "55", "reference": "X-BK"},
        associations={"product_option_values": [10, 20]})
    assert "<product_option_value><id>10</id></product_option_value>" in xml
    assert "<product_option_value><id>20</id></product_option_value>" in xml
