"""In-memory session state for the current upload/mapping.

This is a single-user localhost tool, so a process-global store keyed by an
upload token is enough — no auth, no multi-tenant concerns. Uploaded files live
under ``data/uploads`` and are referenced by token.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import BASE_DIR

UPLOAD_DIR = BASE_DIR / "data" / "uploads"


@dataclass
class UploadSession:
    token: str
    path: Path
    filename: str
    sheet: str | None = None
    header_row: int = 0
    headers: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    # Field name -> spreadsheet header (the confirmed mapping)
    column_map: dict[str, str] = field(default_factory=dict)
    # Field name -> constant value
    constants: dict[str, str] = field(default_factory=dict)


_SESSIONS: dict[str, UploadSession] = {}


def new_session(filename: str, content: bytes) -> UploadSession:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    suffix = Path(filename).suffix or ".dat"
    path = UPLOAD_DIR / f"{token}{suffix}"
    path.write_bytes(content)
    session = UploadSession(token=token, path=path, filename=filename)
    _SESSIONS[token] = session
    return session


def get_session(token: str) -> UploadSession | None:
    return _SESSIONS.get(token)
