# Share the Northwind RAG API the Azure way

**Target pattern**

```text
Partners → Azure API Management (subscription keys, rate limits)
              → Azure Container Apps (FastAPI image)
                    → Azure OpenAI + Azure AI Search  (private to the app)
```

**Faster alternative:** App Service + `X-API-Key` on the app, then add APIM later.

---

## Prerequisites

- [ ] Azure CLI logged in: `az login`
- [ ] Docker **or** `az acr build` (cloud build, no local Docker required)
- [ ] Working local Azure mode (`python scripts/smoke_azure.py`, ingest done)
- [ ] Resource group (e.g. `RAG-pipeline-prework`) with OpenAI + Search

---

## Step 1 — API key on the app (defense in depth)

In `.env` (and later in ACA secrets):

```bash
API_KEYS=partner-demo-key-change-me
REQUIRE_API_KEY=true
```

Local test:

```bash
export PYTHONPATH=$(pwd)
# restart uvicorn after changing .env
curl -s http://127.0.0.1:8000/health   # public
curl -s http://127.0.0.1:8000/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: partner-demo-key-change-me" \
  -d '{"question":"What is the 401(k) match?"}'
```

Without a valid key, `/chat` and `/v1/chat` return **401** when auth is enabled.  
`/health` stays public for probes.

---

## Step 2 — Container image

From **`azure-based-solutions/`**:

```bash
# Local Docker
docker build -t northwind-rag-api:v1 .
docker run --rm -p 8000:8000 --env-file .env northwind-rag-api:v1
```

Or cloud build via the deploy script (ACR).

---

## Step 3 — Deploy Container Apps

```bash
cd azure-based-solutions
export RG=RAG-pipeline-prework
export LOC=eastus
export ACR_NAME=acrnwrag$RANDOM   # must be globally unique
# Load OpenAI/Search from .env automatically if present
chmod +x deploy/deploy-aca.sh
./deploy/deploy-aca.sh
```

Script will print:

```text
Health:  https://<fqdn>/health
Chat:    https://<fqdn>/v1/chat
```

**Ingest note:** Indexing still uses `scripts/ingest.py` from a machine with corpus access (or a one-off Job). The container serves **query** traffic against Azure AI Search; it does not need `rag-documents` mounted if the index is already populated.

---

## Step 4 — Azure API Management

```bash
export RG=RAG-pipeline-prework
export LOC=eastus
export APIM_NAME=apim-northwind-rag   # globally unique
export BACKEND_URL=https://<aca-fqdn>  # from step 3, no trailing slash
chmod +x deploy/deploy-apim.sh
./deploy/deploy-apim.sh
```

First APIM create can take **30–45 minutes**.

The deploy script creates a **partner-demo** subscription when possible and prints the primary key.  
If CLI creation fails, use **Azure Portal → API Management → Subscriptions → + Add** on product **Northwind RAG Demo**.

### Partner call

```bash
export APIM=https://<apim-name>.azure-api.net/rag
export KEY=<subscription-key>

curl -s "${APIM}/health"

curl -s "${APIM}/v1/chat" \
  -H "Ocp-Apim-Subscription-Key: ${KEY}" \
  -H "Content-Type: application/json" \
  -d '{"question":"How many PTO days for 0-2 years of service?"}'

# Or helper:
# export BASE=$APIM KEY=$KEY
# ./scripts/partner_curl_examples.sh
```

Policy file: [`apim-policy.xml`](apim-policy.xml) (rate limit 60/min, quota 2000/day per subscription).

---

## Step 5 — What to give partners

| Item | Value |
|------|--------|
| Base URL | `https://<apim>.azure-api.net/rag` |
| Chat | `POST /v1/chat` |
| Auth header | `Ocp-Apim-Subscription-Key` |
| Body | `{"question":"..."}` optional `departments` |
| OpenAPI | Import from backend or APIM portal |

**Do not share:** Azure OpenAI keys, Search admin keys, resource group access.

---

## Architecture (shared)

```text
Partner  --subscription key-->  APIM  --HTTPS-->  Container Apps (FastAPI)
                                                    |-- Managed secrets
                                                    |-> Azure OpenAI
                                                    |-> Azure AI Search
```

| Surface | Azure service |
|---------|----------------|
| Share / keys / throttle | **API Management** |
| Run API | **Container Apps** (or App Service) |
| Images | **Container Registry** |
| Secrets | ACA secrets / **Key Vault** (next harden) |
| Backend AI | **OpenAI** + **AI Search** (already in use) |

---

## Faster path (skip APIM initially)

1. Deploy only ACA or App Service.  
2. Set `REQUIRE_API_KEY=true` and `API_KEYS=...`.  
3. Share `https://<fqdn>/v1/chat` + `X-API-Key`.  
4. Add APIM when you need a portal, multiple partners, or quotas.

---

## App Service zip deploy (no Docker / no ACR Tasks)

Use when **ACR Tasks** are blocked (common on Free Trial) or Docker is unavailable:

```bash
cd azure-based-solutions
export RG=RAG-pipeline-prework
export LOC=centralus          # Free Trial: try centralus if eastus quota is 0
export APP_NAME=northwind-rag-azure   # globally unique DNS name
export PLAN_NAME=plan-northwind-rag
export SKU=F1                 # Free; use B1 when paid quota allows
./deploy/deploy-appservice.sh
```

| Item | Notes |
|------|--------|
| Build | Oryx installs `requirements.txt` on deploy |
| Auth | App `X-API-Key` from `.env` `API_KEYS` |
| Partner URL | `https://<app>.azurewebsites.net/v1/chat` |

**Live example (this subscription):** `https://northwind-rag-azure.azurewebsites.net`

Point APIM `BACKEND_URL` at the Web App URL when APIM finishes activating.

---

## Checklist

- [ ] Local smoke 5/5 with Azure  
- [ ] `API_KEYS` set for non-local  
- [ ] Docker image builds  
- [ ] ACA (or App Service) healthy `/health`  
- [ ] Index already ingested to AI Search  
- [ ] APIM product + subscription  
- [ ] Partner curl succeeds with subscription key  
- [ ] OpenAI/Search keys never given to partners  

---

## Files in this folder

| File | Purpose |
|------|---------|
| `../Dockerfile` | Production image |
| `deploy-aca.sh` | Build/push ACR + Container App |
| `deploy-apim.sh` | APIM API + product + policy hook |
| `apim-policy.xml` | Rate limit / quota policy |
| `DEPLOY-AZURE-SHARE.md` | This guide |
