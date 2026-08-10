#!/usr/bin/env python3
"""CLI: ingest rag-documents into local JSONL and/or Azure AI Search."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# package root = parent of scripts/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.azure_clients import probe_connectivity  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.ingestion.pipeline import run_ingest  # noqa: E402
from src.observability import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Northwind rag-documents")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Documents root (default: settings.rag_documents_path)",
    )
    parser.add_argument(
        "--force-local",
        action="store_true",
        help="Skip Azure Search upsert; write local JSONL only",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="P1 connectivity probe only; do not ingest",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)  # LOG_LEVEL + quiets Azure HTTP (AZURE_LOG_LEVEL)

    report = probe_connectivity(settings)
    print("=== P1 connectivity ===")
    print(json.dumps(report.as_dict(), indent=2))

    if args.probe_only:
        return 0 if (report.openai_ok or settings.app_mode == "local") else 1

    print("=== P2 ingest ===")
    summary = run_ingest(args.source, settings, force_local=args.force_local)
    print(json.dumps(summary, indent=2, default=str))

    # exit non-zero if any file failed
    if summary.get("files_failed", 0) > 0 and summary.get("chunks_total", 0) == 0:
        return 2
    print(
        f"OK: {summary['files_ok']} files → {summary['chunks_total']} chunks "
        f"→ {summary['index'].get('local_path')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
