"""Light rerank for S1 — lexical boost (optional)."""

from __future__ import annotations

import re
from typing import List, Sequence

from src.config import Settings
from src.retrieval.search import RetrievedChunk

_TOKEN = re.compile(r"[a-z0-9]+", re.I)


def rerank_hits(
    question: str,
    hits: Sequence[RetrievedChunk],
    settings: Settings,
    *,
    context_k: int | None = None,
) -> List[RetrievedChunk]:
    mode = (settings.rerank_mode or "none").lower()
    k = context_k or settings.rag_context_k
    if not hits or mode in {"none", "off", ""}:
        return list(hits)[:k]

    if mode == "lexical":
        q = set(_TOKEN.findall(question.lower()))
        scored: List[RetrievedChunk] = []
        for h in hits:
            blob = f"{h.chunk.section} {h.chunk.filename} {h.chunk.content[:500]}".lower()
            t = set(_TOKEN.findall(blob))
            overlap = len(q & t) / max(1, len(q))
            # blend original score with lexical overlap
            new_score = float(h.score) * 0.7 + overlap * 0.3
            scored.append(
                RetrievedChunk(chunk=h.chunk, score=new_score, source=h.source)
            )
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]

    return list(hits)[:k]
