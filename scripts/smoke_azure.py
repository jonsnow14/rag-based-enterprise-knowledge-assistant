#!/usr/bin/env python3
"""P1 connectivity probe for Azure OpenAI + AI Search."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.azure_clients import ensure_search_index, probe_connectivity  # noqa: E402
from src.config import get_settings  # noqa: E402


def main() -> int:
    s = get_settings()
    report = probe_connectivity(s)
    print(json.dumps(report.as_dict(), indent=2))
    print("effective_mode:", s.effective_mode())
    if report.search_ok:
        try:
            ensure_search_index(s)
            print("search_index: ensured/exists")
        except Exception as exc:  # noqa: BLE001
            print("search_index_error:", exc)
            return 1
    # success if we can run: local fallback, or any azure probe ok
    if s.effective_mode() in {"local", "azure-openai-only"} or report.openai_ok or report.search_ok:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
