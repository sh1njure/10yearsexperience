"""Import product combinations (variants).

A combinations sheet has one row per variant, each linked to a parent product by
reference. This module resolves the attribute columns into attribute-value IDs
(creating groups/values when asked), then POSTs a combination and sets its stock.

Reuses the products importer's retry/result plumbing.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from xml.etree import ElementTree as ET

from .api_client import PrestaShopClient
from .resolver import Resolver, parse_attribute_pairs, split_list
from .importer import ImportConfig, Mode, RowResult, ProgressCb, _with_retry, ceil_to
from .validator import parse_number
from .combination_descriptions import CombinationDescriptionImporter
from . import xml_builder


class CombinationImporter:
    def __init__(self, client: PrestaShopClient, config: ImportConfig):
        self.client = client
        self.config = config
        self._blank_schema: str | None = None
        self._typed_products: set[int] = set()  # products switched to "combinations"
        self.resolver = Resolver(
            client, lang_id=config.lang_id,
            create_missing=config.create_missing and not config.dry_run,
        )
        # Reused to set a per-combination description in the same pass, when the
        # "descriptions" scope is on and the row carries description text.
        self.desc_importer = CombinationDescriptionImporter(client, config)
        # Base (net) price per product, for rounding combination totals.
        self._base_net_cache: dict[int, Decimal] = {}

    async def _schema(self) -> str:
        if self._blank_schema is None:
            self._blank_schema = await self.client.get_xml(
                "combinations", params={"schema": "blank"})
        return self._blank_schema

    async def run(self, rows: list[dict[str, object]],
                  progress: ProgressCb = None) -> list[RowResult]:
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        results: list[RowResult | None] = [None] * len(rows)

        async def worker(index: int, row: dict[str, object]) -> None:
            async with semaphore:
                result = await self._process_row(index, row)
            results[index] = result
            if progress is not None:
                await progress(result)

        await asyncio.gather(*(worker(i, r) for i, r in enumerate(rows)))
        return [r for r in results if r is not None]

    async def _process_row(self, index: int, row: dict[str, object]) -> RowResult:
        reference = str(row.get("reference", "")).strip()
        product_ref = str(row.get("product_reference", "")).strip()
        try:
            if not product_ref:
                return RowResult(index, reference, "error", False,
                                 "No product reference for this combination.")
            product_id = await self.resolver.find_product_id(product_ref)
            if product_id is None:
                return RowResult(index, reference, "error", False,
                                 f"Parent product '{product_ref}' not found.")

            pairs = parse_attribute_pairs(str(row.get("attributes", "")),
                                          str(row.get("values", "")))
            value_ids = await self.resolver.resolve_attribute_value_ids(pairs)
            notes = []
            if len(value_ids) < len(pairs):
                notes.append(f"{len(pairs) - len(value_ids)} attribute(s) unresolved")

            simple = {"id_product": str(product_id)}
            if reference:
                simple["reference"] = reference
            for src, dst in (("supplier_reference", "supplier_reference"),
                             ("price_impact", "price"),
                             ("minimal_quantity", "minimal_quantity"),
                             ("ean13", "ean13")):
                val = row.get(src)
                if val in (None, ""):
                    continue
                if dst == "price":
                    simple[dst] = await self._impact_value(str(val), product_id)
                else:
                    simple[dst] = str(val)
            if str(row.get("default", "")).strip() in ("1", "true", "yes"):
                simple["default_on"] = "1"

            associations = {"product_option_values": value_ids} if value_ids else {}
            schema = await self._schema()

            if self.config.dry_run:
                payload = xml_builder.build_create_xml(
                    schema, simple, associations=associations,
                    lang_id=self.config.lang_id)
                msg = "Combination payload built (not sent)."
                if ("descriptions" in self.config.scope
                        and (str(row.get("description", "")).strip()
                             or str(row.get("description_short", "")).strip())):
                    msg += " A combination description would also be set."
                if notes:
                    msg += " Note: " + "; ".join(notes) + "."
                return RowResult(index, reference, "dry-run", True, msg,
                                 product_id=product_id, payload=payload)

            existing_id = await self._find_combination(reference) if reference else None
            if existing_id and self.config.mode == Mode.CREATE_ONLY:
                return RowResult(index, reference, "skipped", True,
                                 "Combination exists; create-only mode.")

            if existing_id:
                combo_id = existing_id
                existing = await self.client.get_xml(f"combinations/{combo_id}")
                payload = xml_builder.build_update_xml(
                    existing, simple, lang_id=self.config.lang_id)
                await _with_retry(
                    lambda: self.client.update("combinations", combo_id, payload),
                    max_retries=self.config.max_retries)
                action = "updated"
            else:
                if self.config.mode == Mode.UPDATE_ONLY:
                    return RowResult(index, reference, "skipped", True,
                                     "Combination not found; update-only mode.")
                payload = xml_builder.build_create_xml(
                    schema, simple, associations=associations,
                    lang_id=self.config.lang_id)
                created = await _with_retry(
                    lambda: self.client.create("combinations", payload),
                    max_retries=self.config.max_retries)
                combo_id = created.get("id")
                action = "created"

            # PrestaShop 8 only shows the Combinations tab when the product's
            # type is "combinations"; API-created products are "standard".
            if product_id not in self._typed_products:
                try:
                    await self._ensure_combination_type(product_id)
                except Exception as exc:
                    notes.append(f"product type not switched ({exc})")
                self._typed_products.add(product_id)

            if combo_id and "images" in self.config.scope and str(row.get("images", "")).strip():
                try:
                    n = await self._attach_images(product_id, combo_id,
                                                  split_list(str(row["images"])))
                    notes.append(f"{n} image(s) attached" if n
                                 else "images: nothing uploaded")
                except Exception as exc:
                    notes.append(f"images not attached ({exc})")

            if combo_id and "stock" in self.config.scope:
                try:
                    await self._set_stock(product_id, combo_id, row.get("quantity"))
                except Exception as exc:  # combination itself already succeeded
                    notes.append(f"stock not set ({exc})")

            # Per-combination description, written in the same pass. The
            # combination_descriptions resource is idempotent, so this is safe on
            # re-runs. Requires the Combination Descriptions module + its
            # Webservice permission.
            if (combo_id and "descriptions" in self.config.scope
                    and (str(row.get("description", "")).strip()
                         or str(row.get("description_short", "")).strip())):
                try:
                    d_action = await self.desc_importer.write_for_combination(
                        int(combo_id), product_id, self._desc_shop_id(row),
                        row.get("description"), row.get("description_short"))
                    if d_action:
                        notes.append(f"description {d_action}")
                except Exception as exc:  # combination itself already succeeded
                    notes.append(f"description not set ({exc})")

            extra = f" ({len(value_ids)} attribute(s))"
            if notes:
                extra += " Note: " + "; ".join(notes)
            return RowResult(index, reference, action, True,
                             f"Combination {action}.{extra}",
                             product_id=combo_id)

        except Exception as exc:  # continue-on-error
            return RowResult(index, reference, "error", False,
                             message=f"{type(exc).__name__}: {exc}")

    async def _impact_value(self, raw: str, product_id: int) -> str:
        """Combination price impact (net delta) to store.

        Converts a tax-included impact to tax-excluded, and — when round-up is on
        — rounds the combination's FINAL gross price (base + impact) up to the
        configured increment, then back-calculates the net impact.
        """
        num = parse_number(raw)
        if num is None:
            return raw
        includes = self.config.price_includes_tax and self.config.tax_rate > 0
        if not includes:
            return raw  # stored as-is (no tax context to round against)

        divisor = Decimal(1) + Decimal(str(self.config.tax_rate)) / Decimal(100)
        impact_gross = Decimal(str(num))

        if self.config.round_up_to > 0:
            base_net = await self._product_base_net(product_id)
            final_gross = base_net * divisor + impact_gross
            final_gross = ceil_to(final_gross, Decimal(str(self.config.round_up_to)))
            impact_net = final_gross / divisor - base_net
            return f"{impact_net:.6f}"
        return f"{(impact_gross / divisor):.6f}"

    async def _product_base_net(self, product_id: int) -> Decimal:
        """Fetch (and cache) the product's stored base net price."""
        if product_id in self._base_net_cache:
            return self._base_net_cache[product_id]
        price = Decimal(0)
        try:
            data = await self.client.get_json(
                f"products/{product_id}", params={"display": "[price]"})
            prod = data.get("product") if isinstance(data, dict) else None
            if isinstance(prod, dict) and prod.get("price") not in (None, ""):
                price = Decimal(str(prod["price"]))
        except Exception:
            price = Decimal(0)
        self._base_net_cache[product_id] = price
        return price

    @staticmethod
    def _desc_shop_id(row: dict[str, object]) -> int | None:
        """Optional id_shop for the description row (None -> module default)."""
        raw = row.get("id_shop")
        if raw in (None, ""):
            return None
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None

    async def _attach_images(self, product_id: int, combo_id: int,
                             urls: list[str]) -> int:
        """Upload combination images to the product and link them to the combo.

        Skips if the combination already has images, so re-runs don't pile up
        duplicate product images. Attribute links are preserved.
        """
        from .api_client import _localname  # local import, avoid cycle at top
        combo_xml = await self.client.get_xml(f"combinations/{combo_id}")
        root = ET.fromstring(combo_xml)
        combo_el = next(iter(root))
        assoc = next((el for el in combo_el
                      if _localname(el.tag) == "associations"), None)
        if assoc is not None:
            images_el = next((el for el in assoc
                              if _localname(el.tag) == "images"), None)
            if images_el is not None and len(images_el):
                return 0  # already has images

        ids: list[int] = []
        errors: list[str] = []
        for url in urls:
            name = url.rsplit("/", 1)[-1] or url
            try:
                data, filename, ctype = await self.resolver.fetch_image(url)
                res = await self.client.upload_image(product_id, data, filename, ctype)
                if res.get("id"):
                    ids.append(res["id"])
                else:
                    errors.append(f"{name}: upload returned no id")
            except Exception as exc:  # keep going, but remember why
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if not ids:
            # Surface the real reason instead of silently returning 0.
            if errors:
                raise RuntimeError("; ".join(errors[:2]))
            return 0

        if assoc is None:
            assoc = ET.SubElement(combo_el, "associations")
        images_el = ET.SubElement(assoc, "images")
        for iid in ids:
            im = ET.SubElement(images_el, "image")
            ET.SubElement(im, "id").text = str(iid)
        payload = xml_builder.build_update_xml(
            ET.tostring(root, encoding="unicode"), {}, lang_id=self.config.lang_id)
        await _with_retry(
            lambda: self.client.update("combinations", combo_id, payload),
            max_retries=self.config.max_retries)
        return len(ids)

    async def _ensure_combination_type(self, product_id: int) -> None:
        """Switch the parent product to "Product with combinations" (PS 8)."""
        existing = await self.client.get_xml(f"products/{product_id}")
        payload = xml_builder.build_update_xml(
            existing, {"product_type": "combinations"}, lang_id=self.config.lang_id)
        await _with_retry(
            lambda: self.client.update("products", product_id, payload),
            max_retries=self.config.max_retries)

    async def _find_combination(self, reference: str) -> int | None:
        data = await self.client.get_json(
            "combinations",
            params={"filter[reference]": reference, "display": "[id]"})
        items = data.get("combinations") if isinstance(data, dict) else None
        if items:
            first = items[0]
            cid = first.get("id") if isinstance(first, dict) else first
            return int(cid) if cid else None
        return None

    async def _set_stock(self, product_id: int, combo_id: int,
                         quantity: object) -> None:
        if quantity in (None, ""):
            return
        data = await self.client.get_json(
            "stock_availables",
            params={"filter[id_product]": product_id,
                    "filter[id_product_attribute]": combo_id,
                    "display": "[id]"})
        stocks = data.get("stock_availables") if isinstance(data, dict) else None
        if not stocks:
            return
        sa_id = stocks[0].get("id") if isinstance(stocks[0], dict) else stocks[0]
        existing = await self.client.get_xml(f"stock_availables/{sa_id}")
        payload = xml_builder.build_update_xml(
            existing, {"quantity": str(quantity)}, lang_id=self.config.lang_id)
        await _with_retry(
            lambda: self.client.update("stock_availables", int(sa_id), payload),
            max_retries=self.config.max_retries)
