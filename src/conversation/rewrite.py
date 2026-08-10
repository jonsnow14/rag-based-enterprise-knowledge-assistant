"""Strategy C — query rewrite for retrieval (templates first; no full-chat embed)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from src.config import Settings
from src.models.schemas import HistoryMessage

_WHAT_ABOUT = re.compile(
    r"^\s*(what\s+about|how\s+about|and\s+for|for)\s+(.+?)\s*\??\s*$",
    re.I,
)
_EXCEPTION = re.compile(
    r"^\s*(any\s+)?(exception|exceptions|exemption|carve[- ]?out)s?\??\s*$",
    re.I,
)
_PREVIOUS = re.compile(
    r"^\s*(yes,?\s*)?(the\s+)?(previous|older|old|prior|2024)\s*(one|version|policy)?\s*\??\s*$",
    re.I,
)


@dataclass
class RewriteResult:
    retrieval_query: str
    confidence: float
    turn_class: str  # standalone | entity_switch | refinement | meta | offer_followup
    needs_clarification: bool = False
    slots: dict = field(default_factory=dict)
    method: str = "none"  # none | template | passthrough


def _last_user_topic(history: Sequence[HistoryMessage]) -> Optional[str]:
    for m in reversed(list(history)):
        if (m.role or "").lower() == "user" and (m.content or "").strip():
            # strip leading compare words for topic carry
            t = m.content.strip()
            t = re.sub(r"^\s*(what is|what's|how many|how do i)\s+", "", t, flags=re.I)
            return t[:200]
    return None


def _extract_entity_candidate(text: str) -> Optional[str]:
    t = text.strip().rstrip("?.!")
    # drop trailing "plan" noise later
    if len(t) < 2 or len(t) > 80:
        return None
    return t


def rewrite_query(
    question: str,
    history: Optional[Sequence[HistoryMessage]],
    settings: Settings,
) -> RewriteResult:
    """Produce a standalone retrieval query when history exists."""
    q = (question or "").strip()
    hist = list(history or [])

    if not hist:
        return RewriteResult(
            retrieval_query=q,
            confidence=1.0,
            turn_class="standalone",
            method="passthrough",
        )

    # Meta
    if re.match(r"^\s*(thanks|thank you|ok|okay|got it)\s*\.?\s*$", q, re.I):
        return RewriteResult(
            retrieval_query=q,
            confidence=1.0,
            turn_class="meta",
            method="template",
        )

    topic = _last_user_topic(hist) or ""

    m = _WHAT_ABOUT.match(q)
    if m and topic:
        entity = _extract_entity_candidate(m.group(2))
        if entity:
            # Prefer entity + shared topic words (cap length)
            # Drop the previous primary entity-ish first token if present
            retrieval = f"{entity} {topic}"
            # Avoid duplication: if entity already in topic, just entity + key nouns
            if entity.lower() in topic.lower():
                retrieval = topic
            else:
                # replace common plan names in topic with new entity
                for old in ("Enterprise", "Professional", "Standard", "Basic"):
                    if re.search(rf"\b{old}\b", topic, re.I) and not re.search(
                        rf"\b{re.escape(entity)}\b", topic, re.I
                    ):
                        retrieval = re.sub(rf"\b{old}\b", entity, topic, flags=re.I)
                        break
            return RewriteResult(
                retrieval_query=retrieval.strip()[:240],
                confidence=0.95,
                turn_class="entity_switch",
                slots={"primary_entity": entity, "topic": topic},
                method="template",
            )

    if _EXCEPTION.match(q) and topic:
        return RewriteResult(
            retrieval_query=f"{topic} exceptions".strip()[:240],
            confidence=0.90,
            turn_class="refinement",
            slots={"topic": topic, "constraints": ["exceptions"]},
            method="template",
        )

    if _PREVIOUS.match(q) and topic:
        return RewriteResult(
            retrieval_query=topic,
            confidence=0.85,
            turn_class="offer_followup",
            slots={"topic": topic, "temporal": "historical"},
            method="template",
        )

    # Ambiguous "the other one"
    if re.match(r"^\s*(the\s+)?other\s+one\??\s*$", q, re.I):
        return RewriteResult(
            retrieval_query=q,
            confidence=0.3,
            turn_class="ambiguous",
            needs_clarification=True,
            method="template",
        )

    # Default: if question looks complete (>6 tokens), treat as standalone
    tokens = re.findall(r"\w+", q)
    if len(tokens) >= 6:
        return RewriteResult(
            retrieval_query=q,
            confidence=1.0,
            turn_class="standalone",
            method="passthrough",
        )

    # Short follow-up without template — low conf (caller may stay on raw or clarify)
    if topic and len(tokens) <= 5:
        blended = f"{q} {topic}".strip()[:240]
        return RewriteResult(
            retrieval_query=blended,
            confidence=0.55,  # below default τ 0.70 → classic raw unless lowered
            turn_class="refinement",
            slots={"topic": topic},
            method="template",
        )

    return RewriteResult(
        retrieval_query=q,
        confidence=1.0,
        turn_class="standalone",
        method="passthrough",
    )
