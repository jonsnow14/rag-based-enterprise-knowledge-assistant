"""S5 — light ambiguity detection → clarify options."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence

from src.retrieval.search import RetrievedChunk

_UNDERSPEC = re.compile(
    r"^\s*(what|what'?s|whats)\s+(is|are)\s+(the\s+)?(limit|policy|rule|amount|cap)\??\s*$",
    re.I,
)
_SHORT_LIMIT = re.compile(r"^\s*(the\s+)?limit\??\s*$", re.I)


@dataclass
class AmbiguityDecision:
    is_ambiguous: bool
    options: List[str]
    reason: str = ""


def detect_ambiguity(
    question: str,
    hits: Sequence[RetrievedChunk],
) -> AmbiguityDecision:
    q = (question or "").strip()
    # High-precision only — do NOT treat every short question as ambiguous
    # (e.g. "What is Alice's salary?" must escalate, not clarify).
    underspec = bool(_UNDERSPEC.match(q) or _SHORT_LIMIT.match(q))
    if not underspec:
        return AmbiguityDecision(False, [], "specific")

    # Cluster by coarse topic from filename/section
    labels: List[str] = []
    seen = set()
    for h in hits[:8]:
        label = (h.chunk.section or h.chunk.filename or "").strip()
        if not label:
            continue
        # shorten
        short = label.split("|")[0].strip()[:60]
        key = short.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(short)
        if len(labels) >= 4:
            break

    if len(labels) >= 2:
        return AmbiguityDecision(True, labels, "multi_topic_underspecified")
    return AmbiguityDecision(False, labels, "single_or_empty")
