#!/usr/bin/env bash
# Create / update Azure API Management fronting the RAG Container App (or App Service).
#
# Usage:
#   export RG=RAG-pipeline-prework
#   export LOC=eastus
#   export APIM_NAME=apim-northwind-rag   # globally unique
#   export BACKEND_URL=https://<aca-fqdn>  # no trailing slash
#   ./deploy/deploy-apim.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RG="${RG:-RAG-pipeline-prework}"
LOC="${LOC:-eastus}"
APIM_NAME="${APIM_NAME:-}"
BACKEND_URL="${BACKEND_URL:-}"
API_ID="${API_ID:-northwind-rag}"
API_PATH="${API_PATH:-rag}"
PRODUCT_ID="${PRODUCT_ID:-northwind-rag-demo}"

if [[ -z "$APIM_NAME" || -z "$BACKEND_URL" ]]; then
  echo "Required: APIM_NAME and BACKEND_URL (e.g. https://myapp.region.azurecontainerapps.io)"
  exit 1
fi

BACKEND_URL="${BACKEND_URL%/}"

echo "=== APIM $APIM_NAME (this can take 30–45 minutes on first create) ==="
if ! az apim show -g "$RG" -n "$APIM_NAME" >/dev/null 2>&1; then
  az apim create -g "$RG" -n "$APIM_NAME" -l "$LOC" \
    --publisher-email "${PUBLISHER_EMAIL:-admin@example.com}" \
    --publisher-name "${PUBLISHER_NAME:-Northwind RAG}" \
    --sku-name Developer
fi

echo "=== API $API_ID ==="
# Create empty API then import OpenAPI from backend
az apim api show -g "$RG" --service-name "$APIM_NAME" --api-id "$API_ID" >/dev/null 2>&1 || \
  az apim api create -g "$RG" --service-name "$APIM_NAME" \
    --api-id "$API_ID" \
    --path "$API_PATH" \
    --display-name "Northwind RAG Chat" \
    --protocols https \
    --service-url "$BACKEND_URL"

# Update backend URL
az apim api update -g "$RG" --service-name "$APIM_NAME" --api-id "$API_ID" \
  --service-url "$BACKEND_URL"

# Import OpenAPI (operations: /health, /chat, /v1/chat, …)
echo "=== Import OpenAPI from backend ==="
TMP_SPEC=$(mktemp /tmp/openapi-XXXXXX.json)
curl -fsS "${BACKEND_URL}/openapi.json" -o "$TMP_SPEC" || {
  echo "Could not fetch ${BACKEND_URL}/openapi.json — is the app public and healthy?"
  exit 1
}
az apim api import -g "$RG" --service-name "$APIM_NAME" \
  --path "$API_PATH" \
  --api-id "$API_ID" \
  --specification-format OpenApiJson \
  --specification-path "$TMP_SPEC" \
  --service-url "$BACKEND_URL" \
  || az apim api import -g "$RG" --service-name "$APIM_NAME" \
       --path "$API_PATH" \
       --api-id "$API_ID" \
       --specification-format OpenApi \
       --specification-path "$TMP_SPEC" \
       --service-url "$BACKEND_URL"
rm -f "$TMP_SPEC"

echo "=== Product + subscription ==="
az apim product show -g "$RG" --service-name "$APIM_NAME" --product-id "$PRODUCT_ID" >/dev/null 2>&1 || \
  az apim product create -g "$RG" --service-name "$APIM_NAME" \
    --product-id "$PRODUCT_ID" \
    --product-name "Northwind RAG Demo" \
    --subscription-required true \
    --state published \
    --approval-required false

az apim product api add -g "$RG" --service-name "$APIM_NAME" \
  --product-id "$PRODUCT_ID" --api-id "$API_ID" 2>/dev/null || true

# Policy
POLICY_FILE="$ROOT/deploy/apim-policy.xml"
if [[ -f "$POLICY_FILE" ]]; then
  echo "=== Apply API policy ==="
  az apim api policy create -g "$RG" --service-name "$APIM_NAME" \
    --api-id "$API_ID" \
    --policy-format xml \
    --value "@${POLICY_FILE}" 2>/dev/null || \
  az rest --method put \
    --url "https://management.azure.com/subscriptions/$(az account show --query id -o tsv)/resourceGroups/${RG}/providers/Microsoft.ApiManagement/service/${APIM_NAME}/apis/${API_ID}/policies/policy?api-version=2022-08-01" \
    --body "{\"properties\":{\"format\":\"xml\",\"value\":$(python3 -c "import json,pathlib; print(json.dumps(pathlib.Path('$POLICY_FILE').read_text()))")}}" \
    || echo "WARN: policy apply failed — set manually in Portal from deploy/apim-policy.xml"
fi

# Demo subscription (optional — skip with SKIP_SUBSCRIPTION=1)
SUB_NAME="${SUB_NAME:-partner-demo}"
if [[ "${SKIP_SUBSCRIPTION:-0}" != "1" ]]; then
  echo "=== Subscription $SUB_NAME ==="
  if ! az apim subscription show -g "$RG" --service-name "$APIM_NAME" --sid "$SUB_NAME" >/dev/null 2>&1; then
    az apim subscription create -g "$RG" --service-name "$APIM_NAME" \
      --sid "$SUB_NAME" \
      --name "Partner demo" \
      --scope "/products/${PRODUCT_ID}" \
      --state active >/dev/null || \
    az rest --method put \
      --url "https://management.azure.com/subscriptions/$(az account show --query id -o tsv)/resourceGroups/${RG}/providers/Microsoft.ApiManagement/service/${APIM_NAME}/subscriptions/${SUB_NAME}?api-version=2022-08-01" \
      --body "{\"properties\":{\"displayName\":\"Partner demo\",\"scope\":\"/products/${PRODUCT_ID}\",\"state\":\"active\"}}" \
      >/dev/null || echo "WARN: create subscription via Portal if CLI failed"
  fi
  # Primary key (sensitive — print once for operator)
  PRIMARY=$(az apim subscription show -g "$RG" --service-name "$APIM_NAME" --sid "$SUB_NAME" \
    --query properties.primaryKey -o tsv 2>/dev/null || \
    az rest --method post \
      --url "https://management.azure.com/subscriptions/$(az account show --query id -o tsv)/resourceGroups/${RG}/providers/Microsoft.ApiManagement/service/${APIM_NAME}/subscriptions/${SUB_NAME}/listSecrets?api-version=2022-08-01" \
      --query primaryKey -o tsv 2>/dev/null || echo "")
fi

GATEWAY=$(az apim show -g "$RG" -n "$APIM_NAME" --query gatewayUrl -o tsv)
echo ""
echo "=== APIM ready ==="
echo "Gateway:     $GATEWAY"
echo "API path:    ${GATEWAY}/${API_PATH}"
echo "Chat URL:    ${GATEWAY}/${API_PATH}/v1/chat"
echo "Health URL:  ${GATEWAY}/${API_PATH}/health"
if [[ -n "${PRIMARY:-}" ]]; then
  echo "Sub key:     ${PRIMARY}   (subscription: ${SUB_NAME} — store securely, do not commit)"
fi
echo ""
echo "Call with header: Ocp-Apim-Subscription-Key: <key>"
echo ""
echo "Example:"
echo "  export KEY='${PRIMARY:-<subscription-key>}'"
echo "  curl -s '${GATEWAY}/${API_PATH}/v1/chat' \\"
echo "    -H \"Ocp-Apim-Subscription-Key: \$KEY\" \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"question\":\"What is the 401(k) match?\"}'"
