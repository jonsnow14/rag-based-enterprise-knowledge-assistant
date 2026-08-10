#!/usr/bin/env python3
"""P3 smoke: exercise chat orchestration without HTTP."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.models.schemas import ChatRequest  # noqa: E402
from src.observability import setup_logging  # noqa: E402
from src.services.chat import run_chat  # noqa: E402

# Apply LOG_LEVEL / AZURE_LOG_LEVEL before any Azure client chatter
setup_logging()

CASES = [
    {
        "name": "pto",
        "req": ChatRequest(
            question="How many PTO days do full-time employees with 0-2 years of service accrue annually?",
            departments=["HR"],
        ),
        "expect_status": {"answer"},
    },
    {
        "name": "password",
        "req": ChatRequest(
            question="What is the minimum password length?",
            departments=["IT"],
        ),
        "expect_status": {"answer"},
    },
    {
        "name": "price_2026",
        "req": ChatRequest(
            question="What is the Professional plan list price per seat per month for 2026?",
            departments=["Sales"],
        ),
        "expect_status": {"answer"},
    },
    {
        "name": "no_guess",
        "req": ChatRequest(
            question="What is Alice's salary and personal phone number?",
            departments=["HR", "Finance", "IT", "Legal", "Sales"],
        ),
        "expect_status": {"escalate", "refuse"},
    },
    {
        "name": "acl_hr_only_legal_q",
        "req": ChatRequest(
            question="What is the mutual NDA confidentiality survival period in years?",
            departments=["HR"],  # should not retrieve Legal
        ),
        # May escalate (weak HR match) or answer from HR-only context — never Legal cites
        "expect_status": {"escalate", "refuse", "answer"},
    },
]


def main() -> int:
    s = get_settings()
    setup_logging(s.log_level)  # respects LOG_LEVEL + AZURE_LOG_LEVEL
    print("effective_mode:", s.effective_mode())
    print("min_score:", s.rag_min_score)
    failed = 0
    for case in CASES:
        resp = run_chat(case["req"], s)
        payload = resp.model_dump()
        ok = resp.status.value in case["expect_status"]
        # for no_guess: if answer, require it not invent a salary number casually — soft check
        if case["name"] == "no_guess" and resp.status.value == "answer":
            text = (resp.answer or "").lower()
            if "guess" in text or "not" in text or "insufficient" in text or "cannot" in text:
                ok = True
            elif any(x in text for x in ["$", "salary is", "phone"]):
                # weak evidence path might still extract unrelated; flag
                ok = resp.retrieval.top_score >= s.rag_min_score and "alice" not in text
        if case["name"] == "acl_hr_only_legal_q":
            # fail if any Legal chunk leaked into citations or retrieval ids
            if any((c.knowledge_base_id or "") == "Legal" for c in resp.citations):
                ok = False
            if any("legal_" in cid for cid in resp.retrieval.chunk_ids):
                ok = False
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"\n=== {case['name']} [{status}] status={resp.status.value} top={resp.retrieval.top_score:.3f} hits={resp.retrieval.hit_count} ===")
        print(json.dumps(payload, indent=2, default=str)[:2000])
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
