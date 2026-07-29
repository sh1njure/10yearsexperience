"""Import orchestration: build payloads and (optionally) push them.

Responsibilities:

* Dry run (default): build every XML payload and report it without sending.
* Live import: row-by-row with bounded concurrency, retry-with-backoff on 5xx,
  and continue-on-error so one bad row never kills the run.
* Multi-step product creation: product -> stock_available -> combinations ->
  images, gated by the selected import scope.

Progress is reported through an async callback so the web layer can stream it
(SSE) without this module knowing about HTTP.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Awaitable, Callable

from .api_client import PrestaShopClient, PrestaShopError
from .resolver import Resolver, parse_features, split_list
from .validator import parse_number
from . import xml_builder


class Mode(str, Enum):
    CREATE_ONLY = "create_only"
    UPDATE_ONLY = "update_only"
    UPSERT = "upsert"


@dataclass
class RowResult:
    row: int
    reference: str
    action: str                    # created | updated | skipped | error | dry-run
    success: bool
    message: str = ""
    product_id: int | None = None
    payload: str | None = None     # populated in dry-run mode


# Mapped column keys that are associations/relations, not plain product fields.
# They are resolved by name into IDs (see app.resolver) and injected as
# <associations>, never written as simple element text.
SPECIAL_FIELDS = {"categories", "tags", "images", "features"}


@dataclass
class ImportConfig:
    mode: Mode = Mode.UPSERT
    dry_run: bool = True
    concurrency: int = 2
    lang_id: int = 1
    max_retries: int = 3
    scope: set[str] = field(default_factory=lambda: {"products"})
    # Create categories/brands/tags/features that don't exist yet.
    create_missing: bool = False
    # If the mapped price includes tax, divide by (1 + tax_rate/100) so the
    # tax-excluded value the API expects gets stored.
    price_includes_tax: bool = False
    tax_rate: float = 0.0


ProgressCb = Callable[[RowResult], Awaitable[None]] | None


async def _with_retry(coro_factory: Callable[[], Awaitable], *,
                      max_retries: int) -> object:
    """Await ``coro_factory()`` retrying on 5xx with exponential backoff."""
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except PrestaShopError as exc:
            last_exc = exc
            retryable = exc.status_code is not None and 500 <= exc.status_code < 600
            if not retryable or attempt == max_retries:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited unexpectedly")


class Importer:
    def __init__(self, client: PrestaShopClient, config: ImportConfig):
        self.client = client
        self.config = config
        self._blank_product_schema: str | None = None
        # In dry run we resolve names (read-only lookups) but never create.
        self.resolver = Resolver(
            client, lang_id=config.lang_id,
            create_missing=config.create_missing and not config.dry_run,
        )

    async def _product_schema(self) -> str:
        if self._blank_product_schema is None:
            self._blank_product_schema = await self.client.get_xml(
                "products", params={"schema": "blank"}
            )
        return self._blank_product_schema

    async def run(self, rows: list[dict[str, object]],
                  progress: ProgressCb = None) -> list[RowResult]:
        """Import all rows with bounded concurrency, preserving row order."""
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

    async def _prepare(self, row: dict[str, object]) -> tuple[dict, dict, list[str], list[str]]:
        """Split a row into (simple fields, associations, image URLs, notes).

        Resolves category/brand/tag/feature names to IDs. Notes record names
        that could not be resolved (surfaced in the row result).
        """
        simple = {k: v for k, v in row.items()
                  if k not in SPECIAL_FIELDS and v not in (None, "")}

        # link_rewrite must be a valid slug or the product silently fails
        # validation. If it isn't mapped, derive it from the name.
        if not str(simple.get("link_rewrite", "")).strip() and simple.get("name"):
            simple["link_rewrite"] = xml_builder.slugify(str(simple["name"]))

        # A product created via the Webservice without state=1 is treated as a
        # temporary draft: hidden from the catalog list and later auto-deleted.
        # Force the "saved" state so the product actually shows up.
        simple.setdefault("state", "1")

        # Convert a tax-included price to the tax-excluded value the API stores.
        if (self.config.price_includes_tax and self.config.tax_rate > 0
                and simple.get("price")):
            num = parse_number(str(simple["price"]))
            if num is not None:
                divisor = Decimal(1) + Decimal(str(self.config.tax_rate)) / Decimal(100)
                simple["price"] = f"{(num / divisor):.6f}"

        associations: dict[str, object] = {}
        image_urls: list[str] = []
        notes: list[str] = []

        # Brand: id_manufacturer may arrive as a name -> resolve to an id.
        man = simple.get("id_manufacturer")
        if man is not None and not str(man).strip().isdigit():
            mid = await self.resolver.resolve_manufacturer(str(man))
            if mid:
                simple["id_manufacturer"] = str(mid)
            else:
                simple.pop("id_manufacturer", None)
                notes.append(f"brand '{man}' not found")

        # Categories: names -> ids; also default the primary category.
        if "categories" in row and str(row["categories"]).strip():
            names = split_list(str(row["categories"]))
            ids = await self.resolver.resolve_categories(names)
            if ids:
                associations["categories"] = ids
                simple.setdefault("id_category_default", str(ids[-1]))
            missing = len(names) - len(ids)
            if missing:
                notes.append(f"{missing} categor{'y' if missing == 1 else 'ies'} not found")

        # Tags
        if "tags" in row and str(row["tags"]).strip():
            tag_ids = await self.resolver.resolve_tags(split_list(str(row["tags"])))
            if tag_ids:
                associations["tags"] = tag_ids

        # Features "Name:Value:Pos:Custom,..."
        if "features" in row and str(row["features"]).strip():
            specs = parse_features(str(row["features"]))
            pairs = await self.resolver.resolve_features(specs)
            if pairs:
                associations["product_features"] = pairs
            if len(pairs) < len(specs):
                notes.append(f"{len(specs) - len(pairs)} feature(s) unresolved")

        # Images: collect URLs, uploaded after the product exists.
        if "images" in row and str(row["images"]).strip():
            image_urls = split_list(str(row["images"]))

        return simple, associations, image_urls, notes

    async def _process_row(self, index: int, row: dict[str, object]) -> RowResult:
        reference = str(row.get("reference", "")).strip()
        try:
            schema = await self._product_schema()
            simple, associations, image_urls, notes = await self._prepare(row)

            if self.config.dry_run:
                payload = xml_builder.build_create_xml(
                    schema, simple, lang_id=self.config.lang_id,
                    associations=associations,
                )
                msg = "Payload built (not sent)."
                if image_urls:
                    msg += f" {len(image_urls)} image(s) would upload."
                if notes:
                    msg += " Note: " + "; ".join(notes) + "."
                return RowResult(index, reference, "dry-run", True, msg,
                                 payload=payload)

            existing_id = None
            if self.config.mode in (Mode.UPDATE_ONLY, Mode.UPSERT) and reference:
                existing_id = await self._find_by_reference(reference)

            if existing_id:
                if self.config.mode == Mode.CREATE_ONLY:
                    return RowResult(index, reference, "skipped", True,
                                     "Exists; create-only mode.")
                return await self._update(index, reference, existing_id, simple,
                                          associations, image_urls, row, notes)

            if self.config.mode == Mode.UPDATE_ONLY:
                return RowResult(index, reference, "skipped", True,
                                 "Not found; update-only mode.")
            return await self._create(index, reference, simple, associations,
                                      image_urls, schema, row, notes)

        except PrestaShopError as exc:
            return RowResult(index, reference, "error", False,
                             message=str(exc))
        except Exception as exc:  # never let one row kill the run
            return RowResult(index, reference, "error", False,
                             message=f"Unexpected error: {exc}")

    async def _find_by_reference(self, reference: str) -> int | None:
        data = await self.client.get_json(
            "products", params={"filter[reference]": reference, "display": "[id]"},
        )
        products = data.get("products") if isinstance(data, dict) else None
        if products:
            first = products[0]
            pid = first.get("id") if isinstance(first, dict) else first
            return int(pid) if pid else None
        return None

    async def _create(self, index: int, reference: str, simple: dict,
                      associations: dict, image_urls: list[str], schema: str,
                      row: dict, notes: list[str]) -> RowResult:
        payload = xml_builder.build_create_xml(
            schema, simple, lang_id=self.config.lang_id,
            associations=associations,
        )
        created = await _with_retry(
            lambda: self.client.create("products", payload),
            max_retries=self.config.max_retries,
        )
        product_id = created.get("id")
        extra = await self._post_write(product_id, row, image_urls)
        return RowResult(index, reference, "created", True,
                         self._msg("Created.", notes, extra),
                         product_id=product_id)

    async def _update(self, index: int, reference: str, product_id: int,
                      simple: dict, associations: dict, image_urls: list[str],
                      row: dict, notes: list[str]) -> RowResult:
        # GET the full resource, modify mapped fields, PUT it all back.
        existing = await self.client.get_xml(f"products/{product_id}")
        payload = xml_builder.build_update_xml(
            existing, simple, lang_id=self.config.lang_id,
        )
        await _with_retry(
            lambda: self.client.update("products", product_id, payload),
            max_retries=self.config.max_retries,
        )
        extra = await self._post_write(product_id, row, image_urls)
        return RowResult(index, reference, "updated", True,
                         self._msg("Updated.", notes, extra),
                         product_id=product_id)

    async def _post_write(self, product_id: int | None, row: dict,
                          image_urls: list[str]) -> list[str]:
        """Stock + image steps that need the product id (after create/update)."""
        extra: list[str] = []
        if not product_id:
            return extra
        if "stock" in self.config.scope and str(row.get("quantity", "")).strip():
            try:
                await self._set_stock(product_id, row.get("quantity"))
            except Exception as exc:  # product already saved; don't fail the row
                extra.append(f"stock not set ({exc})")
        if image_urls and "images" in self.config.scope:
            ok = await self._upload_images(product_id, image_urls)
            extra.append(f"{ok}/{len(image_urls)} image(s) uploaded")
        return extra

    async def _upload_images(self, product_id: int, urls: list[str]) -> int:
        uploaded = 0
        for url in urls:
            try:
                data, filename, ctype = await self.resolver.fetch_image(url)
                await _with_retry(
                    lambda d=data, f=filename, c=ctype:
                        self.client.upload_image(product_id, d, f, c),
                    max_retries=self.config.max_retries,
                )
                uploaded += 1
            except Exception:
                continue  # continue-on-error per image
        return uploaded

    @staticmethod
    def _msg(base: str, notes: list[str], extra: list[str]) -> str:
        parts = [base] + extra
        if notes:
            parts.append("Note: " + "; ".join(notes))
        return " ".join(parts)

    async def _set_stock(self, product_id: int, quantity: object) -> None:
        """Set quantity via stock_availables (never on the product itself)."""
        if quantity in (None, ""):
            return
        data = await self.client.get_json(
            "stock_availables",
            params={"filter[id_product]": product_id, "display": "[id]"},
        )
        stocks = data.get("stock_availables") if isinstance(data, dict) else None
        if not stocks:
            return
        sa_id = stocks[0].get("id") if isinstance(stocks[0], dict) else stocks[0]
        existing = await self.client.get_xml(f"stock_availables/{sa_id}")
        payload = xml_builder.build_update_xml(
            existing, {"quantity": str(quantity)}, lang_id=self.config.lang_id,
        )
        await _with_retry(
            lambda: self.client.update("stock_availables", int(sa_id), payload),
            max_retries=self.config.max_retries,
        )
