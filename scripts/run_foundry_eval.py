#!/usr/bin/env python3
"""
Run evaluation against the Northwind RAG pipeline using Azure AI Evaluation SDK
(Foundry-compatible) plus local code evaluators.

Usage:
  cd azure-based-solutions
  source .venv/bin/activate
  pip install -r requirements.txt -r requirements-eval.txt
  export PYTHONPATH=$(pwd)

  # Local / code metrics only (no LLM judges) — always works
  python scripts/run_foundry_eval.py --limit 5 --code-only

  # Full Foundry LLM judges (needs AOAI endpoint+key for model_config)
  python scripts/run_foundry_eval.py --data data-set/eval_dataset.jsonl

  # Classic ablation
  FORCE_SINGLE_PATH=true python scripts/run_foundry_eval.py --code-only

Optional Foundry project logging (portal):
  export AZURE_AI_PROJECT_ENDPOINT=https://...
  # or set --project-endpoint

See az-ai-foundary-implementation.md for the full roadmap.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.eval.custom_evaluators import (  # noqa: E402
    citation_presence_evaluator,
    gold_substring_evaluator,
    path_allowed_evaluator,
    status_match_evaluator,
)
from src.models.schemas import ChatRequest, HistoryMessage  # noqa: E402
from src.services.chat import run_chat  # noqa: E402


def load_jsonl(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def _history(row: Dict[str, Any]) -> Optional[List[HistoryMessage]]:
    h = row.get("history") or []
    if not h:
        return None
    out: List[HistoryMessage] = []
    for m in h:
        if isinstance(m, dict):
            out.append(HistoryMessage(role=m.get("role", "user"), content=m.get("content", "")))
    return out or None


def run_target_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Call production run_chat and shape fields for evaluators."""
    q = row.get("question") or row.get("query") or ""
    req = ChatRequest(
        question=q,
        departments=row.get("departments"),
        history=_history(row),
        include_historical=bool(row.get("include_historical", False)),
        include_diagnostics=True,
        rag_mode=row.get("rag_mode") or "auto",
    )
    resp = run_chat(req)
    status = resp.status.value if hasattr(resp.status, "value") else str(resp.status)
    path = "single"
    if resp.diagnostics and resp.diagnostics.path:
        path = resp.diagnostics.path
    elif resp.retrieval and resp.retrieval.path:
        path = resp.retrieval.path

    response_text = resp.answer or resp.message or ""
    context = resp.eval_context or ""
    if not context and resp.diagnostics and resp.diagnostics.eval_context:
        context = resp.diagnostics.eval_context

    return {
        "query": q,
        "response": response_text,
        "context": context or "(no retrieval context)",
        "ground_truth": row.get("expected_answer") or "",
        "status": status,
        "expected_status": row.get("expected_status") or [],
        "citation_count": len(resp.citations or []),
        "path": path,
        "query_id": resp.query_id,
        "case_id": row.get("id", ""),
        "category": row.get("category", ""),
    }


