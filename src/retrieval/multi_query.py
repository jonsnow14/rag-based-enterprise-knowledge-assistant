"""Strategy A — multi-facet detect, parallel retrieve, quota merge, coverage."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config import Settings
from src.observability import get_logger
from src.retrieval.search import RetrievedChunk, RetrievalResult, retrieve

log = get_logger(__name__)

_COMPARE_RE = re.compile(
    r"\b(compare|vs\.?|versus|difference between|differ|both)\b",
    re.I,
)

# High-precision entity pairs in Northwind corpus
_ENTITY_PAIRS: List[Tuple[str, str]] = [
    ("Enterprise", "Standard"),
    ("Enterprise", "Professional"),
    ("Professional", "Standard"),
    ("Professional", "Enterprise"),
    ("meal", "hotel"),
    ("dinner", "hotel"),
    ("PTO", "sick"),
]


@dataclass
class FacetSpec:
    facet_id: str
    label: str
    sub_query: str
    confidence: float


@dataclass
class MultiQueryPlan:
    facets: List[FacetSpec]
    shared_topic: str
    confidence: float
    method: str


@dataclass
class CoverageFacet:
    facet_id: str
    sub_query: str
    accepted: List[str] = field(default_factory=list)
    selected: List[str] = field(default_factory=list)
    covered: bool = False
    top_score: float = 0.0


@dataclass
class MultiQueryResult:
    hits: List[RetrievedChunk]
    coverage: Dict[str, CoverageFacet]
    sub_queries: List[str]
    filters_applied: List[str]
    backend: str
    all_covered: bool
    any_covered: bool
    search_ms: int = 0


def detect_multi_facets(question: str, settings: Settings) -> Optional[MultiQueryPlan]:
    """Return multi plan only on high-precision signals (not bare 'and')."""
    q = (question or "").strip()
    if not q:
        return None

    # Require explicit compare/vs/difference/both — bare "and" must NOT trigger multi
    compare = bool(_COMPARE_RE.search(q))
    if not compare:
        return None

    found: List[Tuple[str, str]] = []
    for a, b in _ENTITY_PAIRS:
        if re.search(rf"\b{re.escape(a)}\b", q, re.I) and re.search(
            rf"\b{re.escape(b)}\b", q, re.I
        ):
            found.append((a, b))

    if not found:
        # compare without known entities — skip multi (avoid weak decompose)
        return None

    a, b = found[0]
    # Shared topic: strip compare words and entity names
    topic = q
    for w in ("compare", "versus", "vs", "difference between", "difference", "both"):
        topic = re.sub(rf"\b{w}\b", " ", topic, flags=re.I)
    topic = re.sub(rf"\b{re.escape(a)}\b", " ", topic, flags=re.I)
    topic = re.sub(rf"\b{re.escape(b)}\b", " ", topic, flags=re.I)
    topic = re.sub(r"\s+", " ", topic).strip(" ?.,")
    if not topic:
        topic = "policy"

    conf = 0.92
    if conf < settings.multi_facet_min_confidence:
        return None

    facets = [
        FacetSpec(
            facet_id=a.lower(),
            label=a,
            sub_query=f"{a} {topic}".strip()[:200],
            confidence=conf,
        ),
        FacetSpec(
            facet_id=b.lower(),
            label=b,
            sub_query=f"{b} {topic}".strip()[:200],
            confidence=conf,
        ),
    ]
    facets = facets[: max(2, min(settings.max_sub_queries, 3))]
    return MultiQueryPlan(
        facets=facets,
        shared_topic=topic,
        confidence=conf,
        method="template_pair",
    )


def _accept_hit(hit: RetrievedChunk, facet: FacetSpec, topic: str) -> bool:
    blob = f"{hit.chunk.section} {hit.chunk.content} {hit.chunk.filename}".lower()
    label = facet.label.lower()
    if label not in blob and facet.facet_id not in blob:
        # soft: allow if topic terms present and score ok — demote later
        return any(t in blob for t in topic.lower().split() if len(t) > 3)
    return True


def execute_multi_query(
    plan: MultiQueryPlan,
    *,
    departments: Sequence[str],
    include_historical: bool,
    settings: Settings,
    top_k_per: Optional[int] = None,
) -> MultiQueryResult:
    k = top_k_per or max(settings.rag_top_k, 5)
    min_per = 2
    max_per = 4
    total_budget = settings.rag_context_k

    t0 = time.perf_counter()
    results_by_facet: Dict[str, RetrievalResult] = {}
    filters: List[str] = []

    def _one(f: FacetSpec) -> Tuple[str, RetrievalResult]:
        r = retrieve(
            f.sub_query,
            departments=list(departments),
            include_historical=include_historical,
            top_k=k,
            settings=settings,
        )
        return f.facet_id, r

    with ThreadPoolExecutor(max_workers=len(plan.facets)) as ex:
        futs = [ex.submit(_one, f) for f in plan.facets]
        for fut in as_completed(futs):
            fid, res = fut.result()
            results_by_facet[fid] = res
            if res.filters_applied and not filters:
                filters = list(res.filters_applied)

    search_ms = int((time.perf_counter() - t0) * 1000)
    coverage: Dict[str, CoverageFacet] = {}
    selected: List[RetrievedChunk] = []
    seen_ids: set[str] = set()

    for f in plan.facets:
        res = results_by_facet.get(f.facet_id) or RetrievalResult()
        accepted: List[RetrievedChunk] = []
        for h in res.hits:
            if _accept_hit(h, f, plan.shared_topic):
                accepted.append(h)
        # if accept emptied, keep top 1 soft (avoid total miss)
        if not accepted and res.hits:
            accepted = res.hits[:1]

        cov = CoverageFacet(
            facet_id=f.facet_id,
            sub_query=f.sub_query,
            accepted=[h.chunk.id for h in accepted],
            covered=bool(accepted),
            top_score=accepted[0].score if accepted else 0.0,
        )
        # quota select within facet
        pick = accepted[:max_per]
        for h in pick[:min_per] if len(pick) >= min_per else pick:
            if h.chunk.id not in seen_ids and len(selected) < total_budget:
                selected.append(h)
                seen_ids.add(h.chunk.id)
                cov.selected.append(h.chunk.id)
        # fill remaining budget fairly
        for h in pick:
            if h.chunk.id not in seen_ids and len(selected) < total_budget:
                selected.append(h)
                seen_ids.add(h.chunk.id)
                cov.selected.append(h.chunk.id)
        coverage[f.facet_id] = cov

    # If budget left, add remaining high scores across facets
    pool: List[RetrievedChunk] = []
    for res in results_by_facet.values():
        pool.extend(res.hits)
    pool.sort(key=lambda h: h.score, reverse=True)
    for h in pool:
        if h.chunk.id not in seen_ids and len(selected) < total_budget:
            selected.append(h)
            seen_ids.add(h.chunk.id)

    backend = "azure"
    for res in results_by_facet.values():
        backend = res.backend
        break

    all_cov = all(c.covered for c in coverage.values()) if coverage else False
    any_cov = any(c.covered for c in coverage.values()) if coverage else False

    log.info(
        "multi_query facets=%s all_covered=%s selected=%s search_ms=%s",
        list(coverage.keys()),
        all_cov,
        len(selected),
        search_ms,
    )

    return MultiQueryResult(
        hits=selected,
        coverage=coverage,
        sub_queries=[f.sub_query for f in plan.facets],
        filters_applied=filters,
        backend=backend,
        all_covered=all_cov,
        any_covered=any_cov,
        search_ms=search_ms,
    )


def coverage_to_dict(cov: Dict[str, CoverageFacet]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in cov.items():
        out[k] = {
            "sub_query": v.sub_query,
            "accepted": v.accepted,
            "selected_for_prompt": v.selected,
            "covered": v.covered,
            "top_score": v.top_score,
        }
    return out
