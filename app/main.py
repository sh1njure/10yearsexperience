"""FastAPI application entrypoint.

Serves the single-page frontend and the JSON/SSE API. Localhost-only by design
(see HOST in .env). Run with::

    uvicorn app.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, get_settings
from .db import init_db
from .routers import import_ as import_router
from .routers import export as export_router
from .routers import mapping, settings, upload

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PrestaShop Supplier Importer", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "configured": bool(s.prestashop_url and
                                               s.prestashop_api_key)}


app.include_router(settings.router)
app.include_router(upload.router)
app.include_router(mapping.router)
app.include_router(import_router.router)
app.include_router(export_router.router)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Static assets (JS/CSS). Mounted last so API routes take precedence.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
