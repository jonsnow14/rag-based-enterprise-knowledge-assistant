"""API key authentication for shared HTTP access (App Service / defense-in-depth)."""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request, status

from src.config import Settings, get_settings

# Paths that stay public (probes, docs optional — lock docs in prod if desired)
PUBLIC_PATHS = {
    "/health",
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
}


def extract_api_key(
    request: Request,
    x_api_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Accept X-API-Key, api-key, or Authorization: Bearer/ApiKey."""
    if x_api_key:
        return x_api_key.strip()
    if api_key:
        return api_key.strip()
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in {"bearer", "apikey", "api-key"}:
        return parts[1].strip()
    return None


def verify_api_key(key: Optional[str], settings: Optional[Settings] = None) -> None:
    s = settings or get_settings()
    if not s.api_key_auth_enabled():
        return  # local dev: open
    accepted = s.accepted_api_keys()
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key auth is required but no API_KEYS are configured",
        )
    if not key or key not in accepted:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Pass header X-API-Key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def api_key_dependency(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    api_key: Optional[str] = Header(default=None, alias="api-key"),
) -> None:
    """FastAPI dependency for protected routes."""
    key = extract_api_key(request, x_api_key=x_api_key, api_key=api_key)
    verify_api_key(key)
