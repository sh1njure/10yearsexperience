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
from enum import Enum
from typing import Awaitable, Callable

from .api_client import PrestaShopClient, PrestaShopError
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


@dataclass
class ImportConfig:
    mode: Mode = Mode.UPSERT
    dry_run: bool = True
    concurrency: int = 2
    lang_id: int = 1
    max_retries: int = 3
    scope: set[str] = field(default_factory=lambda: {"products"})


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

    async def _process_row(self, index: int, row: dict[str, object]) -> RowResult:
        reference = str(row.get("reference", "")).strip()
        try:
            schema = await self._product_schema()

            if self.config.dry_run:
                payload = xml_builder.build_create_xml(
                    schema, row, lang_id=self.config.lang_id,
                )
                return RowResult(index, reference, "dry-run", True,
                                 "Payload built (not sent).", payload=payload)

            existing_id = None
            if self.config.mode in (Mode.UPDATE_ONLY, Mode.UPSERT) and reference:
                existing_id = await self._find_by_reference(reference)

            if existing_id:
                if self.config.mode == Mode.CREATE_ONLY:
                    return RowResult(index, reference, "skipped", True,
                                     "Exists; create-only mode.")
                return await self._update(index, reference, existing_id, row)

            if self.config.mode == Mode.UPDATE_ONLY:
                return RowResult(index, reference, "skipped", True,
                                 "Not found; update-only mode.")
            return await self._create(index, reference, row, schema)

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

    async def _create(self, index: int, reference: str, row: dict[str, object],
                      schema: str) -> RowResult:
        payload = xml_builder.build_create_xml(
            schema, row, lang_id=self.config.lang_id,
        )
        created = await _with_retry(
            lambda: self.client.create("products", payload),
            max_retries=self.config.max_retries,
        )
        product_id = created.get("id")
        if product_id and "stock" in self.config.scope and "quantity" in row:
            await self._set_stock(product_id, row.get("quantity"))
        return RowResult(index, reference, "created", True,
                         "Created.", product_id=product_id)

    async def _update(self, index: int, reference: str, product_id: int,
                      row: dict[str, object]) -> RowResult:
        # GET the full resource, modify mapped fields, PUT it all back.
        existing = await self.client.get_xml(f"products/{product_id}")
        payload = xml_builder.build_update_xml(
            existing, row, lang_id=self.config.lang_id,
        )
        await _with_retry(
            lambda: self.client.update("products", product_id, payload),
            max_retries=self.config.max_retries,
        )
        if "stock" in self.config.scope and "quantity" in row:
            await self._set_stock(product_id, row.get("quantity"))
        return RowResult(index, reference, "updated", True,
                         "Updated.", product_id=product_id)

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
