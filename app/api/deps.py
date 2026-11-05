"""Shared FastAPI dependencies (admin authentication)."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import get_settings

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Admin API key (ADMIN_API_KEY). Leave empty in local development.",
)


def require_admin(x_api_key: str | None = Security(api_key_header)) -> None:
    """Protect the admin API with a shared key.

    When ``ADMIN_API_KEY`` is unset the API is open — acceptable only for local
    development, and the app logs a warning at startup in that case.
    """
    settings = get_settings()
    if not settings.admin_api_key:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
        )
