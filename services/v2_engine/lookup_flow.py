"""/v2/lookup — open-source fallback (search results only).

The EnConvert cloud build turns lookup into far more than a search call: it
renders the top-N results (concurrent, multi-format), runs schema-driven
structured extraction per result, and synthesizes one cited, grounded answer
across the sources. That semantic layer is a cloud-only capability.

This open fallback returns the provider-neutral SEARCH RESULTS (via your own
search provider key) and records the usage/audit rows. Enrichment requests
(``perceive_top`` / ``enrich`` / ``synthesize_answer``) are accepted but ignored
with a warning — the results still come back, just without the cloud value-add.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

from api.v2.schemas.lookup import LookupRequest, LookupResponse, LookupResult
from models import LookupQuery
from services.v2_engine import usage
from services.v2_search.adapter import (
    SearchConfigError,
    SearchResults,
    SearchUnavailableError,
    SearchUpstreamError,
)
from services.v2_search.serper import SerperAdapter
from utils.postgres import get_db

logger = logging.getLogger(__name__)

_CLOUD_ONLY_WARNING = (
    "Result enrichment — page rendering across results, structured extraction, "
    "and synthesized cited answers — requires the EnConvert cloud engine and is "
    "not available in the self-hosted build; returning search results only."
)


def _adapter() -> SerperAdapter:
    """The search provider for this deployment (self-hoster supplies the key)."""
    return SerperAdapter()


async def run(request: LookupRequest, user: dict) -> LookupResponse:
    """Execute one /v2/lookup: search only — no enrichment, extraction, or answer."""
    project_id = int(user["id"])
    start = time.monotonic()

    found = await _search(request)

    warnings: list[str] = list(found.warnings)
    results = [LookupResult(**hit.model_dump()) for hit in found.results]

    # The search succeeded: charge one lookup query and record the audit row.
    usage.increment_lookup_usage(project_id)

    if request.perceive_top > 0 or request.enrich is not None:
        warnings.append(_CLOUD_ONLY_WARNING)

    lookup_id = _persist_lookup_query(
        project_id=project_id,
        request=request,
        results_count=found.total,
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
        perceive_top=0,
        perceive_operation_ids=[],
        answer_box=found.answer_box,
        knowledge_graph=found.knowledge_graph,
        answer=None,
        answer_sources=[],
        credits=found.credits,
        cost_cents=float(found.cost_cents),
        warnings=warnings,
    )


async def _search(request: LookupRequest) -> SearchResults:
    """Run the provider search; map provider faults to clean HTTP statuses."""
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


def _persist_lookup_query(
    *,
    project_id: int,
    request: LookupRequest,
    results_count: int,
    cost_cents: Decimal,
    duration_ms: int,
) -> Optional[int]:
    """Insert one ch_lookup_queries audit row; return its id (best-effort)."""
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
            perceive_top=0,
            perceive_operation_ids=None,
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
