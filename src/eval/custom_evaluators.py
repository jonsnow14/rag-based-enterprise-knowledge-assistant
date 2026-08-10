"""Code-based evaluators (no LLM judge) for status / citations / path hygiene.

Signatures must only use explicit parameters (no **kwargs) so azure-ai-evaluation
can validate column mappings.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Union


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                pass
        return [v]
    return [str(v)]


def status_match_evaluator(
    status: str = "",
    expected_status: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    """1.0 if actual status is in expected_status list."""
    expected = _as_list(expected_status)
    actual = (status or "").strip().lower()
    if not expected:
        return {"status_match": 1.0, "status_actual": actual, "status_expected": expected}
    ok = actual in {e.strip().lower() for e in expected}
    return {
        "status_match": 1.0 if ok else 0.0,
        "status_actual": actual,
        "status_expected": expected,
    }


def citation_presence_evaluator(
    status: str = "",
    citation_count: Union[int, str] = 0,
) -> Dict[str, Any]:
    """When status is answer/partial, require ≥1 citation."""
    st = (status or "").lower()
    try:
        n = int(citation_count or 0)
    except (TypeError, ValueError):
        n = 0
    if st in {"answer", "partial"}:
        ok = n >= 1
        return {"citation_ok": 1.0 if ok else 0.0, "citation_count": n}
    return {"citation_ok": 1.0, "citation_count": n}


def path_allowed_evaluator(
    path: str = "single",
    forbid_path: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
    """Fail if path is in forbid list (control-system hygiene)."""
    forbidden = {p.strip().lower() for p in _as_list(forbid_path)}
    p = (path or "single").strip().lower()
    if not forbidden:
        return {"path_ok": 1.0, "path": p}
    return {"path_ok": 0.0 if p in forbidden else 1.0, "path": p}


def gold_substring_evaluator(
    response: str = "",
    expected_answer: str = "",
    ground_truth: str = "",
) -> Dict[str, Any]:
    """
    Lightweight lexical overlap proxy when full Foundry similarity is off.
    Score = fraction of significant gold tokens found in response (capped 1.0).
    """
    gold = (expected_answer or ground_truth or "").lower()
    resp = (response or "").lower()
    if not gold.strip():
        return {"gold_token_recall": 1.0}
    tokens = [t for t in re.findall(r"[a-z0-9$%./]+", gold) if len(t) > 2]
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "per",
        "year",
        "days",
        "day",
    }
    tokens = [t for t in tokens if t not in stop][:40]
    if not tokens:
        return {"gold_token_recall": 1.0}
    hit = sum(1 for t in tokens if t in resp)
    return {
        "gold_token_recall": round(hit / len(tokens), 4),
        "gold_tokens_checked": len(tokens),
    }
