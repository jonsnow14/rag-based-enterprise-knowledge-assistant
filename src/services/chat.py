"""Chat orchestration with control system: classic single path + conditional A/B/C."""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from src.config import Settings, get_settings
from src.conversation.rewrite import rewrite_query
from src.generation.answer import generate_answer
from src.generation.pack import pack_context_blocks
from src.guardrails.ambiguity import detect_ambiguity
from src.guardrails.citations import ensure_citations
from src.guardrails.evidence import escalate_message, evaluate_evidence
from src.models.schemas import (
    ChatRequest,
    ChatResponse,
    Diagnostics,
    LatencyMs,
    QueryStatus,
    RetrievalInfo,
    VersionUsed,
)
from src.observability import get_logger
from src.retrieval.multi_query import (
    coverage_to_dict,
    detect_multi_facets,
    execute_multi_query,
)
from src.retrieval.rerank import rerank_hits
from src.retrieval.search import RetrievedChunk, RetrievalResult, normalize_departments, retrieve
from src.retrieval.temporal import resolve_temporal_intent

log = get_logger(__name__)


def _version_used_from_hits(hits: List[RetrievedChunk]) -> Optional[VersionUsed]:
    if not hits:
        return None
    c = hits[0].chunk
    return VersionUsed(
        filename=c.filename,
        doc_id=c.doc_id,
        version_label=c.version,
        is_current=c.is_current,
        effective_date=c.effective_date,
    )


def _should_return_diagnostics(req: ChatRequest, s: Settings) -> bool:
    if req.include_diagnostics:
        return True
    return bool(s.include_diagnostics_default) or (s.app_env or "").lower() != "prod"


def single_path(
    question: str,
    *,
    departments: List[str],
    include_historical: bool,
    settings: Settings,
    top_k: Optional[int] = None,
) -> RetrievalResult:
    """Classic path: hybrid retrieve (+ optional lexical rerank)."""
    k_fetch = top_k or max(settings.rag_retrieve_k, settings.rag_top_k)
    result = retrieve(
        question,
        departments=departments,
        include_historical=include_historical,
        top_k=k_fetch,
        settings=settings,
    )
    hits = rerank_hits(question, result.hits, settings, context_k=settings.rag_context_k)
    return RetrievalResult(
        hits=hits,
        filters_applied=result.filters_applied,
        backend=result.backend,
    )


