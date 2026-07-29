"""Tests for the header-matching cascade."""
from app import mapper

PS_FIELDS = [
    "reference", "name", "price", "wholesale_price", "ean13", "weight",
    "active", "id_category_default", "id_manufacturer", "description",
    "link_rewrite",
]


def _by_header(matches):
    return {m.header: m for m in matches}


def test_exact_match():
    m = _by_header(mapper.match_headers(["reference"], PS_FIELDS))["reference"]
    assert m.field == "reference"
    assert m.method == "exact"
    assert m.confidence == 1.0
    assert m.badge == "green"


def test_normalized_match():
    # "Wholesale Price" -> wholesale_price via normalization (spaces/case).
    m = _by_header(mapper.match_headers(["Wholesale Price"], PS_FIELDS))["Wholesale Price"]
    assert m.field == "wholesale_price"
    assert m.method == "normalized"
    assert m.badge == "green"


def test_synonym_match():
    matches = _by_header(mapper.match_headers(["SKU", "RRP", "Title"], PS_FIELDS))
    assert matches["SKU"].field == "reference"
    assert matches["SKU"].method == "synonym"
    assert matches["RRP"].field == "price"
    assert matches["Title"].field == "name"


def test_fuzzy_match_above_threshold():
    # "referance" (typo) should fuzzy-match reference.
    m = _by_header(mapper.match_headers(["referance"], PS_FIELDS))["referance"]
    assert m.field == "reference"
    assert m.method in ("fuzzy", "normalized")
    assert m.confidence >= 0.75


def test_unmatched_column_is_unmapped():
    m = _by_header(mapper.match_headers(["Totally Unrelated Xyz"], PS_FIELDS))["Totally Unrelated Xyz"]
    assert m.field is None
    assert m.badge == "red"


def test_conflict_resolution_one_field_one_header():
    # Two headers both map to reference; only the stronger keeps it.
    matches = _by_header(mapper.match_headers(["reference", "SKU"], PS_FIELDS))
    assert matches["reference"].field == "reference"        # exact wins
    assert matches["SKU"].field is None                     # loser downgraded
    assert "reference" in matches["SKU"].candidates


def test_normalize():
    assert mapper.normalize("Article No.") == "articleno"
    assert mapper.normalize("id_category_default") == "idcategorydefault"


def test_synonyms_only_used_if_field_exists():
    # "brand" -> id_manufacturer only if that field is in the schema.
    matches = _by_header(mapper.match_headers(["brand"], ["reference", "name"]))
    assert matches["brand"].field is None


def test_prestashop_import_template_labels():
    """Headers from PrestaShop's own import template (with (…) hints, *, #)."""
    ps = ["id", "active", "name", "price", "reference", "supplier_reference",
          "quantity", "visibility", "description_short", "available_for_order",
          "show_price", "available_now"]
    headers = ["Active (0/1)", "Name*", "Price tax included", "Reference #",
               "Supplier reference #", "Quantity", "Visibility", "Summary",
               "Available for order (0 = No, 1 = Yes)", "Show price (0 = No, 1 = Yes)",
               "Label when in stock"]
    by = {m.header: m for m in mapper.match_headers(headers, ps)}
    assert by["Active (0/1)"].field == "active"
    assert by["Name*"].field == "name"
    assert by["Reference #"].field == "reference"
    assert by["Available for order (0 = No, 1 = Yes)"].field == "available_for_order"
    assert by["Show price (0 = No, 1 = Yes)"].field == "show_price"
    assert by["Price tax included"].field == "price"
    assert by["Label when in stock"].field == "available_now"


def test_normalize_strips_hints():
    assert mapper.normalize("Active (0/1)") == "active"
    assert mapper.normalize("Name*") == "name"
    assert mapper.normalize("Reference #") == "reference"
    assert mapper.normalize("Categories (x,y,z...)") == "categories"
