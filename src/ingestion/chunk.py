"""Structure-first adaptive chunking (see ADAPTIVE-CHUNKING.md)."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from src.config import Settings, get_settings
from src.ingestion.discover import slugify
from src.ingestion.parse import ParsedDocument, ParsedSection
from src.models.schemas import ChunkRecord
from src.observability import get_logger

log = get_logger(__name__)

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENC = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    # fallback ~ words
    return max(1, len(text.split()))


def adaptive_chunk_size(
    n: int,
    *,
    alpha: float = 3.0,
    c_min: int = 128,
    c_max: int = 512,
) -> int:
    if n <= 0:
        return c_min
    return max(c_min, min(c_max, int(n // alpha)))


def adaptive_overlap(
    c: int,
    *,
    beta: float = 0.12,
    o_min: int = 32,
    o_max: int = 96,
    atomic: bool = False,
) -> int:
    if atomic:
        return 0
    return max(o_min, min(o_max, int(c * beta)))


def window_spans(n_tokens: int, c: int, o: int) -> List[Tuple[int, int]]:
    """Anchored sliding windows over token indices."""
    if n_tokens <= c:
        return [(0, n_tokens)]
    stride = max(1, c - o)
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < n_tokens:
        end = min(n_tokens, start + c)
        spans.append((start, end))
        if end >= n_tokens:
            break
        next_start = start + stride
        if next_start < n_tokens and next_start + c >= n_tokens:
            spans.append((max(0, n_tokens - c), n_tokens))
            break
        start = next_start
    # dedupe
    out: List[Tuple[int, int]] = []
    seen = set()
    for sp in spans:
        if sp not in seen:
            seen.add(sp)
            out.append(sp)
    return out


def _tokenize_words(text: str) -> List[str]:
    return text.split()


def _detok(words: Sequence[str]) -> str:
    return " ".join(words)


def _looks_like_table(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    tabby = sum(1 for ln in lines if "\t" in ln or re.search(r"\s{2,}\S+\s{2,}", ln))
    return tabby >= max(2, len(lines) // 3)


def contextual_prefix(
    *,
    department: str,
    title: str,
    section: str,
    effective_date: Optional[str],
    is_current: bool,
) -> str:
    date_s = effective_date or "n/a"
    return (
        f"[Northwind Traders | {department} | {title} | {date_s} | "
        f"{section} | is_current={str(is_current).lower()}]"
    )


def chunk_document(doc: ParsedDocument, settings: Optional[Settings] = None) -> List[ChunkRecord]:
    s = settings or get_settings()
    chunks: List[ChunkRecord] = []
    ordinal = 0

    for section in doc.sections:
        text = section.text.strip()
        if not text:
            continue

        n = count_tokens(text)
        atomic = section.kind == "sheet" or _looks_like_table(text)
        c_max = s.chunk_c_max_atomic if atomic else s.chunk_c_max
        c = adaptive_chunk_size(n, alpha=s.chunk_alpha, c_min=s.chunk_c_min, c_max=c_max)
        o = adaptive_overlap(c, beta=s.chunk_overlap_beta, atomic=atomic)

        if atomic or n <= c:
            pieces = [(section.title, text, "sheet" if section.kind == "sheet" else ("table" if atomic else "section"), c)]
        else:
            words = _tokenize_words(text)
            # approximate token windows via words if no tiktoken spans on words
            # Use word count proportional to token ratio
            ratio = n / max(1, len(words))
            c_words = max(40, int(c / max(ratio, 0.5)))
            o_words = 0 if atomic else max(10, int(o / max(ratio, 0.5)))
            spans = window_spans(len(words), c_words, o_words)
            pieces = []
            for i, (a, b) in enumerate(spans):
                body = _detok(words[a:b]).strip()
                if body:
                    pieces.append((f"{section.title} (part {i+1})", body, "window", c))

        for title, body, kind, target in pieces:
            prefix = contextual_prefix(
                department=doc.source.department,
                title=doc.title,
                section=title,
                effective_date=doc.effective_date,
                is_current=doc.is_current,
            )
            emb_text = f"{prefix}\n{body}"
            chunk_id = f"{doc.doc_id}_{slugify(title)}_{ordinal:03d}"
            chunks.append(
                ChunkRecord(
                    id=chunk_id,
                    content=body,
                    content_for_embedding=emb_text,
                    doc_id=doc.doc_id,
                    filename=doc.source.filename,
                    section=title,
                    token_count=count_tokens(body),
                    chunk_size_target=target,
                    chunk_kind=kind,
                    department=doc.source.department,
                    access_scope=doc.source.department,
                    knowledge_base_id=doc.source.knowledge_base_id,
                    effective_date=doc.effective_date,
                    is_current=doc.is_current,
                    version=doc.version,
                )
            )
            ordinal += 1

    log.info("chunked %s → %s chunks", doc.source.filename, len(chunks))
    return chunks
