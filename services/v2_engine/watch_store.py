"""ch_watchers / ch_watcher_snapshots persistence (Tasks I.1/I.2).

One module owns every read/write of the watcher tables so the CRUD handlers,
the scheduling flow and the droplet-local poller share identical row semantics
— the same arrangement H.7's ``ingest_store`` uses for ingest. Sessions follow
the gateway convention (sync SQLModel sessions opened per call via ``get_db``;
the worker offloads them with ``asyncio.to_thread``). Timestamps are written
timezone-aware UTC against the TIMESTAMPTZ columns.

Scheduling model (the I.1 replacement for the plan's Cloud Tasks dispatch —
owner decision 2026-06-07: no Google services): the next fire time lives in
``ch_watchers.next_check_at``. ``claim_due_watchers`` selects the active rows
whose time has come — riding the migration's partial index
``idx_watchers_next_check`` — under ``FOR UPDATE SKIP LOCKED`` and advances each
one's ``next_check_at`` by a full interval in the SAME transaction. That
claim-and-advance makes a check at-most-once per cycle: a row being rendered is
already rescheduled, so a second poll (or a second process) never double-fires
it, and a crash mid-render simply skips one cycle rather than orphaning state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlmodel import select

from api.v2.schemas.watch import MIN_FREQUENCY_MINUTES
from models import Watcher, WatcherSnapshot
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Per-process safety net on a single claim so one poll can never pull an
# unbounded backlog into memory; whatever is left over is caught next poll.
MAX_CLAIM_BATCH = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedWatcher:
    """A due watcher detached from its session for out-of-transaction work.

    The poller renders OUTSIDE the claim transaction (renders run 10-30 s;
    holding a row lock that long would serialize the table), so it carries the
    plain field values it needs rather than a live ORM row.
    """

    watcher_id: str
    project_id: int
    url: str
    frequency_minutes: int
    consecutive_errors: int
    diff_mode: str
    # JSONB: the API stores a dict, but a list form is valid JSON too — both are
    # accepted (watch_flow._track_terms coerces either into the filter terms).
    track_fields: Optional[dict[str, Any] | list[Any]]
    webhook_url: Optional[str]
    notify_email: bool


# ── CRUD (Task I.2) ──────────────────────────────────────────────────────────


def create_watcher(
    *,
    watcher_id: str,
    project_id: int,
    url: str,
    frequency_minutes: int,
    diff_mode: str,
    track_fields: Optional[dict[str, Any]],
    webhook_url: Optional[str],
    notify_email: bool,
    next_check_at: datetime,
    now: datetime,
) -> Watcher:
    """Insert one active watcher and return the persisted row.

    ``next_check_at`` is the first fire time (the flow passes ``now`` so the
    poller picks it up on the next tick).
    """
    db = get_db()
    try:
        watcher = Watcher(
            watcher_id=watcher_id,
            project_id=project_id,
            url=url,
            status="active",
            frequency_minutes=frequency_minutes,
            diff_mode=diff_mode,
            track_fields=track_fields,
            webhook_url=webhook_url,
            notify_email=notify_email,
            next_check_at=next_check_at,
            created_at=now,
        )
        db.add(watcher)
        db.commit()
        db.refresh(watcher)
        return watcher
    finally:
        db.close()


def get_watcher_for_project(
    watcher_id: str, project_id: int
) -> Optional[Watcher]:
    """Fetch a watcher scoped to its owner; foreign/unknown ids yield None so
    the handler 404s without leaking existence across tenants."""
    db = get_db()
    try:
        return db.exec(
            select(Watcher).where(
                Watcher.watcher_id == watcher_id,
                Watcher.project_id == project_id,
            )
        ).first()
    finally:
        db.close()


def list_watchers_for_project(
    project_id: int, *, skip: int = 0, limit: int = 20
) -> list[Watcher]:
    """Newest-first page of a project's live (non-deleted) watchers.

    The handler passes ``limit + 1`` so it can detect a further page
    (``has_more``) without a COUNT.
    """
    db = get_db()
    try:
        return list(
            db.exec(
                select(Watcher)
                .where(
                    Watcher.project_id == project_id,
                    Watcher.status != "deleted",
                )
                .order_by(Watcher.created_at.desc())  # type: ignore[attr-defined]
                .offset(max(skip, 0))
                .limit(max(limit, 0))
            ).all()
        )
    finally:
        db.close()


def count_active_for_project(project_id: int) -> int:
    """Number of active watchers a project holds (the max_watchers gate input).

    Counts only ``status='active'`` — paused watchers consume no scheduler work
    and deleted ones are tombstones, so neither counts against the live cap.
    """
    db = get_db()
    try:
        return db.execute(
            select(func.count())
            .select_from(Watcher)
            .where(
                Watcher.project_id == project_id,
                Watcher.status == "active",
            )
        ).scalar_one()
    finally:
        db.close()


def apply_updates(
    watcher_id: str, project_id: int, updates: dict[str, Any]
) -> Optional[Watcher]:
    """Apply a validated PATCH to a non-deleted watcher; return the new row.

    A deleted (tombstoned) watcher is treated as gone (None -> 404); the flow
    has already resolved status/next_check_at into ``updates``.
    """
    db = get_db()
    try:
        watcher = db.exec(
            select(Watcher).where(
                Watcher.watcher_id == watcher_id,
                Watcher.project_id == project_id,
            )
        ).first()
        if watcher is None or watcher.status == "deleted":
            return None
        for key, value in updates.items():
            setattr(watcher, key, value)
        watcher.updated_at = _utcnow()
        db.add(watcher)
        db.commit()
        db.refresh(watcher)
        return watcher
    finally:
        db.close()


def delete_watcher(watcher_id: str, project_id: int) -> Optional[Watcher]:
    """Soft-delete a watcher (status='deleted', schedule cleared); idempotent.

    A terminal row is returned unchanged (idempotent DELETE); an unknown/foreign
    id returns None so the handler 404s. The poller never claims a non-active
    row, so clearing ``next_check_at`` is belt-and-suspenders.
    """
    db = get_db()
    try:
        watcher = db.exec(
            select(Watcher).where(
                Watcher.watcher_id == watcher_id,
                Watcher.project_id == project_id,
            )
        ).first()
        if watcher is None:
            return None
        if watcher.status != "deleted":
            watcher.status = "deleted"
            watcher.next_check_at = None
            watcher.updated_at = _utcnow()
            db.add(watcher)
            db.commit()
            db.refresh(watcher)
        return watcher
    finally:
        db.close()


# ── Scheduler (Task I.1) ─────────────────────────────────────────────────────


def claim_due_watchers(now: datetime, limit: int) -> list[ClaimedWatcher]:
    """Claim up to ``limit`` due active watchers, advancing each one's schedule.

    A single ``FOR UPDATE SKIP LOCKED`` select + per-row provisional advance,
    committed together: the claimed rows are pushed one interval into the future
    (floored at the hourly minimum) before the lock releases, so a concurrent
    poll skips them and the same check never fires twice. The flow overwrites
    ``next_check_at`` precisely once the render finishes.
    """
    batch = max(1, min(limit, MAX_CLAIM_BATCH))
    db = get_db()
    try:
        rows = db.exec(
            select(Watcher)
            .where(
                Watcher.status == "active",
                Watcher.next_check_at.is_not(None),  # type: ignore[union-attr]
                Watcher.next_check_at <= now,
            )
            .order_by(Watcher.next_check_at)  # type: ignore[arg-type]
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).all()

        claimed: list[ClaimedWatcher] = []
        for watcher in rows:
            interval = max(watcher.frequency_minutes, MIN_FREQUENCY_MINUTES)
            watcher.next_check_at = now + timedelta(minutes=interval)
            watcher.updated_at = now
            db.add(watcher)
            claimed.append(
                ClaimedWatcher(
                    watcher_id=watcher.watcher_id,
                    project_id=watcher.project_id,
                    url=watcher.url,
                    frequency_minutes=watcher.frequency_minutes,
                    consecutive_errors=watcher.consecutive_errors,
                    diff_mode=watcher.diff_mode,
                    track_fields=watcher.track_fields,
                    webhook_url=watcher.webhook_url,
                    notify_email=watcher.notify_email,
                )
            )
        db.commit()
        return claimed
    finally:
        db.close()


def latest_snapshot(watcher_id: str) -> Optional[WatcherSnapshot]:
    """Most recent snapshot of any kind (dashboard 'last check', Task I.4).

    Scoped by ``watcher_id`` only — which is globally unique and bound to one
    project, so this is project-safe by construction. The sole caller is the
    scheduler with a claimed (owned) id; any future I.4 handler must verify
    ownership via ``get_watcher_for_project`` before reading snapshots.
    """
    db = get_db()
    try:
        return db.exec(
            select(WatcherSnapshot)
            .where(WatcherSnapshot.watcher_id == watcher_id)
            .order_by(WatcherSnapshot.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()
    finally:
        db.close()


def latest_content_snapshot(watcher_id: str) -> Optional[WatcherSnapshot]:
    """Most recent snapshot that actually captured content (the diff baseline).

    Audit-only rows from blocked / low-quality renders carry a NULL
    ``content_hash`` (Task I.3 step 3); skipping them here means a fresh good
    render diffs against the last GOOD capture, never against a challenge page.
    """
    db = get_db()
    try:
        return db.exec(
            select(WatcherSnapshot)
            .where(
                WatcherSnapshot.watcher_id == watcher_id,
                WatcherSnapshot.content_hash.is_not(None),  # type: ignore[union-attr]
            )
            .order_by(WatcherSnapshot.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()
    finally:
        db.close()


def list_snapshots(watcher_id: str, limit: int = 20) -> list[WatcherSnapshot]:
    """Newest-first page of a watcher's snapshots (dashboard timeline, Task I.4).

    Scoped by ``watcher_id`` only — the handler verifies project ownership via
    ``get_watcher_for_project`` before calling this (the same contract as
    ``latest_snapshot``).
    """
    # The handler already clamps, but cap here too so a direct caller can never
    # issue an unbounded SELECT.
    safe_limit = max(1, min(limit, 200))
    db = get_db()
    try:
        return list(
            db.exec(
                select(WatcherSnapshot)
                .where(WatcherSnapshot.watcher_id == watcher_id)
                .order_by(WatcherSnapshot.created_at.desc())  # type: ignore[attr-defined]
                .limit(safe_limit)
            ).all()
        )
    finally:
        db.close()


def apply_successful_check(
    *,
    watcher_id: str,
    project_id: int,
    now: datetime,
    content_hash: Optional[str],
    snapshot_key: Optional[str],
    structured_data: Optional[dict[str, Any]],
    render_quality_score: Optional[float],
    has_changes: bool,
    similarity: Optional[float],
    changes: Optional[list],
    next_check_at: datetime,
) -> None:
    """Record a completed check: write the snapshot and reschedule the watcher.

    The snapshot row is always written (the check happened). The watcher
    counters are advanced ONLY if it is still active, so a PATCH/DELETE that
    landed during the render is never clobbered back into the schedule. The
    fetch is tenant-scoped (``project_id``) so every store write is uniformly
    project-bounded, matching the CRUD functions.
    """
    db = get_db()
    try:
        db.add(
            WatcherSnapshot(
                watcher_id=watcher_id,
                content_hash=content_hash,
                snapshot_key=snapshot_key,
                structured_data=structured_data,
                render_quality_score=render_quality_score,
                has_changes=has_changes,
                similarity=similarity,
                changes=changes,
                created_at=now,
            )
        )
        watcher = db.exec(
            select(Watcher).where(
                Watcher.watcher_id == watcher_id,
                Watcher.project_id == project_id,
            )
        ).first()
        if watcher is None:
            logger.warning(
                "apply_successful_check: watcher %s not found; snapshot "
                "written but schedule not updated",
                watcher_id,
            )
        elif watcher.status == "active":
            watcher.checks_count += 1
            watcher.last_check_at = now
            watcher.consecutive_errors = 0
            watcher.next_check_at = next_check_at
            if has_changes:
                watcher.last_change_at = now
            watcher.updated_at = now
            db.add(watcher)
        db.commit()
    finally:
        db.close()


def apply_failed_check(
    *,
    watcher_id: str,
    project_id: int,
    now: datetime,
    new_consecutive_errors: int,
    pause: bool,
    next_check_at: Optional[datetime],
) -> bool:
    """Record a failed check; pause the watcher when the error budget is spent.

    Returns True only when THIS call committed the pause (status was still
    active), so the flow emails the owner exactly once. A watcher a concurrent
    op already moved off 'active' is left untouched. Tenant-scoped by
    ``project_id`` for uniformity with the CRUD functions.
    """
    db = get_db()
    try:
        watcher = db.exec(
            select(Watcher).where(
                Watcher.watcher_id == watcher_id,
                Watcher.project_id == project_id,
            )
        ).first()
        if watcher is None or watcher.status != "active":
            return False
        watcher.checks_count += 1
        watcher.last_check_at = now
        watcher.consecutive_errors = new_consecutive_errors
        if pause:
            watcher.status = "paused"
            watcher.next_check_at = None
        else:
            watcher.next_check_at = next_check_at
        watcher.updated_at = now
        db.add(watcher)
        db.commit()
        return pause
    finally:
        db.close()
