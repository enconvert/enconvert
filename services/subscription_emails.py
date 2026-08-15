"""Subscription lifecycle email pass (droplet systemd design, no GCP).

Run by scripts/ops/rotate_usage_periods.py AFTER the billing-rotation drain,
under the enconvert-billing-rotation systemd timer. Four email types:

    overage_receipt   receipt for a captured PayPal overage charge
    renewal           a new billing period started (quota reset)
    upcoming_charge   ~2 days before PayPal charges the payment method
    storage_lapse     cancelled storage add-on expires within ~3 days

Dedup contract (ch_email_log, migration 019): INSERT ... ON CONFLICT
(email_key) DO NOTHING RETURNING id is the claim, COMMITTED BEFORE the Brevo
send so overlapping runs (timer + manual) can never both send the same key.
The Brevo senders return False instead of raising, so a claim whose send
failed is retried by the retry sub-pass (bounded attempts, staleness-aware)
rather than lost.

KNOWN RESIDUAL: a Brevo false-negative (timeout after acceptance) followed by
a retry can duplicate an email. That is an annoyance, not money — this module
never charges anyone (billing_rotation._capture_overage owns charging, with
its own DB-unique dedup).

Legacy column: on a successful storage_lapse send the pass also stamps
ch_subscriptions.storage_lapse_warned_at (migration 009). That column is what
the dormant backend sweep (backend/scripts/ops/send_scheduled_emails.py)
dedups on, so stamping keeps that script a guaranteed no-op if its timer ever
appears. It is never used for candidate FILTERING here — ch_email_log.email_key
governs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from sqlmodel import Session

from utils import email_notifier
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Retry sub-pass bounds (interpolated into the _RETRYABLE SQL below — they are
# module-owned ints, not user input). Receipts get the tightest cap: a
# duplicated receipt for a single charge actively alarms users, while a
# missing one is benign (the charge is visible in the dashboard payment
# history). The grace period doubles as retry SPACING: a row is only
# retryable once it is at least that old, so attempts land on successive
# timer fires instead of burning the whole budget inside one Brevo outage.
RETRY_WINDOW_HOURS = 48
RETRY_GRACE_MINUTES = 15
DEFAULT_RETRY_CAP = 3
RETRY_CAPS = {"overage_receipt": 2}

_DATE_FMT = "%b %d, %Y"


def _aware(dt: datetime) -> datetime:
    """Defensive tz guard: TIMESTAMPTZ columns come back aware from psycopg2,
    but create_all-bootstrapped scratch DBs (bare TIMESTAMP) do not."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _key_ts(dt: datetime) -> str:
    """Canonical UTC timestamp fragment for email keys.

    Always astimezone(utc) BEFORE strftime — never format a datetime whose
    timezone you did not choose (the marker-derived receipt key is the one
    deliberate exception: it reuses the stored string verbatim).
    """
    return _aware(dt).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _fmt_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime(_DATE_FMT)


def _humanize_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass
class EmailCandidate:
    """One email the pass wants to send; produced by the pure builders."""

    project_id: int
    email_type: str
    email_key: str
    # Everything the template needs, JSON-serializable: the retry sub-pass
    # rebuilds the email from this alone (business tables may have moved on).
    context: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure candidate builders (unit-testable without a DB). Each takes a plain
# dict row (as returned by .mappings()) plus the aware-UTC `now`, and returns
# an EmailCandidate or None when the row should produce no email.
# ---------------------------------------------------------------------------


def build_storage_lapse_candidate(row: dict, now: datetime) -> Optional[EmailCandidate]:
    period_end = _aware(row["storage_period_end"])
    if period_end <= now:
        return None
    return EmailCandidate(
        project_id=row["project_id"],
        email_type="storage_lapse",
        email_key=f"storage_lapse:{row['project_id']}:{_key_ts(period_end)}",
        context={
            "project_name": row["project_name"],
            "storage_period_end": period_end.isoformat(),
            "storage_used_str": _humanize_bytes(int(row["storage_used"] or 0)),
            "event_at": period_end.isoformat(),
        },
    )


