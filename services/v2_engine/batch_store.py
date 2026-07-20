"""ch_perceive_batches persistence — the durable /v2/perceive/batch envelope.

Mirrors ingest_store's durability model: the batch envelope (shared render
``options`` + ``output_mode`` + counters) lives in ch_perceive_batches, and the
per-URL work lives in ch_perceive_operations (grouped by batch_id, owned by
operations.py). A process restart therefore RESUMES an in-flight batch instead
of failing it — ``list_active_batch_ids`` (backed by the migration's
``idx_perceive_batches_active`` partial index) is the resume scan, and the
worker re-renders only the still-pending operation rows.

Sessions follow the gateway convention (sync SQLModel sessions via get_db();
the worker offloads them with asyncio.to_thread). Timestamps are UTC-aware.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import update
from sqlmodel import select

from models import PerceiveBatch
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Non-terminal batch statuses (a row in one of these at boot was orphaned by a
# restart and is resumed — mirrors the migration's partial-index predicate).
ACTIVE_BATCH_STATUSES = ("queued", "processing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_batch(
    batch_id: str,
    project_id: int,
    *,
    output_mode: str,
    options: dict[str, Any],
    total: int,
) -> None:
    """Insert the durable batch envelope (status 'queued')."""
    db = get_db()
    try:
        db.add(
            PerceiveBatch(
                batch_id=batch_id,
                project_id=project_id,
                status="queued",
                output_mode=output_mode,
                options=options,
                total=total,
                created_at=_utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def get_batch(batch_id: str) -> Optional[PerceiveBatch]:
    """Fetch one batch row by its public id (no project scope — worker use)."""
    db = get_db()
    try:
        return db.exec(
            select(PerceiveBatch).where(PerceiveBatch.batch_id == batch_id)
        ).first()
    finally:
        db.close()


def get_batch_for_project(
    batch_id: str, project_id: int
) -> Optional[PerceiveBatch]:
    """Project-scoped fetch (handler/cancel — never leaks a foreign batch)."""
    db = get_db()
    try:
        return db.exec(
            select(PerceiveBatch).where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.project_id == project_id,
            )
        ).first()
    finally:
        db.close()


def list_active_batch_ids() -> List[str]:
    """Batch ids in a non-terminal status (the startup resume scan)."""
    db = get_db()
    try:
        rows = db.exec(
            select(PerceiveBatch.batch_id)
            .where(PerceiveBatch.status.in_(ACTIVE_BATCH_STATUSES))  # type: ignore[attr-defined]
            .order_by(PerceiveBatch.id)  # type: ignore[arg-type]
        ).all()
        return list(rows)
    finally:
        db.close()


def transition_status(
    batch_id: str,
    new_status: str,
    *,
    allowed_from: tuple[str, ...] = ACTIVE_BATCH_STATUSES,
    error_message: Optional[str] = None,
    zip_object_key: Optional[str] = None,
    completed: bool = False,
) -> bool:
    """Atomically advance status ONLY if the row is still in ``allowed_from``.

    One conditional UPDATE makes cancellation race-proof: once DELETE has set
    'canceled', every worker transition returns False (no longer in
    ``allowed_from``) and the flow stops without resurrecting the batch.
    """
    values: dict[str, Any] = {"status": new_status, "updated_at": _utcnow()}
    if error_message is not None:
        values["error_message"] = error_message[:2000]
    if zip_object_key is not None:
        values["zip_object_key"] = zip_object_key
    if completed:
        values["completed_at"] = _utcnow()
    db = get_db()
    try:
        result = db.execute(
            update(PerceiveBatch)
            .where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.status.in_(allowed_from),  # type: ignore[attr-defined]
            )
            .values(**values)
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


def update_progress(batch_id: str, *, completed: int, failed: int) -> None:
    """Overwrite the running counters (guarded to active batches).

    A cancel committed mid-loop wins: the conditional UPDATE leaves a
    terminal row's audit counters frozen.
    """
    db = get_db()
    try:
        db.execute(
            update(PerceiveBatch)
            .where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.status.in_(ACTIVE_BATCH_STATUSES),  # type: ignore[attr-defined]
            )
            .values(completed=completed, failed=failed, updated_at=_utcnow())
        )
        db.commit()
    finally:
        db.close()


def finalize(
    batch_id: str,
    *,
    status: str,
    completed: int,
    failed: int,
    zip_object_key: Optional[str] = None,
) -> bool:
    """Mark an ACTIVE batch terminal with final counters (conditional).

    Conditional on the batch still being active so a cancel committed during
    the last render or the zip bundle wins. Returns True when it committed.
    """
    values: dict[str, Any] = {
        "status": status,
        "completed": completed,
        "failed": failed,
        "updated_at": _utcnow(),
        "completed_at": _utcnow(),
    }
    if zip_object_key is not None:
        values["zip_object_key"] = zip_object_key
    db = get_db()
    try:
        result = db.execute(
            update(PerceiveBatch)
            .where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.status.in_(ACTIVE_BATCH_STATUSES),  # type: ignore[attr-defined]
            )
            .values(**values)
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


def cancel_batch(batch_id: str, project_id: int) -> Optional[PerceiveBatch]:
    """Cancel an ACTIVE batch (idempotent); return the current row.

    A single conditional UPDATE (active -> canceled) races safely against the
    worker's own transitions; the re-fetch returns the true current row so a
    DELETE on an already-terminal batch is a no-op, not an error.
    """
    db = get_db()
    try:
        db.execute(
            update(PerceiveBatch)
            .where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.project_id == project_id,
                PerceiveBatch.status.in_(ACTIVE_BATCH_STATUSES),  # type: ignore[attr-defined]
            )
            .values(status="canceled", updated_at=_utcnow(), completed_at=_utcnow())
        )
        db.commit()
        return db.exec(
            select(PerceiveBatch).where(
                PerceiveBatch.batch_id == batch_id,
                PerceiveBatch.project_id == project_id,
            )
        ).first()
    finally:
        db.close()
