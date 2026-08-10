# Evaluation dataset

Small, **corpus-grounded** evaluation set for the Azure productized RAG app, derived from `rag-documents/` (Northwind Traders mock policies).

## Location

```text
data-set/
├── README.md                       # this file
├── eval_dataset.jsonl              # primary machine-readable golden set  [in repo]
├── eval_dataset.csv                # spreadsheet view of questions       [in repo]
├── eval_dataset_with_results.csv   # app answers + Foundry scores        [in repo]
├── foundry_eval_summary.json       # aggregate Foundry metrics           [in repo]
├── foundry_eval_rows.jsonl         # per-row Foundry scores + context    [in repo]
├── eval_results.jsonl              # raw app payloads (local re-run)     [gitignored]
└── foundry_sdk_*.json              # raw azure-ai-evaluation SDK dump    [gitignored]
```

**Shared in the public repo:** golden set + the three report files above  
(`eval_dataset_with_results.csv`, `foundry_eval_summary.json`, `foundry_eval_rows.jsonl`).  
Re-run `scripts/run_eval_dataset.py` / Foundry eval to refresh; intermediate SDK dumps stay local.

## Knowledge base sources

| Department | Documents |
|------------|-----------|
| HR | `LeavePolicy.pdf`, `Benefits.pdf` |
| Finance | `ExpensePolicy.pdf`, `TravelPolicy.docx` |
| IT | `PasswordPolicy.docx`, `VPNGuide.pdf` |
| Legal | `NDA.docx`, `VendorContract.pdf` |
| Sales | `Pricing2025.pdf`, `Pricing2026.pdf`, `Discounts.xlsx` |

Path root: `rag-documents/<Dept>-*/<Dept>/...`

## Schema

### Required eval columns (assignment-style)

| Field | Description |
|-------|-------------|
| `question` | User question (or follow-up utterance) |
| `expected_answer` | Gold answer / expected behavior summary |
| `expected_document` | Filename(s); `;`-separated if multi-doc; empty if none |
| `expected_section` | Section / sheet / heading to ground the answer |
| `difficulty` | `easy` \| `medium` \| `hard` |

### Additional fields (runner-friendly)

| Field | Description |
|-------|-------------|
| `id` | Stable case id (e.g. `S-HR-001`, `F-SALES-002c`) |
| `category` | `straightforward` \| `multi_document` \| `ambiguous` \| `no_answer` \| `follow_up` \| `version_conflict` |
| `expected_status` | Allowed API statuses: `answer`, `partial`, `clarify`, `escalate`, `refuse` |
| `departments` | ACL / filter hint for the query |
| `history` | Prior turns for follow-ups (`[{role, content}, ...]`) |
| `conversation_id` / `turn` | Link multi-turn cases |
| `notes` | Optional judge hints |

## Coverage summary

| Category | Intent | Approx. count |
|----------|--------|----------------|
| **straightforward** | Single-doc, clear fact lookup | ~14 |
| **multi_document** | Needs 2+ docs or sections | ~6 |
| **ambiguous** | Underspecified; clarify or carefully scoped answer | ~4 |
| **no_answer** | Missing / personal / off-corpus | ~4 |
| **follow_up** | Multi-turn entity switch / refinement | ~9 (3 conversations) |
| **version_conflict** | 2025 vs 2026 pricing currency | ~2 |

**Total:** ~39 cases (see `eval_dataset.jsonl` for exact count).

## Difficulty guide

| Level | Meaning |
|-------|---------|
| `easy` | Direct fact; strong lexical match to one section |
| `medium` | Table lookup, multi-clause fact, or mild ambiguity |
| `hard` | Multi-doc, version conflict, or conversational rewrite required |

## Category examples

### Straightforward

- PTO accrual for 0–2 years → `LeavePolicy.pdf` §2.1 → **15 days**
- Min password length → `PasswordPolicy.docx` → **12 characters**
- 2026 Professional price → `Pricing2026.pdf` → **$65/seat/month**

### Multi-document

- Compare 2025 vs 2026 Professional pricing → both rate cards  
- Receipt threshold + ExpensePath for travel → `ExpensePolicy` + `TravelPolicy`  
- Volume + term discount stacking → `Pricing2026` + `Discounts.xlsx`

### Ambiguous

- “What is the policy?” → **clarify**  
- “What is the meal limit?” → expense limits vs travel per diem  

### No answer

- Alice’s salary / phone → **escalate/refuse**  
- World Cup winner → **escalate/refuse**  
- Dual-monitor equipment budget → not in corpus (do not invent)

### Follow-up

Conversation `conv-hr-pto`:

1. PTO for 3–5 years?  
2. What about 11+ years?  
3. Any exception for part-time?

Retrieval must use a **rewritten standalone query** each turn (see root `STRATEGY-AGAINST-COMMON-FAIL-POINTS.md` §4).

### Version conflict

- “Current Professional list price?” → **$65** from `Pricing2026.pdf`, not **$59** from 2025  

## How to use

### Manual review

Open `eval_dataset.csv` in a spreadsheet, or:

```bash
head -n 3 azure-based-solutions/data-set/eval_dataset.jsonl | python -m json.tool
```

### Run questions against the RAG app and fill `response` + `citation`

Workflow:

```text
eval_dataset.jsonl
       │
       │  for each case (in order)
       ▼
  POST /chat  { question, departments, history? }
  or in-process run_chat(...)
       │
       ▼
  extract answer → column "response"
  extract citations → column "citation"
       │
       ▼
  data-set/eval_dataset_with_results.csv
  data-set/eval_results.jsonl   (full raw JSON per case)
```

