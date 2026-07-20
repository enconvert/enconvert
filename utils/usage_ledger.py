"""Append-only usage ledger for the money-critical V1 conversion counter.

Ledger + aggregate design (migration 016): ch_usage_ledger is the
source of truth, ch_usage_periods.conversions_used is the denormalized
aggregate for fast reads. The UNIQUE index on idempotency_key is the
entire dedup mechanism.

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

Scope: this module handles ``conversions_used`` ONLY. The other
Tier-1 counter, ``llm_cost_cents``, keeps its ledger writes inside
services/v2_engine/usage.py (reserve/settle are cap-gated two-phase
writes that cannot flow through this generic single-delta shape, and
coexistence rule 7 keeps V2 write-paths in v2_engine). Tier-2
operational counters get atomic UPDATEs with no ledger by design —
the migration's CHECK constraint enforces that at the DB layer.

KNOWN RESIDUAL (unchanged from the pre-ledger code): when a project has
no current usage-period row, the event is logged and DROPPED — the
conversion stays free and ungated, exactly as before. The fix for that
is guaranteed period provisioning (the rotation poller), not the ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from utils.postgres import get_db

logger = logging.getLogger(__name__)

_FIND_CURRENT_PERIOD = text(
    """
    SELECT id FROM ch_usage_periods
    WHERE project_id = :project_id
      AND period_start <= :now
      AND period_end > :now
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
        (:idempotency_key, :project_id, :period_id, 'conversions_used',
         :event_type, :delta_units, CAST(:context AS JSONB), :now)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
    """
)

# Runs ONLY in the transaction that won the gate. Every SET-clause RHS
# reads the OLD row tuple (Postgres UPDATE semantics), so
# ``conversions_used + :count`` is the new total in both expressions and
# overage derives from it atomically under the same row lock. The limit
# is a correlated scalar subquery against ch_subscriptions
# (project_id is UNIQUE there) — ch_usage_periods itself carries no
# limit column since migration 007. COALESCE(100) mirrors the free-plan
# fallback in api/deps.check_conversion_limit. Atomic w.r.t. concurrent
# conversions (same-row lock); NOT atomic w.r.t. a concurrent plan-limit
# change — identical tolerance to the pre-ledger code.
_BUMP_CONVERSIONS = text(
    """
    UPDATE ch_usage_periods
    SET conversions_used = conversions_used + :count,
        overage_conversions = GREATEST(
            conversions_used + :count - COALESCE(
                (SELECT s.effective_conversion_limit
                 FROM ch_subscriptions s
                 WHERE s.project_id = ch_usage_periods.project_id),
                100
            ),
            0
        ),
        updated_at = :now
    WHERE id = :period_id
    RETURNING conversions_used, overage_conversions
    """
)


def record_conversion_usage(
    *,
    project_id: int,
    idempotency_key: str,
    count: int = 1,
    event_type: str = "v1_conversion",
    context: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Record one conversion-usage event exactly once.

    Returns ``{"conversions_used": n, "overage_conversions": m}`` when
    this call performed the count, or ``None`` when the event was a
    duplicate (already counted) or no current usage period exists
    (logged and dropped — pre-existing behavior, see module docstring).

    ``context`` is an optional JSON string stored on the ledger row for
    audit (e.g. '{"activity_id": 123}').
    """
    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        period_row = db.execute(
            _FIND_CURRENT_PERIOD,
            {"project_id": project_id, "now": now},
        ).first()
        if period_row is None:
            logger.warning(
                "usage ledger: no current usage period for project %s — "
                "conversion event %s DROPPED (uncounted, pre-existing "
                "behavior; rotation poller is the fix)",
                project_id,
                idempotency_key,
            )
            return None
        period_id = int(period_row[0])

        gate = db.execute(
            _INSERT_LEDGER_ROW,
            {
                "idempotency_key": idempotency_key,
                "project_id": project_id,
                "period_id": period_id,
                "event_type": event_type,
                "delta_units": count,
                "context": context,
                "now": now,
            },
        ).first()
        if gate is None:
            # Duplicate delivery: the original already counted it.
            db.commit()
            logger.info(
                "usage ledger: duplicate conversion event %s for project "
                "%s — skipped",
                idempotency_key,
                project_id,
            )
            return None

        totals = db.execute(
            _BUMP_CONVERSIONS,
            {"count": count, "period_id": period_id, "now": now},
        ).first()
        db.commit()
        return {
            "conversions_used": int(totals[0]),
            "overage_conversions": int(totals[1]),
        }
    finally:
        # Closing an uncommitted session rolls back, so an exception
        # between the INSERT and the UPDATE leaves NO half-applied state
        # (no ledger row without its aggregate bump, or vice versa).
        db.close()
