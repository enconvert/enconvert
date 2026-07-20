"""V2 usage counters and storage accounting (Task F.5).

Coexistence rule 3 (V3 plan section 5): V2 quotas are SEPARATE counters
on ch_usage_periods — a /v2/perceive success bumps perceive_operations
and never touches V1's conversions_used. Rule 7 keeps shared utilities
read-only from V2, so the write-side lives here in v2_engine instead of
inside utils/subscription.py.

Storage accounting mirrors the V1 policy in monitoring/metrics.py:
output bytes count toward project.storage_used, the usage-period peak
is updated, and projects without a storage plan get every object
scheduled for retention cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text, update
from sqlmodel import select

from models import Project, UsagePeriod
from utils.postgres import get_db
from utils.subscription import update_storage_peak

logger = logging.getLogger(__name__)

# ── LLM cost ledger (migration 016) ──────────────────────────────────────
# llm_cost_cents is a Tier-1 money counter: every aggregate change writes a
# paired ch_usage_ledger row IN THE SAME TRANSACTION, so
# SUM(ledger deltas) == aggregate at every commit boundary. Two rows per
# extract call, with DISTINCT keys (a shared key would make the settle
# collide with the reserve and silently drop the downward reconciliation):
#   v2:llm:reserve:{usage_key}  delta = +reserve (the booked worst case)
#   v2:llm:settle:{usage_key}   delta = actual - reserved (normally <= 0)
# A crash between reserve and settle leaves ledger == aggregate == reserve
# (consistent, conservatively high) — the reconcile job sees NO drift for
# in-flight or crashed operations, by construction.
_INSERT_LLM_LEDGER_ROW = text(
    """
    INSERT INTO ch_usage_ledger
        (idempotency_key, project_id, usage_period_id, counter,
         event_type, delta_cost_cents, created_at)
    VALUES
        (:idempotency_key, :project_id, :period_id, 'llm_cost_cents',
         :event_type, :delta_cost_cents, :now)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
    """
)


def _current_period_clause(project_id: int, now: datetime) -> tuple[Any, ...]:
    return (
        UsagePeriod.project_id == project_id,
        UsagePeriod.period_start <= now,
        UsagePeriod.period_end > now,
    )


def get_period_llm_spend_cents(project_id: int) -> Optional[Decimal]:
    """Current period's accumulated LLM spend, for the F.6 budget gate.

    Returns None when the project has no active usage-period row —
    schema_llm treats that as fail-CLOSED (no period to account the
    spend on means no spend).
    """
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        period = db.exec(
            select(UsagePeriod).where(*_current_period_clause(project_id, now))
        ).first()
        if period is None:
            return None
        return Decimal(period.llm_cost_cents or 0)
    finally:
        db.close()


def reserve_llm_budget(
    project_id: int,
    reserve_cents: Decimal,
    cap_cents: Decimal,
    *,
    idempotency_key: str = "",
) -> Optional[int]:
    """Atomically gate AND book ``reserve_cents`` against the period cap
    (F.6 How step 6, hardened for concurrency; ledgered per migration 016).

    A SINGLE conditional UPDATE — ``SET llm_cost_cents = llm_cost_cents +
    :reserve WHERE <current period> AND llm_cost_cents + :reserve <=
    :cap`` — so the cap check and the booking commit together. No
    read-then-act window exists: N concurrent callers cannot collectively
    exceed the cap, because each one's reservation only succeeds while
    headroom remains. Returns the reserved period's primary key (so the
    caller can settle against that exact row), or None when no current
    period exists OR the reservation would breach the cap. Booking the
    worst-case cost up front and reconciling down (settle_llm_cost) bounds
    total period spend to the cap even under a crash between the two.

    ``idempotency_key`` (the caller's per-extract-call usage key) writes a
    ``v2:llm:reserve:{key}`` ledger row in the SAME transaction as the
    booking — committed together or not at all, so SUM(ledger) always
    equals the aggregate. A DUPLICATE reserve key fails CLOSED (returns
    None, no API call): the original reservation stands, and a second
    unbooked LLM call must never be made. Empty key = legacy
    unledgered behavior (kept for direct callers/tests; prod callers
    always pass one).
    """
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        ledger_period_id: Optional[int] = None
        if idempotency_key:
            period = db.execute(
                text(
                    "SELECT id FROM ch_usage_periods WHERE project_id = "
                    ":pid AND period_start <= :now AND period_end > :now "
                    "LIMIT 1"
                ),
                {"pid": project_id, "now": now},
            ).first()
            if period is None:
                return None
            ledger_period_id = int(period[0])
            gate = db.execute(
                _INSERT_LLM_LEDGER_ROW,
                {
                    "idempotency_key": f"v2:llm:reserve:{idempotency_key}",
                    "project_id": project_id,
                    "period_id": ledger_period_id,
                    "event_type": "llm_reserve",
                    "delta_cost_cents": reserve_cents,
                    "now": now,
                },
            ).first()
            if gate is None:
                db.rollback()
                logger.warning(
                    "reserve_llm_budget: duplicate reserve key %s for "
                    "project %s — failing closed (no second call)",
                    idempotency_key,
                    project_id,
                )
                return None
        # Keyed path: the booking UPDATE targets the EXACT row the ledger
        # row was written against (WHERE id = ledger_period_id) — never
        # re-resolve "current period" between the two statements, or a
        # plan change committing in between could land them on DIFFERENT
        # periods (permanent ledger/aggregate divergence on both). The
        # time-based clause remains only for the legacy unledgered path.
        if ledger_period_id is not None:
            where_clause = (UsagePeriod.id == ledger_period_id,)
        else:
            where_clause = _current_period_clause(project_id, now)
        result = db.execute(
            update(UsagePeriod)
            .where(
                *where_clause,
                UsagePeriod.llm_cost_cents + reserve_cents <= cap_cents,
            )
            .values(
                llm_cost_cents=UsagePeriod.llm_cost_cents + reserve_cents,
                updated_at=now,
            )
            .returning(UsagePeriod.id)
        )
        row = result.first()
        if row is None:
            # Cap reached: roll back so the ledger row vanishes WITH the
            # booking it would have described.
            db.rollback()
            return None
        db.commit()
        return int(row[0])
    finally:
        db.close()


def settle_llm_cost(
    period_id: int,
    reserved_cents: Decimal,
    actual_cents: Decimal,
    *,
    idempotency_key: str = "",
) -> bool:
    """Reconcile a prior reservation to the real cost (F.6 How step 6).

    Applies the delta ``actual - reserved`` (normally <= 0, releasing the
    unused hold) to the EXACT period row the reservation was booked
    against — keyed on the immutable primary key, so a billing-period
    rollover between reserve and settle cannot mis-credit a different
    period. Returns False when the row vanished (logged by the caller as
    a conservative over-count, never an under-count). A zero delta is a
    no-op (ledger and aggregate stay equal without a row).

    ``idempotency_key`` writes a ``v2:llm:settle:{key}`` ledger row in
    the same transaction as the delta. A DUPLICATE settle key no-ops and
    returns True: the original settle already reconciled this hold, so a
    replay must not double-apply the delta (the old code would have).
    """
    delta = actual_cents - reserved_cents
    if delta == 0:
        return True
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        if idempotency_key:
            owner = db.execute(
                text(
                    "SELECT project_id FROM ch_usage_periods "
                    "WHERE id = :period_id"
                ),
                {"period_id": period_id},
            ).first()
            if owner is None:
                return False
            gate = db.execute(
                _INSERT_LLM_LEDGER_ROW,
                {
                    "idempotency_key": f"v2:llm:settle:{idempotency_key}",
                    "project_id": int(owner[0]),
                    "period_id": period_id,
                    "event_type": "llm_settle",
                    "delta_cost_cents": delta,
                    "now": now,
                },
            ).first()
            if gate is None:
                db.rollback()
                logger.info(
                    "settle_llm_cost: duplicate settle key %s for period "
                    "%s — already reconciled, skipping",
                    idempotency_key,
                    period_id,
                )
                return True
        result = db.execute(
            update(UsagePeriod)
            .where(UsagePeriod.id == period_id)
            .values(
                llm_cost_cents=UsagePeriod.llm_cost_cents + delta,
                updated_at=now,
            )
        )
        if not result.rowcount:
            # Period row vanished mid-transaction: drop the settle
            # ledger row with the failed delta (never half-apply).
            db.rollback()
            return False
        db.commit()
        return True
    finally:
        db.close()


# V2 usage counters this module is allowed to bump. A whitelist keeps the
# dynamic setattr below from ever touching an arbitrary attribute.
_INCREMENTABLE_COUNTERS = frozenset(
    {
        "perceive_operations",
        "lookup_queries",
        "ingest_pages",
        "watch_checks",
        "distill_operations",
    }
)


def _increment_period_counter(project_id: int, counter: str, count: int) -> None:
    """Bump a single ch_usage_periods V2 counter for the current period.

    ONE atomic ``UPDATE ... SET counter = counter + :n`` — the same
    row-lock shape as reserve_llm_budget — replacing the old
    read-in-Python/+=/write-back, which lost updates when two operations
    for one project completed concurrently. Tier-2 counters are quota
    gates with no overage billing, so there is deliberately NO ledger row
    and NO idempotency key here (the migration-016 CHECK constraint
    enforces that ledger rows exist only for the two money counters).

    There is no V2 overage concept (hard 402 at the limit, enforced
    before the operation in api.deps.check_v2_quota); a missing current
    period is a no-op, matching utils.subscription.increment_conversion_usage.
    """
    if counter not in _INCREMENTABLE_COUNTERS:
        raise ValueError(f"unknown usage counter: {counter!r}")
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        column = getattr(UsagePeriod, counter)
        db.execute(
            update(UsagePeriod)
            .where(*_current_period_clause(project_id, now))
            .values(**{counter: column + count, "updated_at": now})
        )
        db.commit()
    finally:
        db.close()


def increment_perceive_usage(project_id: int, count: int = 1) -> None:
    """Bump ch_usage_periods.perceive_operations for the current period."""
    _increment_period_counter(project_id, "perceive_operations", count)


def increment_lookup_usage(project_id: int, count: int = 1) -> None:
    """Bump ch_usage_periods.lookup_queries for the current period (H.3)."""
    _increment_period_counter(project_id, "lookup_queries", count)


def increment_distill_usage(project_id: int, count: int = 1) -> None:
    """Bump ch_usage_periods.distill_operations for the period (H.5).

    One distill operation == one URL distilled (the handler reserves a
    fast units=1 gate; the flow re-checks per URL and increments here only
    for URLs that completed, so our own render failures never bill the
    caller)."""
    _increment_period_counter(project_id, "distill_operations", count)


def increment_ingest_usage(project_id: int, count: int = 1) -> None:
    """Bump ch_usage_periods.ingest_pages for the current period (H.7).

    One ingest page == one URL whose render + chunk + JSONL stage completed
    (the flow increments here only for completed pages, so our own render
    failures never bill the caller)."""
    _increment_period_counter(project_id, "ingest_pages", count)


def record_storage_and_retention(
    project_id: int,
    uploads: dict[str, dict[str, Any]],
    subscription: dict[str, Any],
) -> None:
    """Account uploaded artifact bytes and schedule retention cleanup.

    ``uploads`` maps output name -> {"key": object_key, "size_bytes": n}.
    Best-effort: accounting failures are logged, never raised — the
    perception already succeeded.
    """
    total_bytes = sum(
        int(entry.get("size_bytes", 0) or 0) for entry in uploads.values()
    )
    if total_bytes:
        try:
            db = get_db()
            try:
                project = db.exec(
                    select(Project).where(Project.id == project_id)
                ).first()
                if project is not None:
                    project.storage_used = (
                        project.storage_used or 0
                    ) + total_bytes
                    db.add(project)
                    db.commit()
                    update_storage_peak(project_id, project.storage_used)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("v2 storage accounting failed")

    # Same retention policy as V1: no storage plan -> objects expire.
    if subscription.get("plan_slug") == "admin":
        return
    if int(subscription.get("storage_bytes", 0) or 0) != 0:
        return
    retention_hours = int(subscription.get("file_retention_hours", 1) or 1)
    try:
        from utils.retention import schedule_file_cleanup

        for entry in uploads.values():
            key = entry.get("key")
            if key:
                schedule_file_cleanup(key, project_id, retention_hours)
    except Exception:  # noqa: BLE001 — scheduling must not fail the request
        logger.warning("v2 retention scheduling failed", exc_info=True)
