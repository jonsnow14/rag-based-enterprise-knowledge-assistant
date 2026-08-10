"""Strategy B — light temporal intent (filter-first; high confidence only)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.config import Settings
from src.models.schemas import ChatRequest

_YEAR = re.compile(r"\b(20[12]\d)\b")
_PREV = re.compile(
    r"\b(previous|older|prior|last year|old policy|historical|superseded)\b",
    re.I,
)
_COMPARE_YEARS = re.compile(
    r"\b(compare|vs\.?|versus)\b.*\b(20[12]\d)\b.*\b(20[12]\d)\b",
    re.I,
)
_POLICY_CUE = re.compile(
    r"\b(policy|leave|pto|vacation|version|handbook|effective)\b",
    re.I,
)
_PRICE_CUE = re.compile(r"\b(price|pricing|list price|per seat|sku)\b", re.I)


@dataclass
class TemporalDecision:
    intent: str  # current | historical | as_of | compare_versions
    confidence: float
    year: Optional[str] = None
    include_historical: bool = False
    reason: str = ""


def resolve_temporal_intent(
    question: str,
    req: ChatRequest,
    settings: Settings,
) -> TemporalDecision:
    """Detect temporal intent; default current with high conf when no signal."""
    q = question or ""

    if req.include_historical and not _YEAR.search(q) and not _PREV.search(q):
        return TemporalDecision(
            intent="historical",
            confidence=0.9,
            include_historical=True,
            reason="request.include_historical",
        )

    if req.as_of_date:
        return TemporalDecision(
            intent="as_of",
            confidence=0.9,
            include_historical=True,
            reason=f"as_of={req.as_of_date}",
        )

    if _COMPARE_YEARS.search(q):
        return TemporalDecision(
            intent="compare_versions",
            confidence=0.85,
            include_historical=True,
            reason="compare_years",
        )

    years = _YEAR.findall(q)
    if years and _PRICE_CUE.search(q) and not _POLICY_CUE.search(q):
        # Product year (e.g. 2026 Professional price) — NOT policy version
        return TemporalDecision(
            intent="current",
            confidence=0.95,
            year=years[-1],
            include_historical=False,
            reason="product_year_not_policy",
        )

    if _PREV.search(q):
        return TemporalDecision(
            intent="historical",
            confidence=0.88,
            include_historical=True,
            reason="previous_keyword",
        )

    if years and _POLICY_CUE.search(q):
        return TemporalDecision(
            intent="historical",
            confidence=0.82,
            year=years[-1],
            include_historical=True,
            reason="policy_year",
        )

    return TemporalDecision(
        intent="current",
        confidence=1.0,
        include_historical=False,
        reason="default_current",
    )
