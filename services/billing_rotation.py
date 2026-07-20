"""Droplet-local usage-period rotation (no-GCP design).

Owner decision (Group I onward): NO Google services. Monthly usage-period
rotation was previously triggered ONLY by a Google Cloud Task scheduled at
period_end (utils/cloud_tasks.schedule_usage_period_rotation) — with Cloud
Tasks not running, periods never rolled over, get_current_usage_period
returned None, and every increment/quota check silently no-opped (usage
uncounted AND ungated). This module replaces that trigger with the same
poller shape as batch/ingest/watch workers (started from main.py lifespan).

Each tick scans ch_subscriptions for active rows whose current_period_end
has passed and rotates each:

* the ENTIRE rotation body is synchronous and runs in a worker thread
  (``asyncio.to_thread``) — never on the event loop. A sync FOR UPDATE
  wait on the loop while another rotator holds the lock across PayPal
  HTTP would wedge the whole process; in a thread it just waits.
* ``FOR UPDATE ... SKIP LOCKED`` on the subscription row — a concurrent
  rotation (second process, a manual /internal/rotate-usage-period call,
  an overlapping tick) simply skips: "another rotator owns it". The lock
  is held across the WHOLE rotation including the overage capture, and
  nothing commits mid-flight, so the charge + the period advance land in
  ONE transaction.
* overage capture is deduped at the DATABASE level: the capture row
  writes a synthetic ``overage:{project_id}:{period_start}`` value into
  ch_payment_history.paypal_transaction_id, which migration 016's
  partial UNIQUE index enforces — a second capture attempt for the same
  period cannot even insert its row. The marker is checked BEFORE
  calling PayPal. KNOWN RESIDUAL: a crash in the window between
  PayPal accepting the capture and the transaction committing loses the
  marker and would retry the charge next tick — rare, and the PayPal
  dashboard reconciles it; the full charge-intent (marker-first) pattern
  is documented future hardening in USAGE-TRACKING-DEPLOYMENT.md.
* pending plan change applied at the boundary (unchanged behavior).
* the new period row is ``INSERT ... ON CONFLICT (project_id,
  period_start) DO NOTHING`` — a pre-existing row means "this rotation
  already ran"; clobbering it would wipe real accrued usage. Contrast the
  backend's plan-change upsert, which uses DO UPDATE-with-reset because
  there the intent is "start the new plan's counts fresh".
* a sub that is MONTHS overdue (the pre-poller live state) walks forward
  one month per loop iteration until the period containing now, creating
  the interim (empty) period rows so boundaries stay contiguous. Bounded
  by _MAX_CATCHUP_MONTHS as a runaway valve.

Durability matches the watch worker: the schedule IS the data
(current_period_end), so downtime just means the first tick after boot
sweeps up everything overdue.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dateutil.relativedelta import relativedelta
from sqlalchemy import text
from sqlmodel import Session, select

from models import PaymentHistory, Plan, Subscription, UsagePeriod
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Rotation granularity is monthly; a 15-minute poll keeps a fresh period in
# place within minutes of the boundary at negligible cost (one indexed scan).
POLL_INTERVAL_SECONDS = 900
# Runaway valve for the catch-up walk (3 years of missed rotations).
_MAX_CATCHUP_MONTHS = 36

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_BASE_URL = (
    "https://api-m.paypal.com"
    if PAYPAL_MODE == "live"
    else "https://api-m.sandbox.paypal.com"
)

_worker_task: Optional[asyncio.Task] = None

# Every counter column is explicit (not left to DDL defaults): the prod
# schema has DEFAULTs from migrations 001/011, but create_all-bootstrapped
# dev DBs do not — raw INSERTs bypass SQLModel's Python-side defaults.
_INSERT_PERIOD_IF_ABSENT = text(
    """
    INSERT INTO ch_usage_periods
        (project_id, period_start, period_end, plan_id,
         conversions_used, overage_conversions, storage_bytes_peak,
         perceive_operations, ingest_pages, watch_checks,
         lookup_queries, distill_operations, llm_cost_cents, created_at)
    VALUES
        (:project_id, :period_start, :period_end, :plan_id,
         0, 0, 0, 0, 0, 0, 0, 0, 0, :now)
    ON CONFLICT (project_id, period_start) DO NOTHING
    """
)

_DUE_PROJECT_IDS = text(
    """
    SELECT project_id FROM ch_subscriptions
    WHERE status = 'active' AND current_period_end <= :now
    ORDER BY current_period_end
    LIMIT :batch
    """
)


def _overage_marker(project_id: int, period_start: datetime) -> str:
    """Deterministic dedup key for one period's overage capture.

    Stored in ch_payment_history.paypal_transaction_id, where migration
    016's partial UNIQUE index makes double-insertion impossible at the
    DB layer. Synthetic values can never collide with real PayPal
    transaction ids (which never contain ':').

    The timestamp is normalized to UTC before formatting: psycopg2 returns
    TIMESTAMPTZ in the SESSION timezone, and formatting that directly would
    make the marker digits depend on the server's timezone setting (breaking
    the email pass's marker-to-period lookup on non-UTC databases). Dedup is
    unaffected either way — check and insert always render through this same
    function within one process."""
    ts = period_start if period_start.tzinfo else period_start.replace(tzinfo=timezone.utc)
    return f"overage:{project_id}:{ts.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def _capture_overage(
    db: Session, sub: Subscription, plan: Plan, usage: UsagePeriod
) -> None:
    """Capture overage charges from PayPal for an ENDING period (sync).

    Runs inside the caller's FOR-UPDATE-locked transaction and NEVER
    commits — the caller's single final commit persists the charge record
    together with the period advance, atomically. (The old version
    committed here, which released the row lock mid-rotation and let a
    concurrent rotator double-charge.)

    Dedup: skips if this period's overage marker row already exists;
    writes the marker row on success. Only charges when overage is
    enabled, accrued, priced, and a PayPal subscription exists.
    """
    if not sub.overage_enabled:
        return
    if not sub.payment_subscription_id:
        return
    if usage.overage_conversions <= 0:
        return
    if plan.overage_rate_cents <= 0:
        return
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        logger.warning(
            "Cannot capture overage: PayPal credentials not configured in gateway"
        )
        return

    overage_amount = usage.overage_conversions * plan.overage_rate_cents / 100
    if overage_amount < 0.01:
        return

    marker = _overage_marker(sub.project_id, usage.period_start)
    already = db.exec(
        select(PaymentHistory).where(
            PaymentHistory.paypal_transaction_id == marker
        )
    ).first()
    if already:
        logger.info(
            "Overage for project %s period %s already captured (%s) — skipping",
            sub.project_id, usage.period_start, marker,
        )
        return

    amount_str = f"{overage_amount:.2f}"
    note = (
        f"Overage: {usage.overage_conversions} extra conversions "
        f"at ${plan.overage_rate_cents / 100:.3f}/each"
    )

    try:
        with httpx.Client(timeout=30.0) as client:
            token_resp = client.post(
                f"{PAYPAL_BASE_URL}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                logger.error(
                    "PayPal token request failed for overage capture: %s",
                    token_resp.text,
                )
                return

            token = token_resp.json()["access_token"]

            capture_resp = client.post(
                f"{PAYPAL_BASE_URL}/v1/billing/subscriptions/"
                f"{sub.payment_subscription_id}/capture",
                json={
                    "note": note,
                    "capture_type": "OUTSTANDING_BALANCE",
                    "amount": {"currency_code": "USD", "value": amount_str},
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            if capture_resp.status_code in (200, 202):
                logger.info(
                    "Overage capture of $%s accepted for project %s (%s conversions)",
                    amount_str, sub.project_id, usage.overage_conversions,
                )
                db.add(PaymentHistory(
                    project_id=sub.project_id,
                    paypal_transaction_id=marker,
                    paypal_subscription_id=sub.payment_subscription_id,
                    subscription_type="overage",
                    amount_value=amount_str,
                    amount_currency="USD",
                    status="CAPTURED",
                    payment_time=datetime.now(timezone.utc),
                ))
                # NO commit here — the caller's final commit is the only one.
            else:
                logger.error(
                    "Overage capture failed for project %s: HTTP %s - %s",
                    sub.project_id, capture_resp.status_code, capture_resp.text,
                )
    except Exception:
        logger.exception(
            "Exception during overage capture for project %s", sub.project_id
        )


def _rotate_sync(project_id: int) -> dict:
    """Advance one project's billing period(s) up to now (sync body).

    Runs in a worker thread. Safe to call concurrently and to re-call:
    SKIP LOCKED + the under-lock re-check + ON CONFLICT DO NOTHING make
    the whole function idempotent per boundary, and the single final
    commit keeps overage charge + period advance atomic.
    """
    db = get_db()
    try:
        sub = db.exec(
            select(Subscription)
            .where(
                Subscription.project_id == project_id,
                Subscription.status == "active",
            )
            .with_for_update(skip_locked=True)
        ).first()
        if not sub:
            # Either no active subscription, or another rotator holds the
            # row lock right now (SKIP LOCKED) — both are clean skips.
            return {"status": "skipped", "reason": "no active subscription or locked"}

        plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first()
        if not plan:
            return {"status": "skipped", "reason": "plan not found"}

        now = datetime.now(timezone.utc)

        # Re-check under the row lock: a concurrent rotation already
        # advanced this subscription.
        if sub.current_period_end > now:
            return {"status": "skipped", "reason": "period not yet ended"}

        rotations = 0
        while sub.current_period_end <= now and rotations < _MAX_CATCHUP_MONTHS:
            # Capture overage for the ending period BEFORE advancing —
            # in THIS transaction, deduped by the DB-unique marker row.
            ending_usage = db.exec(
                select(UsagePeriod).where(
                    UsagePeriod.project_id == project_id,
                    UsagePeriod.period_start == sub.current_period_start,
                )
            ).first()
            if ending_usage:
                _capture_overage(db, sub, plan, ending_usage)

            new_start = sub.current_period_end
            new_end = new_start + relativedelta(months=1)

            # Apply pending plan change at the first boundary
            if sub.pending_plan_id:
                pending_plan = db.exec(
                    select(Plan).where(Plan.id == sub.pending_plan_id)
                ).first()
                if pending_plan:
                    sub.plan_id = pending_plan.id
                    sub.effective_conversion_limit = pending_plan.conversion_limit
                    sub.effective_max_file_size = pending_plan.max_file_size
                    sub.effective_file_retention_hours = pending_plan.file_retention_hours
                    sub.effective_batch_limit = pending_plan.batch_limit
                    plan = pending_plan
                    logger.info(
                        "Applied pending plan change to %s for project %s",
                        pending_plan.slug, project_id,
                    )
                sub.pending_plan_id = None

            # Create the new usage period — DO NOTHING on conflict: an
            # existing row means this boundary was already rotated, and
            # overwriting it would wipe real accrued usage.
            db.execute(
                _INSERT_PERIOD_IF_ABSENT,
                {
                    "project_id": project_id,
                    "period_start": new_start,
                    "period_end": new_end,
                    "plan_id": plan.id,
                    "now": now,
                },
            )

            sub.current_period_start = new_start
            sub.current_period_end = new_end
            sub.updated_at = now
            db.add(sub)
            rotations += 1

        # THE single commit: overage PaymentHistory row(s), new period
        # row(s) and the subscription advance land together or not at all.
        db.commit()
        logger.info(
            "Rotated usage period for project %s (%d boundary(ies)): now %s -> %s",
            project_id, rotations, sub.current_period_start, sub.current_period_end,
        )
        return {"status": "ok", "rotations": rotations}
    finally:
        db.close()


async def rotate_project_period(project_id: int) -> dict:
    """Async wrapper: run the sync rotation body in a worker thread.

    Shared by the poller tick and the /internal/rotate-usage-period route.
    Never blocks the event loop — the FOR UPDATE wait, the PayPal HTTP
    calls and the commit all happen off-loop.
    """
    return await asyncio.to_thread(_rotate_sync, project_id)


def _due_project_ids(now: datetime, batch: int) -> list[int]:
    """Projects whose active subscription period has ended (sync)."""
    db = get_db()
    try:
        rows = db.execute(_DUE_PROJECT_IDS, {"now": now, "batch": batch}).all()
        return [int(r[0]) for r in rows]
    finally:
        db.close()


async def tick(now: Optional[datetime] = None, batch: int = 50) -> int:
    """One scan: rotate every due subscription. Returns the count.

    Shared by the poll loop and test harnesses (same convention as
    watch_worker.tick). A per-project failure is isolated so one bad
    subscription never blocks the rest. Non-ok outcomes for a DUE
    subscription are logged — a sub that skips every tick (e.g. missing
    plan row) would otherwise silently occupy a batch slot forever.
    """
    moment = now or datetime.now(timezone.utc)
    due = await asyncio.to_thread(_due_project_ids, moment, batch)
    rotated = 0
    for project_id in due:
        try:
            result = await rotate_project_period(project_id)
            if result.get("status") == "ok":
                rotated += 1
            else:
                logger.warning(
                    "billing rotation: due project %s not rotated: %s",
                    project_id, result.get("reason", "unknown"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad project must not stop the tick
            logger.exception(
                "billing rotation: rotate for project %s crashed", project_id
            )
    return rotated


async def _poll_loop() -> None:
    """Scan immediately, then every POLL_INTERVAL_SECONDS.

    The immediate first scan matters here more than for any other worker:
    the live DB has subscriptions that are MONTHS overdue (rotation was
    Cloud-Tasks-triggered and Cloud Tasks is not running), so the first
    boot with this worker performs the catch-up.
    """
    while True:
        try:
            rotated = await tick()
            if rotated:
                logger.info("billing rotation: rotated %d subscription(s)", rotated)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.exception("billing rotation: poll tick crashed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def startup() -> None:
    """Lifespan hook: start the poller."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(
            _poll_loop(), name="billing-rotation-worker"
        )


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
