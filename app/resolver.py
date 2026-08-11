"""Resolve human names in the spreadsheet to PrestaShop IDs / associations.

The supplier sheet (PrestaShop's own import template) uses names, not IDs:
categories, brand, tags and features are all text. The Webservice API needs
numeric IDs and association structures. This module bridges that gap: it looks
each name up via the API and, when ``create_missing`` is on, creates the record.

Results are cached per run so the same brand/category isn't looked up twice.
All network work goes through :class:`~app.api_client.PrestaShopClient`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import httpx

try:
    from PIL import Image
except ImportError:  # Pillow optional; without it oversized images just fail as before
    Image = None

from .api_client import PrestaShopClient, PrestaShopError, _localname
from . import xml_builder

# PrestaShop rejects product images over ~2000 KB. Shrink anything above this
# (kept a little under the hard limit) before upload.
MAX_IMAGE_BYTES = 1_900_000
MAX_IMAGE_DIMENSION = 1600


def shrink_image(content: bytes) -> tuple[bytes, str, str] | None:
    """Downscale/recompress an oversized image under ``MAX_IMAGE_BYTES``.

    Returns ``(bytes, content_type, extension)`` as JPEG (transparency flattened
    onto white), or ``None`` if Pillow is unavailable or the data can't be read
    (caller then uploads the original and lets PrestaShop reject it as before).
    """
    if Image is None:
        return None
    try:
        img = Image.open(BytesIO(content))
        img.load()
    except Exception:
        return None

    # JPEG has no alpha — flatten transparency onto white.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    else:
        img = img.convert("RGB")

    if max(img.size) > MAX_IMAGE_DIMENSION:
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))

    # Drop quality (then dimensions) until it fits.
    for quality in (85, 75, 65, 55):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue(), "image/jpeg", "jpg"

    img.thumbnail((1200, 1200))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=70, optimize=True)
    return buf.getvalue(), "image/jpeg", "jpg"


@dataclass
class FeatureSpec:
    name: str
    value: str
    position: str = "0"
    customized: str = "0"


def parse_features(raw: str) -> list[FeatureSpec]:
    """Parse the ``Feature (Name:Value:Position:Customized)`` column.

    Example input (comma-separated features, colon-separated parts)::

        "Manufacturer:Fumagalli:0:0,Finish:Black:2:0,Max wattage:60 W:6:0"

    Values themselves may contain spaces; only the 4 colon parts are split, and
    extra colons beyond the 4th are treated as part of the value.
    """
    specs: list[FeatureSpec] = []
    if not raw:
        return specs
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        # Re-join middle parts so a value like "Class II - double" survives;
        # last two parts are position + customized when present.
        if len(parts) >= 4:
            value = ":".join(parts[1:-2]).strip()
            position, customized = parts[-2].strip(), parts[-1].strip()
        else:
            value = ":".join(parts[1:]).strip()
            position, customized = "0", "0"
        specs.append(FeatureSpec(name, value, position or "0", customized or "0"))
    return specs


def split_list(raw: str) -> list[str]:
    """Split a ``x,y,z`` style cell into trimmed, non-empty items."""
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


@dataclass
class AttributePair:
    group_name: str
    group_type: str          # select | radio | color
    group_position: str
    value_name: str
    value_position: str


def _split_value_pos(item: str) -> tuple[str, str]:
    """Split "Name:0" -> ("Name", "0"); keep colons inside the name."""
    parts = item.split(":")
    if len(parts) >= 2 and parts[-1].strip().lstrip("-").isdigit():
        return ":".join(parts[:-1]).strip(), parts[-1].strip()
    return item.strip(), "0"


def parse_attribute_pairs(attr_cell: str, value_cell: str) -> list[AttributePair]:
    """Pair up the two combination columns into (group, value) attributes.

    ``attr_cell``  = "Colour:color:0, Configuration:select:1"
    ``value_cell`` = "Black:0, Lantern head only:0"
    -> [Colour/color = Black, Configuration/select = Lantern head only]
    """
    attrs = [a.strip() for a in (attr_cell or "").split(",") if a.strip()]
    values = [v.strip() for v in (value_cell or "").split(",") if v.strip()]
    pairs: list[AttributePair] = []
    for attr, value in zip(attrs, values):
        a_parts = attr.split(":")
        group_name = a_parts[0].strip()
        group_type = a_parts[1].strip() if len(a_parts) >= 2 else "select"
        group_pos = a_parts[2].strip() if len(a_parts) >= 3 else "0"
        value_name, value_pos = _split_value_pos(value)
        pairs.append(AttributePair(group_name, group_type or "select",
                                   group_pos, value_name, value_pos))
    return pairs


class Resolver:
    def __init__(self, client: PrestaShopClient, *, lang_id: int = 1,
                 create_missing: bool = False):
        self.client = client
        self.lang_id = lang_id
        self.create_missing = create_missing
        self._cat_cache: dict[str, int | None] = {}
        self._man_cache: dict[str, int | None] = {}
        self._tag_cache: dict[str, int | None] = {}
        self._feat_cache: dict[str, int | None] = {}
        self._featval_cache: dict[tuple[int, str], int | None] = {}
        self._group_cache: dict[str, int | None] = {}
        self._value_cache: dict[tuple[int, str], int | None] = {}
        self._schemas: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    async def _schema(self, resource: str) -> str:
        if resource not in self._schemas:
            self._schemas[resource] = await self.client.get_xml(
                resource, params={"schema": "blank"})
        return self._schemas[resource]

    async def _find_id(self, resource: str, filters: dict) -> int | None:
        params = {**{f"filter[{k}]": v for k, v in filters.items()},
                  "display": "[id]"}
        data = await self.client.get_json(resource, params=params)
        items = data.get(resource) if isinstance(data, dict) else None
        if items:
            first = items[0]
            pid = first.get("id") if isinstance(first, dict) else first
            return int(pid) if pid else None
        return None

    # ------------------------------ categories ------------------------ #
    async def resolve_categories(self, names: list[str]) -> list[int]:
        ids: list[int] = []
        for name in names:
            cid = await self._resolve_category(name)
            if cid is not None:
                ids.append(cid)
        return ids

    async def _resolve_category(self, name: str) -> int | None:
        if name in self._cat_cache:
            return self._cat_cache[name]
        cid = await self._find_id("categories", {"name": name})
        if cid is None and self.create_missing:
            cid = await self._create_category(name)
        self._cat_cache[name] = cid
        return cid

    async def _create_category(self, name: str, id_parent: int = 2) -> int | None:
        schema = await self._schema("categories")
        values = {
            "name": name,
            "link_rewrite": xml_builder.slugify(name),
            "id_parent": str(id_parent),
            "active": "1",
        }
        xml = xml_builder.build_create_xml(schema, values, lang_id=self.lang_id)
        result = await self.client.create("categories", xml)
        return result.get("id")

    # ---------------------------- manufacturers ----------------------- #
    async def resolve_manufacturer(self, name: str) -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in self._man_cache:
            return self._man_cache[name]
        mid = await self._find_id("manufacturers", {"name": name})
        if mid is None and self.create_missing:
            schema = await self._schema("manufacturers")
            xml = xml_builder.build_create_xml(
                schema, {"name": name, "active": "1"}, lang_id=self.lang_id)
            result = await self.client.create("manufacturers", xml)
            mid = result.get("id")
        self._man_cache[name] = mid
        return mid

    # -------------------------------- tags ---------------------------- #
    async def resolve_tags(self, names: list[str]) -> list[int]:
        ids: list[int] = []
        for name in names:
            tid = await self._resolve_tag(name)
            if tid is not None:
                ids.append(tid)
        return ids

    async def _resolve_tag(self, name: str) -> int | None:
        if name in self._tag_cache:
            return self._tag_cache[name]
        tid = await self._find_id("tags", {"name": name})
        if tid is None and self.create_missing:
            schema = await self._schema("tags")
            xml = xml_builder.build_create_xml(
                schema, {"name": name, "id_lang": str(self.lang_id)},
                lang_id=self.lang_id)
            result = await self.client.create("tags", xml)
            tid = result.get("id")
        self._tag_cache[name] = tid
        return tid

    # ------------------------------ features -------------------------- #
    async def resolve_features(self, specs: list[FeatureSpec]) -> list[tuple[int, int]]:
        pairs: list[tuple[int, int]] = []
        for spec in specs:
            feat_id = await self._resolve_feature(spec.name)
            if feat_id is None:
                continue
            value_id = await self._resolve_feature_value(feat_id, spec.value,
                                                         spec.customized)
            if value_id is not None:
                pairs.append((feat_id, value_id))
        return pairs

    async def _resolve_feature(self, name: str) -> int | None:
        if name in self._feat_cache:
            return self._feat_cache[name]
        fid = await self._find_id("product_features", {"name": name})
        if fid is None and self.create_missing:
            schema = await self._schema("product_features")
            xml = xml_builder.build_create_xml(
                schema, {"name": name}, lang_id=self.lang_id)
            result = await self.client.create("product_features", xml)
            fid = result.get("id")
        self._feat_cache[name] = fid
        return fid

    async def _resolve_feature_value(self, feature_id: int, value: str,
                                     customized: str = "0") -> int | None:
        cache_key = (feature_id, value)
        if cache_key in self._featval_cache:
            return self._featval_cache[cache_key]
        vid = await self._find_id("product_feature_values",
                                  {"id_feature": feature_id, "value": value})
        if vid is None and self.create_missing:
            schema = await self._schema("product_feature_values")
            xml = xml_builder.build_create_xml(
                schema,
                {"id_feature": str(feature_id), "value": value,
                 "custom": "1" if customized in ("1", "true") else "0"},
                lang_id=self.lang_id)
            result = await self.client.create("product_feature_values", xml)
            vid = result.get("id")
        self._featval_cache[cache_key] = vid
        return vid

    # ---------------------- attributes (combinations) ----------------- #
    async def resolve_attribute_value_ids(self, pairs: list[AttributePair]) -> list[int]:
        """Resolve (and optionally create) each attribute value -> value id."""
        ids: list[int] = []
        for pair in pairs:
            group_id = await self._resolve_attribute_group(
                pair.group_name, pair.group_type, pair.group_position)
            if group_id is None:
                continue
            value_id = await self._resolve_attribute_value(
                group_id, pair.value_name, pair.value_position,
                is_color=pair.group_type == "color")
            if value_id is not None:
                ids.append(value_id)
        return ids

    async def _resolve_attribute_group(self, name: str, group_type: str,
                                       position: str) -> int | None:
        if name in self._group_cache:
            return self._group_cache[name]
        gid = await self._find_id("product_options", {"name": name})
        if gid is None and self.create_missing:
            schema = await self._schema("product_options")
            values = {
                "name": name,
                "public_name": name,
                "group_type": group_type or "select",
                "is_color_group": "1" if group_type == "color" else "0",
                "position": position or "0",
            }
            xml = xml_builder.build_create_xml(schema, values, lang_id=self.lang_id)
            result = await self.client.create("product_options", xml)
            gid = result.get("id")
        self._group_cache[name] = gid
        return gid

    async def _resolve_attribute_value(self, group_id: int, value: str,
                                       position: str, is_color: bool = False) -> int | None:
        cache_key = (group_id, value)
        if cache_key in self._value_cache:
            return self._value_cache[cache_key]
        vid = await self._find_id(
            "product_option_values",
            {"id_attribute_group": group_id, "name": value})
        if vid is None and self.create_missing:
            schema = await self._schema("product_option_values")
            values = {
                "id_attribute_group": str(group_id),
                "name": value,
                "position": position or "0",
            }
            xml = xml_builder.build_create_xml(schema, values, lang_id=self.lang_id)
            result = await self.client.create("product_option_values", xml)
            vid = result.get("id")
        self._value_cache[cache_key] = vid
        return vid

    async def find_product_id(self, reference: str) -> int | None:
        return await self._find_id("products", {"reference": reference})

    # ------------------------------- images --------------------------- #
    async def fetch_image(self, url: str) -> tuple[bytes, str, str]:
        """Download an image URL; return (bytes, filename, content_type).

        Images over PrestaShop's size limit are downscaled/recompressed so the
        upload succeeds instead of being rejected as "too large".
        """
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as c:
            resp = await c.get(url)
            resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        filename = url.rsplit("/", 1)[-1] or "image.jpg"

        if len(content) > MAX_IMAGE_BYTES:
            shrunk = shrink_image(content)
            if shrunk is not None:
                content, content_type, ext = shrunk
                base = filename.rsplit(".", 1)[0] if "." in filename else filename
                filename = f"{base}.{ext}"

        return content, filename, content_type
