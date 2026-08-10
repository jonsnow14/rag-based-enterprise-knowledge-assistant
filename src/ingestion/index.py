"""Upsert chunks to Azure AI Search and/or local JSONL index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.azure_clients import ensure_search_index, get_search_client
from src.config import Settings, get_settings
from src.models.schemas import ChunkRecord
from src.observability import get_logger

log = get_logger(__name__)


def chunk_to_search_doc(chunk: ChunkRecord) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "id": chunk.id,
        "content": chunk.content,
        "doc_id": chunk.doc_id,
        "filename": chunk.filename,
        "section": chunk.section,
        "department": chunk.department,
        "access_scope": chunk.access_scope,
        "knowledge_base_id": chunk.knowledge_base_id,
        "effective_date": chunk.effective_date or "",
        "is_current": chunk.is_current,
        "version": chunk.version,
        "chunk_kind": chunk.chunk_kind,
        "token_count": chunk.token_count,
    }
    if chunk.content_vector:
        doc["contentVector"] = chunk.content_vector
    return doc


def save_local_index(chunks: List[ChunkRecord], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # replace entire index for deterministic re-ingest in v1
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            row = c.model_dump()
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("wrote local index %s (%s chunks)", path, len(chunks))
    return len(chunks)


def load_local_index(path: Path) -> List[ChunkRecord]:
    if not path.is_file():
        return []
    out: List[ChunkRecord] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(ChunkRecord.model_validate(json.loads(line)))
    return out


def upsert_azure_search(chunks: List[ChunkRecord], settings: Optional[Settings] = None) -> int:
    s = settings or get_settings()
    if not chunks:
        return 0
    dim = len(chunks[0].content_vector or []) or 1536
    ensure_search_index(s, vector_dim=dim)
    client = get_search_client(s)
    if client is None:
        raise RuntimeError("Azure Search client not configured")

    # delete by doc_id groups then upload — v1: full merge upload by key
    docs = [chunk_to_search_doc(c) for c in chunks]
    batch_size = 50
    total = 0
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        results = client.upload_documents(documents=batch)
        ok = sum(1 for r in results if r.succeeded)
        total += ok
        log.info("uploaded search batch %s–%s ok=%s", i, i + len(batch), ok)
    return total


def upsert_chunks(
    chunks: List[ChunkRecord],
    settings: Optional[Settings] = None,
    *,
    force_local: bool = False,
) -> dict:
    """
    Always write local JSONL. Also push to Azure Search when configured
    and not force_local.
    """
    s = settings or get_settings()
    local_path = Path(s.local_index_path)
    if not local_path.is_absolute():
        local_path = s.package_root() / local_path

    local_n = save_local_index(chunks, local_path)
    azure_n = 0
    azure_error = None
    mode = s.effective_mode()

    if not force_local and s.azure_search_configured() and mode in {"azure", "azure-openai-only"}:
        # still try search if keys present
        try:
            if s.azure_search_configured():
                azure_n = upsert_azure_search(chunks, s)
        except Exception as exc:  # noqa: BLE001
            azure_error = str(exc)[:400]
            log.error("azure upsert failed: %s", azure_error)

    return {
        "local_chunks": local_n,
        "local_path": str(local_path),
        "azure_chunks": azure_n,
        "azure_error": azure_error,
        "effective_mode": mode,
    }
