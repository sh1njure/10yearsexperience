"""Pre-import validation.

Runs the checks from the brief over the mapped rows and returns a flat list of
issues. Hard errors block the import; warnings do not. Checks that need shop
state (do referenced categories exist?) accept that state as an argument so the
module stays pure and testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# PrestaShop fields that must be present for a product to be created.
DEFAULT_REQUIRED_FIELDS = ["reference", "name", "price"]

# Fields that must parse as a number when present.
NUMERIC_FIELDS = {"price", "wholesale_price", "weight", "width", "height",
                  "depth", "quantity", "ecotax"}


@dataclass
class Issue:
    row: int | None          # 0-indexed data row, or None for structural issues
    field: str | None
    message: str
    severity: str            # "error" | "warning"


def parse_number(value: str) -> Decimal | None:
    """Parse a possibly messy numeric string (commas, currency symbols)."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    text = (text.replace("€", "").replace("$", "").replace("£", "")
            .replace(" ", ""))
    # Handle "1.234,56" (EU) vs "1,234.56" (US) heuristically.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def validate(rows: list[dict[str, object]], mapped_fields: set[str], *,
             required_fields: list[str] | None = None,
             known_category_ids: set[str] | None = None,
             category_field: str = "id_category_default") -> list[Issue]:
    """Validate mapped rows. Returns all issues (errors + warnings)."""
    required_fields = required_fields or DEFAULT_REQUIRED_FIELDS
    issues: list[Issue] = []

    # 1. Required fields must be mapped at all.
    for req in required_fields:
        if req not in mapped_fields:
            issues.append(Issue(None, req,
                                f"Required field '{req}' is not mapped.",
                                "error"))

    # 2. Per-row checks.
    seen_refs: dict[str, int] = {}
    for i, row in enumerate(rows):
        # Required values present per row.
        for req in required_fields:
            if req in mapped_fields and not str(row.get(req, "")).strip():
                issues.append(Issue(i, req,
                                    f"Missing required value '{req}'.", "error"))

        # Numeric fields parse.
        for fld in NUMERIC_FIELDS & mapped_fields:
            raw = row.get(fld)
            if raw not in (None, "") and parse_number(str(raw)) is None:
                issues.append(Issue(i, fld,
                                    f"'{raw}' is not a valid number.", "error"))

        # Reference uniqueness within the file.
        if "reference" in mapped_fields:
            ref = str(row.get("reference", "")).strip()
            if ref:
                if ref in seen_refs:
                    issues.append(Issue(i, "reference",
                                        f"Duplicate reference '{ref}' (also row "
                                        f"{seen_refs[ref]}).", "error"))
                else:
                    seen_refs[ref] = i

        # Referenced category exists in the shop.
        if known_category_ids is not None and category_field in mapped_fields:
            cat = str(row.get(category_field, "")).strip()
            if cat and cat not in known_category_ids:
                issues.append(Issue(i, category_field,
                                    f"Category '{cat}' does not exist in the shop.",
                                    "error"))
    return issues


def has_blocking_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
