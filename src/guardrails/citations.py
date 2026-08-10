"""Citation allowlist: only chunk_ids that were retrieved may appear."""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set

from src.models.schemas import Citation
from src.retrieval.search import RetrievedChunk

_CITE_RE = re.compile(
    r"(?:chunk_id|citation|source)\s*[:=]\s*[`'\"]?([a-zA-Z0-9_\-\.]+)[`'\"]?",
    re.I,
)
_BARE_ID_RE = re.compile(r"\b([a-z]+_[a-z0-9]+_[a-z0-9_]+_\d{3})\b", re.I)


def allowed_ids(hits: Sequence[RetrievedChunk]) -> Set[str]:
    return {h.chunk.id for h in hits}


def citations_from_hits(hits: Sequence[RetrievedChunk]) -> List[Citation]:
    out: List[Citation] = []
    for h in hits:
        c = h.chunk
        out.append(
            Citation(
                chunk_id=c.id,
                doc_id=c.doc_id,
                section=c.section,
                filename=c.filename,
                knowledge_base_id=c.knowledge_base_id,
            )
        )
    return out


def filter_citations(
    proposed: Iterable[Citation],
    hits: Sequence[RetrievedChunk],
) -> List[Citation]:
    allow = allowed_ids(hits)
    by_id = {h.chunk.id: h.chunk for h in hits}
    out: List[Citation] = []
    seen: Set[str] = set()
    for cite in proposed:
        cid = cite.chunk_id
        if cid not in allow or cid in seen:
            continue
        seen.add(cid)
        ch = by_id[cid]
        out.append(
            Citation(
                chunk_id=cid,
                doc_id=cite.doc_id or ch.doc_id,
                section=cite.section or ch.section,
                filename=cite.filename or ch.filename,
                knowledge_base_id=cite.knowledge_base_id or ch.knowledge_base_id,
            )
        )
    return out


def extract_cited_ids_from_text(text: str, allow: Set[str]) -> List[str]:
    found: List[str] = []
    for m in _CITE_RE.finditer(text or ""):
        cid = m.group(1)
        if cid in allow and cid not in found:
            found.append(cid)
    for m in _BARE_ID_RE.finditer(text or ""):
        cid = m.group(1)
        if cid in allow and cid not in found:
            found.append(cid)
    return found


def ensure_citations(
    answer_text: str,
    hits: Sequence[RetrievedChunk],
) -> List[Citation]:
    """Prefer model-mentioned ids; else attach top retrieved hits used as context."""
    allow = allowed_ids(hits)
    mentioned = extract_cited_ids_from_text(answer_text, allow)
    if mentioned:
        by_id = {h.chunk.id: h for h in hits}
        return [
            Citation(
                chunk_id=cid,
                doc_id=by_id[cid].chunk.doc_id,
                section=by_id[cid].chunk.section,
                filename=by_id[cid].chunk.filename,
                knowledge_base_id=by_id[cid].chunk.knowledge_base_id,
            )
            for cid in mentioned
            if cid in by_id
        ]
    # default: cite all context hits (transparent grounding)
    return citations_from_hits(hits)
