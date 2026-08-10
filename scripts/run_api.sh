#!/usr/bin/env bash
# Run API from azure-based-solutions package root
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
exec python -m uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload
