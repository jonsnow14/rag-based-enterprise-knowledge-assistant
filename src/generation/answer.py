"""Grounded answer generation via Azure OpenAI or extractive fallback."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple  # noqa: F401 — Optional used

from src.azure_clients import get_openai_client
from src.config import Settings, get_settings
from src.observability import get_logger
from src.generation.pack import pack_context_blocks
from src.retrieval.search import RetrievedChunk

log = get_logger(__name__)

SYSTEM_PROMPT = """You are the Northwind Traders enterprise knowledge assistant.
Answer ONLY using the provided context chunks (EVIDENCE).
If the context is insufficient for any part of the question, say so explicitly — do not guess or invent.
When comparing facets, use only the evidence under each facet; if a facet is missing, state the gap.
When you use a fact, mention its chunk_id in parentheses like (chunk_id=abc_def_000).
Never invent policy numbers, prices, dates, or citations.
Prefer citing the document version/year when present (filename or is_current).
Keep answers concise and factual.
Prior chat is for continuity only — never treat prior assistant text as evidence."""


def build_context(hits: Sequence[RetrievedChunk], *, max_chars: int = 12000) -> str:
    return pack_context_blocks(hits, coverage=None, max_chars=max_chars)


def generate_extractive(question: str, hits: Sequence[RetrievedChunk]) -> str:
    """Offline fallback when Azure OpenAI is unavailable."""
    if not hits:
        return "No evidence available."
    lines = [
        "Based only on retrieved knowledge base passages (extractive mode — Azure OpenAI not configured):",
        "",
    ]
    for h in hits[:3]:
        c = h.chunk
        snippet = c.content.strip().replace("\n", " ")
        if len(snippet) > 400:
            snippet = snippet[:400] + "…"
        lines.append(f"- ({c.id}) [{c.filename} / {c.section}] {snippet}")
    lines.append("")
    lines.append(
        "For a synthesized natural-language answer, configure AZURE_OPENAI_* in .env and re-run."
    )
    return "\n".join(lines)


def generate_answer(
    question: str,
    hits: Sequence[RetrievedChunk],
    settings: Optional[Settings] = None,
    *,
    context: Optional[str] = None,
    partial_note: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Returns (answer_text, engine) where engine is 'azure_openai' | 'extractive'.
    """
    s = settings or get_settings()
    ctx = context if context is not None else build_context(hits)
    client = get_openai_client(s)
    if client is None:
        log.info("using extractive generation (no Azure OpenAI)")
        return generate_extractive(question, hits), "extractive"

    extra = f"\nNote: {partial_note}\n" if partial_note else ""
    user = f"Context:\n{ctx}\n{extra}\nQuestion: {question}\n\nAnswer:"
    try:
        resp = client.chat.completions.create(
            model=s.azure_openai_chat_deployment,
            temperature=0.0,
            max_tokens=800,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return generate_extractive(question, hits), "extractive"
        return text, "azure_openai"
    except Exception as exc:  # noqa: BLE001
        log.warning("chat completion failed: %s — extractive fallback", exc)
        return generate_extractive(question, hits), "extractive"
