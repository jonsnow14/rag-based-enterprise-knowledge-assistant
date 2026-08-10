"""Hybrid-ish retrieval: Azure AI Search or local JSONL (vector + keyword)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from src.azure_clients import get_search_client
from src.config import KNOWLEDGE_BASE_IDS, Settings, get_settings
from src.ingestion.embed import embed_texts
from src.ingestion.index import load_local_index
from src.models.schemas import ChunkRecord
from src.observability import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


@dataclass
class RetrievedChunk:
    chunk: ChunkRecord
    score: float
    source: str = "local"  # local | azure


@dataclass
class RetrievalResult:
    hits: List[RetrievedChunk] = field(default_factory=list)
    filters_applied: List[str] = field(default_factory=list)
    backend: str = "local"

    @property
    def top_score(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    @property
    def chunk_ids(self) -> List[str]:
        return [h.chunk.id for h in self.hits]


def normalize_departments(deps: Optional[Sequence[str]]) -> List[str]:
    valid = {kb.lower(): kb for kb in KNOWLEDGE_BASE_IDS}
    if not deps:
        return list(KNOWLEDGE_BASE_IDS)
    out: List[str] = []
    for d in deps:
        key = (d or "").strip().lower()
        if key in valid:
            out.append(valid[key])
    return out


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _keyword_score(query: str, text: str) -> float:
    q = set(_TOKEN_RE.findall(query.lower()))
    if not q:
        return 0.0
    t = set(_TOKEN_RE.findall(text.lower()))
    if not t:
        return 0.0
    inter = len(q & t)
    return inter / max(1, len(q))


def _local_index_path(settings: Settings) -> Path:
    p = Path(settings.local_index_path)
    if not p.is_absolute():
        p = settings.package_root() / p
    return p


def retrieve_local(
    question: str,
    *,
    departments: List[str],
    include_historical: bool,
    top_k: int,
    settings: Settings,
) -> RetrievalResult:
    path = _local_index_path(settings)
    chunks = load_local_index(path)
    filters = [f"access_scope in {departments}"]
    if not include_historical and settings.rag_default_is_current:
        filters.append("is_current eq true")

    filtered: List[ChunkRecord] = []
    dept_set = set(departments)
    for c in chunks:
        if c.access_scope not in dept_set and c.department not in dept_set:
            continue
        if not include_historical and settings.rag_default_is_current and not c.is_current:
            continue
        filtered.append(c)

    if not filtered:
        return RetrievalResult(hits=[], filters_applied=filters, backend="local")

    q_vec = embed_texts([question], settings)[0]
    scored: List[RetrievedChunk] = []
    for c in filtered:
        v_score = _cosine(q_vec, c.content_vector or [])
        # blend embedding text + body for keyword
        blob = f"{c.section}\n{c.content}\n{c.filename}"
        k_score = _keyword_score(question, blob)
        # keyword helps when local hash embeddings are weak
        score = 0.45 * max(0.0, v_score) + 0.55 * k_score
        scored.append(RetrievedChunk(chunk=c, score=score, source="local"))

    scored.sort(key=lambda h: h.score, reverse=True)
    return RetrievalResult(hits=scored[:top_k], filters_applied=filters, backend="local")


def retrieve_azure(
    question: str,
    *,
    departments: List[str],
    include_historical: bool,
    top_k: int,
    settings: Settings,
) -> RetrievalResult:
    client = get_search_client(settings)
    if client is None:
        raise RuntimeError("Azure Search not configured")

    # OData filter
    dept_clause = " or ".join(f"access_scope eq '{d}'" for d in departments)
    filt_parts = [f"({dept_clause})"] if dept_clause else []
    if not include_historical and settings.rag_default_is_current:
        filt_parts.append("is_current eq true")
    filter_expr = " and ".join(filt_parts) if filt_parts else None
    filters = [filter_expr] if filter_expr else []

    q_vec = embed_texts([question], settings)[0]

    from azure.search.documents.models import VectorizedQuery

    vector_query = VectorizedQuery(
        vector=q_vec,
        k_nearest_neighbors=top_k,
        fields="contentVector",
    )
    results = client.search(
        search_text=question,
        vector_queries=[vector_query],
        filter=filter_expr,
        top=top_k,
        select=[
            "id",
            "content",
            "doc_id",
            "filename",
            "section",
            "department",
            "access_scope",
            "knowledge_base_id",
            "effective_date",
            "is_current",
            "version",
            "chunk_kind",
            "token_count",
        ],
    )

    hits: List[RetrievedChunk] = []
    for r in results:
        score = float(r.get("@search.score") or 0.0)
        chunk = ChunkRecord(
            id=r["id"],
            content=r.get("content") or "",
            doc_id=r.get("doc_id") or "",
            filename=r.get("filename") or "",
            section=r.get("section") or "",
            department=r.get("department") or "",
            access_scope=r.get("access_scope") or r.get("department") or "",
            knowledge_base_id=r.get("knowledge_base_id") or r.get("department") or "",
            effective_date=r.get("effective_date") or None,
            is_current=bool(r.get("is_current", True)),
            version=r.get("version") or "1.0",
            chunk_kind=r.get("chunk_kind") or "section",
            token_count=int(r.get("token_count") or 0),
        )
        hits.append(RetrievedChunk(chunk=chunk, score=score, source="azure"))

    hits.sort(key=lambda h: h.score, reverse=True)
    return RetrievalResult(hits=hits[:top_k], filters_applied=filters, backend="azure")


def retrieve(
    question: str,
    *,
    departments: Optional[Sequence[str]] = None,
    include_historical: bool = False,
    top_k: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> RetrievalResult:
    s = settings or get_settings()
    deps = normalize_departments(departments if departments is not None else s.allowed_departments_default())
    k = top_k or s.rag_top_k

    if s.azure_search_configured() and s.effective_mode() == "azure":
        try:
            return retrieve_azure(
                question,
                departments=deps,
                include_historical=include_historical,
                top_k=k,
                settings=s,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("azure retrieve failed, falling back to local: %s", exc)

    return retrieve_local(
        question,
        departments=deps,
        include_historical=include_historical,
        top_k=k,
        settings=s,
    )
