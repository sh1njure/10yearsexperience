"""Read supplier spreadsheets (.xlsx / .csv) for preview and mapping.

Uses pandas (with openpyxl for xlsx) so both formats share one code path. The
UI drives two decisions this module supports: which sheet, and which row is the
header row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class SheetPreview:
    sheet: str
    # Raw rows as lists of stringified cells (header row not yet applied).
    rows: list[list[str]] = field(default_factory=list)
    n_rows_total: int = 0


@dataclass
class ParsedTable:
    headers: list[str]
    rows: list[dict[str, str]]      # each row keyed by header
    sheet: str
    header_row: int


def list_sheets(path: str | Path) -> list[str]:
    """Return sheet names. CSV files report a single synthetic sheet."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return ["CSV"]
    xls = pd.ExcelFile(path, engine="openpyxl")
    return list(xls.sheet_names)


def preview(path: str | Path, sheet: str | None = None,
            n: int = 10) -> SheetPreview:
    """Return the first ``n`` raw rows of a sheet, header-agnostic.

    Cells are stringified; NaN becomes an empty string. This feeds the "confirm
    the header row" UI, so no header is applied yet (``header=None``).
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
        sheet_name = "CSV"
    else:
        sheet_name = sheet or list_sheets(path)[0]
        df = pd.read_excel(path, sheet_name=sheet_name, header=None,
                           dtype=str, engine="openpyxl")
        df = df.fillna("")

    total = len(df)
    head = df.head(n)
    rows = [[_clean(c) for c in row] for row in head.itertuples(index=False)]
    return SheetPreview(sheet=sheet_name, rows=rows, n_rows_total=total)


def parse(path: str | Path, sheet: str | None = None,
          header_row: int = 0) -> ParsedTable:
    """Parse the full table using ``header_row`` (0-indexed) as the header.

    Returns headers plus row dicts keyed by header name. Duplicate/blank header
    names are disambiguated so downstream mapping keys stay unique.
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, header=header_row, dtype=str, keep_default_na=False)
        sheet_name = "CSV"
    else:
        sheet_name = sheet or list_sheets(path)[0]
        df = pd.read_excel(path, sheet_name=sheet_name, header=header_row,
                           dtype=str, engine="openpyxl")
        df = df.fillna("")

    headers = _dedupe_headers([str(c) for c in df.columns])
    df.columns = headers
    rows = [
        {h: _clean(row[i]) for i, h in enumerate(headers)}
        for row in df.itertuples(index=False)
    ]
    return ParsedTable(headers=headers, rows=rows, sheet=sheet_name,
                       header_row=header_row)


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() in ("nan", "nat", "none") else text.strip()


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for i, h in enumerate(headers):
        name = h.strip() or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        result.append(name)
    return result
