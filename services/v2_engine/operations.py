"""ch_perceive_operations persistence (Task F.5).

One module owns every read/write of the perceive operations table so
the handler, the flow, and the F.8 batch worker share identical row
semantics. Sessions follow the existing gateway convention (sync
SQLModel sessions opened per call via ``get_db()``) — the V1 handlers
do the same from async code; revisit only if a dedicated async engine
lands.

Timestamps are written timezone-aware (UTC) explicitly so created_at /
completed_at comparisons inside a row are always coherent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import select

from models import PerceiveOperation
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Reserved key inside output_keys: request fingerprint for the 1 h cache
# (cache_mode="enabled"). Underscore-prefixed keys are never presented
# as outputs.
FINGERPRINT_KEY = "_fingerprint"

# Reserved key inside output_keys: the batch ZIP artifact entry
# ({key, size_bytes}) stamped on completed rows of an
# output_mode="zip" batch (F.8).
BATCH_ZIP_KEY = "_batch_zip"

# Reserved keys inside output_keys (QA report 2026-08-06, fixes D1/D4):
# the main-document HTTP status and the fired quality deductions for
# this render, so GET /v2/perceive/{id} and cache hits can return them.
# Underscore-prefixed keys are never presented as outputs.
HTTP_STATUS_KEY = "_http_status"
DEDUCTIONS_KEY = "_deductions"

CACHE_TTL_SECONDS = 3600  # plan section 4: 1 h TTL


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the render-affecting request surface.

    A cached operation may only serve a request whose fingerprint
    matches exactly — outputs, extract set, schema, pdf options,
    viewport, js_code and wait_for all change what a render produces.
    """
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_operation(
    *,
    operation_id: str,
    project_id: int,
    url: str,
    outputs_requested: list[str],
    batch_id: Optional[str] = None,
) -> None:
    """Insert the 'processing' row at request start.

    F.8: batch URLs are pre-created as 'queued' rows (so a batch's
    total is known and restart-visible); when the worker starts one,
    this claims the existing row in place (queued -> processing)
    instead of inserting a duplicate operation_id. A row already
    'processing' is a durable-batch RESUME (the previous process died
    mid-render) — it is re-claimed in place, not re-inserted (the
    operation_id is unique), so resume never raises.
    """
    db = get_db()
    try:
        existing = db.exec(
            select(PerceiveOperation).where(
                PerceiveOperation.operation_id == operation_id
            )
        ).first()
        if existing is not None:
            if existing.status in ("queued", "processing"):
                existing.status = "processing"
                db.add(existing)
                db.commit()
            else:
                logger.error(
                    "create_operation: %s already exists with status %s",
                    operation_id,
                    existing.status,
                )
            return
        db.add(
            PerceiveOperation(
                operation_id=operation_id,
                project_id=project_id,
                url=url,
                status="processing",
                outputs_requested=list(outputs_requested),
                batch_id=batch_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


def create_queued_operations(
    *,
    batch_id: str,
    project_id: int,
    entries: list[tuple[str, str]],
    outputs_requested: list[str],
) -> None:
    """Bulk-insert the 'queued' rows for a batch (F.8).

    ``entries`` are (operation_id, url) pairs in batch order. Creating
    every row up front makes the batch's total restart-visible and
    lets the status endpoint report progress without any in-memory
    bookkeeping.
    """
    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        for operation_id, url in entries:
            db.add(
                PerceiveOperation(
                    operation_id=operation_id,
                    project_id=project_id,
                    url=url,
                    status="queued",
                    outputs_requested=list(outputs_requested),
                    batch_id=batch_id,
                    created_at=now,
                )
            )
        db.commit()
    finally:
        db.close()


def list_batch_operations(
    batch_id: str, project_id: int
) -> list[PerceiveOperation]:
    """Every operation row of one batch, in insertion (= batch) order.

    Project-scoped: a foreign batch_id yields [] and the handler 404s —
    existence is never leaked across tenants (same rule as the single
    status GET).
    """
    db = get_db()
    try:
        return list(
            db.exec(
                select(PerceiveOperation)
                .where(
                    PerceiveOperation.batch_id == batch_id,
                    PerceiveOperation.project_id == project_id,
                )
                .order_by(PerceiveOperation.id)  # type: ignore[arg-type]
            ).all()
        )
    finally:
        db.close()


def attach_batch_zip(
    batch_id: str, project_id: int, zip_entry: dict[str, Any]
) -> None:
    """Stamp the batch ZIP artifact on every completed row (F.8 zip mode).

    Stored under the reserved underscore key (like FINGERPRINT_KEY) so
    outputs_from_keys never presents it as a per-URL output; the batch
    status endpoint lifts it from any completed row.
    """
    db = get_db()
    try:
        rows = db.exec(
            select(PerceiveOperation).where(
                PerceiveOperation.batch_id == batch_id,
                PerceiveOperation.project_id == project_id,
                PerceiveOperation.status == "completed",
            )
        ).all()
        for row in rows:
            keys = dict(row.output_keys or {})
            keys[BATCH_ZIP_KEY] = zip_entry
            row.output_keys = keys
            db.add(row)
        if rows:
            db.commit()
    finally:
        db.close()


def fail_stale_operations(reason: str) -> int:
    """Mark every orphaned NON-BATCH row failed; returns the count (F.8).

    Runs once at startup. The gateway is a single process (the browser
    singleton requires it), so any 'queued'/'processing' row at boot was
    orphaned by a restart. Single-URL perceive operations (batch_id IS
    NULL) have no owner to resume them, so they are failed explicitly
    (beats V1's stuck-forever 'In Progress' rows). BATCH rows are left
    UNTOUCHED — the durable batch resume (batch_worker.startup ->
    ch_perceive_batches) re-enqueues those batches and re-renders only
    the still-pending URLs, so failing them here would wrongly abort a
    recoverable batch.
    """
    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        non_terminal = PerceiveOperation.status.in_(  # type: ignore[attr-defined]
            ("queued", "processing")
        )
        rows = db.exec(
            select(PerceiveOperation).where(
                non_terminal,
                PerceiveOperation.batch_id.is_(None),  # type: ignore[union-attr]
            )
        ).all()
        for row in rows:
            row.status = "failed"
            row.error_message = reason[:2000]
            row.completed_at = now
            db.add(row)
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


def complete_operation(
    *,
    operation_id: str,
    url_final: Optional[str],
    content_hash: Optional[str],
    output_keys: dict[str, Any],
    structured_data: Optional[dict[str, Any]],
    extraction_tier: Optional[str],
    cache_hit: bool,
    duration_ms: int,
    render_quality_score: Optional[float] = None,
    llm_input_tokens: int = 0,
    llm_output_tokens: int = 0,
    llm_cost_cents: Decimal = Decimal("0"),
) -> None:
    """Mark the row completed with its artifacts and metadata.

    The llm_* fields (Task F.6) record what THIS operation spent on
    Tier-3 extraction; cache-hit completions keep the zero defaults —
    serving from cache costs nothing new.
    """
    db = get_db()
    try:
        op = db.exec(
            select(PerceiveOperation).where(
                PerceiveOperation.operation_id == operation_id
            )
        ).first()
        if op is None:
            logger.error("complete_operation: %s not found", operation_id)
            return
        op.status = "completed"
        op.url_final = url_final
        op.content_hash = content_hash
        op.output_keys = output_keys
        op.structured_data = structured_data
        op.extraction_tier = extraction_tier
        op.cache_hit = cache_hit
        op.duration_ms = duration_ms
        op.render_quality_score = render_quality_score
        op.llm_input_tokens = llm_input_tokens
        op.llm_output_tokens = llm_output_tokens
        op.llm_cost_cents = llm_cost_cents
        op.completed_at = datetime.now(timezone.utc)
        db.add(op)
        db.commit()
    finally:
        db.close()


def fail_operation(
    *,
    operation_id: str,
    error_message: str,
    duration_ms: Optional[int] = None,
) -> None:
    """Mark the row failed. Never raises — failure paths call this."""
    try:
        db = get_db()
    except Exception:  # noqa: BLE001 — DB down must not mask the real error
        logger.exception("fail_operation: could not open session")
        return
    try:
        op = db.exec(
            select(PerceiveOperation).where(
                PerceiveOperation.operation_id == operation_id
            )
        ).first()
        if op is None:
            return
        op.status = "failed"
        op.error_message = error_message[:2000]
        op.duration_ms = duration_ms
        op.completed_at = datetime.now(timezone.utc)
        db.add(op)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("fail_operation: persistence failed")
    finally:
        db.close()


def get_operation(operation_id: str) -> Optional[PerceiveOperation]:
    """Fetch one operation row by its public id."""
    db = get_db()
    try:
        return db.exec(
            select(PerceiveOperation).where(
                PerceiveOperation.operation_id == operation_id
            )
        ).first()
    finally:
        db.close()


def find_cached_operation(
    *,
    project_id: int,
    url: str,
    fingerprint: str,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> Optional[PerceiveOperation]:
    """Newest completed operation matching url + request fingerprint.

    Scoped to the requesting project — cache entries never cross
    tenants. Candidates are fetched on the (project_id, created_at)
    index and the fingerprint is compared in Python (it lives inside
    the output_keys JSONB).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    db = get_db()
    try:
        candidates = db.exec(
            select(PerceiveOperation)
            .where(
                PerceiveOperation.project_id == project_id,
                PerceiveOperation.url == url,
                PerceiveOperation.status == "completed",
                # F1 (QA report 2026-08-06): cache-hit rows are NOT cache
                # candidates. Before this, a hit row (fresh created_at,
                # copied fingerprint) renewed the 1 h TTL indefinitely —
                # an hourly poller would never see a fresh render again.
                PerceiveOperation.cache_hit == False,  # noqa: E712
                PerceiveOperation.created_at >= cutoff,
            )
            .order_by(PerceiveOperation.created_at.desc())  # type: ignore[union-attr]
            .limit(20)
        ).all()
    finally:
        db.close()

    for candidate in candidates:
        keys = candidate.output_keys or {}
        if keys.get(FINGERPRINT_KEY) == fingerprint:
            return candidate
    return None
