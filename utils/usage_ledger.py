"""Append-only usage ledger for the money-critical unified ops counter.

Ledger + aggregate design (migrations 016 + 029): ch_usage_ledger is the
source of truth, ch_usage_periods.ops_used is the denormalized aggregate
for fast reads. The UNIQUE index on idempotency_key is the entire dedup
mechanism. Since migration 029 every billable unit of work across every
endpoint (V1 conversions, perceive, lookup, distill, ingest, discover)
flows through ONE choke point here — record_op_usage — writing
counter='ops_used' rows; 'conversions_used' rows remain valid history
from the pre-unified V1 writes but are never written again.

The one invariant everything hangs on:

    a ledger row exists  <=>  the aggregate was bumped by that row's delta

which requires (a) the ledger INSERT and the aggregate UPDATE to commit
in ONE transaction, and (b) the INSERT to run FIRST — it is the gate.
If the aggregate ran first, two concurrent callers with the same
idempotency_key would both bump the aggregate and only the second
ledger INSERT would no-op, by which point the double-count already
happened. With the INSERT first, ON CONFLICT (idempotency_key)
DO NOTHING RETURNING id under READ COMMITTED means: of two racers, the
second blocks on the unique index until the first commits, then gets
zero rows back and never touches the aggregate.

Scope: this module handles ``ops_used`` (and its per-endpoint breakdown
columns) ONLY. The other Tier-1 counter, ``llm_cost_cents``, keeps its
ledger writes inside services/v2_engine/usage.py (reserve/settle are
cap-gated two-phase writes that cannot flow through this generic
single-delta shape, and coexistence rule 7 keeps V2 write-paths in
v2_engine).

CLOSED HOLE (was "KNOWN RESIDUAL: event logged and DROPPED"): when a
project has no current usage-period row (a subscription created without
one, or the rotation poller not yet caught up past a boundary),
_ensure_current_period provisions the row in the SAME transaction as the
ledger insert, executing the billing rotation's own INSERT statement
(imported, not copied — the SQL cannot drift) with the identical
ON CONFLICT (project_id, period_start) DO NOTHING semantics. Only a
project with no ch_subscriptions row at all still cannot be counted
(logged at ERROR, returns False); for that same case the quota gate
(api.deps.check_ops_quota) fails CLOSED with 402, so such a project can
no longer run ops for free. The subscription cursor
(ch_subscriptions.current_period_*) is never advanced here — the
rotation poller stays its sole owner, and its DO NOTHING treats rows
provisioned here as "this boundary already rotated".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta
from sqlalchemy import text
from sqlmodel import Session

from services.billing_rotation import _INSERT_PERIOD_IF_ABSENT, _MAX_CATCHUP_MONTHS
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Per-endpoint telemetry breakdowns of ops_used (migration 030 keeps these
# as columns; they are NOT caps). The whitelist keeps the SQL interpolation
# below from ever touching an arbitrary column.
_BREAKDOWN_COLUMNS = frozenset(
    {
        "conversions_used",
        "perceive_operations",
        "ingest_pages",
        "lookup_queries",
        "distill_operations",
    }
)

# App-validated event_type vocabulary for ops rows (llm_reserve/llm_settle
# live in v2_engine; plan_change_reset is written by the backend).
_OP_EVENT_TYPES = frozenset(
    {
        "v1_conversion",
        "v2_perceive",
        "v2_lookup",
        "v2_distill",
        "v2_ingest",
        "v2_discover",
    }
)

_FIND_CURRENT_PERIOD = text(
    """
    SELECT id FROM ch_usage_periods
    WHERE project_id = :project_id
      AND period_start <= :now
      AND period_end > :now
    LIMIT 1
    """
)

# The billing window as the subscription row declares it. project_id is
# UNIQUE on ch_subscriptions (models.Subscription), but ORDER BY id DESC
# matches the defensive convention the alert path in api/deps.py uses.
_FIND_SUBSCRIPTION_WINDOW = text(
    """
    SELECT current_period_start, current_period_end, plan_id
    FROM ch_subscriptions
    WHERE project_id = :project_id
    ORDER BY id DESC
    LIMIT 1
    """
)

# THE GATE. Zero rows back == this exact event was already counted.
_INSERT_LEDGER_ROW = text(
    """
    INSERT INTO ch_usage_ledger
        (idempotency_key, project_id, usage_period_id, counter,
         event_type, delta_units, context, created_at)
    VALUES
        (:idempotency_key, :project_id, :period_id, 'ops_used',
         :event_type, :delta_units, CAST(:context AS JSONB), :now)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
    """
)

# Runs ONLY in the transaction that won the gate. Every SET-clause RHS
# reads the OLD row tuple (Postgres UPDATE semantics), so
# ``ops_used + :units`` is the new total in both expressions and overage
# derives from it atomically under the same row lock. The limit is a
# correlated scalar subquery resolving the effective ops cap exactly like
# the read path (utils.subscription.get_subscription): subscription
# override first, then the materialized effective value (NULLIF heals the
# 0 rows created between the 029 apply and this build's deploy), then the
# plan cap. A resolved NULL (no subscription) or 0 (unlimited) means "no
# overage tracked": the outer COALESCE substitutes the new total itself as
# the limit, so GREATEST(total - total, 0) pins overage_ops at 0 without
# a second subquery evaluation. Atomic w.r.t. concurrent ops (same-row
# lock); NOT atomic w.r.t. a concurrent plan-limit change — identical
# tolerance to the pre-unified code.
#
# {breakdown_set} is filled from the _BREAKDOWN_COLUMNS whitelist only —
# never from caller input directly.
_BUMP_OPS_TEMPLATE = """
    UPDATE ch_usage_periods
    SET ops_used = ops_used + :units,{breakdown_set}
        overage_ops = GREATEST(
            ops_used + :units - COALESCE(
                NULLIF(
                    (SELECT COALESCE(s.override_ops_month,
                                     NULLIF(s.effective_ops_month, 0),
                                     p.ops_month)
                     FROM ch_subscriptions s
                     JOIN ch_plans p ON p.id = s.plan_id
                     WHERE s.project_id = ch_usage_periods.project_id),
                    0),
                ops_used + :units),
            0),
        updated_at = :now
    WHERE id = :period_id
    RETURNING ops_used, overage_ops
