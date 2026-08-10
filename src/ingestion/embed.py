"""Embed chunks via Azure OpenAI or local deterministic fallback."""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Iterable, List, Optional, Sequence

from src.azure_clients import DEFAULT_EMBED_DIM, get_openai_client
from src.config import Settings, get_settings
from src.models.schemas import ChunkRecord
from src.observability import get_logger

log = get_logger(__name__)


def _local_embed(text: str, dim: int = DEFAULT_EMBED_DIM) -> List[float]:
    """
    Deterministic pseudo-embedding for offline demo (not semantic SOTA).
    Enough to exercise the pipeline when Azure OpenAI is unavailable.
    """
    vec = [0.0] * dim
    tokens = text.lower().split()
    if not tokens:
        return vec
    for tok in tokens:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        # map hash bytes to a few indices
        for i in range(0, min(len(h), 16), 2):
            idx = int.from_bytes(h[i : i + 2], "little") % dim
            sign = 1.0 if h[i] % 2 == 0 else -1.0
            vec[idx] += sign
    # l2 normalize
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(
    texts: Sequence[str],
    settings: Optional[Settings] = None,
    *,
    batch_size: int = 16,
) -> List[List[float]]:
    s = settings or get_settings()
    client = get_openai_client(s)
    if client is None:
        log.warning("Azure OpenAI not configured — using local hash embeddings")
        return [_local_embed(t) for t in texts]

    out: List[List[float]] = []
    model = s.azure_openai_embed_deployment
    for i in range(0, len(texts), batch_size):
        batch = list(texts[i : i + batch_size])
        # Azure rejects empty strings
        batch = [b if b.strip() else " " for b in batch]
        resp = client.embeddings.create(model=model, input=batch)
        # ensure order by index
        ordered = sorted(resp.data, key=lambda d: d.index)
        out.extend([list(d.embedding) for d in ordered])
        log.info("embedded batch %s–%s / %s", i, i + len(batch), len(texts))
    return out


def embed_chunks(chunks: List[ChunkRecord], settings: Optional[Settings] = None) -> List[ChunkRecord]:
    if not chunks:
        return chunks
    s = settings or get_settings()
    texts = [c.content_for_embedding or c.content for c in chunks]
    vectors = embed_texts(texts, s)
    for c, v in zip(chunks, vectors):
        c.content_vector = v
    return chunks