def run_code_only(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score every row with code evaluators (no azure-ai-evaluation required)."""
    per_row: List[Dict[str, Any]] = []
    sums: Dict[str, float] = {
        "status_match": 0.0,
        "citation_ok": 0.0,
        "gold_token_recall": 0.0,
    }
    n = 0
    t0 = time.perf_counter()
    for row in rows:
        n += 1
        out = run_target_row(row)
        sm = status_match_evaluator(
            status=out["status"], expected_status=out["expected_status"]
        )
        ci = citation_presence_evaluator(
            status=out["status"], citation_count=out["citation_count"]
        )
        gold = gold_substring_evaluator(
            response=out["response"], expected_answer=out["ground_truth"]
        )
        path_m = path_allowed_evaluator(path=out["path"], forbid_path=row.get("forbid_path"))
        metrics = {**sm, **ci, **gold, **path_m}
        for k in sums:
            sums[k] += float(metrics.get(k, 0.0))
        per_row.append({"case_id": out["case_id"], "category": out["category"], **out, **metrics})
        print(
            f"  [{n}/{len(rows)}] {out['case_id']} status={out['status']} "
            f"path={out['path']} status_match={metrics['status_match']} "
            f"gold={metrics['gold_token_recall']:.2f}"
        )

    elapsed = time.perf_counter() - t0
    means = {k: (sums[k] / n if n else 0.0) for k in sums}
    return {
        "mode": "code_only",
        "count": n,
        "elapsed_sec": round(elapsed, 2),
        "metrics_mean": means,
        "rows": per_row,
    }


def run_foundry_sdk(
    data_path: Path,
    rows: List[Dict[str, Any]],
    *,
    model_config: Optional[Dict[str, str]] = None,
    project_endpoint: Optional[str] = None,
    output_path: Path,
) -> Dict[str, Any]:
    """
    Use azure.ai.evaluation.evaluate on **pre-generated** pipeline outputs.

    We do not pass `target=` into evaluate() — the Foundry batch engine can break
    nested Azure OpenAI embedding calls. Instead:
      1) run_chat for each row (our process)
      2) evaluate(data=precomputed.jsonl, evaluators=...)
    """
    from azure.ai.evaluation import evaluate

    print("=== Phase A: generate pipeline outputs (run_chat) ===")
    generated: List[Dict[str, Any]] = []
    for i, row in enumerate(rows, 1):
        out = run_target_row(row)
        print(
            f"  gen [{i}/{len(rows)}] {out.get('case_id')} "
            f"status={out['status']} path={out['path']}"
        )
        generated.append(
            {
                "query": out["query"],
                "response": out["response"],
                "context": out["context"],
                "ground_truth": out["ground_truth"],
                "expected_answer": out["ground_truth"],
                "status": out["status"],
                "expected_status": out["expected_status"],
                "citation_count": out["citation_count"],
                "path": out["path"],
                "case_id": out.get("case_id", ""),
                "category": out.get("category", ""),
            }
        )

    work = output_path.parent / f"_work_{output_path.stem}.jsonl"
    with work.open("w", encoding="utf-8") as f:
        for g in generated:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    evaluators: Dict[str, Any] = {
        "status_match": status_match_evaluator,
        "citation_ok": citation_presence_evaluator,
        "gold_token_recall": gold_substring_evaluator,
    }

    llm_enabled = False
    if model_config and model_config.get("azure_endpoint") and model_config.get("api_key"):
        try:
            from azure.ai.evaluation import GroundednessEvaluator, RelevanceEvaluator

            evaluators["groundedness"] = GroundednessEvaluator(model_config=model_config)
            evaluators["relevance"] = RelevanceEvaluator(model_config=model_config)
            llm_enabled = True
            print("LLM judges: GroundednessEvaluator + RelevanceEvaluator enabled")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: could not init LLM judges: {exc} — code evaluators only")

    # Precomputed columns live in data → use ${data.*}
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
    if llm_enabled:
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

    kwargs: Dict[str, Any] = {
        "data": str(work),
        "evaluators": evaluators,
        "evaluator_config": eval_config,
        "output_path": str(output_path),
        "evaluation_name": f"northwind-rag-{output_path.stem}",
    }

    if project_endpoint:
        kwargs["azure_ai_project"] = project_endpoint
        print(f"Logging to Foundry project: {project_endpoint}")

    print(f"=== Phase B: evaluate() rows={len(generated)} llm_judges={llm_enabled} ===")
    result = evaluate(**kwargs)

    metrics = getattr(result, "metrics", None)
    if metrics is None and isinstance(result, dict):
        metrics = result.get("metrics")
    studio_url = getattr(result, "studio_url", None)
    if studio_url is None and isinstance(result, dict):
        studio_url = result.get("studio_url")

    summary: Dict[str, Any] = {
        "mode": "foundry_sdk_pregenerate",
        "llm_judges": llm_enabled,
        "count": len(rows),
        "output_path": str(output_path),
        "metrics": metrics,
        "studio_url": studio_url,
        "generated_preview": [
            {
                "case_id": g["case_id"],
                "status": g["status"],
                "path": g["path"],
                "response_head": (g["response"] or "")[:120],
            }
            for g in generated[:5]
        ],
    }
    try:
        work.unlink(missing_ok=True)
    except OSError:
        pass
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Azure AI Foundry / Evaluation SDK runner")
    ap.add_argument(
        "--data",
        type=Path,
        default=ROOT / "data-set" / "eval_dataset.jsonl",
        help="Golden JSONL dataset",
    )
    ap.add_argument("--limit", type=int, default=None, help="Max rows (smoke)")
    ap.add_argument(
        "--code-only",
        action="store_true",
        help="Skip azure-ai-evaluation LLM judges; code metrics only",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path (default eval/results/foundry_<ts>.json)",
    )
    ap.add_argument(
        "--project-endpoint",
        default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"),
        help="Optional Foundry project endpoint for portal logging",
    )
    ap.add_argument(
        "--force-single",
        action="store_true",
        help="Set FORCE_SINGLE_PATH for classic ablation",
    )
    args = ap.parse_args()

    if args.force_single:
        os.environ["FORCE_SINGLE_PATH"] = "true"
        get_settings.cache_clear()

    get_settings.cache_clear()
    s = get_settings()
    print(
        f"settings mode={s.effective_mode()} force_single={s.force_single_path} "
        f"enhanced={s.enhanced_enabled()}"
    )

    if not args.data.is_file():
        print(f"ERROR: dataset not found: {args.data}")
        return 2

    rows = load_jsonl(args.data, limit=args.limit)
    print(f"Loaded {len(rows)} cases from {args.data}")

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out or (out_dir / f"foundry_{ts}.json")

    if args.code_only:
        print("=== code-only evaluation ===")
        result = run_code_only(rows)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nmetrics_mean: {json.dumps(result['metrics_mean'], indent=2)}")
        print(f"Wrote {out_path}")
        # soft gate
        means = result["metrics_mean"]
        if means.get("status_match", 0) < 0.5:
            print("WARN: status_match mean < 0.5")
            return 1
        return 0

    # Foundry SDK path
    try:
        import azure.ai.evaluation  # noqa: F401
    except ImportError:
        print(
            "azure-ai-evaluation not installed. "
            "pip install -r requirements-eval.txt  OR  use --code-only"
        )
        return 2

    model_config = {
        "azure_endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT") or s.azure_openai_endpoint,
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY") or s.azure_openai_api_key,
        "azure_deployment": os.environ.get("AZURE_OPENAI_EVAL_DEPLOYMENT")
        or os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or s.azure_openai_chat_deployment,
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION") or s.azure_openai_api_version,
    }
    # strip empty
    if not model_config["azure_endpoint"] or not model_config["api_key"]:
        print("WARN: no AOAI credentials — falling back to --code-only behavior inside SDK path")
        model_config = None

    try:
        summary = run_foundry_sdk(
            args.data,
            rows,
            model_config=model_config,
            project_endpoint=args.project_endpoint,
            output_path=out_path,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Foundry evaluate() failed: {exc}")
        print("Falling back to code-only…")
        result = run_code_only(rows)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {out_path}")
        return 1

    # also write summary sidecar
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {out_path} and {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