"""


def _bump_ops_sql(breakdown: Optional[str]) -> text:
    """Build the aggregate UPDATE, optionally bumping one breakdown column
    in the SAME statement (same row lock, same transaction)."""
    breakdown_set = ""
    if breakdown is not None:
        # Whitelist re-checked here so no future caller can bypass it.
        if breakdown not in _BREAKDOWN_COLUMNS:
            raise ValueError(f"unknown ops breakdown column: {breakdown!r}")
        breakdown_set = f"\n        {breakdown} = {breakdown} + :units,"
    return text(_BUMP_OPS_TEMPLATE.format(breakdown_set=breakdown_set))


def _provision_windows(
    period_start: datetime, period_end: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Pure window math for _ensure_current_period.

    Given the subscription's declared billing window, return the list of
    period windows to provision, in ascending order, ending with the one
    containing ``now``. Mirrors the rotation walk in
    services.billing_rotation._rotate_sync exactly: a stale window rolls
    forward one month per step from current_period_end (creating the
    interim empty windows so boundaries stay contiguous and the
    ai-credit carryover chain in the INSERT stays intact), bounded by the
    rotation's own _MAX_CATCHUP_MONTHS runaway valve.

    Returns [] when no window containing ``now`` can be derived: the
    subscription window starts in the future, or the walk exhausts the
    valve without reaching ``now`` — both mean "cannot provision".

    Naive datetimes are treated as UTC (matches _overage_marker's
    tolerance for create_all-bootstrapped dev DBs).
    """
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=timezone.utc)
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    if period_start > now:
        return []
    if period_end > now:
        return [(period_start, period_end)]
    windows: list[tuple[datetime, datetime]] = []
    start, end = period_start, period_end
    steps = 0
    while end <= now and steps < _MAX_CATCHUP_MONTHS:
        start, end = end, end + relativedelta(months=1)
        windows.append((start, end))
        steps += 1
    if end <= now:
        return []
    return windows


