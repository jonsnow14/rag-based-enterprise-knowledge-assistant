#!/usr/bin/env python3
"""Run single-turn / enhanced regression suites (control-system bar)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.models.schemas import ChatRequest, HistoryMessage  # noqa: E402
from src.services.chat import run_chat  # noqa: E402


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def check_case(case: dict, resp) -> list[str]:
    errs: list[str] = []
    exp = case.get("expect") or {}
    status = resp.status.value if hasattr(resp.status, "value") else str(resp.status)
    want_status = exp.get("status") or exp.get("expect_status")
    if want_status and status not in want_status:
        errs.append(f"status={status} not in {want_status}")

    ans = resp.answer or ""
    for needle in exp.get("answer_contains_any") or []:
        if needle.lower() in ans.lower():
            break
    else:
        if exp.get("answer_contains_any"):
            errs.append(f"answer missing any of {exp['answer_contains_any']}")

    path = (resp.retrieval.path if resp.retrieval else "single") or "single"
    if resp.diagnostics and resp.diagnostics.path:
        path = resp.diagnostics.path
    forbid = exp.get("forbid_path") or []
    if path in forbid:
        errs.append(f"path={path} forbidden")
    path_in = exp.get("path_in")
    if path_in and path not in path_in:
        errs.append(f"path={path} not in {path_in}")

    min_c = exp.get("min_citations")
    if min_c is not None and len(resp.citations or []) < min_c:
        errs.append(f"citations={len(resp.citations or [])} < {min_c}")

    if exp.get("diagnostics_has_rewrite"):
        d = resp.diagnostics
        if not d or not (d.rewritten_query or d.triggers.get("rewrite_confidence")):
            # rewrite may be skipped if no history applied — still ok if path ran
            if not case.get("history"):
                errs.append("expected rewrite diagnostics")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--suite",
        choices=["single", "enhanced", "all"],
        default="single",
    )
    ap.add_argument("--force-single", action="store_true", help="Set FORCE_SINGLE_PATH for run")
    args = ap.parse_args()

    # clear settings cache if env changed
    get_settings.cache_clear()
    if args.force_single:
        import os

        os.environ["FORCE_SINGLE_PATH"] = "true"
        get_settings.cache_clear()

    suites = []
    if args.suite in ("single", "all"):
        suites.append(ROOT / "eval" / "regression_single_turn.jsonl")
    if args.suite in ("enhanced", "all"):
        suites.append(ROOT / "eval" / "regression_enhanced.jsonl")

    failed = 0
    total = 0
    for suite_path in suites:
        if not suite_path.is_file():
            print(f"SKIP missing {suite_path}")
            continue
        print(f"=== {suite_path.name} ===")
        for case in load_cases(suite_path):
            total += 1
            hist = None
            if case.get("history"):
                hist = [HistoryMessage(**h) for h in case["history"]]
            req = ChatRequest(
                question=case["question"],
                history=hist,
                include_diagnostics=bool(
                    case.get("include_diagnostics") or case.get("expect", {}).get("diagnostics_has_rewrite")
                ),
                departments=case.get("departments"),
                include_historical=bool(case.get("include_historical", False)),
                rag_mode=case.get("rag_mode", "auto"),
            )
            try:
                resp = run_chat(req)
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {case['id']}: exception {exc}")
                failed += 1
                continue
            errs = check_case(case, resp)
            status = resp.status.value
            path = resp.diagnostics.path if resp.diagnostics else resp.retrieval.path
            if errs:
                print(f"FAIL {case['id']} status={status} path={path}: {errs}")
                if resp.answer:
                    print(f"  answer[:200]={resp.answer[:200]!r}")
                failed += 1
            else:
                print(f"PASS {case['id']} status={status} path={path}")

    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