#### Option A — In-process (no HTTP server)

From `azure-based-solutions/` (uses `.env` + local index / Azure like smoke tests):

```bash
cd azure-based-solutions
# ensure index exists first if needed:
#   python scripts/ingest.py
python scripts/run_eval_dataset.py
```

#### Option B — HTTP against a running API

```bash
# terminal 1
cd azure-based-solutions
./scripts/run_api.sh

# terminal 2
cd azure-based-solutions
python scripts/run_eval_dataset.py --mode http --base-url http://127.0.0.1:8000

# if API keys are enabled:
export API_KEY=your-key
python scripts/run_eval_dataset.py --mode http --base-url http://127.0.0.1:8000 --api-key "$API_KEY"
```

#### Useful flags

```bash
python scripts/run_eval_dataset.py --limit 5              # smoke first 5 cases
python scripts/run_eval_dataset.py --start-from S-HR-001  # resume from id
python scripts/run_eval_dataset.py --sleep 0.5            # slow down for rate limits
python scripts/run_eval_dataset.py \
  --input data-set/eval_dataset.jsonl \
  --output data-set/eval_dataset_with_results.csv
```

#### Output columns added

| Column | Source |
|--------|--------|
| `status` | `ChatResponse.status` (`answer`, `escalate`, `clarify`, …) |
| **`response`** | `answer` text, or `[status] message` if no answer |
| **`citation`** | Joined citations: `filename \| section \| chunk_id \| kb=…` |
| `query_id` | Trace id from the app |
| `latency_ms` | Client-side wall time per case |
| `hit_count` / `top_score` | Retrieval diagnostics |
| `path` | Control-system path (`single` / `multi` / `single_fallback`) |
| `retrieval_query` | Query used for search (after rewrite if any) |
| `status_match` | 1 if `status` ∈ `expected_status` (code / Foundry) |
| `citation_ok` | 1 if answer/partial has ≥1 citation |
| `gold_token_recall` | Lexical overlap proxy vs `expected_answer` |
| `groundedness` / `groundedness_passed` | Azure AI Foundry groundedness (1–5) |
| `relevance` / `relevance_passed` | Azure AI Foundry answer relevance (1–5) |
| `foundry_scored` | `foundry` if SDK scored; `code` if code-only |

#### Foundry reports (same folder)

| File | Contents |
|------|----------|
| `foundry_eval_summary.json` | Aggregate metrics + timestamp |
| `foundry_eval_rows.jsonl` | Per-case scores + context pack |
| `foundry_sdk_*.json` | Raw `azure.ai.evaluation.evaluate` output |

```bash
# Full pipeline: app answers → CSV → Foundry judges
python scripts/run_eval_dataset.py

# Skip LLM judges (code metrics only)
python scripts/run_eval_dataset.py --no-foundry
```

Follow-up cases send `history` from the JSONL so Strategy C rewrite can resolve “What about Standard?”.

#### One-shot curl (single question)

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is the minimum password length?",
    "departments": ["IT"]
  }' | python -m json.tool
```

Response shape (fields used for the CSV):

```json
{
  "status": "answer",
  "answer": "Minimum password length is 12 characters...",
  "citations": [
    {
      "chunk_id": "...",
      "filename": "PasswordPolicy.docx",
      "section": "2. Password Requirements",
      "knowledge_base_id": "IT"
    }
  ]
}
```

### Automated assert pattern (optional)

```python
import json
from pathlib import Path

for line in Path("data-set/eval_dataset.jsonl").read_text().splitlines():
    case = json.loads(line)
    # req = ChatRequest(question=case["question"], departments=case["departments"], history=case.get("history") or [])
    # resp = run_chat(req)
    # assert resp.status in case["expected_status"]
    # optionally: check expected_document appears in citations / retrieval
```

### Judging tips

| Check | Pass if |
|-------|---------|
| Status | `resp.status` ∈ `expected_status` |
| Document | Citation or retrieved filename intersects `expected_document` (when not null) |
| Factual | Key numbers/strings from `expected_answer` present (normalize $ and en-dashes) |
| No-answer | No fabricated personal data or off-corpus sports/trivia presented as KB fact |
| Follow-up | Turn N does not cite only the previous entity’s exclusive facts when entity switched |
| Version | “Current” pricing questions cite 2026, not unlabeled 2025 |

## Alignment with fail-point strategies

| Dataset category | Strategy doc section |
|------------------|----------------------|
| `multi_document` | Scenario A — multi-query + coverage |
| `version_conflict` | Scenario B — `is_current` / effective dates + offer previous |
| `follow_up` | Scenario C — rewrite + slots |
| `no_answer` / weak evidence | Evidence gate / escalate |
| `ambiguous` | Clarify status |

See: `STRATEGY-AGAINST-COMMON-FAIL-POINTS.md` (repo root).

## Extending the set

1. Extract facts only from `rag-documents/` (do not invent policy numbers).  
2. Add a stable `id` and one of the `category` values.  
3. Prefer section headings that appear in the source (e.g. `2.1 Annual / Paid Time Off (PTO)`).  
4. For multi-turn, share `conversation_id` and increment `turn`; fill `history`.  
5. Keep `expected_answer` short and checkable (numbers + short clause).

## Provenance

- Content extracted from project `rag-documents/` mock Northwind policies (effective dates primarily **2026**).  
- Pricing intentionally includes **2025** and **2026** rate cards for version tests.  
- NDA / vendor templates are mock internal samples, not live legal advice.
