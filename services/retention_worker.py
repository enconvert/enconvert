"""Droplet-local file-retention poller (no-GCP design).

Owner decision (Group I onward): NO Google services. Time-based file deletion
was previously scheduled ONLY via Google Cloud Tasks
(utils/cloud_tasks.schedule_file_cleanup) calling /internal/cleanup-file at
delete-time. With Cloud Tasks not running on the droplet, that scheduling
silently no-opped and expired files were never deleted.

This worker sweeps the durable ch_scheduled_deletions schedule instead — the
same poller shape as batch/ingest/watch/billing, started from main.py's
lifespan. Durability matches the watch worker: the schedule IS the data
(delete_at), so downtime just means the first tick after boot sweeps up
everything overdue.

Each tick claims due rows FOR UPDATE SKIP LOCKED (a second process or an
overlapping tick simply skips), deletes each object from Spaces, reconciles
storage bookkeeping, and stamps deleted_at. A per-row failure records
last_error and leaves the row PENDING for retry, capped at MAX_ATTEMPTS so a
poison row can never cycle forever. One bad row never aborts the batch, and a
bad tick never kills the loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from models import ScheduledDeletion
from utils.postgres import get_db
from utils.retention import _delete_and_reconcile

logger = logging.getLogger(__name__)

# Free-tier retention is as short as 1 hour, so poll tightly: a 15-minute sweep
# bounds worst-case over-retention to ~one interval past expiry.
POLL_INTERVAL_SECONDS = 900
# Rows per transaction. The row locks (and the Spaces deletes) are held for the
# batch — the same accepted pattern as billing_rotation holding a lock across
# PayPal HTTP. Kept modest so the transaction stays short.
BATCH = 50
# Bound per-tick work so a large backlog drains over several ticks rather than
# one unboundedly long transaction.
MAX_BATCHES_PER_TICK = 40
# Give up re-claiming a row that keeps failing (object may linger, but the row
# stops cycling; last_error records why).
MAX_ATTEMPTS = 5

_worker_task: Optional[asyncio.Task] = None


def _sweep_batch_sync(now: datetime, batch: int) -> int:
    """Claim and process up to ``batch`` due rows (sync body, one transaction).

    Returns the number of rows CLAIMED (not just deleted), so the caller can
    tell whether more due rows may remain and keep draining.
    """
    db = get_db()
    try:
        rows = db.exec(
            select(ScheduledDeletion)
            .where(
                ScheduledDeletion.deleted_at.is_(None),
                ScheduledDeletion.delete_at <= now,
            )
            .order_by(ScheduledDeletion.delete_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).all()

        done = 0
        for row in rows:
            row.attempts = (row.attempts or 0) + 1
            try:
                _delete_and_reconcile(db, row.object_key, row.project_id)
                row.deleted_at = now
                done += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
                row.last_error = str(exc)[:500]
                logger.exception(
                    "retention: delete failed for %s", row.object_key
                )
                if row.attempts >= MAX_ATTEMPTS:
                    row.deleted_at = now  # give up; stop re-claiming this poison row
                    logger.error(
                        "retention: giving up on %s after %d attempts",
                        row.object_key, row.attempts,
                    )
            db.add(row)

        db.commit()
        if done:
            logger.info(
                "retention: deleted %d of %d claimed object(s)", done, len(rows)
            )
        return len(rows)
    finally:
        db.close()


async def tick(now: Optional[datetime] = None) -> int:
    """One sweep: process every due row, draining in bounded batches.

    Returns the total number of rows claimed across the batches. Shared by the
    poll loop and test harnesses (same convention as the other workers).
    """
    moment = now or datetime.now(timezone.utc)
    total = 0
    for _ in range(MAX_BATCHES_PER_TICK):
        claimed = await asyncio.to_thread(_sweep_batch_sync, moment, BATCH)
        total += claimed
        if claimed < BATCH:
            break
    return total


async def _poll_loop() -> None:
    """Scan immediately, then every POLL_INTERVAL_SECONDS.

    The immediate first scan matters: on the first boot with this worker the
    live DB may hold a backlog of rows already past delete_at (retention was
    Cloud-Tasks-triggered and Cloud Tasks is not running).
    """
    while True:
        try:
            swept = await tick()
            if swept:
                logger.info("retention: swept %d due row(s)", swept)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.exception("retention: poll tick crashed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def startup() -> None:
    """Lifespan hook: start the poller."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_poll_loop(), name="retention-worker")


async def shutdown() -> None:
    """Lifespan hook: stop the poller between ticks."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
