"""Context packing for single vs multi-facet prompts."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from src.retrieval.multi_query import CoverageFacet
from src.retrieval.search import RetrievedChunk


def pack_context_blocks(
    hits: Sequence[RetrievedChunk],
    *,
    coverage: Optional[Dict[str, CoverageFacet]] = None,
    max_chars: int = 12000,
) -> str:
    if not coverage:
        return _flat_pack(hits, max_chars=max_chars)

    parts: List[str] = []
    total = 0
    used: set[str] = set()
    for facet_id, cov in coverage.items():
        header = f"## Facet: {facet_id}\n"
        parts.append(header)
        total += len(header)
        for h in hits:
            if h.chunk.id not in cov.selected and h.chunk.id not in cov.accepted:
                continue
            if h.chunk.id in used:
                continue
            block = _block(h)
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
            used.add(h.chunk.id)
    # remainder
    for h in hits:
        if h.chunk.id in used:
            continue
        block = _block(h)
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
        used.add(h.chunk.id)
    return "\n".join(parts)


def _block(h: RetrievedChunk) -> str:
    c = h.chunk
    return (
        f"---\nchunk_id={c.id}\n"
        f"file={c.filename} section={c.section} dept={c.department} "
        f"is_current={c.is_current} version={c.version} score={h.score:.3f}\n"
        f"{c.content}\n"
    )


def _flat_pack(hits: Sequence[RetrievedChunk], *, max_chars: int) -> str:
    parts: List[str] = []
    total = 0
    for h in hits:
        block = _block(h)
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)
