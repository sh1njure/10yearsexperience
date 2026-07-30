"""Bulk-import per-combination descriptions.

Targets the ``combination_descriptions`` Webservice resource added by the
``combinationdescriptions`` PrestaShop module (a per-combination description
field PrestaShop lacks natively).

A descriptions sheet has one row per combination. Each row identifies its
combination by **reference** (the combination/``id_product_attribute`` reference,
the same key the combinations importer writes), which we resolve to an
``id_product_attribute`` via the ``combinations`` resource. A row may instead
carry an explicit ``id_product_attribute`` to skip the lookup.

The resource is idempotent on ``(id_product_attribute, id_shop)`` server-side, so
re-running a sheet updates in place and never duplicates. We still look the row
up first so the UI can report created / updated / skipped and honour the
create-only / update-only modes, exactly like the other importers.

Reuses the products importer's retry / result / progress plumbing.
"""
from __future__ import annotations

import asyncio

from .api_client import PrestaShopClient
from .importer import ImportConfig, Mode, RowResult, ProgressCb, _with_retry
from . import xml_builder

RESOURCE = "combination_descriptions"


class CombinationDescriptionImporter:
    def __init__(self, client: PrestaShopClient, config: ImportConfig):
        self.client = client
        self.config = config
        self._blank_schema: str | None = None
        # Cache reference -> (id_product_attribute, id_product) lookups so a
        # sheet that repeats a reference doesn't re-hit the API.
        self._combo_cache: dict[str, tuple[int, int] | None] = {}

    async def _schema(self) -> str:
        if self._blank_schema is None:
            self._blank_schema = await self.client.get_xml(
                RESOURCE, params={"schema": "blank"})
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
        try:
            id_pa, id_product = await self._resolve_combination(row, reference)
            if id_pa is None:
                return RowResult(
                    index, reference, "error", False,
                    "Combination not found "
                    f"(reference '{reference}' / id_product_attribute unset).")

            id_shop = self._shop_id(row)

            values: dict[str, object] = {"id_product_attribute": str(id_pa)}
            if id_product:
                values["id_product"] = str(id_product)
            if id_shop is not None:
                values["id_shop"] = str(id_shop)

            has_text = False
            for src, dst in (("description", "description"),
                             ("description_short", "description_short")):
                val = row.get(src)
                if val not in (None, ""):
                    values[dst] = str(val)
                    has_text = True

            if not has_text:
                return RowResult(index, reference, "skipped", True,
                                 "No description text in this row.",
                                 product_id=id_pa)

            schema = await self._schema()

            if self.config.dry_run:
                payload = xml_builder.build_create_xml(
                    schema, values, lang_id=self.config.lang_id)
                return RowResult(index, reference, "dry-run", True,
                                 "Description payload built (not sent).",
                                 product_id=id_pa, payload=payload)

            action = await self._upsert(id_pa, id_shop, values, schema)
            if action == "skipped":
                return RowResult(index, reference, "skipped", True,
                                 "Description exists/absent for the selected mode.",
                                 product_id=id_pa)
            return RowResult(index, reference, action, True,
                             f"Description {action} (id_product_attribute {id_pa}).",
                             product_id=id_pa)

        except Exception as exc:  # continue-on-error
            return RowResult(index, reference, "error", False,
                             message=f"{type(exc).__name__}: {exc}")

    async def _upsert(self, id_pa: int, id_shop: int | None,
                      values: dict[str, object], schema: str) -> str:
        """Create or update the description row; return the action taken.

        Honours the create-only / update-only / upsert mode. Returns one of
        "created", "updated" or "skipped".
        """
        existing_id = await self._find_description(id_pa, id_shop)
        if existing_id:
            if self.config.mode == Mode.CREATE_ONLY:
                return "skipped"
            existing = await self.client.get_xml(f"{RESOURCE}/{existing_id}")
            payload = xml_builder.build_update_xml(
                existing, values, lang_id=self.config.lang_id)
            await _with_retry(
                lambda: self.client.update(RESOURCE, existing_id, payload),
                max_retries=self.config.max_retries)
            return "updated"
        if self.config.mode == Mode.UPDATE_ONLY:
            return "skipped"
        payload = xml_builder.build_create_xml(
            schema, values, lang_id=self.config.lang_id)
        await _with_retry(
            lambda: self.client.create(RESOURCE, payload),
            max_retries=self.config.max_retries)
        return "created"

    async def write_for_combination(self, id_pa: int, id_product: int | None,
                                    id_shop: int | None, description: object,
                                    description_short: object) -> str | None:
        """Write one combination's description given an already-known combination.

        Used by the combinations importer to set a description in the same pass
        that creates/updates the combination. Returns the action ("created",
        "updated", "skipped") or None when the row carries no description text.
        """
        values: dict[str, object] = {"id_product_attribute": str(id_pa)}
        if id_product:
            values["id_product"] = str(id_product)
        if id_shop is not None:
            values["id_shop"] = str(id_shop)

        has_text = False
        if description not in (None, ""):
            values["description"] = str(description)
            has_text = True
        if description_short not in (None, ""):
            values["description_short"] = str(description_short)
            has_text = True
        if not has_text:
            return None

        schema = await self._schema()
        return await self._upsert(id_pa, id_shop, values, schema)

    def _shop_id(self, row: dict[str, object]) -> int | None:
        """Explicit id_shop from the row, else None (module defaults to the
        context shop)."""
        raw = row.get("id_shop")
        if raw in (None, ""):
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    async def _resolve_combination(self, row: dict[str, object],
                                   reference: str) -> tuple[int | None, int | None]:
        """Return (id_product_attribute, id_product) for the row.

        Prefers an explicit id_product_attribute column; otherwise looks the
        combination up by reference.
        """
        explicit = row.get("id_product_attribute")
        if explicit not in (None, "") and str(explicit).strip().isdigit():
            id_pa = int(str(explicit).strip())
            id_product = None
            raw_prod = row.get("id_product")
            if raw_prod not in (None, "") and str(raw_prod).strip().isdigit():
                id_product = int(str(raw_prod).strip())
            return id_pa, id_product

        if not reference:
            return None, None
        if reference in self._combo_cache:
            cached = self._combo_cache[reference]
            return (cached if cached is not None else (None, None))

        data = await self.client.get_json(
            "combinations",
            params={"filter[reference]": reference,
                    "display": "[id,id_product]"})
        items = data.get("combinations") if isinstance(data, dict) else None
        result: tuple[int, int] | None = None
        if items:
            first = items[0]
            if isinstance(first, dict):
                cid = first.get("id")
                pid = first.get("id_product")
                if cid:
                    result = (int(cid), int(pid) if pid else 0)
        self._combo_cache[reference] = result
        return (result if result is not None else (None, None))

    async def _find_description(self, id_pa: int,
                                id_shop: int | None) -> int | None:
        """Find an existing combination_description row for this combination."""
        params = {"filter[id_product_attribute]": id_pa, "display": "[id]"}
        if id_shop is not None:
            params["filter[id_shop]"] = id_shop
        data = await self.client.get_json(RESOURCE, params=params)
        items = data.get(RESOURCE) if isinstance(data, dict) else None
        if items:
            first = items[0]
            rid = first.get("id") if isinstance(first, dict) else first
            return int(rid) if rid else None
        return None
