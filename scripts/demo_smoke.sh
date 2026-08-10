#!/usr/bin/env bash
# Midnight demo: health + 5 chat scenarios via HTTP (or in-process fallback).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"
USE_HTTP="${USE_HTTP:-1}"
PID_FILE="/tmp/northwind-rag-demo-$$.pid"
LOG_FILE="/tmp/northwind-rag-demo-$$.log"

cleanup() {
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT

echo "=== Northwind RAG demo smoke ==="
echo "root=$ROOT"
echo "mode check..."
python - <<'PY'
from src.config import get_settings
s = get_settings()
print(f"  effective_mode={s.effective_mode()}")
print(f"  openai={s.azure_openai_configured()} search={s.azure_search_configured()}")
from pathlib import Path
p = Path(s.local_index_path)
if not p.is_absolute():
    p = s.package_root() / p
n = sum(1 for line in p.open() if line.strip()) if p.is_file() else 0
print(f"  local_chunks={n} path={p}")
if n < 1:
    raise SystemExit("local index empty — run: python scripts/ingest.py --force-local")
PY

if [[ "$USE_HTTP" == "1" ]]; then
  echo "starting API on ${BASE} ..."
  uvicorn src.api.app:app --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for i in $(seq 1 30); do
    if curl -sf "$BASE/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  echo "--- GET /health ---"
  curl -sS "$BASE/health" | python -m json.tool

  chat() {
    local name="$1"
    local body="$2"
    echo ""
    echo "--- $name ---"
    curl -sS -X POST "$BASE/chat" \
      -H "Content-Type: application/json" \
      -d "$body" | python -m json.tool | head -n 40
  }

  chat "PTO (HR)" \
    '{"question":"How many PTO days for full-time employees with 0-2 years of service?","departments":["HR"]}'
  chat "Password (IT)" \
    '{"question":"What is the minimum password length?","departments":["IT"]}'
  chat "Pricing 2026 (Sales)" \
    '{"question":"What is the Professional plan list price per seat per month for 2026?","departments":["Sales"]}'
  chat "No-guess personal" \
    '{"question":"What is Alice salary and personal phone number?","departments":["HR","Finance"]}'
  chat "ACL HR vs Legal NDA" \
    '{"question":"What is the mutual NDA confidentiality survival period?","departments":["HR"]}'
else
  echo "USE_HTTP=0 — in-process smoke"
  python scripts/smoke_query.py
fi

echo ""
echo "=== demo smoke finished ==="
