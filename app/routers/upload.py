"""Upload + spreadsheet preview endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from .. import excel_parser, state

router = APIRouter(prefix="/api/upload", tags=["upload"])


@router.post("")
async def upload(file: UploadFile) -> dict:
    """Accept an .xlsx/.csv, store it, return a token and the sheet list."""
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "Only .xlsx, .xls or .csv files are supported.")
    content = await file.read()
    session = state.new_session(file.filename or "upload", content)
    try:
        sheets = excel_parser.list_sheets(session.path)
    except Exception as exc:
        raise HTTPException(400, f"Could not read file: {exc}") from exc
    return {"token": session.token, "filename": session.filename, "sheets": sheets}


@router.get("/{token}/preview")
def preview(token: str, sheet: str | None = None) -> dict:
    """Return the first 10 raw rows so the user can pick the header row."""
    session = _require(token)
    prev = excel_parser.preview(session.path, sheet=sheet, n=10)
    return {"sheet": prev.sheet, "rows": prev.rows,
            "n_rows_total": prev.n_rows_total}


class ParseIn(BaseModel):
    sheet: str | None = None
    header_row: int = 0


@router.post("/{token}/parse")
def parse(token: str, payload: ParseIn) -> dict:
    """Parse the full table with the confirmed header row; cache it."""
    session = _require(token)
    table = excel_parser.parse(session.path, sheet=payload.sheet,
                               header_row=payload.header_row)
    session.sheet = table.sheet
    session.header_row = table.header_row
    session.headers = table.headers
    session.rows = table.rows
    return {"sheet": table.sheet, "header_row": table.header_row,
            "headers": table.headers, "row_count": len(table.rows),
            "sample": table.rows[:5]}


def _require(token: str) -> state.UploadSession:
    session = state.get_session(token)
    if session is None:
        raise HTTPException(404, "Upload session not found — re-upload the file.")
    return session
