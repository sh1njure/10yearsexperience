"""Export shop data back into the spreadsheet templates the importer reads.

Reverse of the import: read products (or combinations) from the Webservice and
resolve IDs back to names (categories, brand, attributes) so the produced file
round-trips through the same mapping.
"""
from __future__ import annotations

from .api_client import PrestaShopClient

# Column order mirrors the PrestaShop import templates so exports re-import
# cleanly through the same auto-matcher.
PRODUCT_HEADERS = [
    "ID", "Active (0/1)", "Name", "Categories (x,y,z...)",
    "Price tax excluded", "Reference #", "Supplier reference #", "Brand",
    "Quantity", "Visibility", "Summary",
    "Available for order (0 = No, 1 = Yes)", "Show price (0 = No, 1 = Yes)",
    "Label when in stock",
]

COMBINATION_HEADERS = [
    "Product Reference", "Attribute (Name:Type:Position)*",
    "Value (Value:Position)*", "Reference", "Supplier reference",
    "Impact on price", "Quantity", "Minimal quantity",
    "Default (0 = No, 1 = Yes)",
]


def _ml(value: object, lang_id: int) -> str:
    """Extract a single-language string from a multilingual JSON field."""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("id")) == str(lang_id):
                return str(item.get("value", "") or "")
        if value and isinstance(value[0], dict):
            return str(value[0].get("value", "") or "")
        return ""
    return "" if value is None else str(value)


def _as_list(assoc_value: object) -> list[dict]:
    if isinstance(assoc_value, list):
        return [x for x in assoc_value if isinstance(x, dict)]
    if isinstance(assoc_value, dict):
        return [assoc_value]
    return []


async def _id_name_map(client: PrestaShopClient, resource: str,
                       lang_id: int) -> dict[str, str]:
    data = await client.get_json(resource, params={"display": "[id,name]"})
    items = data.get(resource, []) if isinstance(data, dict) else []
    out: dict[str, str] = {}
    for it in items:
        out[str(it.get("id"))] = _ml(it.get("name"), lang_id)
    return out


async def _stock_map(client: PrestaShopClient) -> dict[tuple[str, str], str]:
    """(id_product, id_product_attribute) -> quantity."""
    data = await client.get_json(
        "stock_availables",
        params={"display": "[id_product,id_product_attribute,quantity]"})
    items = data.get("stock_availables", []) if isinstance(data, dict) else []
    out: dict[tuple[str, str], str] = {}
    for it in items:
        key = (str(it.get("id_product")), str(it.get("id_product_attribute")))
        out[key] = str(it.get("quantity", ""))
    return out


async def export_products(client: PrestaShopClient, lang_id: int) -> list[list]:
    """Return rows (including header) for a products export."""
    manufacturers = await _id_name_map(client, "manufacturers", lang_id)
    categories = await _id_name_map(client, "categories", lang_id)
    stock = await _stock_map(client)

    data = await client.get_json("products", params={"display": "full"})
    products = data.get("products", []) if isinstance(data, dict) else []

    rows: list[list] = [list(PRODUCT_HEADERS)]
    for p in products:
        pid = str(p.get("id", ""))
        assoc = p.get("associations", {}) if isinstance(p, dict) else {}
        cat_ids = [str(c.get("id")) for c in _as_list(assoc.get("categories"))]
        cat_names = [categories.get(cid, "") for cid in cat_ids if categories.get(cid)]
        rows.append([
            pid,
            p.get("active", ""),
            _ml(p.get("name"), lang_id),
            ",".join(cat_names),
            p.get("price", ""),
            p.get("reference", ""),
            p.get("supplier_reference", ""),
            manufacturers.get(str(p.get("id_manufacturer")), ""),
            stock.get((pid, "0"), ""),
            p.get("visibility", ""),
            _ml(p.get("description_short"), lang_id),
            p.get("available_for_order", ""),
            p.get("show_price", ""),
            _ml(p.get("available_now"), lang_id),
        ])
    return rows


async def export_combinations(client: PrestaShopClient, lang_id: int) -> list[list]:
    """Return rows (including header) for a combinations export."""
    stock = await _stock_map(client)

    # Parent product references.
    pdata = await client.get_json("products", params={"display": "[id,reference]"})
    product_ref = {str(p.get("id")): str(p.get("reference", ""))
                   for p in (pdata.get("products", []) if isinstance(pdata, dict) else [])}

    # Attribute groups + values.
    gdata = await client.get_json(
        "product_options", params={"display": "[id,group_type]"})
    group_meta = {str(g.get("id")): str(g.get("group_type", "select"))
                  for g in (gdata.get("product_options", []) if isinstance(gdata, dict) else [])}
    group_name = await _id_name_map(client, "product_options", lang_id)

    vdata = await client.get_json(
        "product_option_values", params={"display": "full"})
    value_meta: dict[str, tuple[str, str]] = {}
    for v in (vdata.get("product_option_values", []) if isinstance(vdata, dict) else []):
        value_meta[str(v.get("id"))] = (str(v.get("id_attribute_group")),
                                        _ml(v.get("name"), lang_id))

    data = await client.get_json("combinations", params={"display": "full"})
    combinations = data.get("combinations", []) if isinstance(data, dict) else []

    rows: list[list] = [list(COMBINATION_HEADERS)]
    for c in combinations:
        cid = str(c.get("id", ""))
        pid = str(c.get("id_product", ""))
        assoc = c.get("associations", {}) if isinstance(c, dict) else {}
        val_ids = [str(v.get("id")) for v in _as_list(assoc.get("product_option_values"))]

        attr_parts, value_parts = [], []
        for i, vid in enumerate(val_ids):
            gid, vname = value_meta.get(vid, ("", ""))
            gname = group_name.get(gid, "")
            gtype = group_meta.get(gid, "select")
            if gname:
                attr_parts.append(f"{gname}:{gtype}:{i}")
                value_parts.append(f"{vname}:0")

        rows.append([
            product_ref.get(pid, ""),
            ", ".join(attr_parts),
            ", ".join(value_parts),
            c.get("reference", ""),
            c.get("supplier_reference", ""),
            c.get("price", ""),
            stock.get((pid, cid), ""),
            c.get("minimal_quantity", ""),
            c.get("default_on", ""),
        ])
    return rows
