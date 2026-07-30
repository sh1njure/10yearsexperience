"""Validation, dry-run and import-execution endpoints."""
from __future__ import annotations

import asyncio
import io
import csv
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel

from .. import db, state, validator
from ..api_client import PrestaShopClient
from ..config import get_settings
from ..importer import Importer, ImportConfig, Mode
from ..combinations import CombinationImporter
from ..combination_descriptions import CombinationDescriptionImporter

router = APIRouter(prefix="/api/import", tags=["import"])


def _build_rows(session: state.UploadSession) -> list[dict]:
    """Project spreadsheet rows into PrestaShop-field dicts.

    Applies the confirmed column map (field -> header) and constant values.
    """
    out: list[dict] = []
    for row in session.rows:
        mapped: dict[str, object] = {}
        for field_name, header in session.column_map.items():
            if header in row:
                mapped[field_name] = row[header]
        for field_name, value in session.constants.items():
            mapped.setdefault(field_name, value)
        out.append(mapped)
    return out


def _mapped_fields(session: state.UploadSession) -> set[str]:
    return set(session.column_map) | set(session.constants)


@router.post("/{token}/validate")
def validate_rows(token: str) -> dict:
    session = _require(token)
    rows = _build_rows(session)
    issues = validator.validate(rows, _mapped_fields(session))
    return {
        "total_rows": len(rows),
        "error_count": sum(1 for i in issues if i.severity == "error"),
        "warning_count": sum(1 for i in issues if i.severity == "warning"),
        "blocking": validator.has_blocking_errors(issues),
        "issues": [
            {"row": i.row, "field": i.field, "message": i.message,
             "severity": i.severity}
            for i in issues
        ],
    }


class RunIn(BaseModel):
    mode: str = "upsert"
    dry_run: bool = True
    concurrency: int = 2
    scope: list[str] = ["products"]
    create_missing: bool = False
    price_includes_tax: bool = False
    tax_rate: float = 0.0
    import_type: str = "products"
    profile_name: str | None = None


@router.post("/{token}/run")
async def run(token: str, payload: RunIn):
    """Run an import, streaming per-row progress as SSE.

    Dry run is the default: payloads are built and returned, nothing is sent.
    """
    session = _require(token)
    rows = _build_rows(session)

    combinations_mode = payload.import_type == "combinations"
    descriptions_mode = payload.import_type == "combination_descriptions"

    # Product validation only applies to product imports; combinations and
    # combination descriptions have a different required-field shape (checked
    # implicitly during resolution).
    if not combinations_mode and not descriptions_mode:
        issues = validator.validate(rows, _mapped_fields(session))
        if validator.has_blocking_errors(issues):
            raise HTTPException(400, "Validation errors block the import. "
                                     "Run /validate to see them.")

    s = get_settings()
    config = ImportConfig(
        mode=Mode(payload.mode),
        dry_run=payload.dry_run,
        concurrency=max(1, min(payload.concurrency, 5)),
        lang_id=s.default_lang_id,
        scope=set(payload.scope),
        create_missing=payload.create_missing,
        price_includes_tax=payload.price_includes_tax,
        tax_rate=payload.tax_rate,
    )
    run_id = db.create_run(payload.mode, payload.dry_run, len(rows),
                           payload.profile_name)

    async def event_stream():
        client = PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                  default_lang_id=s.default_lang_id)
        if descriptions_mode:
            importer = CombinationDescriptionImporter(client, config)
        elif combinations_mode:
            importer = CombinationImporter(client, config)
        else:
            importer = Importer(client, config)
        queue: asyncio.Queue = asyncio.Queue()
        collected: list = []

        async def progress(result):
            await queue.put(result)

        async def driver():
            try:
                await importer.run(rows, progress=progress)
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(driver())
        yield _sse({"type": "start", "run_id": run_id, "total": len(rows)})
        while True:
            item = await queue.get()
            if item is None:
                break
            collected.append(item)
            yield _sse({
                "type": "row", "row": item.row, "reference": item.reference,
                "action": item.action, "success": item.success,
                "message": item.message, "product_id": item.product_id,
                "payload": item.payload,
            })
        await task
        await client.aclose()

        succeeded = sum(1 for r in collected if r.success)
        failed = len(collected) - succeeded
        summary = [
            {"row": r.row, "reference": r.reference, "action": r.action,
             "success": r.success, "message": r.message}
            for r in sorted(collected, key=lambda x: x.row)
        ]
        db.finish_run(run_id, succeeded, failed, summary)
        yield _sse({"type": "done", "run_id": run_id,
                    "succeeded": succeeded, "failed": failed})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/history")
def history() -> dict:
    return {"runs": db.list_runs()}


@router.get("/history/{run_id}/csv")
def history_csv(run_id: int):
    for run_row in db.list_runs(limit=1000):
        if run_row["id"] == run_id:
            break
    else:
        raise HTTPException(404, "Run not found.")
    profile = db.get_profile  # noqa: F841 (kept for clarity)
    # summary is stored on the run; re-read directly.
    with db.connect() as conn:
        row = conn.execute("SELECT summary FROM import_runs WHERE id = ?",
                           (run_id,)).fetchone()
    summary = json.loads(row["summary"]) if row and row["summary"] else []
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["row", "reference", "action", "success", "message"])
    writer.writeheader()
    for r in summary:
        writer.writerow(r)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition":
                                      f"attachment; filename=import_{run_id}.csv"})


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _require(token: str) -> state.UploadSession:
    session = state.get_session(token)
    if session is None:
        raise HTTPException(404, "Upload session not found.")
    return session
