#!/usr/bin/env bash
# Example partner calls — copy values after ACA / APIM deploy.
# Usage:
#   export BASE=https://<apim>.azure-api.net/rag   # or https://<aca-fqdn>
#   export KEY=<Ocp-Apim-Subscription-Key or X-API-Key>
#   ./scripts/partner_curl_examples.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
KEY="${KEY:-}"
AUTH_MODE="${AUTH_MODE:-auto}"  # auto | apim | x-api-key | none

hdrs=(-H "Content-Type: application/json")
if [[ "$AUTH_MODE" == "apim" || ( "$AUTH_MODE" == "auto" && "$BASE" == *azure-api.net* ) ]]; then
  if [[ -z "$KEY" ]]; then echo "Set KEY= APIM subscription key"; exit 1; fi
  hdrs+=(-H "Ocp-Apim-Subscription-Key: $KEY")
elif [[ "$AUTH_MODE" == "x-api-key" || ( "$AUTH_MODE" == "auto" && -n "$KEY" ) ]]; then
  hdrs+=(-H "X-API-Key: $KEY")
fi

echo "=== GET ${BASE}/health ==="
curl -sS "${BASE}/health" | python3 -m json.tool || curl -sS "${BASE}/health"
echo ""

Q="${1:-How many PTO days for 0-2 years of service?}"
echo "=== POST ${BASE}/v1/chat ==="
curl -sS "${BASE}/v1/chat" "${hdrs[@]}" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'question': sys.argv[1]}))" "$Q")" \
  | python3 -m json.tool