def run_chat(req: ChatRequest, settings: Optional[Settings] = None) -> ChatResponse:
    s = settings or get_settings()
    query_id = str(uuid.uuid4())
    t_start = time.perf_counter()
    latency = LatencyMs()

    diag = Diagnostics(
        query_id=query_id,
        raw_question=req.question,
        retrieval_query=req.question,
        flags={
            "enhanced": s.enhanced_enabled(),
            "rewrite": s.enable_query_rewrite,
            "multi_query": s.enable_multi_query,
            "temporal": s.enable_temporal_intent,
            "version_offer": s.enable_version_offer,
            "force_single": s.force_single_path,
            "rerank_mode": s.rerank_mode,
        },
    )

    # ACL
    if req.departments is not None and len(req.departments) == 0:
        return ChatResponse(
            query_id=query_id,
            status=QueryStatus.ACCESS_DENIED,
            message="No departments authorized for this caller.",
            effective_kb=[],
            diagnostics=diag if _should_return_diagnostics(req, s) else None,
        )

    effective = normalize_departments(
        req.departments if req.departments is not None else s.allowed_departments_default()
    )
    if not effective:
        return ChatResponse(
            query_id=query_id,
            status=QueryStatus.ACCESS_DENIED,
            message="No valid departments in request.",
            effective_kb=[],
            diagnostics=diag if _should_return_diagnostics(req, s) else None,
        )

    rag_mode = (req.rag_mode or "auto").strip().lower()
    force_classic = (
        s.force_single_path
        or not s.rag_enhanced_pipeline
        or rag_mode == "single"
    )

    question = req.question.strip()
    include_historical = bool(req.include_historical)
    path = "single"
    fallback_reason: Optional[str] = None
    coverage_dict = None
    sub_queries: List[str] = []
    multi_partial = False
    partial_note: Optional[str] = None
    context_override: Optional[str] = None

    # --- C: rewrite (conditional) ---
    if (
        not force_classic
        and s.enable_query_rewrite
        and req.history
        and rag_mode != "single"
    ):
        t0 = time.perf_counter()
        rw = rewrite_query(question, req.history, s)
        latency.rewrite = int((time.perf_counter() - t0) * 1000)
        diag.turn_class = rw.turn_class
        diag.triggers["rewrite_confidence"] = rw.confidence
        diag.triggers["rewrite_method"] = rw.method

        if rw.needs_clarification:
            latency.total = int((time.perf_counter() - t_start) * 1000)
            diag.latency_ms = latency
            diag.path = "clarify"
            return ChatResponse(
                query_id=query_id,
                status=QueryStatus.CLARIFY,
                message="Please specify which topic or entity you mean.",
                clarification_options=[
                    "Restate with the full subject (e.g. plan name + policy)",
                    "Ask a complete standalone question",
                ],
                effective_kb=effective,
                diagnostics=diag if _should_return_diagnostics(req, s) else None,
            )

        if rw.turn_class == "meta":
            latency.total = int((time.perf_counter() - t_start) * 1000)
            diag.latency_ms = latency
            return ChatResponse(
                query_id=query_id,
                status=QueryStatus.ANSWER,
                answer="You're welcome. Ask another policy question anytime.",
                effective_kb=effective,
                diagnostics=diag if _should_return_diagnostics(req, s) else None,
            )

        if rw.confidence >= s.rewrite_min_confidence:
            question = rw.retrieval_query
            diag.rewritten_query = rw.retrieval_query
            diag.retrieval_query = rw.retrieval_query
            if rw.slots.get("temporal") == "historical":
                include_historical = True
        else:
            diag.triggers["rewrite_skipped"] = "below_confidence"
    else:
        diag.retrieval_query = question

    # --- B: temporal (conditional) ---
    if not force_classic and s.enable_temporal_intent and rag_mode != "single":
        temporal = resolve_temporal_intent(question, req, s)
        diag.temporal_intent = temporal.intent
        diag.triggers["temporal_confidence"] = temporal.confidence
        diag.triggers["temporal_reason"] = temporal.reason
        if temporal.confidence >= s.temporal_min_confidence:
            if temporal.include_historical:
                include_historical = True
    else:
        diag.temporal_intent = "current" if not include_historical else "historical"

    # --- A: multi-query (conditional) ---
    multi_result = None
    if (
        not force_classic
        and s.enable_multi_query
        and rag_mode != "single"
    ):
        plan = detect_multi_facets(question, s)
        if plan and plan.confidence >= s.multi_facet_min_confidence:
            diag.triggers["multi_facet_confidence"] = plan.confidence
            diag.triggers["multi_method"] = plan.method
            try:
                multi_result = execute_multi_query(
                    plan,
                    departments=effective,
                    include_historical=include_historical,
                    settings=s,
                )
                latency.search = multi_result.search_ms
                path = "multi"
                sub_queries = multi_result.sub_queries
                coverage_dict = coverage_to_dict(multi_result.coverage)
                if not multi_result.all_covered and multi_result.any_covered:
                    multi_partial = True
                    missing = [
                        k for k, v in multi_result.coverage.items() if not v.covered
                    ]
                    partial_note = (
                        f"Missing or weak evidence for facet(s): {', '.join(missing)}. "
                        "Do not invent those sides."
                    )
                if not multi_result.any_covered:
                    multi_result = None
                    path = "single"
                    fallback_reason = "multi_zero_coverage"
            except Exception as exc:  # noqa: BLE001
                log.warning("multi_query failed: %s", exc)
                multi_result = None
                fallback_reason = "multi_execute_error"
                path = "single"

    # --- Execute single if not multi (or for fallback compare) ---
    result: RetrievalResult
    if multi_result is not None:
        result = RetrievalResult(
            hits=multi_result.hits,
            filters_applied=multi_result.filters_applied,
            backend=multi_result.backend,
        )
        context_override = pack_context_blocks(
            multi_result.hits, coverage=multi_result.coverage
        )

        # Fallback to single if multi underperforms
        if s.multi_query_fallback_to_single and multi_result.hits:
            t0 = time.perf_counter()
            single_res = single_path(
                question,
                departments=effective,
                include_historical=include_historical,
                settings=s,
            )
            latency.search += int((time.perf_counter() - t0) * 1000)
            # Prefer single if multi has partial and single score much better
            if (
                multi_partial
                and single_res.top_score > multi_result.hits[0].score * 1.5
                and single_res.hits
            ):
                result = single_res
                path = "single_fallback"
                fallback_reason = "multi_underperformed"
                multi_partial = False
                partial_note = None
                context_override = None
                coverage_dict = None
                sub_queries = []
    else:
        t0 = time.perf_counter()
        result = single_path(
            question,
            departments=effective,
            include_historical=include_historical,
            settings=s,
        )
        latency.search = int((time.perf_counter() - t0) * 1000)
        if fallback_reason:
            path = "single_fallback" if fallback_reason.startswith("multi") else "single"

    diag.path = path
    diag.fallback_reason = fallback_reason
    diag.sub_queries = sub_queries
    diag.filters = list(result.filters_applied)
    diag.coverage = coverage_dict
    diag.selected_chunk_ids = result.chunk_ids
    diag.top_score = result.top_score

    if s.log_retrieval_plan:
        log.info(
            "plan query_id=%s path=%s fallback=%s q=%r hits=%s top=%.4f",
            query_id,
            path,
            fallback_reason,
            question[:120],
            len(result.hits),
            result.top_score,
        )

    # --- S5 ambiguity (after retrieve, single path only) ---
    if (
        not force_classic
        and s.ambiguity_clarify_enabled
        and path == "single"
        and not multi_partial
    ):
        amb = detect_ambiguity(req.question, result.hits)
        if amb.is_ambiguous and amb.options:
            latency.total = int((time.perf_counter() - t_start) * 1000)
            diag.latency_ms = latency
            diag.triggers["ambiguity"] = amb.reason
            return ChatResponse(
                query_id=query_id,
                status=QueryStatus.CLARIFY,
                message="Your question matches several topics. Please pick one:",
                clarification_options=amb.options,
                effective_kb=effective,
                retrieval=RetrievalInfo(
                    hit_count=len(result.hits),
                    top_score=result.top_score,
                    filters_applied=result.filters_applied,
                    chunk_ids=result.chunk_ids,
                    path=path,
                ),
                diagnostics=diag if _should_return_diagnostics(req, s) else None,
            )

    # --- Evidence gate (use original user question for personal/out-of-corpus) ---
    evidence = evaluate_evidence(result, s, question=req.question)
    retrieval_info = RetrievalInfo(
        hit_count=len(result.hits),
        top_score=result.top_score,
        filters_applied=result.filters_applied,
        chunk_ids=result.chunk_ids,
        path=path,
    )

    if not evidence.pass_gate:
        # Multi partial with some coverage: still allow partial answer if configured
        if multi_partial and s.allow_partial_answers and result.hits:
            pass  # fall through to generate with partial_note
        else:
            latency.total = int((time.perf_counter() - t_start) * 1000)
            diag.latency_ms = latency
            log.info(
                "evidence gate fail query_id=%s reason=%s top=%.3f path=%s",
                query_id,
                evidence.reason,
                evidence.top_score,
                path,
            )
            packed = pack_context_blocks(result.hits) if result.hits else ""
            if req.include_diagnostics:
                diag.eval_context = packed
            want_diag = _should_return_diagnostics(req, s)
            return ChatResponse(
                query_id=query_id,
                status=QueryStatus.ESCALATE,
                answer=None,
                message=escalate_message(effective, evidence.reason),
                citations=[],
                effective_kb=effective,
                retrieval=retrieval_info,
                diagnostics=diag if want_diag else None,
                eval_context=packed if (want_diag or req.include_diagnostics) else None,
            )

    # --- Generate ---
    packed_context = context_override or pack_context_blocks(result.hits)
    t0 = time.perf_counter()
    answer_text, engine = generate_answer(
        req.question,  # user-facing question (not only rewrite)
        result.hits,
        s,
        context=packed_context,
        partial_note=partial_note,
    )
    latency.llm = int((time.perf_counter() - t0) * 1000)
    citations = ensure_citations(answer_text, result.hits)
    version_used = _version_used_from_hits(result.hits)

    status = QueryStatus.ANSWER
    message = None if engine == "azure_openai" else "extractive_fallback"
    if multi_partial and s.allow_partial_answers:
        status = QueryStatus.PARTIAL
        if message:
            message = f"{message}; partial_coverage"
        else:
            message = "partial_coverage"

    latency.total = int((time.perf_counter() - t_start) * 1000)
    diag.latency_ms = latency
    # Always attach pack for Foundry / offline eval when diagnostics requested
    if req.include_diagnostics:
        diag.eval_context = packed_context

    log.info(
        "chat ok query_id=%s path=%s engine=%s hits=%s top=%.3f cites=%s status=%s",
        query_id,
        path,
        engine,
        len(result.hits),
        result.top_score,
        len(citations),
        status.value,
    )

    want_diag = _should_return_diagnostics(req, s)
    return ChatResponse(
        query_id=query_id,
        status=status,
        answer=answer_text,
        message=message,
        citations=citations,
        effective_kb=effective,
        retrieval=retrieval_info,
        version_used=version_used,
        alternate_versions=[],  # offer when ENABLE_VERSION_OFFER + catalog
        diagnostics=diag if want_diag else None,
        eval_context=packed_context if (want_diag or req.include_diagnostics) else None,
    )
