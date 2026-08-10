"""API and pipeline schemas — agent-free productized path + control system."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QueryStatus(str, Enum):
    ANSWER = "answer"
    PARTIAL = "partial"
    REFUSE = "refuse"
    ESCALATE = "escalate"
    CLARIFY = "clarify"
    ACCESS_DENIED = "access_denied"
    IRRELEVANT = "irrelevant"
    ERROR = "error"


class HistoryMessage(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str = Field(..., min_length=0, max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[str] = None
    history: Optional[List[HistoryMessage]] = Field(
        default=None,
        description="Prior turns for Strategy C rewrite (not used as search text).",
    )
    departments: Optional[List[str]] = Field(
        default=None,
        description="Caller-allowed knowledge bases (dev mock / gateway claims)",
    )
    include_historical: bool = Field(
        default=False,
        description="If true, do not force is_current eq true",
    )
    as_of_date: Optional[str] = Field(
        default=None,
        description="Optional as-of date YYYY-MM-DD for temporal intent",
    )
    include_diagnostics: bool = Field(
        default=False,
        description="If true, return diagnostics object (path, rewrite, sub_queries, …)",
    )
    rag_mode: str = Field(
        default="auto",
        description="auto | single | enhanced — single forces classic path",
    )


class Citation(BaseModel):
    chunk_id: str
    doc_id: Optional[str] = None
    section: Optional[str] = None
    filename: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    version_label: Optional[str] = None


class RetrievalInfo(BaseModel):
    hit_count: int = 0
    top_score: float = 0.0
    filters_applied: List[str] = Field(default_factory=list)
    chunk_ids: List[str] = Field(default_factory=list)
    path: str = "single"  # single | multi | single_fallback


class VersionUsed(BaseModel):
    filename: Optional[str] = None
    doc_id: Optional[str] = None
    version_label: Optional[str] = None
    is_current: Optional[bool] = None
    effective_date: Optional[str] = None


class LatencyMs(BaseModel):
    rewrite: int = 0
    embed: int = 0
    search: int = 0
    merge: int = 0
    llm: int = 0
    total: int = 0


class Diagnostics(BaseModel):
    """Control-system telemetry — always built; returned when include_diagnostics."""

    query_id: str = ""
    path: str = "single"
    fallback_reason: Optional[str] = None
    flags: Dict[str, Any] = Field(default_factory=dict)
    triggers: Dict[str, Any] = Field(default_factory=dict)
    raw_question: str = ""
    retrieval_query: str = ""
    sub_queries: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    coverage: Optional[Dict[str, Any]] = None
    selected_chunk_ids: List[str] = Field(default_factory=list)
    top_score: float = 0.0
    latency_ms: LatencyMs = Field(default_factory=LatencyMs)
    turn_class: Optional[str] = None
    temporal_intent: Optional[str] = None
    rewritten_query: Optional[str] = None
    # Populated when include_diagnostics / eval capture — context pack for Foundry groundedness
    eval_context: Optional[str] = None


class ChatResponse(BaseModel):
    query_id: str
    status: QueryStatus
    answer: Optional[str] = None
    message: Optional[str] = None
    citations: List[Citation] = Field(default_factory=list)
    effective_kb: List[str] = Field(default_factory=list)
    retrieval: RetrievalInfo = Field(default_factory=RetrievalInfo)
    clarification_options: List[str] = Field(default_factory=list)
    version_used: Optional[VersionUsed] = None
    alternate_versions: List[Dict[str, Any]] = Field(default_factory=list)
    suggested_actions: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Optional[Diagnostics] = None
    # Explicit eval fields (Foundry target / offline scoring)
    eval_context: Optional[str] = Field(
        default=None,
        description="Retrieval context pack for groundedness evaluators",
    )


class ChunkRecord(BaseModel):
    """Index document shape (ingest → Azure AI Search or local store)."""

    id: str
    content: str
    content_for_embedding: Optional[str] = None
    doc_id: str
    filename: str
    section: str = ""
    token_count: int = 0
    chunk_size_target: Optional[int] = None
    chunk_kind: str = "section"  # section | table | window | sheet
    department: str
    access_scope: str
    knowledge_base_id: str
    effective_date: Optional[str] = None
    is_current: bool = True
    version: str = "1.0"
    content_vector: Optional[List[float]] = None
    extra: dict[str, Any] = Field(default_factory=dict)
