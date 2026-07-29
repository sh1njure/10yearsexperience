"""Application configuration loaded from environment / .env file.

Secrets (shop URL + API key) live in .env which is git-ignored. This module
exposes a single cached ``Settings`` instance plus a small helper to update the
runtime connection settings from the UI without restarting the server.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (one level above the ``app`` package).
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration.

    Values come from environment variables / the ``.env`` file. The connection
    fields can also be overridden at runtime via the Settings UI (see
    ``update_connection``) so the user does not have to edit ``.env`` by hand.
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    prestashop_url: str = Field(default="", alias="PRESTASHOP_URL")
    prestashop_api_key: str = Field(default="", alias="PRESTASHOP_API_KEY")
    default_lang_id: int = Field(default=1, alias="DEFAULT_LANG_ID")
    database_path: str = Field(default="data/app.sqlite3", alias="DATABASE_PATH")
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    @property
    def normalized_url(self) -> str:
        """Shop base URL without a trailing slash."""
        return self.prestashop_url.rstrip("/")

    @property
    def db_file(self) -> Path:
        path = Path(self.database_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()


def update_connection(url: str | None = None, api_key: str | None = None,
                      default_lang_id: int | None = None) -> Settings:
    """Update connection settings in the live process.

    This does not persist to ``.env`` (that stays user-managed); it only changes
    the in-memory settings so a "Test connection" / import can run immediately.
    """
    settings = get_settings()
    if url is not None:
        settings.prestashop_url = url
    if api_key is not None:
        settings.prestashop_api_key = api_key
    if default_lang_id is not None:
        settings.default_lang_id = default_lang_id
    return settings
