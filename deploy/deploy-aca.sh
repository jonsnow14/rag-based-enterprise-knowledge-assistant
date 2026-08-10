#!/usr/bin/env bash
# Deploy Northwind RAG API to Azure Container Apps.
# Prerequisites: az login, Docker (or az acr build), existing OpenAI + Search.
#
# Usage:
#   export RG=RAG-pipeline-prework
#   export LOC=eastus
#   export ACR_NAME=acrnorthwindrag$RANDOM   # globally unique, lowercase alphanumeric
#   export ACA_ENV=env-northwind-rag
#   export ACA_APP=northwind-rag-api
#   ./deploy/deploy-aca.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RG="${RG:-RAG-pipeline-prework}"
LOC="${LOC:-eastus}"
ACR_NAME="${ACR_NAME:-}"
ACA_ENV="${ACA_ENV:-env-northwind-rag}"
ACA_APP="${ACA_APP:-northwind-rag-api}"
IMAGE_NAME="${IMAGE_NAME:-northwind-rag-api}"
IMAGE_TAG="${IMAGE_TAG:-v1}"

if [[ -z "$ACR_NAME" ]]; then
  echo "Set ACR_NAME to a unique Azure Container Registry name (lowercase alphanumeric)."
  exit 1
fi

echo "=== Resource group $RG ($LOC) ==="
az group show -n "$RG" >/dev/null 2>&1 || az group create -n "$RG" -l "$LOC"

echo "=== ACR $ACR_NAME ==="
az acr show -n "$ACR_NAME" -g "$RG" >/dev/null 2>&1 || \
  az acr create -n "$ACR_NAME" -g "$RG" -l "$LOC" --sku Basic

echo "=== Build & push image ==="
az acr build -r "$ACR_NAME" -t "${IMAGE_NAME}:${IMAGE_TAG}" -f Dockerfile .

LOGIN_SERVER=$(az acr show -n "$ACR_NAME" -g "$RG" --query loginServer -o tsv)
IMAGE="${LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=== Log Analytics + Container Apps env ==="
WORKSPACE="${ACA_ENV}-logs"
az monitor log-analytics workspace show -g "$RG" -n "$WORKSPACE" >/dev/null 2>&1 || \
  az monitor log-analytics workspace create -g "$RG" -n "$WORKSPACE" -l "$LOC"
LOG_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$WORKSPACE" --query customerId -o tsv)
LOG_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$WORKSPACE" --query primarySharedKey -o tsv)

az containerapp env show -g "$RG" -n "$ACA_ENV" >/dev/null 2>&1 || \
  az containerapp env create -g "$RG" -n "$ACA_ENV" -l "$LOC" \
    --logs-workspace-id "$LOG_ID" --logs-workspace-key "$LOG_KEY"

echo "=== Ensure ACR admin for pull (demo) ==="
az acr update -n "$ACR_NAME" --admin-enabled true >/dev/null
ACR_USER=$(az acr credential show -n "$ACR_NAME" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR_NAME" --query passwords[0].value -o tsv)

# Load secrets from local .env if present (do not echo values)
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
REQUIRE_API_KEY="${REQUIRE_API_KEY:-false}"

ENV_VARS=(
  APP_MODE=azure
  APP_ENV=prod
  LOG_LEVEL=WARNING
  AZURE_LOG_LEVEL=WARNING
  PYTHONPATH=/app
  AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT"
  AZURE_OPENAI_API_KEY=secretref:azure-openai-key
  AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION"
  AZURE_OPENAI_CHAT_DEPLOYMENT="$AZURE_OPENAI_CHAT_DEPLOYMENT"
  AZURE_OPENAI_EMBED_DEPLOYMENT="$AZURE_OPENAI_EMBED_DEPLOYMENT"
  AZURE_SEARCH_ENDPOINT="$AZURE_SEARCH_ENDPOINT"
  AZURE_SEARCH_API_KEY=secretref:azure-search-key
  AZURE_SEARCH_INDEX="$AZURE_SEARCH_INDEX"
  API_KEYS=secretref:api-keys
  REQUIRE_API_KEY="$REQUIRE_API_KEY"
  RAG_MIN_SCORE_AZURE=0.015
)

echo "=== Deploy / update Container App $ACA_APP ==="
if az containerapp show -g "$RG" -n "$ACA_APP" >/dev/null 2>&1; then
  # Refresh secrets then image + env (secretref values must exist)
  az containerapp secret set -g "$RG" -n "$ACA_APP" \
    --secrets \
      azure-openai-key="$AZURE_OPENAI_API_KEY" \
      azure-search-key="$AZURE_SEARCH_API_KEY" \
      api-keys="${API_KEYS:-placeholder-set-me}" >/dev/null
  az containerapp registry set -g "$RG" -n "$ACA_APP" \
    --server "$LOGIN_SERVER" \
    --username "$ACR_USER" \
    --password "$ACR_PASS" >/dev/null 2>&1 || true
  az containerapp update -g "$RG" -n "$ACA_APP" \
    --image "$IMAGE" \
    --set-env-vars "${ENV_VARS[@]}"
else
  az containerapp create -g "$RG" -n "$ACA_APP" \
    --environment "$ACA_ENV" \
    --image "$IMAGE" \
    --registry-server "$LOGIN_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    --target-port 8000 \
    --ingress external \
    --min-replicas 1 \
    --max-replicas 3 \
    --cpu 0.5 --memory 1.0Gi \
    --secrets \
      azure-openai-key="$AZURE_OPENAI_API_KEY" \
      azure-search-key="$AZURE_SEARCH_API_KEY" \
      api-keys="${API_KEYS:-placeholder-set-me}" \
    --env-vars "${ENV_VARS[@]}"
fi

FQDN=$(az containerapp show -g "$RG" -n "$ACA_APP" --query properties.configuration.ingress.fqdn -o tsv)
echo ""
echo "=== Deployed ==="
echo "Health:  https://${FQDN}/health"
echo "OpenAPI: https://${FQDN}/openapi.json"
echo "Chat:    https://${FQDN}/v1/chat"
echo "Next:    BACKEND_URL=https://${FQDN} ./deploy/deploy-apim.sh"
echo "Docs:    deploy/DEPLOY-AZURE-SHARE.md"
