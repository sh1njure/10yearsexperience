"""Auto-match spreadsheet headers to live PrestaShop fields.

Matching cascade, highest confidence first:

1. exact match (identical strings)
2. normalized match (lowercase, strip spaces/underscores/punctuation)
3. synonym-dictionary match (data/synonyms.json)
4. fuzzy match via rapidfuzz, accepted only above a confidence threshold

The synonym dictionary lives in a JSON file so it can be extended without
touching code. All functions here are pure and unit-testable — no API calls.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from .config import BASE_DIR

DEFAULT_SYNONYMS_PATH = BASE_DIR / "data" / "synonyms.json"

# Confidence bands used for the green/amber/red badge in the UI.
GREEN_THRESHOLD = 0.90
AMBER_THRESHOLD = 0.75   # fuzzy matches at/above this are "amber", below -> unmapped


@dataclass
class Match:
    header: str
    field: str | None            # matched PrestaShop field, or None if unmapped
    confidence: float            # 0..1
    method: str                  # exact | normalized | synonym | fuzzy | none
    candidates: list[str] = field(default_factory=list)  # alternatives for the dropdown

    @property
    def badge(self) -> str:
        if self.field is None:
            return "red"
        if self.confidence >= GREEN_THRESHOLD:
            return "green"
        if self.confidence >= AMBER_THRESHOLD:
            return "amber"
        return "red"


def normalize(text: str) -> str:
    """Lowercase and strip spaces, underscores and punctuation."""
    text = (text or "").lower().strip()
    return re.sub(r"[\s_\-./]+", "", re.sub(r"[^\w\s]", "", text))


def load_synonyms(path: str | Path = DEFAULT_SYNONYMS_PATH) -> dict[str, list[str]]:
    """Load the synonym dictionary, ignoring comment keys (leading underscore)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)}


def _build_synonym_index(synonyms: dict[str, list[str]]) -> dict[str, str]:
    """Map normalized synonym -> canonical field name."""
    index: dict[str, str] = {}
    for field_name, terms in synonyms.items():
        index[normalize(field_name)] = field_name
        for term in terms:
            index.setdefault(normalize(term), field_name)
    return index


def match_headers(headers: list[str], ps_fields: list[str], *,
                  synonyms: dict[str, list[str]] | None = None,
                  fuzzy_threshold: float = AMBER_THRESHOLD) -> list[Match]:
    """Match each spreadsheet header to a PrestaShop field.

    ``ps_fields`` is the live field list from ``?schema=blank`` — never
    hardcoded. Returns one :class:`Match` per header. A field is only matched
    once (the highest-confidence header wins) to avoid two columns mapping to
    the same target.
    """
    synonyms = synonyms if synonyms is not None else load_synonyms()
    syn_index = _build_synonym_index(synonyms)

    norm_fields = {normalize(f): f for f in ps_fields}
    exact_fields = {f: f for f in ps_fields}

    results: list[Match] = []
    for header in headers:
        results.append(
            _match_one(header, ps_fields, exact_fields, norm_fields,
                       syn_index, fuzzy_threshold)
        )

    _resolve_conflicts(results)
    return results


def _match_one(header: str, ps_fields: list[str], exact_fields: dict[str, str],
               norm_fields: dict[str, str], syn_index: dict[str, str],
               fuzzy_threshold: float) -> Match:
    norm = normalize(header)

    # 1. exact
    if header in exact_fields:
        return Match(header, header, 1.0, "exact")

    # 2. normalized
    if norm in norm_fields:
        return Match(header, norm_fields[norm], 0.97, "normalized")

    # 3. synonym
    if norm in syn_index:
        target = syn_index[norm]
        if target in ps_fields:  # only if the shop actually has that field
            return Match(header, target, 0.92, "synonym")

    # 4. fuzzy
    candidates = _fuzzy_candidates(header, ps_fields)
    if candidates:
        best_field, best_score = candidates[0]
        confidence = best_score / 100.0
        if confidence >= fuzzy_threshold:
            return Match(header, best_field, round(confidence, 3), "fuzzy",
                         candidates=[c for c, _ in candidates[:5]])
        return Match(header, None, round(confidence, 3), "none",
                     candidates=[c for c, _ in candidates[:5]])

    return Match(header, None, 0.0, "none")


def _fuzzy_candidates(header: str, ps_fields: list[str]) -> list[tuple[str, float]]:
    """Return (field, score 0..100) pairs ranked best-first."""
    matches = process.extract(
        header, ps_fields, scorer=fuzz.token_sort_ratio, limit=5,
    )
    return [(field_name, score) for field_name, score, _ in matches]


def _resolve_conflicts(results: list[Match]) -> None:
    """Ensure each PrestaShop field is claimed by at most one header.

    When two headers map to the same field, the higher-confidence match keeps
    it; the loser is downgraded to unmapped (its candidates stay for override).
    """
    best_for_field: dict[str, Match] = {}
    for m in results:
        if m.field is None:
            continue
        current = best_for_field.get(m.field)
        if current is None or m.confidence > current.confidence:
            best_for_field[m.field] = m

    winners = set(id(m) for m in best_for_field.values())
    for m in results:
        if m.field is not None and id(m) not in winners:
            m.method = "none"
            if m.field not in m.candidates:
                m.candidates.insert(0, m.field)
            m.field = None
