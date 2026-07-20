"""/v2/lookup orchestration (Task H.3, plan sections 4 + 8).

The flow is deliberately thin:

1. Call the neutral search adapter (Serper today) and translate any
   provider failure into a clean HTTP status -- never leaking raw
   provider text (security rule: generic client errors, detail to logs).
2. On success, consume one ``lookup_queries`` from the V2 quota and
   record one ``ch_lookup_queries`` audit row (H.3 verification c: one
   row per query).
3. If ``perceive_top > 0``, auto-perceive the top-N result URLs through
   the existing ``perceive_flow.run`` -- each becomes a first-class
   ``/v2/perceive`` operation (its own ``ch_perceive_operations`` row +
   its own quota decrement). Auto-perceive is BEST-EFFORT: a single URL
   failing, or the perceive quota running out mid-way, degrades to a
   warning and still returns the search results (availability over
   failure -- the SERP is the primary product here).

Quota model: the lookup quota is enforced once, in the handler
(``check_v2_quota("lookup_queries")``), before this flow runs. The
auto-perceive loop re-checks the *perceive* quota per URL so a free user
cannot mint unlimited free renders by funnelling them through lookup; it
stops at the boundary instead of 402-ing the whole search.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

from api.deps import check_v2_quota
from api.v2.schemas.lookup import LookupRequest, LookupResponse, LookupResult
from api.v2.schemas.perceive import PerceiveRequest
from models import LookupQuery
from services.v2_engine import perceive_flow, usage
from services.v2_engine.extractors import schema_llm
from services.v2_search.adapter import (
    SearchConfigError,
    SearchResults,
    SearchUnavailableError,
    SearchUpstreamError,
)
from services.v2_search.serper import SerperAdapter
from utils.postgres import get_db
from utils.storage import download_from_storage

logger = logging.getLogger(__name__)

# Auto-perceive keeps it light: markdown only (no screenshot/pdf render,
# no LLM extraction) -- the agent wants the page text behind each hit.
_PERCEIVE_OUTPUTS: list[str] = ["markdown"]

# Bytes of perceived-markdown to feed the answer synthesizer per source.
_ANSWER_SOURCE_BYTES = 6_000


def _adapter():
    """The search provider for this deployment.

    A single seam so a future Brave/Tavily swap (or a test fake) is one
    line; the rest of the flow only knows the neutral SearchAdapter API.
    """
    return SerperAdapter()


async def run(request: LookupRequest, user: dict) -> LookupResponse:
    """Execute one /v2/lookup operation end-to-end."""
    project_id = int(user["id"])
    start = time.monotonic()

    found = await _search(request)

    warnings: list[str] = list(found.warnings)
    results = [LookupResult(**hit.model_dump()) for hit in found.results]

    # The search succeeded: charge one lookup query and record the audit
    # row regardless of how auto-perceive fares below.
    usage.increment_lookup_usage(project_id)

    perceive_ids: list[str] = []
    if request.perceive_top > 0:
        if request.enrich is not None:
            perceive_ids = await _enrich(request, results, user, warnings)
        else:
            perceive_ids = await _auto_perceive(request, results, user, warnings)

    answer: Optional[str] = None
    answer_sources: list[str] = []
    if request.enrich is not None and request.enrich.synthesize_answer:
        answer, answer_sources = await _synthesize_answer(
            request, results, user, warnings
        )

    lookup_id = _persist_lookup_query(
        project_id=project_id,
        request=request,
        results_count=found.total,
        perceive_ids=perceive_ids,
        cost_cents=found.cost_cents,
        duration_ms=int((time.monotonic() - start) * 1000),
    )

    return LookupResponse(
        lookup_id=lookup_id,
        query=request.query,
        category=request.category,
        country=request.country,
        locale=request.locale,
        time_filter=request.time_filter,
        total=found.total,
        results=results,
        perceive_top=len(perceive_ids),
        perceive_operation_ids=perceive_ids,
        answer_box=found.answer_box,
        knowledge_graph=found.knowledge_graph,
        answer=answer,
        answer_sources=answer_sources,
        credits=found.credits,
        cost_cents=float(found.cost_cents),
        warnings=warnings,
    )


async def _search(request: LookupRequest) -> SearchResults:
    """Run the provider search; map provider faults to HTTP statuses."""
    adapter = _adapter()
    try:
        return await adapter.search(
            query=request.query,
            category=request.category,
            country=request.country,
            locale=request.locale,
            time_filter=request.time_filter,
            num=request.num_results,
            page=request.page,
            location=request.location,
            autocorrect=request.autocorrect,
        )
    except SearchConfigError as exc:
        # Server misconfiguration (missing/invalid key). Detail to logs
        # only; the client gets a generic, retryable message.
        logger.error("/v2/lookup: search provider misconfigured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily unavailable. Please try again later.",
        ) from exc
    except SearchUnavailableError as exc:
        logger.warning("/v2/lookup: search provider unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily unavailable. Please try again later.",
        ) from exc
    except SearchUpstreamError as exc:
        logger.warning("/v2/lookup: search provider upstream error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="The search provider returned an error. Please try again.",
        ) from exc


async def _auto_perceive(
    request: LookupRequest,
    results: list[LookupResult],
    user: dict,
    warnings: list[str],
) -> list[str]:
    """Perceive the top-N navigable result URLs; return their operation ids.

    Best-effort and quota-aware: stops cleanly when the perceive quota is
    exhausted (or the plan lacks perceive) and skips individual URLs that
    fail, recording each as a warning. Each successful perception attaches
    its full PerceiveResponse to the matching result.
    """
    candidates = [hit for hit in results if hit.url][: request.perceive_top]
    if not candidates:
        warnings.append(
            "perceive_top was requested but no result had a navigable URL; "
            "auto-perceive was skipped."
        )
        return []

    operation_ids: list[str] = []
    for result in candidates:
        try:
            check_v2_quota(user, "perceive_operations", units=1)
        except HTTPException:
            warnings.append(
                f"auto-perceive stopped at {len(operation_ids)}/"
                f"{len(candidates)}: perceive quota exhausted or not "
                "included in your plan."
            )
            break

        operation_id = f"per_{uuid.uuid4().hex}"
        try:
            perceive_request = PerceiveRequest(
                url=result.url, outputs=list(_PERCEIVE_OUTPUTS)
            )
            result.perceive = await perceive_flow.run(
                perceive_request, operation_id, user
            )
        except HTTPException as exc:
            # Keep the provider/SSRF detail server-side only: echoing
            # exc.detail (e.g. "resolves to a private address") would let a
            # caller who can influence the SERP probe internal hosts.
            logger.warning(
                "auto-perceive failed for %s: %s", result.url, exc.detail
            )
            warnings.append(f"auto-perceive failed for {result.url}.")
            continue
        except Exception:  # noqa: BLE001 — one bad URL must not sink the search
            logger.exception("auto-perceive crashed for %s", result.url)
            warnings.append(f"auto-perceive failed for {result.url}.")
            continue

        operation_ids.append(operation_id)

    return operation_ids


async def _perceive_one(
    result: LookupResult,
    user: dict,
    outputs: list[str],
    schema: Optional[dict],
) -> tuple[Optional[str], Optional[str]]:
    """Perceive one result URL with the requested outputs/schema.

    Returns (operation_id, warning). Never raises — a failure yields
    (None, warning). Keeps provider/SSRF detail server-side only.
    """
    operation_id = f"per_{uuid.uuid4().hex}"
    try:
        perceive_request = PerceiveRequest(
            url=result.url,
            outputs=list(outputs),
            extraction_schema=schema,
        )
        result.perceive = await perceive_flow.run(perceive_request, operation_id, user)
        return operation_id, None
    except HTTPException as exc:
        logger.warning("enrichment failed for %s: %s", result.url, exc.detail)
        return None, f"enrichment failed for {result.url}."
    except Exception:  # noqa: BLE001 — one bad URL must not sink the search
        logger.exception("enrichment crashed for %s", result.url)
        return None, f"enrichment failed for {result.url}."


async def _enrich(
    request: LookupRequest,
    results: list[LookupResult],
    user: dict,
    warnings: list[str],
) -> list[str]:
    """Concurrent, multi-format enrichment of the top-N result URLs.

    Renders up to ``enrich.concurrency`` results in parallel (markdown/HTML
    take the no-browser TLS path and truly parallelize; screenshot/pdf
    serialize on the shared browser). Optionally runs structured extraction
    per result. Quota-safe: if the whole candidate set fits the perceive
    quota it runs concurrently; otherwise it falls back to a sequential,
    stop-at-boundary pass so a near-limit account never over-mints renders.
    """
    enrich = request.enrich
    outputs = list(enrich.outputs)
    if enrich.extraction_schema and "structured" not in outputs:
        outputs.append("structured")
    if enrich.synthesize_answer and "markdown" not in outputs:
        outputs.append("markdown")

    candidates = [hit for hit in results if hit.url][: request.perceive_top]
    if not candidates:
        warnings.append(
            "perceive_top was requested but no result had a navigable URL; "
            "enrichment was skipped."
        )
        return []

    # Reserve the whole set up front; on failure, degrade to sequential.
    try:
        check_v2_quota(user, "perceive_operations", units=len(candidates))
        run_concurrently = True
    except HTTPException:
        run_concurrently = False

    if not run_concurrently:
        return await _enrich_sequential(
            candidates, user, warnings, outputs, enrich.extraction_schema
        )

    semaphore = asyncio.Semaphore(enrich.concurrency)

    async def _one(result: LookupResult) -> Optional[str]:
        async with semaphore:
            operation_id, warning = await _perceive_one(
                result, user, outputs, enrich.extraction_schema
            )
            if warning:
                warnings.append(warning)
            return operation_id

    ids = await asyncio.gather(*(_one(result) for result in candidates))
    return [op_id for op_id in ids if op_id]


async def _enrich_sequential(
    candidates: list[LookupResult],
    user: dict,
    warnings: list[str],
    outputs: list[str],
    schema: Optional[dict],
) -> list[str]:
    """Sequential enrichment with a per-URL quota gate (stop-at-boundary)."""
    operation_ids: list[str] = []
    for result in candidates:
        try:
            check_v2_quota(user, "perceive_operations", units=1)
        except HTTPException:
            warnings.append(
                f"enrichment stopped at {len(operation_ids)}/{len(candidates)}: "
                "perceive quota exhausted or not included in your plan."
            )
            break
        operation_id, warning = await _perceive_one(result, user, outputs, schema)
        if warning:
            warnings.append(warning)
        if operation_id:
            operation_ids.append(operation_id)
    return operation_ids


async def _synthesize_answer(
    request: LookupRequest,
    results: list[LookupResult],
    user: dict,
    warnings: list[str],
) -> tuple[Optional[str], list[str]]:
    """Synthesize one cited answer grounded in the enriched results.

    Prefers each perceived result's rendered markdown (downloaded from
    storage) as the source text; falls back to the result snippet. Reuses the
    Haiku stack + budget ledger. Best-effort: any failure degrades to a
    warning and returns no answer.
    """
    subscription = user.get("subscription") or {}
    llm_enabled = bool(subscription.get("llm_extraction_enabled")) and (
        subscription.get("agent_model_tier") not in (None, "none", "", 0)
    )
    if not llm_enabled:
        warnings.append(
            "synthesize_answer requires LLM extraction, which is not enabled "
            "on your plan; the answer was skipped."
        )
        return None, []

    candidates = [hit for hit in results if hit.url][: max(request.perceive_top, 1)]
    sources: list[tuple[str, str]] = []
    for result in candidates:
        text = await _source_text(result)
        if result.url and text.strip():
            sources.append((result.url, text))
    if not sources:
        warnings.append("no source text was available to synthesize an answer.")
        return None, []

    question = (request.enrich.answer_prompt or request.query).strip()
    plan_slug = str(subscription.get("plan_slug") or user.get("plan_slug") or "")
    result = await schema_llm.answer_from_sources(
        question,
        sources,
        str(int(user["id"])),
        plan_slug=plan_slug,
        usage_key=f"lookup_answer_{uuid.uuid4().hex}",
    )
    if not result.answer:
        warnings.append(
            f"answer synthesis was skipped ({result.skipped_reason or 'no output'})."
        )
        return None, []
    return result.answer, result.sources_used


async def _source_text(result: LookupResult) -> str:
    """Best source text for the answer: perceived markdown, else the snippet."""
    perceive = result.perceive
    outputs = getattr(perceive, "outputs", None) if perceive is not None else None
    if outputs and "markdown" in outputs:
        object_key = getattr(outputs["markdown"], "object_key", None)
        if object_key:
            try:
                raw = await asyncio.to_thread(download_from_storage, object_key)
                text = raw.decode("utf-8", errors="ignore")
                return text.encode("utf-8")[:_ANSWER_SOURCE_BYTES].decode(
                    "utf-8", errors="ignore"
                )
            except Exception:  # noqa: BLE001 — fall back to the snippet
                logger.warning(
                    "answer source download failed for %s", result.url, exc_info=True
                )
    return (result.snippet or "")[:_ANSWER_SOURCE_BYTES]


def _persist_lookup_query(
    *,
    project_id: int,
    request: LookupRequest,
    results_count: int,
    perceive_ids: list[str],
    cost_cents: Decimal,
    duration_ms: int,
) -> Optional[int]:
    """Insert one ch_lookup_queries audit row; return its id.

    Best-effort: the search (and any perception) already succeeded, so an
    audit-write failure is logged and swallowed rather than 500-ing a
    good response. Returns None on failure.
    """
    db = get_db()
    try:
        row = LookupQuery(
            project_id=project_id,
            query=request.query,
            category=request.category,
            country=request.country,
            locale=request.locale,
            time_filter=request.time_filter,
            results_count=results_count,
            perceive_top=len(perceive_ids),
            perceive_operation_ids=perceive_ids or None,
            serper_cost_cents=cost_cents,
            status="completed",
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception("/v2/lookup: failed to persist ch_lookup_queries row")
        return None
    finally:
        db.close()
