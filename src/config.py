"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical department / knowledge-base ids for Northwind corpus
KNOWLEDGE_BASE_IDS: tuple[str, ...] = (
    "Finance",
    "HR",
    "IT",
    "Legal",
    "Sales",
)

# Repository root (this package is the app root once published as a standalone repo)
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration for ingest + query (agent-free productized path)."""

    model_config = SettingsConfigDict(
        env_file=str(_PACKAGE_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "northwind-rag-azure"
    app_env: str = "local"
    app_mode: str = "azure"  # azure | local
    log_level: str = "INFO"
    dev_default_departments: str = "HR,Finance,IT,Legal,Sales"
    # Override with RAG_DOCUMENTS_PATH (e.g. ./rag-documents or absolute path)
    rag_documents_path: str = str(_PACKAGE_ROOT / "rag-documents")

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = "gpt-4o-mini"
    azure_openai_embed_deployment: str = "text-embedding-3-small"

    # Azure AI Search
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index: str = "northwind-chunks"

    # Optional Content Safety
    azure_content_safety_endpoint: str = ""
    azure_content_safety_key: str = ""

    # Adaptive chunking
    chunk_alpha: float = 3.0
    chunk_c_min: int = 128
    chunk_c_max: int = 512
    chunk_c_max_atomic: int = 800
    chunk_overlap_beta: float = 0.12

    # Retrieval / evidence gate
    rag_top_k: int = 5
    rag_retrieve_k: int = 15  # fetch more before optional rerank / multi merge
    rag_context_k: int = 5  # chunks sent to generator
    # Local hybrid (cosine+keyword) is ~0–1; Azure Search RRF scores are often ~0.01–0.05
    rag_min_score: float = 0.30
    rag_min_score_azure: float = 0.015
    rag_default_is_current: bool = True
    rag_min_keyword_overlap: float = 0.0  # optional lexical floor (0 = off)

    # --- Control system (smart when confident / classic when not) ---
    rag_enhanced_pipeline: bool = True
    force_single_path: bool = False  # kill switch → classic only
    enable_query_rewrite: bool = True  # Strategy C
    enable_multi_query: bool = True  # Strategy A
    enable_temporal_intent: bool = True  # Strategy B1
    enable_version_offer: bool = False  # off until multi-version corpus
    enable_version_collapse: bool = True
    allow_partial_answers: bool = True
    ambiguity_clarify_enabled: bool = True
    multi_query_fallback_to_single: bool = True
    rewrite_min_confidence: float = 0.70
    multi_facet_min_confidence: float = 0.75
    temporal_min_confidence: float = 0.80
    max_sub_queries: int = 2
    multi_query_timeout_ms: int = 8000
    include_diagnostics_default: bool = False
    log_retrieval_plan: bool = True
    # none | lexical (S1 light)
    rerank_mode: str = "lexical"

    # Local fallback
    local_index_path: str = str(_PACKAGE_ROOT / "data" / "local_index.jsonl")

    # API sharing / auth (App Service path or defense-in-depth behind APIM)
    # If empty, API key check is disabled (local dev). Set in production.
    api_keys: str = ""  # comma-separated keys accepted in X-API-Key header
    require_api_key: bool = False  # if true, reject when no valid key (even if api_keys empty → all denied)

    def enhanced_enabled(self) -> bool:
        """True when enhanced branches may run (not kill-switched)."""
        return bool(self.rag_enhanced_pipeline) and not bool(self.force_single_path)

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        return (v or "INFO").upper()

    @field_validator("app_mode")
    @classmethod
    def normalize_mode(cls, v: str) -> str:
        mode = (v or "azure").strip().lower()
        if mode not in {"azure", "local"}:
            return "azure"
        return mode

    def package_root(self) -> Path:
        return _PACKAGE_ROOT

    def project_root(self) -> Path:
        """Repo root (same as package root for standalone layout)."""
        return _PACKAGE_ROOT

    def documents_path(self) -> Path:
        p = Path(self.rag_documents_path)
        if not p.is_absolute():
            p = (_PACKAGE_ROOT / p).resolve()
        return p

    def allowed_departments_default(self) -> List[str]:
        """Dev mock group list when Entra is not wired."""
        parts = [p.strip() for p in self.dev_default_departments.split(",") if p.strip()]
        valid = {kb.lower(): kb for kb in KNOWLEDGE_BASE_IDS}
        out: List[str] = []
        for p in parts:
            key = p.lower()
            if key in valid:
                out.append(valid[key])
        return out or list(KNOWLEDGE_BASE_IDS)

    def azure_openai_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    def azure_search_configured(self) -> bool:
        return bool(self.azure_search_endpoint and self.azure_search_api_key)

    def content_safety_configured(self) -> bool:
        return bool(self.azure_content_safety_endpoint and self.azure_content_safety_key)

    def accepted_api_keys(self) -> List[str]:
        return [k.strip() for k in (self.api_keys or "").split(",") if k.strip()]

    def api_key_auth_enabled(self) -> bool:
        """Auth is on if keys are configured or explicitly required."""
        return self.require_api_key or bool(self.accepted_api_keys())

    def effective_mode(self) -> str:
        """Resolve runtime mode; demote to local if azure incomplete."""
        if self.app_mode == "local":
            return "local"
        if self.azure_openai_configured() and self.azure_search_configured():
            return "azure"
        if self.azure_openai_configured():
            return "azure-openai-only"  # chat/embed OK; search may be local
        return "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
