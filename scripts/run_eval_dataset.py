#!/usr/bin/env python3
"""
Run evaluation dataset questions against the RAG app one-by-one,
write results to CSV/JSONL, then score with Azure AI Foundry evaluators
and merge Foundry metrics into the same CSV.

Usage (from azure-based-solutions/):

  # In-process + Foundry judges (uses .env AOAI)
  python scripts/run_eval_dataset.py

  # App responses only (skip Foundry LLM judges)
  python scripts/run_eval_dataset.py --no-foundry

  # Against running API
  python scripts/run_eval_dataset.py --mode http --base-url http://127.0.0.1:8000

  python scripts/run_eval_dataset.py --limit 5
  python scripts/run_eval_dataset.py --input data-set/eval_dataset.jsonl \\
      --output data-set/eval_dataset_with_results.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_INPUT = ROOT / "data-set" / "eval_dataset.jsonl"
DEFAULT_OUTPUT = ROOT / "data-set" / "eval_dataset_with_results.csv"
DEFAULT_JSONL_OUT = ROOT / "data-set" / "eval_results.jsonl"
DEFAULT_FOUNDRY_SUMMARY = ROOT / "data-set" / "foundry_eval_summary.json"
DEFAULT_FOUNDRY_DETAIL = ROOT / "data-set" / "foundry_eval_rows.jsonl"

# Preserve original case columns + app result columns + Foundry columns
BASE_FIELDNAMES = [
    "id",
    "category",
    "difficulty",
    "question",
    "expected_answer",
    "expected_document",
    "expected_section",
    "expected_status",
    "departments",
    "conversation_id",
    "turn",
    "status",
    "response",
    "citation",
    "query_id",
    "latency_ms",
    "hit_count",
    "top_score",
    "path",
    "retrieval_query",
    # Foundry / code metrics
    "status_match",
    "citation_ok",
    "gold_token_recall",
    "groundedness",
    "groundedness_passed",
    "relevance",
    "relevance_passed",
    "foundry_scored",
]


def _load_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases


def _format_citations(citations: Any) -> str:
    if not citations:
        return ""
    parts: List[str] = []
    for c in citations:
        if isinstance(c, dict):
            fn = c.get("filename") or c.get("doc_id") or ""
            sec = c.get("section") or ""
            cid = c.get("chunk_id") or ""
            kb = c.get("knowledge_base_id") or ""
            ver = c.get("version_label") or ""
        else:
            fn = getattr(c, "filename", None) or getattr(c, "doc_id", None) or ""
            sec = getattr(c, "section", None) or ""
            cid = getattr(c, "chunk_id", None) or ""
            kb = getattr(c, "knowledge_base_id", None) or ""
            ver = getattr(c, "version_label", None) or ""
        bits = [x for x in [fn, sec, cid, f"kb={kb}" if kb else "", f"v={ver}" if ver else ""] if x]
        parts.append(" | ".join(bits) if bits else str(c))
    return "; ".join(parts)


def _format_response(payload: Dict[str, Any]) -> str:
    answer = payload.get("answer")
    if answer:
        return str(answer).strip()
    message = payload.get("message")
    status = payload.get("status", "")
    if message:
        return f"[{status}] {message}".strip()
    return f"[{status}]"


def _call_inprocess(case: Dict[str, Any]) -> Dict[str, Any]:
    from src.config import get_settings
    from src.models.schemas import ChatRequest, HistoryMessage
    from src.services.chat import run_chat

    history_raw = case.get("history") or []
    history = None
    if history_raw:
        history = [HistoryMessage(role=h["role"], content=h["content"]) for h in history_raw]

    depts = case.get("departments")
    req = ChatRequest(
        question=case["question"],
        departments=depts,
        history=history,
        conversation_id=case.get("conversation_id"),
        include_diagnostics=True,
        include_historical=bool(case.get("include_historical", False)),
    )
    resp = run_chat(req, get_settings())
    return resp.model_dump(mode="json")


def _call_http(
    case: Dict[str, Any],
    base_url: str,
    api_key: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/v1/chat"
    body: Dict[str, Any] = {
        "question": case["question"],
        "include_diagnostics": True,
    }
    if case.get("departments"):
        body["departments"] = case["departments"]
    if case.get("history"):
        body["history"] = case["history"]
    if case.get("conversation_id"):
        body["conversation_id"] = case["conversation_id"]

    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return {
            "status": "error",
            "answer": None,
            "message": f"HTTP {e.code}: {err_body}",
            "citations": [],
            "query_id": "",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "answer": None,
            "message": f"request failed: {e}",
            "citations": [],
            "query_id": "",
        }


def _apply_code_metrics(row: Dict[str, Any], case: Dict[str, Any]) -> None:
    from src.eval.custom_evaluators import (
        citation_presence_evaluator,
        gold_substring_evaluator,
        status_match_evaluator,
    )

    sm = status_match_evaluator(
        status=str(row.get("status") or ""),
        expected_status=case.get("expected_status") or [],
    )
    ci = citation_presence_evaluator(
        status=str(row.get("status") or ""),
        citation_count=int(row.get("_citation_count") or 0),
    )
    gold = gold_substring_evaluator(
        response=str(row.get("response") or ""),
        expected_answer=str(row.get("expected_answer") or ""),
    )
    row["status_match"] = sm.get("status_match", "")
    row["citation_ok"] = ci.get("citation_ok", "")
    row["gold_token_recall"] = gold.get("gold_token_recall", "")
    row["foundry_scored"] = "code"


def _run_foundry_on_generated(
    generated: List[Dict[str, Any]],
    *,
    detail_path: Path,
    summary_path: Path,
    project_endpoint: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Score pre-generated rows with azure-ai-evaluation.
    Returns map case_id -> metric dict.
    """
    from src.config import get_settings
    from src.eval.custom_evaluators import (
        citation_presence_evaluator,
        gold_substring_evaluator,
        status_match_evaluator,
    )

    try:
        from azure.ai.evaluation import evaluate
    except ImportError as exc:
        print(f"WARN: azure-ai-evaluation not installed ({exc}); code metrics only")
        return {}

    s = get_settings()
    work = detail_path.parent / f"_foundry_work_{int(time.time())}.jsonl"
    with work.open("w", encoding="utf-8") as f:
        for g in generated:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    evaluators: Dict[str, Any] = {
        "status_match": status_match_evaluator,
        "citation_ok": citation_presence_evaluator,
        "gold_token_recall": gold_substring_evaluator,
    }
    llm_on = False
    model_config = {
        "azure_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT") or s.azure_openai_endpoint,
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY") or s.azure_openai_api_key,
        "azure_deployment": os.environ.get("AZURE_OPENAI_EVAL_DEPLOYMENT")
        or os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or s.azure_openai_chat_deployment,
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION") or s.azure_openai_api_version,
    }
    if model_config["azure_endpoint"] and model_config["api_key"]:
        try:
            from azure.ai.evaluation import GroundednessEvaluator, RelevanceEvaluator

            evaluators["groundedness"] = GroundednessEvaluator(model_config=model_config)
            evaluators["relevance"] = RelevanceEvaluator(model_config=model_config)
            llm_on = True
            print("Foundry LLM judges: groundedness + relevance ON")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: LLM judges unavailable: {exc}")

    eval_config: Dict[str, Any] = {
        "status_match": {
            "column_mapping": {
                "status": "${data.status}",
                "expected_status": "${data.expected_status}",
            }
        },
        "citation_ok": {
            "column_mapping": {
                "status": "${data.status}",
                "citation_count": "${data.citation_count}",
            }
        },
        "gold_token_recall": {
            "column_mapping": {
                "response": "${data.response}",
                "expected_answer": "${data.ground_truth}",
                "ground_truth": "${data.ground_truth}",
            }
        },
    }
    if llm_on:
        eval_config["groundedness"] = {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
                "context": "${data.context}",
            }
        }
        eval_config["relevance"] = {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
            }
        }

    out_eval = detail_path.parent / f"foundry_sdk_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    kwargs: Dict[str, Any] = {
        "data": str(work),
        "evaluators": evaluators,
        "evaluator_config": eval_config,
        "output_path": str(out_eval),
        "evaluation_name": f"northwind-dataset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
    }
    if project_endpoint:
        kwargs["azure_ai_project"] = project_endpoint
        print(f"Foundry project endpoint: {project_endpoint}")

    print(f"Running azure.ai.evaluation.evaluate on {len(generated)} rows…")
    result = evaluate(**kwargs)

    metrics = getattr(result, "metrics", None)
    if metrics is None and isinstance(result, dict):
        metrics = result.get("metrics")
    studio_url = getattr(result, "studio_url", None)
    if studio_url is None and isinstance(result, dict):
        studio_url = result.get("studio_url")

    # Parse row-level output if present
    by_id: Dict[str, Dict[str, Any]] = {}
    rows_data = None
    if isinstance(result, dict):
        rows_data = result.get("rows")
    if rows_data is None and hasattr(result, "rows"):
        rows_data = result.rows

    # Also try reading evaluate output file (often JSON lines / nested)
    if out_eval.is_file():
        try:
            raw = json.loads(out_eval.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("rows"):
                rows_data = raw["rows"]
            if isinstance(raw, dict) and raw.get("metrics") and not metrics:
                metrics = raw["metrics"]
        except json.JSONDecodeError:
            pass

    if rows_data:
        for r in rows_data:
            # SDK shape: inputs.case_id, outputs.status_match.status_match, …
            cid = (
                r.get("inputs.case_id")
                or r.get("case_id")
                or r.get("inputs.id")
                or r.get("data.case_id")
            )
            if not cid:
                q = r.get("inputs.query") or r.get("query") or r.get("data.query")
                if q:
                    for g in generated:
                        if g["query"] == q:
                            cid = g["case_id"]
                            break
            if not cid:
                continue
            m: Dict[str, Any] = {"foundry_scored": "foundry"}

            def _get(*keys: str) -> Any:
                for k in keys:
                    if k in r and r[k] is not None:
                        return r[k]
                return None

            sm = _get(
                "outputs.status_match.status_match",
                "outputs.status_match",
            )
            if sm is not None:
                m["status_match"] = sm
            co = _get("outputs.citation_ok.citation_ok", "outputs.citation_ok")
            if co is not None:
                m["citation_ok"] = co
            gtr = _get(
                "outputs.gold_token_recall.gold_token_recall",
                "outputs.gold_token_recall",
            )
            if gtr is not None:
                m["gold_token_recall"] = gtr
            gr = _get(
                "outputs.groundedness.groundedness",
                "outputs.groundedness.groundedness_score",
                "outputs.groundedness",
            )
            if gr is not None:
                m["groundedness"] = gr
            grp = _get("outputs.groundedness.groundedness_passed")
            if grp is not None:
                m["groundedness_passed"] = grp
            rel = _get(
                "outputs.relevance.relevance",
                "outputs.relevance.relevance_score",
                "outputs.relevance",
            )
            if rel is not None:
                m["relevance"] = rel
            relp = _get("outputs.relevance.relevance_passed")
            if relp is not None:
                m["relevance_passed"] = relp
            by_id[str(cid)] = m

    # Fallback: if no per-row parse, keep code metrics already on rows
    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(generated),
        "llm_judges": llm_on,
        "metrics_aggregate": metrics,
        "studio_url": studio_url,
        "sdk_output": str(out_eval),
        "per_row_parsed": len(by_id),
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"Wrote Foundry summary: {summary_path}")
    if studio_url:
        print(f"Foundry studio URL: {studio_url}")

    # Write detail JSONL of generated + metrics
    with detail_path.open("w", encoding="utf-8") as f:
        for g in generated:
            cid = g["case_id"]
            merged = {**g, **(by_id.get(cid) or {})}
            f.write(json.dumps(merged, ensure_ascii=False, default=str) + "\n")
    print(f"Wrote Foundry row detail: {detail_path}")

    try:
        work.unlink(missing_ok=True)
    except OSError:
        pass

    return by_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run eval dataset against RAG, write CSV, score with Foundry"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="eval_dataset.jsonl path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output CSV with results")
    parser.add_argument("--jsonl-out", type=Path, default=DEFAULT_JSONL_OUT, help="full raw results JSONL")
    parser.add_argument(
        "--foundry-summary",
        type=Path,
        default=DEFAULT_FOUNDRY_SUMMARY,
        help="Foundry aggregate metrics JSON",
    )
    parser.add_argument(
        "--foundry-detail",
        type=Path,
        default=DEFAULT_FOUNDRY_DETAIL,
        help="Per-row Foundry scores JSONL",
    )
    parser.add_argument(
        "--mode",
        choices=("inprocess", "http"),
        default="inprocess",
        help="inprocess=call run_chat directly; http=POST /v1/chat",
    )
    parser.add_argument("--base-url", default=os.environ.get("RAG_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("RAG_API_KEY"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--limit", type=int, default=0, help="if >0, only first N cases")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds between requests")
    parser.add_argument("--start-from", type=str, default="", help="skip until this case id")
    parser.add_argument(
        "--no-foundry",
        action="store_true",
        help="Skip azure-ai-evaluation LLM judges (still compute code metrics)",
    )
    parser.add_argument(
        "--project-endpoint",
        default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"),
        help="Optional Foundry project endpoint for portal logging",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    cases = _load_cases(args.input)
    if args.start_from:
        idx = next((i for i, c in enumerate(cases) if c.get("id") == args.start_from), None)
        if idx is None:
            print(f"ERROR: start-from id not found: {args.start_from}", file=sys.stderr)
            return 1
        cases = cases[idx:]
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    print(f"mode={args.mode} cases={len(cases)} input={args.input}")
    if args.mode == "http":
        print(f"base_url={args.base_url}")
    else:
        from src.config import get_settings
        from src.observability import setup_logging

        s = get_settings()
        setup_logging(s.log_level)
        print(f"effective_mode={s.effective_mode()}")

    results: List[Dict[str, Any]] = []
    generated_for_foundry: List[Dict[str, Any]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)

    # Overwrite raw JSONL for this full run
    if args.jsonl_out.is_file():
        args.jsonl_out.unlink()

    for i, case in enumerate(cases, 1):
        cid = case.get("id", f"row-{i}")
        q = case.get("question", "")
        print(f"[{i}/{len(cases)}] {cid}: {q[:80]}{'…' if len(q) > 80 else ''}")

        t0 = time.time()
        if args.mode == "http":
            payload = _call_http(case, args.base_url, args.api_key, args.timeout)
        else:
            try:
                payload = _call_inprocess(case)
            except Exception as e:  # noqa: BLE001
                payload = {
                    "status": "error",
                    "answer": None,
                    "message": str(e)[:500],
                    "citations": [],
                    "query_id": "",
                }
        elapsed_ms = int((time.time() - t0) * 1000)

        response_text = _format_response(payload)
        citation_text = _format_citations(payload.get("citations") or [])
        status = payload.get("status", "")
        retrieval = payload.get("retrieval") or {}
        diagnostics = payload.get("diagnostics") or {}
        path = diagnostics.get("path") or retrieval.get("path") or ""
        retrieval_query = diagnostics.get("retrieval_query") or q
        cites = payload.get("citations") or []
        eval_context = payload.get("eval_context") or diagnostics.get("eval_context") or ""

        print(
            f"    status={status} path={path} "
            f"citations={len(cites)} {elapsed_ms}ms"
        )

        row: Dict[str, Any] = {
            "id": cid,
            "category": case.get("category", ""),
            "difficulty": case.get("difficulty", ""),
            "question": q,
            "expected_answer": case.get("expected_answer", ""),
            "expected_document": case.get("expected_document") or "",
            "expected_section": case.get("expected_section") or "",
            "expected_status": "|".join(case.get("expected_status") or []),
            "departments": "|".join(case.get("departments") or []),
            "conversation_id": case.get("conversation_id") or "",
            "turn": case.get("turn") or "",
            "status": status,
            "response": response_text,
            "citation": citation_text,
            "query_id": payload.get("query_id") or "",
            "latency_ms": elapsed_ms,
            "hit_count": retrieval.get("hit_count", ""),
            "top_score": retrieval.get("top_score", ""),
            "path": path,
            "retrieval_query": retrieval_query,
            "_citation_count": len(cites),
        }
        _apply_code_metrics(row, case)
        results.append(row)

        generated_for_foundry.append(
            {
                "case_id": cid,
                "query": q,
                "response": response_text,
                "context": eval_context or "(no retrieval context)",
                "ground_truth": case.get("expected_answer") or "",
                "status": status,
                "expected_status": case.get("expected_status") or [],
                "citation_count": len(cites),
                "path": path,
                "category": case.get("category", ""),
            }
        )

        raw_line = {
            "id": cid,
            "request": {
                "question": q,
                "departments": case.get("departments"),
                "history": case.get("history") or [],
            },
            "response": payload,
            "latency_ms": elapsed_ms,
            "code_metrics": {
                "status_match": row.get("status_match"),
                "citation_ok": row.get("citation_ok"),
                "gold_token_recall": row.get("gold_token_recall"),
            },
        }
        with args.jsonl_out.open("a", encoding="utf-8") as jf:
            jf.write(json.dumps(raw_line, ensure_ascii=False, default=str) + "\n")

        if args.sleep > 0:
            time.sleep(args.sleep)

    # Foundry scoring
    foundry_by_id: Dict[str, Dict[str, Any]] = {}
    if not args.no_foundry:
        try:
            foundry_by_id = _run_foundry_on_generated(
                generated_for_foundry,
                detail_path=args.foundry_detail,
                summary_path=args.foundry_summary,
                project_endpoint=args.project_endpoint,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: Foundry evaluate failed ({exc}); CSV keeps code metrics only")
            args.foundry_summary.write_text(
                json.dumps(
                    {
                        "error": str(exc),
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "note": "code metrics only on CSV",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    else:
        print("Skipping Foundry SDK (--no-foundry); code metrics only")

    # Merge Foundry per-row scores into CSV rows
    for row in results:
        extra = foundry_by_id.get(str(row["id"])) or {}
        for k in (
            "status_match",
            "citation_ok",
            "gold_token_recall",
            "groundedness",
            "groundedness_passed",
            "relevance",
            "relevance_passed",
            "foundry_scored",
        ):
            if k in extra and extra[k] is not None and extra[k] != "":
                row[k] = extra[k]
        row.pop("_citation_count", None)

    with args.output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BASE_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for row in results:
            w.writerow({k: row.get(k, "") for k in BASE_FIELDNAMES})

    # Aggregate printout
    n = len(results)
    sm = sum(float(r.get("status_match") or 0) for r in results) / n if n else 0
    co = sum(float(r.get("citation_ok") or 0) for r in results) / n if n else 0
    gr = [float(r["groundedness"]) for r in results if r.get("groundedness") not in ("", None)]
    rel = [float(r["relevance"]) for r in results if r.get("relevance") not in ("", None)]

    print(f"\nWrote CSV:   {args.output}")
    print(f"Wrote JSONL: {args.jsonl_out}")
    print(f"Cases: {n}")
    print(f"  mean status_match={sm:.3f}  citation_ok={co:.3f}")
    if gr:
        print(f"  mean groundedness={sum(gr)/len(gr):.3f} (n={len(gr)})")
    if rel:
        print(f"  mean relevance={sum(rel)/len(rel):.3f} (n={len(rel)})")
    if args.foundry_summary.is_file():
        print(f"  Foundry summary: {args.foundry_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
