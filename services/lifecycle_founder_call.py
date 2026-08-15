"""Founder-call invitation pass (onboarding Phase 8).

Invites a small, tightly-guarded set of engaged free-tier users to book a
20-minute call with the founder (Cal.com link from ``FOUNDER_CALL_URL``).
Invoked by ``services.lifecycle_emails.run_lifecycle_pass`` after the six
lifecycle stages, sharing its claim budget, dry-run mode, and the
``ch_email_log`` claim-before-send machinery (``_claim_and_send``).

WHO QUALIFIES (every guard is conjunctive, and all but the builder's
re-assertions live in the candidate SQL so ineligible users never reach the
claim path):

- On the free plan (``ch_plans.slug = 'free'``) on an owned project whose
  CURRENT usage period sits in the 50% band:
  ``ops_used >= 0.5 * effective_ops_month AND ops_used < effective_ops_month``
  with ``effective_ops_month > 0`` (0 = unlimited never qualifies).
- HAS NEVER PAID. Signal: no ``ch_payment_history`` row with
  ``status = 'COMPLETED'`` on any project the user OWNS. Chosen over a
  plan-slug check because ``ch_subscriptions`` holds only the CURRENT plan
  (``project_id`` is UNIQUE — no history): a formerly-paying user who
  downgraded back to free would pass a slug check, but their payment rows
  persist. CEO decision 2026-08-13: the offer goes to free users who have
  not previously subscribed to any plan.
- Account is at least 48h old, email verified, active, not banned, not
  opted out, not suppressed, and ``created_at >= LIFECYCLE_EPOCH`` (the
  standing epoch rule — see note below).
- Real integrator: at least one owned API key with ``first_used_at`` set.
- Human usage shape: >= 2 distinct UTC calendar days in ``ch_activity``
  (filters the single-session ``for i in range(60)`` test loop).
- No founder_call email in the last 30 days, and fewer than 2 lifetime
  offers (the ``founder_call:u{id}:{seq}`` key with seq in {1,2} makes the
  UNIQUE index on ``ch_email_log.email_key`` the lifetime cap itself).
- Local quiet hours: weekdays 09:00-17:59 in the user's timezone
  (``ch_users.timezone`` validated via LEFT JOIN pg_timezone_names;
  unknown/NULL falls back to UTC). A candidate outside the window is simply
  not selected this tick and re-evaluates on a later fire.
- Weekly cap: ``FOUNDER_CALL_SLOTS_PER_WEEK`` (default 5) minus the
  founder_call rows already claimed this ISO week (``date_trunc('week')``
  is Monday-start). Advisory backstop — Cal.com's own availability is the
  authoritative limiter.

Ranked by ops ratio DESC (closest to the ceiling first). ``DISTINCT ON
(u.id)`` keeps one row per user across multiple owned projects (hottest
project wins).

EPOCH NOTE: founder_call obeys ``LIFECYCLE_EPOCH`` like every other stage —
the structural guarantee that an enabled system can never sweep the
historical user base. Pre-epoch engaged users can be invited deliberately
with a one-off ``run_lifecycle_emails.py --only-stage founder_call
--epoch <older date> --max-sends N`` run.

DRY-RUN NOTE: a dry-run founder_call claim consumes a seq slot until
``--purge-dry-run-claims`` releases it, identical to the lifecycle stages.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session

import config
from services import lifecycle_emails as le

logger = logging.getLogger("uvicorn.error")

EMAIL_TYPE = "founder_call"
LIFETIME_OFFER_CAP = 2
COOLDOWN_DAYS = 30
ACCOUNT_MIN_AGE_HOURS = 48
MIN_ACTIVE_DAYS = 2
BAND_LOW = 0.5


def founder_call_email_key(user_id: int, seq: int) -> str:
    return f"founder_call:u{int(user_id)}:{int(seq)}"


# ---------------------------------------------------------------------------
# SQL. House rules: ch_activity.project_id is VARCHAR — always
# a.project_id = p.id::text (cast the ch_projects side) so
# idx_ch_activity_project_status_ts stays usable.
# ---------------------------------------------------------------------------

_SENT_THIS_WEEK = text("""
    SELECT COUNT(*) AS sent_this_week
    FROM ch_email_log
    WHERE email_type = 'founder_call'
      AND created_at >= date_trunc('week', CAST(:now AS timestamptz))
