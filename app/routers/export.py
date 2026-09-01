"""Export shop data as an .xlsx matching the import templates."""
from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from .. import exporter
from ..api_client import PrestaShopClient, PrestaShopError
from ..config import get_settings

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")


@router.get("/{kind}")
async def export(kind: str):
    """Export 'products' or 'combinations' as a downloadable .xlsx."""
    if kind not in ("products", "combinations"):
        raise HTTPException(400, "kind must be 'products' or 'combinations'.")

    s = get_settings()
    if not s.normalized_url or not s.prestashop_api_key:
        raise HTTPException(400, "Configure the shop connection first.")

    async with PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                default_lang_id=s.default_lang_id) as client:
        try:
            if kind == "products":
                rows = await exporter.export_products(client, s.default_lang_id)
                sheet_name = "PRODUCT EXPORT"
            else:
                rows = await exporter.export_combinations(client, s.default_lang_id)
                sheet_name = "COMBINATIONS EXPORT"
        except PrestaShopError as exc:
            raise HTTPException(502, f"Export failed: {exc}") from exc

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    for row in rows:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"{kind}_export_{stamp}.xlsx"
    return StreamingResponse(
        buf, media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