def parse_overage_marker(marker: str) -> Optional[datetime]:
    """Recover the period_start encoded in an overage dedup marker.

    Marker shape (billing_rotation): ``overage:{project_id}:{%Y%m%d%H%M%S}``,
    rendered from the aware-UTC period_start. Returns None on any mismatch —
    the receipt then simply omits the conversion count.
    """
    parts = marker.split(":")
    if len(parts) != 3 or parts[0] != "overage":
        return None
    try:
        return datetime.strptime(parts[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_overage_receipt_candidate(row: dict, now: datetime) -> Optional[EmailCandidate]:
    payment_time = _aware(row["payment_time"]) if row["payment_time"] else now
    return EmailCandidate(
        project_id=row["project_id"],
        email_type="overage_receipt",
        # Reuses the DB-unique marker verbatim: immune to reformatting drift.
        email_key=f"overage_receipt:{row['paypal_transaction_id']}",
        context={
            "amount": row["amount_value"],
            "currency": row["amount_currency"],
            "charged_on": payment_time.isoformat(),
            "plan_name": row["plan_name"],
            # Filled by the DB pass (usage-period lookup); None means unknown.
            "overage_ops": row.get("overage_ops"),
            # Receipts never expire inside the retry window: a late receipt
            # for a real charge is still correct. No event_at on purpose.
        },
    )


def build_renewal_candidate(row: dict, now: datetime) -> Optional[EmailCandidate]:
    period_start = _aware(row["current_period_start"])
    period_end = _aware(row["current_period_end"])
    return EmailCandidate(
        project_id=row["project_id"],
        email_type="renewal",
        email_key=f"renewal:{row['project_id']}:{_key_ts(period_start)}",
        context={
            "plan_name": row["plan_name"],
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "ops_limit": int(row["ops_limit"]),
        },
    )


def build_upcoming_plan_charge_candidate(row: dict, now: datetime) -> Optional[EmailCandidate]:
    period_end = _aware(row["current_period_end"])
    if period_end <= now:
        return None  # the charge date already passed; silence beats confusion
    line_items = [
        {
            "label": f"{row['plan_name']} plan renewal",
            "amount": f"{int(row['price_monthly']) / 100:.2f}",
            "currency": "USD",
        }
    ]
    # Mirror _capture_overage's own guards (billing_rotation) including the
    # sub-cent floor, so we never predict a charge rotation would skip.
    accrued = int(row.get("overage_ops") or 0)
    rate_cents = float(row.get("overage_rate_cents") or 0)
    if (
        row.get("overage_allowed")
        and row.get("overage_enabled")
        and rate_cents > 0
        and accrued > 0
    ):
        overage_cents = accrued * rate_cents
        if overage_cents >= 1:
            line_items.append(
                {
                    "label": f"Estimated overage ({accrued} operations so far)",
                    "amount": f"{overage_cents / 100:.2f}",
                    "currency": "USD",
                }
            )
    return EmailCandidate(
        project_id=row["project_id"],
        email_type="upcoming_charge",
        email_key=f"upcoming_charge:plan:{row['project_id']}:{_key_ts(period_end)}",
        context={
            "charge_date": period_end.isoformat(),
            "line_items": line_items,
            "event_at": period_end.isoformat(),
        },
    )


def build_upcoming_storage_charge_candidate(row: dict, now: datetime) -> Optional[EmailCandidate]:
    period_end = _aware(row["storage_period_end"])
    if period_end <= now:
        return None
    return EmailCandidate(
        project_id=row["project_id"],
        email_type="upcoming_charge",
        email_key=f"upcoming_charge:storage:{row['project_id']}:{_key_ts(period_end)}",
        context={
            "charge_date": period_end.isoformat(),
            "line_items": [
                {
                    "label": f"{row['storage_plan_name']} storage add-on renewal",
                    "amount": f"{int(row['price_monthly']) / 100:.2f}",
                    "currency": "USD",
                }
            ],
            "event_at": period_end.isoformat(),
        },
    )


def is_expired(email_type: str, context: dict, now: datetime) -> bool:
    """Retry staleness: reminder types expire once the event they announce has
    happened; receipts and renewal notices stay valid inside the window."""
    event_at = context.get("event_at")
    if not event_at:
        return False
    return datetime.fromisoformat(event_at) <= now


# ---------------------------------------------------------------------------
# Send dispatch: rebuild + send an email from (email_type, recipient, context).
# Used by BOTH the first-send path and the retry sub-pass, so the two can
# never drift apart. Calls go through the email_notifier MODULE attribute so
# test harnesses can monkeypatch the senders.
# ---------------------------------------------------------------------------


def _send_storage_lapse(recipient: str, ctx: dict, now: datetime) -> bool:
    return email_notifier.send_storage_lapse_warning_email(
        recipient,
        ctx["project_name"],
        _fmt_date(ctx["storage_period_end"]),
        ctx["storage_used_str"],
    )


def _send_overage_receipt(recipient: str, ctx: dict, now: datetime) -> bool:
    return email_notifier.send_overage_receipt_email(
        recipient,
        ctx["amount"],
        ctx["currency"],
        _fmt_date(ctx["charged_on"]),
        ctx["plan_name"],
        # Legacy fallback: rows CLAIMED by the pre-unified build inside the
        # retry window carry the old context key.
        ctx.get("overage_ops", ctx.get("overage_conversions")),
    )


def _send_renewal(recipient: str, ctx: dict, now: datetime) -> bool:
    return email_notifier.send_renewal_notice_email(
        recipient,
        ctx["plan_name"],
        _fmt_date(ctx["period_start"]),
        _fmt_date(ctx["period_end"]),
        # Legacy fallback (see _send_overage_receipt).
        int(ctx.get("ops_limit", ctx.get("conversion_limit", 0))),
    )


def _send_upcoming_charge(recipient: str, ctx: dict, now: datetime) -> bool:
    return email_notifier.send_upcoming_charge_email(
        recipient, _fmt_date(ctx["charge_date"]), ctx["line_items"]
    )


_SENDERS: dict[str, Callable[[str, dict, datetime], bool]] = {
    "storage_lapse": _send_storage_lapse,
    "overage_receipt": _send_overage_receipt,
    "renewal": _send_renewal,
    "upcoming_charge": _send_upcoming_charge,
}

# Legacy dedup column stamped on successful send (see module docstring).
_LEGACY_STAMPS = {
    "storage_lapse": text(
        "UPDATE ch_subscriptions SET storage_lapse_warned_at = :now"
        " WHERE project_id = :project_id"
    ),
}


# ---------------------------------------------------------------------------
# Candidate queries. Module-level text() constants (house pattern:
# billing_rotation._DUE_PROJECT_IDS). All windows compare aware-UTC instants.
# None of them anti-join ch_email_log: volumes are one row per subscription
# and the unique-key claim is the authoritative dedup — a conflicting claim
# costs one INSERT that inserts nothing.
# ---------------------------------------------------------------------------

_STORAGE_LAPSE_CANDIDATES = text(
    """
    SELECT s.project_id, s.storage_period_end,
           pr.name AS project_name, pr.storage_used
    FROM ch_subscriptions s
    JOIN ch_projects pr ON pr.id = s.project_id
    WHERE s.storage_plan_id IS NOT NULL
      -- cancelled add-on: the PayPal ref is cleared but access runs to
      -- storage_period_end, after which a lazy check zeroes the fields.
      -- Active add-ons (ref present) renew via webhook and never lapse.
      AND s.storage_payment_subscription_id IS NULL
      AND s.storage_period_end IS NOT NULL
      AND s.storage_period_end >  :now
      AND s.storage_period_end <= :now + INTERVAL '3 days'
      AND COALESCE(pr.storage_used, 0) > 0
    """
)

_OVERAGE_RECEIPT_CANDIDATES = text(
    """
    SELECT ph.project_id, ph.paypal_transaction_id, ph.amount_value,
           ph.amount_currency, ph.payment_time, p.name AS plan_name
    FROM ch_payment_history ph
    JOIN ch_subscriptions s ON s.project_id = ph.project_id
    JOIN ch_plans p ON p.id = s.plan_id
    WHERE ph.subscription_type = 'overage'
      AND ph.status = 'CAPTURED'
      AND ph.paypal_transaction_id LIKE 'overage:%'
      -- 7-day window bounds both the first-deploy sweep and how late a
      -- receipt can arrive after downtime
      AND ph.payment_time > :now - INTERVAL '7 days'
    """
)

# The marker encodes period_start at SECOND granularity (%Y%m%d%H%M%S drops
# microseconds), so the lookup must match at that precision — an exact
# equality would miss any boundary carrying sub-second precision. The join
# also recovers the plan the overage ACCRUED on: the email pass runs after
# the rotation drain, and rotation applies pending plan changes in the same
# commit as the capture, so the subscription's current plan_id may already
# name the NEXT plan.
_OVERAGE_PERIOD_LOOKUP = text(
    """
    SELECT up.overage_ops, p.name AS period_plan_name
    FROM ch_usage_periods up
    JOIN ch_plans p ON p.id = up.plan_id
    WHERE up.project_id = :project_id
      AND up.period_start >= :period_start
      AND up.period_start <  :period_start + INTERVAL '1 second'
    """
)

# ops_limit is the RESOLVED effective unified ops cap (migration 029):
# override, then the materialized effective column (NULLIF heals rows
# created in the migration-apply -> deploy gap), then the plan cap — the
# same resolution as utils.subscription.get_subscription. Resolved 0 =
# unlimited (enterprise); those rarely pass the price_monthly > 0 filter.
_RENEWAL_CANDIDATES = text(
    """
    SELECT s.project_id, s.current_period_start, s.current_period_end,
           COALESCE(s.override_ops_month, NULLIF(s.effective_ops_month, 0),
                    p.ops_month) AS ops_limit,
           p.name AS plan_name
    FROM ch_subscriptions s
    JOIN ch_plans p ON p.id = s.plan_id
    WHERE s.status = 'active'
      AND s.payment_subscription_id IS NOT NULL
      AND p.price_monthly > 0            -- free plans rotate too; never spam them
      AND s.current_period_start <= :now
      AND s.current_period_start >  :now - INTERVAL '3 days'
      -- a ROTATED boundary, not first activation: rotation keeps periods
      -- contiguous (prior period_end == new period_start); a fresh subscribe
      -- creates only one period. Multi-month catch-up fires ONCE by
      -- construction — current_period_start is the FINAL committed boundary.
      AND EXISTS (
          SELECT 1 FROM ch_usage_periods up
          WHERE up.project_id = s.project_id
            AND up.period_end = s.current_period_start
      )
    """
)

# The renewal line item must reflect what PayPal will ACTUALLY charge at the
# boundary: when a plan change is scheduled (pending_plan_id set), the backend
# has already swapped the PayPal subscription to the new plan, so the pending
# plan's name/price win via COALESCE. The overage line stays on the CURRENT
# plan's rate — _capture_overage bills the ending period before the pending
# swap is applied (billing_rotation). The price gate also uses the COALESCEd
# price: a scheduled downgrade-to-free (the deferred-cancel path) means NO
# charge is coming, so no reminder. payment_provider IS NOT NULL matters
# independently of payment_subscription_id: the backend's cancel paths null
# the provider but deliberately KEEP payment_subscription_id for webhook
# matching — "your card will be charged" must never reach a canceller.
_UPCOMING_PLAN_CANDIDATES = text(
    """
    SELECT s.project_id, s.current_period_end,
           s.overage_enabled,
           COALESCE(pp.name, p.name) AS plan_name,
           COALESCE(pp.price_monthly, p.price_monthly) AS price_monthly,
           p.overage_allowed,
           p.overage_rate_cents,
           COALESCE(up.overage_ops, 0) AS overage_ops
    FROM ch_subscriptions s
    JOIN ch_plans p ON p.id = s.plan_id
    LEFT JOIN ch_plans pp ON pp.id = s.pending_plan_id
    LEFT JOIN ch_usage_periods up
           ON up.project_id = s.project_id
          AND up.period_start = s.current_period_start
    WHERE s.status = 'active'
      AND s.payment_provider IS NOT NULL
      AND s.payment_subscription_id IS NOT NULL
      AND COALESCE(pp.price_monthly, p.price_monthly) > 0
      AND s.current_period_end >  :now
      AND s.current_period_end <= :now + INTERVAL '2 days'
    """
)

_UPCOMING_STORAGE_CANDIDATES = text(
    """
    SELECT s.project_id, s.storage_period_end,
           sp.name AS storage_plan_name, sp.price_monthly
    FROM ch_subscriptions s
    JOIN ch_storage_plans sp ON sp.id = s.storage_plan_id
    WHERE s.storage_payment_subscription_id IS NOT NULL
      AND s.storage_period_end IS NOT NULL
      AND s.storage_period_end >  :now
      AND s.storage_period_end <= :now + INTERVAL '2 days'
    """
)

_OWNER_EMAIL = text(
    """
    SELECT u.email FROM ch_project_members m
    JOIN ch_users u ON u.id = m.user_id
    WHERE m.project_id = :project_id
      AND m.role = 'owner'
      AND u.active IS TRUE      -- never email suspended accounts
    LIMIT 1
    """
)

# created_at is the ACTUAL claim instant (:claimed_at, a fresh clock read),
# never the pass-start moment: the retry grace period below compares against
# created_at, and backdating claims made minutes into a long pass would let a
# concurrent run's retry pick up a row whose first send is still in flight.
_CLAIM = text(
    """
    INSERT INTO ch_email_log
        (project_id, email_type, email_key, recipient, sent_ok, attempts,
         context, created_at)
    VALUES (:project_id, :email_type, :email_key, :recipient, FALSE, 0,
            CAST(:context AS JSONB), :claimed_at)
    ON CONFLICT (email_key) DO NOTHING
    RETURNING id
    """
)

_RECORD_OUTCOME = text(
    """
    UPDATE ch_email_log
       SET sent_ok = :ok,
           attempts = attempts + 1,
           sent_at = CASE WHEN :ok THEN CAST(:now AS timestamptz) END,
           last_error = :error
     WHERE id = :id
    """
)

# Unlocked id scan: each id is then re-claimed and processed in its OWN short
# transaction (see _retry_unsent), so a mid-batch crash duplicates at most the
# single in-flight email instead of rolling back a whole batch of recorded
# outcomes. The age floor serves three purposes at once: it defers retries of
# THIS run's failures to the next timer fire (never re-hit a failing Brevo
# seconds later, which would burn the receipt cap inside one outage), it keeps
# hands off rows a concurrent run claimed moments ago and is still sending,
# and it resurrects claimants that crashed between claim commit and send.
_RETRYABLE_IDS = text(
    f"""
    SELECT id
    FROM ch_email_log
    WHERE sent_ok = FALSE
      -- Onboarding lifecycle rows are retried by services/lifecycle_emails.py
      -- (which owns their stage rechecks and dry-run semantics), never here.
      AND email_type NOT LIKE 'lifecycle%'
      AND email_type <> 'founder_call'
      AND created_at > :now - INTERVAL '{int(RETRY_WINDOW_HOURS)} hours'
      AND created_at < :now - INTERVAL '{int(RETRY_GRACE_MINUTES)} minutes'
      AND attempts < :max_cap
    ORDER BY created_at
    """
)

_RETRY_CLAIM_ROW = text(
    """
    SELECT id, project_id, email_type, email_key, recipient, context, attempts
    FROM ch_email_log
    WHERE id = :id AND sent_ok = FALSE
    FOR UPDATE SKIP LOCKED
    """
)

_MARK_EXPIRED = text(
    """
    UPDATE ch_email_log
       SET attempts = :max_cap, last_error = 'expired'
     WHERE id = :id
    """
)


def _owner_email(db: Session, project_id: int) -> Optional[str]:
    row = db.execute(_OWNER_EMAIL, {"project_id": project_id}).first()
    return row[0] if row else None


def _stamp_legacy(db: Session, email_type: str, project_id: int, now: datetime) -> None:
    """Stamp the migration-009 dedup column in its OWN short transaction.

    Deliberately isolated from the outcome commit and bounded by a lock
    timeout: billing_rotation holds FOR UPDATE on the same ch_subscriptions
    row across its PayPal HTTP calls (worst case minutes during catch-up), and
    a blocked belt-and-braces stamp must never stall the pass or poison the
    already-committed outcome. The stamp only keeps the dormant backend sweep
    a no-op — losing it costs nothing (ch_email_log.email_key is the real
    dedup), so a timeout is logged and swallowed.
    """
    try:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(
            _LEGACY_STAMPS[email_type],
            {"now": now, "project_id": project_id},
        )
        db.commit()
    except Exception:  # noqa: BLE001 — best-effort by design
        db.rollback()
        logger.warning(
            "legacy %s stamp skipped for project %s (lock busy or write failed)",
            email_type, project_id,
        )


def _claim_and_send(
    db: Session, candidate: EmailCandidate, recipient: str, now: datetime
) -> str:
    """The concurrency contract: durable claim BEFORE the HTTP call.

    Returns "sent", "send_failed" or "already_claimed".
    """
    row = db.execute(
        _CLAIM,
        {
            "project_id": candidate.project_id,
            "email_type": candidate.email_type,
            "email_key": candidate.email_key,
            "recipient": recipient,
            "context": json.dumps(candidate.context),
            # Fresh clock read, NOT the pass moment: see the _CLAIM comment.
            "claimed_at": datetime.now(timezone.utc),
        },
    ).first()
    db.commit()
    if row is None:
        return "already_claimed"

    ok = _SENDERS[candidate.email_type](recipient, candidate.context, now)

    db.execute(
        _RECORD_OUTCOME,
        {
            "ok": ok,
            "now": now,
            "error": None if ok else "send returned False",
            "id": row[0],
        },
    )
    db.commit()
    if ok and candidate.email_type in _LEGACY_STAMPS:
        _stamp_legacy(db, candidate.email_type, candidate.project_id, now)
    return "sent" if ok else "send_failed"


def _run_type_pass(
    db: Session,
    now: datetime,
    query: TextClause,
    builder: Callable[[dict, datetime], Optional[EmailCandidate]],
    enrich: Optional[Callable[[Session, dict], None]] = None,
) -> dict:
    """Generic pass skeleton: SELECT candidates, build, resolve owner, claim,
    send. ``enrich`` may mutate the row dict with extra lookups (receipts)."""
    counts = {"candidates": 0, "sent": 0, "failed": 0, "skipped": 0, "already": 0}
    rows = db.execute(query, {"now": now}).mappings().all()
    for raw in rows:
        row = dict(raw)
        if enrich is not None:
            enrich(db, row)
        candidate = builder(row, now)
        if candidate is None:
            continue
        counts["candidates"] += 1
        # Resolve BEFORE claiming: a project with no active owner email today
        # leaves the key unclaimed, so a still-in-window email can go out once
        # the owner is fixed.
        recipient = _owner_email(db, candidate.project_id)
        if not recipient:
            counts["skipped"] += 1
            continue
        outcome = _claim_and_send(db, candidate, recipient, now)
        if outcome == "sent":
            counts["sent"] += 1
        elif outcome == "send_failed":
            counts["failed"] += 1
        else:
            counts["already"] += 1
    return counts


def _enrich_overage_row(db: Session, row: dict) -> None:
    """Best-effort receipt enrichment: parse the marker's period_start and
    look up the ending usage period for the overage op count AND the plan
    the overage actually accrued on. Misses just omit the count and fall
    back to the subscription's current plan name — never block a receipt on
    bookkeeping."""
    row["overage_ops"] = None
    period_start = parse_overage_marker(row["paypal_transaction_id"])
    if period_start is None:
        return
    hit = db.execute(
        _OVERAGE_PERIOD_LOOKUP,
        {"project_id": row["project_id"], "period_start": period_start},
    ).first()
    if hit is not None:
        row["overage_ops"] = int(hit[0])
        row["plan_name"] = hit[1]


def _pass_overage_receipts(db: Session, now: datetime) -> dict:
    return _run_type_pass(
        db, now, _OVERAGE_RECEIPT_CANDIDATES, build_overage_receipt_candidate,
        enrich=_enrich_overage_row,
    )


def _pass_renewal_notices(db: Session, now: datetime) -> dict:
    return _run_type_pass(db, now, _RENEWAL_CANDIDATES, build_renewal_candidate)


def _pass_upcoming_charges(db: Session, now: datetime) -> dict:
    plan = _run_type_pass(
        db, now, _UPCOMING_PLAN_CANDIDATES, build_upcoming_plan_charge_candidate
    )
    storage = _run_type_pass(
        db, now, _UPCOMING_STORAGE_CANDIDATES, build_upcoming_storage_charge_candidate
    )
    return {key: plan[key] + storage[key] for key in plan}


def _pass_storage_lapse(db: Session, now: datetime) -> dict:
    return _run_type_pass(db, now, _STORAGE_LAPSE_CANDIDATES, build_storage_lapse_candidate)


def _retry_unsent(db: Session, now: datetime) -> dict:
    """Resend claimed-but-unsent rows from their stored context alone.

    One SHORT transaction per row: re-claim the id FOR UPDATE SKIP LOCKED,
    send inside that row lock (bounded by Brevo's 13s timeout — the same
    lock-across-HTTP shape billing_rotation accepts for PayPal), record the
    outcome, commit. A mid-batch crash therefore duplicates at most the one
    in-flight email instead of rolling back a whole batch of recorded
    outcomes, and holding exactly one email row (plus, briefly, one
    subscription row via _stamp_legacy's separate transaction) removes any
    multi-row lock-ordering deadlock between overlapping runs.
    """
    counts = {"retried": 0, "sent": 0, "failed": 0, "expired": 0}
    ids = [
        r[0]
        for r in db.execute(
            _RETRYABLE_IDS, {"now": now, "max_cap": DEFAULT_RETRY_CAP}
        ).all()
    ]
    db.commit()  # end the scan snapshot before taking any row locks
    for row_id in ids:
        row = db.execute(_RETRY_CLAIM_ROW, {"id": row_id}).mappings().first()
        if row is None:  # a concurrent run holds or already finished it
            db.commit()
            continue
        cap = RETRY_CAPS.get(row["email_type"], DEFAULT_RETRY_CAP)
        sender = _SENDERS.get(row["email_type"])
        if row["attempts"] >= cap or sender is None:
            # over-cap, or an unknown type from a future build: leave it alone
            db.commit()
            continue
        context = row["context"] or {}
        if isinstance(context, str):  # driver-dependent JSONB decoding
            context = json.loads(context)
        if is_expired(row["email_type"], context, now):
            db.execute(_MARK_EXPIRED, {"max_cap": cap, "id": row["id"]})
            db.commit()
            counts["expired"] += 1
            continue
        counts["retried"] += 1
        ok = sender(row["recipient"], context, now)
        db.execute(
            _RECORD_OUTCOME,
            {
                "ok": ok,
                "now": now,
                "error": None if ok else "send returned False",
                "id": row["id"],
            },
        )
        db.commit()
        if ok and row["email_type"] in _LEGACY_STAMPS:
            _stamp_legacy(db, row["email_type"], row["project_id"], now)
        counts["sent" if ok else "failed"] += 1
    return counts


# Ordered: receipts first (money-adjacent, references the rotation output that
# just committed); the rest are independent of each other.
_PASSES: list[tuple[str, Callable]] = [
    ("overage_receipt", _pass_overage_receipts),
    ("renewal", _pass_renewal_notices),
    ("upcoming_charge", _pass_upcoming_charges),
    ("storage_lapse", _pass_storage_lapse),
]


def run_email_pass(now: Optional[datetime] = None) -> dict:
    """One full subscription-email sweep. Sync; script/systemd context.

    Fresh Session per type-pass so one poisoned transaction cannot sink the
    rest; per-type crash isolation mirrors billing_rotation.tick's per-project
    isolation. Returns a summary the caller maps to exit codes
    (status != "ok" -> exit 1: the failed systemd unit IS the signal).
    """
    moment = now or datetime.now(timezone.utc)
    results: dict[str, dict] = {}
    crashed: list[str] = []
    for name, pass_fn in _PASSES:
        db = get_db()
        try:
            results[name] = pass_fn(db, moment)
        except Exception as exc:  # noqa: BLE001 — one broken type must not block the rest
            logger.exception("email pass %s crashed", name)
            results[name] = {"error": str(exc)}
            crashed.append(name)
        finally:
            db.close()

    db = get_db()
    try:
        retry = _retry_unsent(db, moment)
    except Exception as exc:  # noqa: BLE001
        logger.exception("email retry sub-pass crashed")
        retry = {"error": str(exc), "failed": 0}
        crashed.append("retry")
    finally:
        db.close()

    failed_sends = sum(
        r.get("failed", 0) for r in results.values() if isinstance(r, dict)
    ) + retry.get("failed", 0)
    return {
        "status": "ok" if not crashed and failed_sends == 0 else "partial",
        "types": results,
        "retry": retry,
        "crashed_types": crashed,
        "failed_sends": failed_sends,
    }