""")

_CANDIDATES = text(f"""
    SELECT * FROM (
        SELECT DISTINCT ON (u.id)
            u.id AS user_id, u.email, u.full_name, u.created_at,
            u.is_email_verified, u.email_verified_at,
            m.project_id,
            up.ops_used,
            s.effective_ops_month,
            (up.ops_used::float / s.effective_ops_month) AS ops_ratio,
            (SELECT COUNT(*) FROM ch_email_log el
              WHERE el.user_id = u.id
                AND el.email_type = 'founder_call') AS prior_offers
        FROM ch_users u
        JOIN ch_project_members m
          ON m.user_id = u.id AND m.role = 'owner'
        JOIN ch_subscriptions s ON s.project_id = m.project_id
        JOIN ch_plans p ON p.id = s.plan_id
        JOIN ch_usage_periods up
          ON up.project_id = m.project_id
         AND CAST(:now AS timestamptz) >= up.period_start
         AND CAST(:now AS timestamptz) <  up.period_end
        LEFT JOIN pg_timezone_names tz ON tz.name = u.timezone
        WHERE p.slug = 'free'
          AND s.effective_ops_month > 0
          AND up.ops_used >= {BAND_LOW} * s.effective_ops_month
          AND up.ops_used <  s.effective_ops_month
          -- never paid: no COMPLETED payment on any project the user owns
          AND NOT EXISTS (
              SELECT 1
              FROM ch_payment_history ph
              JOIN ch_project_members mo
                ON mo.project_id = ph.project_id
               AND mo.user_id = u.id
               AND mo.role = 'owner'
              WHERE ph.status = 'COMPLETED'
          )
          AND u.created_at <= CAST(:now AS timestamptz)
                              - INTERVAL '{ACCOUNT_MIN_AGE_HOURS} hours'
          AND u.is_email_verified = TRUE
          -- real integrator: an owned key that has authenticated
          AND EXISTS (
              SELECT 1
              FROM ch_api_keys k
              JOIN ch_project_members mk
                ON mk.project_id = k.project_id
               AND mk.user_id = u.id
               AND mk.role = 'owner'
              WHERE k.first_used_at IS NOT NULL
          )
          -- >= 2 distinct UTC activity days across owned projects
          AND (
              SELECT COUNT(DISTINCT (a."timestamp" AT TIME ZONE 'UTC')::date)
              FROM ch_activity a
              JOIN ch_projects pr ON a.project_id = pr.id::text
              JOIN ch_project_members ma
                ON ma.project_id = pr.id
               AND ma.user_id = u.id
               AND ma.role = 'owner'
          ) >= {MIN_ACTIVE_DAYS}
          -- lifetime cap + 30-day cooldown on the shared key namespace
          AND (SELECT COUNT(*) FROM ch_email_log el2
                WHERE el2.user_id = u.id
                  AND el2.email_type = 'founder_call') < {LIFETIME_OFFER_CAP}
          AND NOT EXISTS (
              SELECT 1 FROM ch_email_log el3
              WHERE el3.user_id = u.id
                AND el3.email_type = 'founder_call'
                AND el3.created_at > CAST(:now AS timestamptz)
                                     - INTERVAL '{COOLDOWN_DAYS} days'
          )
          -- weekday 09:00-17:59 in the user's (validated) timezone
          AND EXTRACT(ISODOW FROM (CAST(:now AS timestamptz)
                  AT TIME ZONE COALESCE(tz.name, 'UTC'))) BETWEEN 1 AND 5
          AND EXTRACT(HOUR FROM (CAST(:now AS timestamptz)
                  AT TIME ZONE COALESCE(tz.name, 'UTC'))) BETWEEN 9 AND 17
          AND u.active IS TRUE
          AND u.banned_at IS NULL
          AND u.lifecycle_opt_out_at IS NULL
          AND u.email_suppressed_at IS NULL
          AND u.created_at >= :epoch
        ORDER BY u.id, (up.ops_used::float / s.effective_ops_month) DESC
    ) c
    ORDER BY c.ops_ratio DESC
    LIMIT :slots
