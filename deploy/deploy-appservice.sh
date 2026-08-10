#!/usr/bin/env bash
# Deploy Northwind RAG API to Azure App Service (Python zip deploy).
# Use when ACR Tasks / local Docker are unavailable (common on credit subs).
#
# Usage:
#   export RG=RAG-pipeline-prework
#   export LOC=eastus
#   export APP_NAME=app-northwind-rag-api   # globally unique DNS
#   ./deploy/deploy-appservice.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RG="${RG:-RAG-pipeline-prework}"
# Free Trial often has App Service quota only in some regions (centralus worked; eastus did not).
LOC="${LOC:-centralus}"
APP_NAME="${APP_NAME:-}"
PLAN_NAME="${PLAN_NAME:-plan-northwind-rag}"
SKU="${SKU:-F1}"   # F1 free tier on Free Trial; B1 when quota allows

if [[ -z "$APP_NAME" ]]; then
  echo "Set APP_NAME to a unique Web App name (letters/digits/hyphens)."
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${AZURE_OPENAI_ENDPOINT:?Set AZURE_OPENAI_ENDPOINT in env or .env}"
: "${AZURE_OPENAI_API_KEY:?Set AZURE_OPENAI_API_KEY}"
: "${AZURE_SEARCH_ENDPOINT:?Set AZURE_SEARCH_ENDPOINT}"
: "${AZURE_SEARCH_API_KEY:?Set AZURE_SEARCH_API_KEY}"
AZURE_OPENAI_CHAT_DEPLOYMENT="${AZURE_OPENAI_CHAT_DEPLOYMENT:-gpt-4.1-mini}"
AZURE_OPENAI_EMBED_DEPLOYMENT="${AZURE_OPENAI_EMBED_DEPLOYMENT:-text-embedding-3-small}"
AZURE_SEARCH_INDEX="${AZURE_SEARCH_INDEX:-northwind-chunks}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-10-21}"
API_KEYS="${API_KEYS:-}"
REQUIRE_API_KEY="${REQUIRE_API_KEY:-true}"

echo "=== Ensure Microsoft.Web registered ==="
az provider register -n Microsoft.Web --wait 2>/dev/null || az provider register -n Microsoft.Web
# brief poll
for i in $(seq 1 40); do
  st=$(az provider show -n Microsoft.Web --query registrationState -o tsv)
  [[ "$st" == "Registered" ]] && break
  echo "Microsoft.Web=$st ..."
  sleep 10
done

echo "=== Resource group $RG ==="
az group show -n "$RG" >/dev/null 2>&1 || az group create -n "$RG" -l "$LOC"

echo "=== App Service plan $PLAN_NAME ($SKU, $LOC) ==="
az appservice plan show -g "$RG" -n "$PLAN_NAME" >/dev/null 2>&1 || \
  az appservice plan create -g "$RG" -n "$PLAN_NAME" -l "$LOC" --is-linux --sku "$SKU"

echo "=== Web App $APP_NAME ==="
az webapp show -g "$RG" -n "$APP_NAME" >/dev/null 2>&1 || \
  az webapp create -g "$RG" -n "$APP_NAME" -p "$PLAN_NAME" --runtime "PYTHON:3.12"

echo "=== App settings (secrets) ==="
az webapp config appsettings set -g "$RG" -n "$APP_NAME" --settings \
  APP_MODE=azure \
  APP_ENV=prod \
  LOG_LEVEL=WARNING \
  AZURE_LOG_LEVEL=WARNING \
  PYTHONPATH=/home/site/wwwroot \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
  AZURE_OPENAI_API_KEY="$AZURE_OPENAI_API_KEY" \
  AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
  AZURE_OPENAI_CHAT_DEPLOYMENT="$AZURE_OPENAI_CHAT_DEPLOYMENT" \
  AZURE_OPENAI_EMBED_DEPLOYMENT="$AZURE_OPENAI_EMBED_DEPLOYMENT" \
  AZURE_SEARCH_ENDPOINT="$AZURE_SEARCH_ENDPOINT" \
  AZURE_SEARCH_API_KEY="$AZURE_SEARCH_API_KEY" \
  AZURE_SEARCH_INDEX="$AZURE_SEARCH_INDEX" \
  API_KEYS="${API_KEYS:-placeholder-set-me}" \
  REQUIRE_API_KEY="$REQUIRE_API_KEY" \
  RAG_MIN_SCORE_AZURE=0.015 \
  WEBSITES_PORT=8000 \
  >/dev/null

# Startup: App Service sets PORT; uvicorn binds to it
STARTUP='bash -c "python -m uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --workers 1"'
az webapp config set -g "$RG" -n "$APP_NAME" --startup-file "$STARTUP" >/dev/null

echo "=== Zip package (source only) ==="
ZIP=/tmp/northwind-rag-api-deploy.zip
rm -f "$ZIP"
# Exclude venv, data, secrets, git, caches
(
  cd "$ROOT"
  zip -qr "$ZIP" \
    src requirements.txt pyproject.toml scripts \
    -x '*/__pycache__/*' '*.pyc' 'src/**/__pycache__/*'
)
ls -lh "$ZIP"

echo "=== Deploy zip ==="
az webapp deploy -g "$RG" -n "$APP_NAME" --src-path "$ZIP" --type zip --async false

echo "=== Restart ==="
az webapp restart -g "$RG" -n "$APP_NAME"

HOST=$(az webapp show -g "$RG" -n "$APP_NAME" --query defaultHostName -o tsv)
echo ""
echo "=== Deployed (App Service) ==="
echo "Health:  https://${HOST}/health"
echo "OpenAPI: https://${HOST}/openapi.json"
echo "Chat:    https://${HOST}/v1/chat"
echo "Auth:    X-API-Key (from .env API_KEYS)"
echo "Next:    BACKEND_URL=https://${HOST} ./deploy/deploy-apim.sh"
echo "$HOST" > /tmp/northwind-backend-host.txt
