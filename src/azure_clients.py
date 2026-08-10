"""P1 — Azure OpenAI / AI Search client helpers and connectivity probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.config import Settings, get_settings
from src.observability import get_logger

log = get_logger(__name__)

# Match common text-embedding-3-small output
DEFAULT_EMBED_DIM = 1536


@dataclass
class ConnectivityReport:
    app_mode: str
    effective_mode: str
    openai_ok: bool
    search_ok: bool
    openai_error: Optional[str] = None
    search_error: Optional[str] = None
    embed_deployment: str = ""
    chat_deployment: str = ""
    search_index: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_mode": self.app_mode,
            "effective_mode": self.effective_mode,
            "openai_ok": self.openai_ok,
            "search_ok": self.search_ok,
            "openai_error": self.openai_error,
            "search_error": self.search_error,
            "embed_deployment": self.embed_deployment,
            "chat_deployment": self.chat_deployment,
            "search_index": self.search_index,
        }


def get_openai_client(settings: Optional[Settings] = None):
    """Return Azure OpenAI client or None if not configured."""
    s = settings or get_settings()
    if not s.azure_openai_configured():
        return None
    from openai import AzureOpenAI

    return AzureOpenAI(
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
        azure_endpoint=s.azure_openai_endpoint.rstrip("/"),
    )


def get_search_index_client(settings: Optional[Settings] = None):
    """Index client for create/update schema."""
    s = settings or get_settings()
    if not s.azure_search_configured():
        return None
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient

    return SearchIndexClient(
        endpoint=s.azure_search_endpoint.rstrip("/"),
        credential=AzureKeyCredential(s.azure_search_api_key),
    )


def get_search_client(settings: Optional[Settings] = None):
    """Document search/upload client for the configured index."""
    s = settings or get_settings()
    if not s.azure_search_configured():
        return None
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient

    return SearchClient(
        endpoint=s.azure_search_endpoint.rstrip("/"),
        index_name=s.azure_search_index,
        credential=AzureKeyCredential(s.azure_search_api_key),
    )


def ensure_search_index(settings: Optional[Settings] = None, vector_dim: int = DEFAULT_EMBED_DIM) -> bool:
    """
    Create the hybrid index if missing. Returns True if index exists/created.
    No-op (False) when Search is not configured.
    """
    s = settings or get_settings()
    client = get_search_index_client(s)
    if client is None:
        log.info("search not configured — skip ensure_search_index")
        return False

    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    name = s.azure_search_index
    existing = {idx.name for idx in client.list_indexes()}
    if name in existing:
        log.info("search index already exists: %s", name)
        return True

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.lucene"),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=vector_dim,
            vector_search_profile_name="vs-profile",
        ),
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="filename", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="section", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="department", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="access_scope", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(
            name="knowledge_base_id", type=SearchFieldDataType.String, filterable=True, facetable=True
        ),
        SimpleField(name="effective_date", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="is_current", type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_kind", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="token_count", type=SearchFieldDataType.Int32, filterable=True),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw-config")],
        profiles=[VectorSearchProfile(name="vs-profile", algorithm_configuration_name="hnsw-config")],
    )
    index = SearchIndex(name=name, fields=fields, vector_search=vector_search)
    client.create_index(index)
    log.info("created search index %s dim=%s", name, vector_dim)
    return True


def probe_connectivity(settings: Optional[Settings] = None) -> ConnectivityReport:
    """Lightweight P1 connectivity report (does not fail hard)."""
    s = settings or get_settings()
    report = ConnectivityReport(
        app_mode=s.app_mode,
        effective_mode=s.effective_mode(),
        openai_ok=False,
        search_ok=False,
        embed_deployment=s.azure_openai_embed_deployment,
        chat_deployment=s.azure_openai_chat_deployment,
        search_index=s.azure_search_index,
    )

    client = get_openai_client(s)
    if client is None:
        report.openai_error = "not_configured"
    else:
        try:
            # cheap call: list is not always available on Azure; use tiny embed
            client.embeddings.create(model=s.azure_openai_embed_deployment, input=["ping"])
            report.openai_ok = True
        except Exception as exc:  # noqa: BLE001 — surface for operator
            report.openai_error = str(exc)[:300]
            log.warning("openai probe failed: %s", report.openai_error)

    sc = get_search_index_client(s)
    if sc is None:
        report.search_error = "not_configured"
    else:
        try:
            _ = list(sc.list_indexes())
            report.search_ok = True
        except Exception as exc:  # noqa: BLE001
            report.search_error = str(exc)[:300]
            log.warning("search probe failed: %s", report.search_error)

    return report