""")


# ---------------------------------------------------------------------------
# Pure builder (unit-testable with plain dicts). Re-asserts the guards the
# SQL already applied — a builder must be safe against a query drifting
# (the structural epoch test covers this module too).
# ---------------------------------------------------------------------------


def build_founder_call_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[le.LifecycleCandidate]:
    created_at = row.get("created_at")
    if created_at is None or le._aware(created_at) < epoch:
        return None
    if le._aware(created_at) > now - timedelta(hours=ACCOUNT_MIN_AGE_HOURS):
        return None
    if not row.get("is_email_verified"):
        return None
    limit = int(row.get("effective_ops_month") or 0)
    used = int(row.get("ops_used") or 0)
    if limit <= 0 or used < BAND_LOW * limit or used >= limit:
        return None
    prior = int(row.get("prior_offers") or 0)
    if prior >= LIFETIME_OFFER_CAP:
        return None
    seq = prior + 1
    user_id = int(row["user_id"])
    return le.LifecycleCandidate(
        user_id=user_id,
        project_id=int(row.get("project_id") or 0),
        stage="founder_call",
        email_type=EMAIL_TYPE,
        email_key=founder_call_email_key(user_id, seq),
        # call_url is resolved by lifecycle_mail.founder_call from config at
        # send time; storing seq keeps the retry sub-pass rebuild exact.
        context={"seq": seq},
    )


def _weekly_slots_remaining(db: Session, now: datetime) -> int:
    cap = int(getattr(config, "FOUNDER_CALL_SLOTS_PER_WEEK", 5) or 0)
    if cap <= 0:
        return 0
    row = db.execute(_SENT_THIS_WEEK, {"now": now}).mappings().first()
    sent = int(row["sent_this_week"]) if row else 0
    return max(0, cap - sent)


def list_founder_call_candidates(
    db: Session, now: datetime, epoch: datetime
) -> list[dict]:
    """Zero-write preview used by --list-candidates. Never claims."""
    slots = _weekly_slots_remaining(db, now)
    if slots <= 0:
        return []
    out: list[dict] = []
    rows = db.execute(
        _CANDIDATES, {"now": now, "epoch": epoch, "slots": slots}
    ).mappings()
    for raw in rows:
        candidate = build_founder_call_candidate(dict(raw), now, epoch)
        if candidate is None:
            continue
        out.append(
            {
                "user_id": candidate.user_id,
                "email": raw["email"],
                "email_key": candidate.email_key,
                "email_type": candidate.email_type,
                "ops_ratio": round(float(raw["ops_ratio"]), 3),
                "context": candidate.context,
            }
        )
    return out


def run_founder_call_pass(
    db: Session,
    now: datetime,
    epoch: datetime,
    budget: dict,
    dry_run: Optional[bool] = None,
    deadline: Optional[float] = None,
) -> dict:
    """One founder-call sweep. Mirrors lifecycle_emails._run_stage: SELECT,
    build, suppression-gate, claim (budget-consuming), recheck, send.

    ``dry_run``/``deadline`` are optional so the original
    ``(db, now, epoch, budget)`` call shape keeps working; when ``dry_run``
    is not supplied the config default applies (safe: config defaults to
    dry-run on)."""
    effective_dry_run = (
        dry_run
        if dry_run is not None
        else bool(getattr(config, "LIFECYCLE_DRY_RUN", True))
    )
    counts = {
        "candidates": 0, "claimed": 0, "sent": 0, "failed": 0,
        "skipped": 0, "already": 0, "cleared": 0, "dry_run": 0,
        "slots_remaining": 0,
    }
    if not getattr(config, "FOUNDER_CALL_URL", None):
        logger.info("[lifecycle] founder_call: FOUNDER_CALL_URL unset — skipped")
        return counts

    slots = _weekly_slots_remaining(db, now)
    counts["slots_remaining"] = slots
    if slots <= 0:
        logger.info("[lifecycle] founder_call: weekly slot cap reached — skipped")
        return counts

    rows = db.execute(
        _CANDIDATES, {"now": now, "epoch": epoch, "slots": slots}
    ).mappings().all()
    for raw in rows:
        candidate = build_founder_call_candidate(dict(raw), now, epoch)
        if candidate is None:
            continue
        counts["candidates"] += 1
        if budget["remaining"] <= 0:
            logger.info("[lifecycle] claim budget exhausted in founder_call")
            break
        if deadline is not None and time.monotonic() > deadline:
            logger.info("[lifecycle] time budget exhausted in founder_call")
            break
        recipient = le._lifecycle_recipient(db, candidate.user_id)
        if recipient is None:
            counts["skipped"] += 1
            continue
        outcome = le._claim_and_send(
            db, candidate, recipient, now, effective_dry_run
        )
        if outcome == "already_claimed":
            counts["already"] += 1
            continue
        budget["remaining"] -= 1
        counts["claimed"] += 1
        if outcome == "sent":
            counts["sent"] += 1
        elif outcome == "send_failed":
            counts["failed"] += 1
        elif outcome == "condition_cleared":
            counts["cleared"] += 1
        elif outcome == "dry_run":
            counts["dry_run"] += 1
    return counts
