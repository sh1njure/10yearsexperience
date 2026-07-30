"""Auto-matching and mapping-profile endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, mapper, state
from ..api_client import PrestaShopClient, PrestaShopError
from ..config import get_settings

router = APIRouter(prefix="/api/mapping", tags=["mapping"])


# Canonical mapping targets for a combinations import (pseudo-fields the
# combination importer understands; not raw schema fields).
COMBINATION_TARGETS = [
    "product_reference", "attributes", "values", "reference",
    "supplier_reference", "ean13", "price_impact", "quantity",
    "minimal_quantity", "default", "images",
]

# Canonical mapping targets for a combination-descriptions import. The
# combination is identified by its reference (resolved to id_product_attribute)
# or by an explicit id_product_attribute; id_product / id_shop are optional.
COMBINATION_DESCRIPTION_TARGETS = [
    "reference", "id_product_attribute", "id_product", "id_shop",
    "description", "description_short",
]


@router.post("/{token}/automatch")
async def automatch(token: str, resource: str = "products",
                    import_type: str = "products") -> dict:
    """Auto-match the uploaded headers against the target field list.

    For products this is the live product schema (plus synthetic association
    targets); for combinations it is the combination pseudo-field list.
    """
    session = state.get_session(token)
    if session is None or not session.headers:
        raise HTTPException(404, "Parse the uploaded file first.")

    if import_type == "combinations":
        field_names = list(COMBINATION_TARGETS)
    elif import_type == "combination_descriptions":
        field_names = list(COMBINATION_DESCRIPTION_TARGETS)
    else:
        s = get_settings()
        async with PrestaShopClient(s.normalized_url, s.prestashop_api_key,
                                    default_lang_id=s.default_lang_id) as client:
            try:
                schema = await client.fetch_schema(resource)
            except PrestaShopError as exc:
                raise HTTPException(502, f"Could not fetch schema: {exc}") from exc
        # Augment product fields with synthetic association targets.
        field_names = schema.field_names()
        for special in ("categories", "tags", "images", "features"):
            if special not in field_names:
                field_names = field_names + [special]

    matches = mapper.match_headers(session.headers, field_names)
    return {
        "resource": resource,
        "import_type": import_type,
        "ps_fields": field_names,
        "matches": [
            {"header": m.header, "field": m.field, "confidence": m.confidence,
             "method": m.method, "badge": m.badge, "candidates": m.candidates}
            for m in matches
        ],
    }


class ConfirmMapIn(BaseModel):
    # PrestaShop field name -> spreadsheet header
    column_map: dict[str, str]
    # PrestaShop field name -> constant value
    constants: dict[str, str] = {}


@router.post("/{token}/confirm")
def confirm(token: str, payload: ConfirmMapIn) -> dict:
    session = state.get_session(token)
    if session is None:
        raise HTTPException(404, "Upload session not found.")
    session.column_map = payload.column_map
    session.constants = payload.constants
    return {"ok": True, "mapped_fields": sorted(
        set(payload.column_map) | set(payload.constants))}


# --------------------------- profiles --------------------------------- #
class ProfileIn(BaseModel):
    name: str
    column_map: dict[str, str]
    constants: dict[str, str] = {}
    scope: list[str] = []
    mode: str = "upsert"


@router.get("/profiles")
def list_profiles() -> dict:
    return {"profiles": db.list_profiles()}


@router.post("/profiles")
def save_profile(payload: ProfileIn) -> dict:
    db.save_profile(payload.name, payload.model_dump())
    return {"ok": True, "name": payload.name}


@router.get("/profiles/{name}")
def get_profile(name: str) -> dict:
    profile = db.get_profile(name)
    if profile is None:
        raise HTTPException(404, "Profile not found.")
    return profile


@router.delete("/profiles/{name}")
def delete_profile(name: str) -> dict:
    db.delete_profile(name)
    return {"ok": True}
