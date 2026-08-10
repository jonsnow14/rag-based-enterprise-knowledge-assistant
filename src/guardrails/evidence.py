"""Evidence gate: fail closed when retrieval is empty or weak."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.config import Settings, get_settings
from src.retrieval.search import RetrievalResult

# Personal / HRIS-style asks that enterprise policy PDFs will not answer
_PERSONAL_DATA_RE = re.compile(
    r"\b("
    r"salary|compensation|phone number|ssn|social security|"
    r"home address|my manager said|employee id|"
    r"alice'?s|bob'?s|personal email"
    r")\b",
    re.I,
)


def looks_like_personal_or_unknowable(question: str) -> bool:
    return bool(_PERSONAL_DATA_RE.search(question or ""))


@dataclass
class EvidenceDecision:
    pass_gate: bool
    reason: str
    top_score: float
    hit_count: int
    min_score: float


def evaluate_evidence(
    result: RetrievalResult,
    settings: Optional[Settings] = None,
    *,
    min_score: Optional[float] = None,
    question: Optional[str] = None,
) -> EvidenceDecision:
    s = settings or get_settings()
    if min_score is not None:
        threshold = min_score
    elif getattr(result, "backend", "local") == "azure":
        threshold = s.rag_min_score_azure
    else:
        threshold = s.rag_min_score
    top = result.top_score
    n = len(result.hits)

    if question and looks_like_personal_or_unknowable(question):
        # Personal facts are never in this corpus — do not answer from weak lexical hits
        return EvidenceDecision(
            pass_gate=False,
            reason="out_of_corpus",
            top_score=top,
            hit_count=n,
            min_score=threshold,
        )

    if n == 0:
        return EvidenceDecision(
            pass_gate=False,
            reason="no_hits",
            top_score=0.0,
            hit_count=0,
            min_score=threshold,
        )
    if top < threshold:
        return EvidenceDecision(
            pass_gate=False,
            reason="below_min_score",
            top_score=top,
            hit_count=n,
            min_score=threshold,
        )
    return EvidenceDecision(
        pass_gate=True,
        reason="ok",
        top_score=top,
        hit_count=n,
        min_score=threshold,
    )


DEPT_CONTACT = {
    "HR": "people-ops@northwindtraders.example or #ask-hr",
    "Finance": "ap@northwindtraders.example or #ask-finance",
    "IT": "itsupport@northwindtraders.example or ext 4357",
    "Legal": "Legal department / contract desk",
    "Sales": "Sales Operations / Deal Desk",
}


def escalate_message(departments: list[str], reason: str) -> str:
    contacts = []
    for d in departments or ["HR"]:
        if d in DEPT_CONTACT:
            contacts.append(f"{d}: {DEPT_CONTACT[d]}")
    contact_line = "; ".join(contacts) if contacts else "your department policy owner"
    if reason == "no_hits":
        detail = "No relevant passages were found in the knowledge base for your question."
    elif reason == "below_min_score":
        detail = "Retrieved passages were too weak to answer confidently."
    elif reason == "out_of_corpus":
        detail = (
            "That request asks for personal or operational data that is not in the "
            "published policy knowledge base."
        )
    else:
        detail = "There is not enough verified evidence in the knowledge base."
    return (
        f"{detail} I will not guess. "
        f"Please contact a human owner for help ({contact_line})."
    )