def _ensure_current_period(
    db: Session, project_id: int, now: datetime
) -> Optional[int]:
    """Provision the missing current usage-period row for ``project_id``.

    Returns the current period's id, or None when provisioning is
    impossible: no ch_subscriptions row exists for the project, the
    subscription window starts in the future, or the window is more than
    _MAX_CATCHUP_MONTHS stale.

    Executes _INSERT_PERIOD_IF_ABSENT — the billing rotation's INSERT,
    imported so the two paths can never drift — once per rolled window.
    NEVER commits: runs inside the caller's transaction so (in the ledger
    path) the period row, the ledger row and the aggregate bump land
    atomically. Concurrency: two racing provisioners collide on the
    UNIQUE (project_id, period_start) constraint; the loser blocks until
    the winner commits, DO-NOTHINGs, and the follow-up SELECT (a new
    READ COMMITTED snapshot) sees the winner's row.

    Deliberately does NOT advance ch_subscriptions.current_period_* and
    does NOT apply pending plan changes or capture overage — those stay
    with the rotation poller, which will sweep this subscription on its
    next tick and find the period row(s) already in place.
    """
    sub_row = db.execute(
        _FIND_SUBSCRIPTION_WINDOW, {"project_id": project_id}
    ).first()
    if sub_row is None:
        return None
    windows = _provision_windows(sub_row[0], sub_row[1], now)
    if not windows:
        return None
    plan_id = int(sub_row[2])
    for window_start, window_end in windows:
        db.execute(
            _INSERT_PERIOD_IF_ABSENT,
            {
                "project_id": project_id,
                "period_start": window_start,
                "period_end": window_end,
                "plan_id": plan_id,
                "now": now,
            },
        )
    row = db.execute(
        _FIND_CURRENT_PERIOD, {"project_id": project_id, "now": now}
    ).first()
    return int(row[0]) if row is not None else None


def record_op_usage(
    *,
    project_id: int,
    idempotency_key: str,
    event_type: str,
    units: int = 1,
    breakdown: Optional[str] = None,
    context: Optional[str] = None,
) -> bool:
    """Record ``units`` ops against the current usage period, exactly once
    per ``idempotency_key``. THE single write path for the unified counter.

    Returns True when this call performed the count, False when the event
    was a duplicate (already counted) or the project has no
    ch_subscriptions row at all (logged at ERROR and uncounted — the
    quota gate fails closed for that same case). A merely-missing period
    row is no longer a drop: _ensure_current_period provisions it in
    this same transaction (see module docstring).

    ``event_type`` must be one of the app-validated op vocabulary
    (v1_conversion / v2_perceive / v2_lookup / v2_distill / v2_ingest /
    v2_discover). ``breakdown`` names the per-endpoint telemetry column to
    bump alongside ops_used (one of conversions_used / perceive_operations
    / ingest_pages / lookup_queries / distill_operations); None (discover)
    bumps only the unified counter. ``context`` is an optional JSON string
    stored on the ledger row for audit (e.g. '{"activity_id": 123}').
    """
    if event_type not in _OP_EVENT_TYPES:
        raise ValueError(f"unknown ops event_type: {event_type!r}")
    bump_sql = _bump_ops_sql(breakdown)

    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        period_row = db.execute(
            _FIND_CURRENT_PERIOD,
            {"project_id": project_id, "now": now},
        ).first()
        if period_row is None:
            # Provision the missing period in THIS transaction (same
            # commit as the ledger row + aggregate bump). None means no
            # subscription window to derive it from — nothing to bill
            # against, so the event stays uncounted; the quota gate
            # (check_ops_quota) fails closed for the same condition.
            provisioned = _ensure_current_period(db, project_id, now)
            if provisioned is None:
                logger.error(
                    "usage ledger: no usage period for project %s and none "
                    "provisionable (no ch_subscriptions row / future or "
                    ">%d-months-stale window) — ops event %s UNCOUNTED",
                    project_id,
                    _MAX_CATCHUP_MONTHS,
                    idempotency_key,
                )
                return False
            period_id = provisioned
        else:
            period_id = int(period_row[0])

        gate = db.execute(
            _INSERT_LEDGER_ROW,
            {
                "idempotency_key": idempotency_key,
                "project_id": project_id,
                "period_id": period_id,
                "event_type": event_type,
                "delta_units": units,
                "context": context,
                "now": now,
            },
        ).first()
        if gate is None:
            # Duplicate delivery: the original already counted it.
            db.commit()
            logger.info(
                "usage ledger: duplicate ops event %s for project %s — "
                "skipped",
                idempotency_key,
                project_id,
            )
            return False

        db.execute(
            bump_sql,
            {"units": units, "period_id": period_id, "now": now},
        )
        db.commit()
        return True
    finally:
        # Closing an uncommitted session rolls back, so an exception
        # between the INSERT and the UPDATE leaves NO half-applied state
        # (no ledger row without its aggregate bump, or vice versa).
        db.close()
