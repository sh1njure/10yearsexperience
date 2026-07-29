"""Settings + connection-test endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..api_client import PrestaShopClient, PrestaShopError
from ..config import get_settings, update_connection

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Resources whose schema the mapper cares about.
SCHEMA_RESOURCES = ["products", "categories", "stock_availables",
                    "combinations", "manufacturers"]


class ConnectionIn(BaseModel):
    url: str | None = None
    api_key: str | None = None
    default_lang_id: int | None = None


@router.get("")
def read_settings() -> dict:
    """Return current (non-secret) settings. The API key is masked."""
    s = get_settings()
    key = s.prestashop_api_key
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
    return {
        "url": s.normalized_url,
        "api_key_masked": masked,
        "has_api_key": bool(key),
        "default_lang_id": s.default_lang_id,
    }


@router.post("")
def write_settings(payload: ConnectionIn) -> dict:
    """Update the live connection settings (not persisted to .env)."""
    update_connection(payload.url, payload.api_key, payload.default_lang_id)
    return read_settings()


@router.post("/test")
async def test_connection(payload: ConnectionIn) -> dict:
    """Hit GET /api/ and report which resources the key can access."""
    s = update_connection(payload.url, payload.api_key, payload.default_lang_id)
    if not s.normalized_url or not s.prestashop_api_key:
        return {"ok": False, "error": "Shop URL and API key are required."}

    async with PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                default_lang_id=s.default_lang_id) as client:
        try:
            resources = await client.test_connection()
        except PrestaShopError as exc:
            return {"ok": False, "error": str(exc),
                    "status_code": exc.status_code}
    return {"ok": True, "resource_count": len(resources), "resources": resources}


@router.get("/schema/{resource}")
async def get_schema(resource: str) -> dict:
    """Fetch and parse a live blank schema for one resource."""
    s = get_settings()
    async with PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                default_lang_id=s.default_lang_id) as client:
        try:
            schema = await client.fetch_schema(resource)
        except PrestaShopError as exc:
            return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "resource": resource,
        "fields": [
            {"name": f.name, "required": f.required, "read_only": f.read_only,
             "multilingual": f.multilingual}
            for f in schema.fields
        ],
    }
