"""V2 usage recording and storage accounting (Task F.5; unified ops 029).

Since migration 029 every V2 op bills THE unified counter
(ch_usage_periods.ops_used) through the single ledgered choke point
utils.usage_ledger.record_op_usage; the per-endpoint columns
(perceive_operations, lookup_queries, distill_operations, ingest_pages)
survive only as telemetry BREAKDOWNS bumped in the same atomic UPDATE.
The increment_* wrappers here keep the flow-facing surface stable and own
the per-endpoint idempotency-key shapes. Coexistence rule 7 still keeps
V2 write-paths in v2_engine: the LLM reserve/settle two-phase writes live
here, not in utils/.

Storage accounting mirrors the V1 policy in monitoring/metrics.py:
output bytes count toward project.storage_used, the usage-period peak
is updated, and projects without a storage plan get every object
scheduled for retention cleanup.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text, update
from sqlmodel import select

from models import Project, UsagePeriod
from utils.postgres import get_db
from utils.subscription import update_storage_peak
from utils.usage_ledger import record_op_usage

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
    cap_cents: Optional[Decimal] = None,
    *,
    idempotency_key: str = "",
    unlimited: bool = False,
) -> Optional[int]:
    """Atomically gate AND book ``reserve_cents`` against the period's AI
    credits (F.6 How step 6, hardened for concurrency; ledgered per 016).

    PERIOD CEILING (contract, migration 029): the period cap is the row's
    own ``ai_credits_granted_cents`` — the plan's monthly AI-credit grant
    (subscription override wins) plus full rollover of the previous
    period's unspent credits, materialized at period creation. The old
    slug-keyed $5/$20 constants no longer gate anything at period level.
    Remaining credits = ai_credits_granted_cents - llm_cost_cents.

    ``cap_cents`` survives for the existing callers but is a PER-REQUEST
    ceiling only: a reservation larger than it fails closed (returns
    None, no API call). Pass None to skip that check.

    ``unlimited=True`` (admin projects: ADMIN_SUBSCRIPTION has no real
    plan row, so its periods carry 0 granted credits) still BOOKS the
    cost and writes the ledger row but skips the credit ceiling —
    replacing the pre-029 slug-keyed elevated cap that kept admin LLM
    extraction usable. Callers must derive it from plan_slug == "admin"
    only; paying plans always gate on their credits.

    A SINGLE conditional UPDATE — ``SET llm_cost_cents = llm_cost_cents +
    :reserve WHERE <period row> AND llm_cost_cents + :reserve <=
    ai_credits_granted_cents`` — so the credit check and the booking
    commit together. No read-then-act window exists: N concurrent callers
    cannot collectively exceed the credits, because each one's reservation
    only succeeds while headroom remains. Returns the reserved period's
    primary key (so the caller can settle against that exact row), or None
    when no current period exists OR the reservation would breach the
    credits. Booking the worst-case cost up front and reconciling down
    (settle_llm_cost) bounds total period spend to the credits even under
    a crash between the two.

    ``idempotency_key`` (the caller's per-extract-call usage key) writes a
    ``v2:llm:reserve:{key}`` ledger row in the SAME transaction as the
    booking — committed together or not at all, so SUM(ledger) always
    equals the aggregate. A DUPLICATE reserve key fails CLOSED (returns
    None, no API call): the original reservation stands, and a second
    unbooked LLM call must never be made. Empty key = legacy
    unledgered behavior (kept for direct callers/tests; prod callers
    always pass one).
    """
    if cap_cents is not None and reserve_cents > cap_cents:
        # Per-request ceiling breached — never book, never call.
        return None
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
        if not unlimited:
            # Column-vs-column on the SAME row: the materialized
            # credit grant is the ceiling (migration 029).
            where_clause = (
                *where_clause,
                UsagePeriod.llm_cost_cents + reserve_cents
                <= UsagePeriod.ai_credits_granted_cents,
            )
        result = db.execute(
            update(UsagePeriod)
            .where(*where_clause)
            .values(
                llm_cost_cents=UsagePeriod.llm_cost_cents + reserve_cents,
                updated_at=now,
            )
            .returning(UsagePeriod.id)
        )
        row = result.first()
        if row is None:
            # Credits exhausted: roll back so the ledger row vanishes WITH
            # the booking it would have described.
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


def _record_v2_op(
    project_id: int,
    *,
    prefix: str,
    event_type: str,
    breakdown: Optional[str],
    idempotency_key: Optional[str],
    count: int,
) -> None:
    """Bill ``count`` ops through the unified ledger choke point.

    Every V2 op is Tier-1 money since migration 029: the write goes
    through utils.usage_ledger.record_op_usage (ledger row gates the
    aggregate bump of ops_used + the per-endpoint breakdown column in ONE
    transaction). ``idempotency_key`` is the caller's NATURAL id
    (operation_id, '{job_id}:{md5(url)[:16]}', ...) which this helper
    folds into the contract key shape 'v2:op:<endpoint>:<natural id>'; a
    key already carrying the 'v2:op:' prefix is used verbatim so callers
    that build the full key stay correct. None falls back to uuid4 —
    still ledgered for audit, just never deduplicable, so a retried call
    double-counts exactly like the pre-ledger code (accepted where no
    natural id is threadable to the increment site).

    A missing current period stays a logged no-op inside record_op_usage
    (pre-existing behavior; the rotation poller is the fix).
    """
    if idempotency_key is None:
        key = f"{prefix}unkeyed:{uuid.uuid4().hex}"
    elif idempotency_key.startswith("v2:op:"):
        key = idempotency_key
    else:
        key = f"{prefix}{idempotency_key}"
    record_op_usage(
        project_id=project_id,
        idempotency_key=key,
        event_type=event_type,
        units=count,
        breakdown=breakdown,
    )


def increment_perceive_usage(
    project_id: int, idempotency_key: Optional[str] = None, count: int = 1
) -> None:
    """Bill perceive ops (1/URL; cache hits bill — existing behavior).
    Natural key: the operation id -> 'v2:op:perceive:{operation_id}'."""
    _record_v2_op(
        project_id,
        prefix="v2:op:perceive:",
        event_type="v2_perceive",
        breakdown="perceive_operations",
        idempotency_key=idempotency_key,
        count=count,
    )


def increment_lookup_usage(
    project_id: int, idempotency_key: Optional[str] = None, count: int = 1
) -> None:
    """Bill lookup ops (H.3): 1/query, PLUS the auto-perceive enrichment
    ops billed separately through increment_perceive_usage (compounding
    kept — founder decision 2026-08-13)."""
    _record_v2_op(
        project_id,
        prefix="v2:op:lookup:",
        event_type="v2_lookup",
        breakdown="lookup_queries",
        idempotency_key=idempotency_key,
        count=count,
    )


def increment_distill_usage(
    project_id: int, idempotency_key: Optional[str] = None, count: int = 1
) -> None:
    """Bill distill ops (H.5): one per COMPLETED URL (the handler reserves
    a fast units gate; the flow re-checks per URL and bills here only for
    URLs that completed, so our own render failures never bill the
    caller). Natural key: '{operation_id}:{md5(url)[:16]}'."""
    _record_v2_op(
        project_id,
        prefix="v2:op:distill:",
        event_type="v2_distill",
        breakdown="distill_operations",
        idempotency_key=idempotency_key,
        count=count,
    )


def increment_ingest_usage(
    project_id: int, idempotency_key: Optional[str] = None, count: int = 1
) -> None:
    """Bill ingest ops (H.7): one per page whose render + chunk + JSONL
    stage completed (the flow bills only completed pages, so our own
    render failures never bill the caller). Natural key:
    '{job_id}:{md5(url)[:16]}'."""
    _record_v2_op(
        project_id,
        prefix="v2:op:ingest:",
        event_type="v2_ingest",
        breakdown="ingest_pages",
        idempotency_key=idempotency_key,
        count=count,
    )


def increment_discover_usage(
    project_id: int, idempotency_key: Optional[str] = None
) -> None:
    """Bill /v2/discover: 1 op per call (deliberate change with migration
    029 — discover was previously uncounted). No breakdown column exists
    for discover, so only the unified ops_used moves; the ledger row
    (event_type='v2_discover') is the per-endpoint audit trail. Discover
    has no natural operation id, so the default key is uuid4-based
    ('v2:op:discover:{uuid4}' per the contract) — per-call billing, no
    dedup possible or needed."""
    _record_v2_op(
        project_id,
        prefix="v2:op:discover:",
        event_type="v2_discover",
        breakdown=None,
        idempotency_key=idempotency_key,
        count=1,
    )


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
