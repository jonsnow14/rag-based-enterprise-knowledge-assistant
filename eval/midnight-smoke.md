# Midnight smoke results

**Date:** 2026-08-09  
**App:** `azure-based-solutions` · phase P3+  
**Mode:** **azure** (Track A) after wiring `RAG-pipeline-prework` resources  
**Index:** Azure AI Search `northwind-chunks` + `data/local_index.jsonl` — **111 chunks** from **11** documents

## Commands

```bash
cd azure-based-solutions
source .venv/bin/activate
export PYTHONPATH=$(pwd)
python scripts/ingest.py --force-local   # if index missing
python scripts/smoke_query.py
bash scripts/demo_smoke.sh
```

## Case matrix

| # | Scenario | Expected | Result (2026-08-09) |
|---|----------|----------|---------------------|
| 1 | PTO 0–2 years (HR) | `answer` + LeavePolicy cite | **PASS** (extractive; §2.1 PTO chunk retrieved) |
| 2 | Min password length (IT) | `answer` + PasswordPolicy | **PASS** (requirements section in hits) |
| 3 | Professional price 2026 (Sales) | `answer` + Pricing2026 / Discounts | **PASS** |
| 4 | Alice salary / phone | `escalate` / `refuse` — no invent | **PASS** (`out_of_corpus`) |
| 5 | NDA question with HR-only ACL | no Legal leak; escalate or weak refuse | **PASS** (`below_min_score`, HR-only filters) |

**Automated:** `python scripts/smoke_query.py` → **5/5 passed**

## Metrics (lightweight)

| Metric | Value |
|--------|--------|
| Ingest files OK | 11/11 |
| Chunks | 111 |
| Chunks by dept | Finance 21 · HR 23 · IT 21 · Legal 27 · Sales 19 |
| Smoke pass rate | 5/5 |
| Generation engine (no keys) | extractive_fallback |
| p50 chat latency (local) | &lt; 1s per query |

## Known limitations (freeze)

1. Without `AZURE_OPENAI_*`, answers are **extractive passages**, not LLM synthesis.  
2. Without Azure AI Search, retrieval is **local JSONL** (hash embeddings + keyword blend).  
3. Content Safety / Entra not wired (optional post-midnight).  
4. Adaptive chunk hyperparameters not sweep-calibrated.  

## Next (post-midnight)

- Fill `.env` → re-ingest + natural language answers  
- ACA deploy  
- Full eval baseline vs adaptive  
