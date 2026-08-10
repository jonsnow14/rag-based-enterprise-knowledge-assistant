"""FastAPI entrypoint — health + /chat + /v1/chat (shared Azure API surface)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src import __version__
from src.api.auth import PUBLIC_PATHS, extract_api_key, verify_api_key
from src.config import get_settings
from src.models.schemas import ChatRequest, ChatResponse
from src.observability import get_logger, setup_logging
from src.services.chat import run_chat

log = get_logger(__name__)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforce X-API-Key when API_KEYS / REQUIRE_API_KEY is set (except public paths)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        # allow unversioned and docs
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        settings = get_settings()
        if not settings.api_key_auth_enabled():
            return await call_next(request)
        key = extract_api_key(
            request,
            x_api_key=request.headers.get("x-api-key"),
            api_key=request.headers.get("api-key"),
        )
        try:
            verify_api_key(key, settings)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )
        return await call_next(request)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info(
        "starting %s version=%s env=%s mode=%s effective_mode=%s api_key_auth=%s",
        settings.app_name,
        __version__,
        settings.app_env,
        settings.app_mode,
        settings.effective_mode(),
        settings.api_key_auth_enabled(),
    )
    log.info("documents_path=%s", settings.documents_path())
    yield
    log.info("shutdown complete")


def _run_chat_endpoint(body: ChatRequest) -> ChatResponse:
    try:
        return run_chat(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"index unavailable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("chat failed")
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Northwind Traders Knowledge Assistant",
        description=(
            "Azure productized RAG (agent-free). "
            "Ingest + /chat with ACL filters, evidence gate, grounded answer, citation allowlist. "
            "Share via Azure Container Apps / App Service + API Management. "
            "Auth: optional X-API-Key when API_KEYS is set "
            "(behind APIM, partners typically use Ocp-Apim-Subscription-Key)."
        ),
        version=__version__,
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # Document API key for Swagger "Authorize" when sharing the app URL directly
    app.openapi_schema = None  # force rebuild after security scheme inject

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Required when API_KEYS / REQUIRE_API_KEY is set on the app.",
        }
        # Apply globally so /chat and /v1/chat show the lock icon
        schema["security"] = [{"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        s = get_settings()
        docs = s.documents_path()
        local_index = Path(s.local_index_path)
        if not local_index.is_absolute():
            local_index = s.package_root() / local_index
        local_chunks = 0
        if local_index.is_file():
            try:
                local_chunks = sum(1 for line in local_index.open() if line.strip())
            except OSError:
                local_chunks = -1
        return {
            "status": "ok",
            "app": s.app_name,
            "version": __version__,
            "env": s.app_env,
            "app_mode": s.app_mode,
            "effective_mode": s.effective_mode(),
            "api_key_auth": s.api_key_auth_enabled(),
            "azure_openai_configured": s.azure_openai_configured(),
            "azure_search_configured": s.azure_search_configured(),
            "content_safety_configured": s.content_safety_configured(),
            "documents_path": str(docs),
            "documents_path_exists": docs.is_dir(),
            "local_index_path": str(local_index),
            "local_index_chunks": local_chunks,
            "phase": "control-system",
            "endpoints": ["/", "/health", "/chat", "/v1/chat"],
            "control": {
                "enhanced": s.enhanced_enabled(),
                "force_single_path": s.force_single_path,
                "multi_query": s.enable_multi_query,
                "query_rewrite": s.enable_query_rewrite,
                "temporal_intent": s.enable_temporal_intent,
            },
        }

    @app.get("/")
    def root() -> Dict[str, str]:
        return {
            "service": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
            "chat": "/chat",
            "chat_v1": "/v1/chat",
            "architecture": "azure-productized-agent-free",
            "auth": "X-API-Key when API_KEYS configured",
        }

    @app.post("/chat", response_model=ChatResponse, tags=["chat"])
    def chat(body: ChatRequest) -> ChatResponse:
        return _run_chat_endpoint(body)

    @app.post("/v1/chat", response_model=ChatResponse, tags=["chat"])
    def chat_v1(body: ChatRequest) -> ChatResponse:
        """Versioned alias for stable partner integrations."""
        return _run_chat_endpoint(body)

    return app


app = create_app()
