"""Export shop data as an .xlsx matching the import templates."""
from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from openpyxl import Workbook

from .. import exporter
from ..api_client import PrestaShopClient, PrestaShopError
from ..config import get_settings

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")


@router.get("/attributes-report", response_class=HTMLResponse)
async def attributes_report():
    """Browser page: all attribute groups/values, usage and duplicate/unused flags."""
    s = get_settings()
    if not s.normalized_url or not s.prestashop_api_key:
        raise HTTPException(400, "Configure the shop connection first.")
    async with PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                default_lang_id=s.default_lang_id) as client:
        try:
            report = await exporter.attributes_report(client, s.default_lang_id)
        except PrestaShopError as exc:
            raise HTTPException(502, f"Report failed: {exc}") from exc

    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    rows = []
    text_lines = []
    for g in report["groups"]:
        gflag = " [DUPLICATE GROUP]" if g["dup_group"] else ""
        gcolor = "#fde8e8" if g["dup_group"] else "#eef2ff"
        rows.append(f'<tr style="background:{gcolor}"><td colspan="4"><b>Group #{g["id"]} — {esc(g["name"])}</b>'
                    f'{gflag} · {g["value_count"]} values · used by {g["used_total"]} combos</td></tr>')
        text_lines.append(f'GROUP #{g["id"]} {g["name"]}{gflag} (values={g["value_count"]}, used={g["used_total"]})')
        for v in g["values"]:
            flags = []
            if v["dup_value"]:
                flags.append("DUP")
            if v["used"] == 0:
                flags.append("UNUSED")
            flag_txt = ", ".join(flags)
            vcolor = "#fff5f5" if (v["dup_value"] and v["used"] == 0) else ("#fffbea" if v["used"] == 0 else "#fff")
            rows.append(f'<tr style="background:{vcolor}"><td></td><td>#{v["id"]}</td>'
                        f'<td>{esc(v["name"])}</td><td>used by {v["used"]} · '
                        f'<b style="color:#b91c1c">{flag_txt}</b></td></tr>')
            text_lines.append(f'  value #{v["id"]} "{v["name"]}" used={v["used"]} {flag_txt}')

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Attributes report</title>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#0f172a}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td{{border:1px solid #e5e7eb;padding:6px 10px}}
h1{{font-size:20px}} .legend span{{display:inline-block;margin-right:16px}} pre{{background:#0b1020;color:#d1d5db;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}</style></head>
<body><h1>Attributes report</h1>
<p class="legend">
<span style="background:#fde8e8;padding:2px 8px">duplicate group</span>
<span style="background:#fffbea;padding:2px 8px">unused value (0 combos)</span>
<span style="background:#fff5f5;padding:2px 8px">unused + duplicate → safe to delete</span></p>
<table><tr><td></td><td>id</td><td>name</td><td>usage / flags</td></tr>{''.join(rows)}</table>
<h2 style="font-size:16px;margin-top:24px">Copy this to share:</h2>
<pre>{esc(chr(10).join(text_lines))}</pre>
</body></html>"""
    return HTMLResponse(html)


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
