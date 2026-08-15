"""Onboarding lifecycle email pass (droplet systemd design, no GCP).

Run by scripts/ops/run_lifecycle_emails.py under the enconvert-lifecycle-emails
systemd timer. Structural clone of services/subscription_emails.py: the same
claim-before-send dedup through ch_email_log (INSERT .. ON CONFLICT (email_key)
DO NOTHING RETURNING id, committed BEFORE the Brevo call) and the same bounded,
staleness-aware retry sub-pass — but user-scoped instead of project-scoped,
with these lifecycle-specific rules layered on top:

    * EPOCH — only users with created_at >= LIFECYCLE_EPOCH are ever
      candidates. Enforced in EVERY candidate query AND re-asserted in every
      pure builder, so neither layer alone can leak a legacy user.
    * SUPPRESSION — lifecycle_opt_out_at / email_suppressed_at / active /
      banned_at are checked in the candidate queries, again in
      _lifecycle_recipient BEFORE the claim, and again in the per-stage
      recheck between claim and send. They are deliberately NOT checked in
      the transport (utils/lifecycle_mail or email_notifier): transactional
      mail (password resets) must still deliver to unsubscribed users.
    * RECHECK — between the committed claim and the send, one indexed SELECT
      re-asserts the stage condition (the world may have moved: the user
      verified, created a key, succeeded). A failed recheck closes the row
      with last_error='condition_cleared' so the retry sub-pass never
      resurrects it.
    * DRY RUN — LIFECYCLE_DRY_RUN writes claims and logs the decision but
      skips the send and marks the row last_error='dry_run'. Dry-run rows are
      excluded from the retry query; going live, purge them first
      (run_lifecycle_emails.py --purge-dry-run-claims) so the real pass can
      re-claim and actually send.
    * BUDGET — MAX_LIFECYCLE_SENDS_PER_TICK counts successful CLAIMS per run
      across all stages; remaining candidates simply wait for the next timer
      fire.

Six stages (email_type / email_key per the Phase-6 contract; all keys are
``lifecycle:u{user_id}:{stage}``, so each stage fires at most once per user):

    verify_1   not verified, account >= 2h old
    verify_2   not verified, account >= 26h old, verify_1 was sent
    no_key     verified >= 24h, no private key on any owned project
    no_call    oldest private key >= 24h old, no key ever used
    stuck      a key first used >= 4h ago, zero Success activity, >= 2 Failed
    activated  any key has first_used_at (the "it worked" note)

founder_call is owned by services/lifecycle_founder_call.py (a later phase);
its import is guarded so this module works standalone.

Verify-link tokens: the nudge emails need a working link, and raw tokens are
never stored (only SHA-256 hashes, ch_email_verify_tokens), so a prior token
cannot be reused — each live nudge mints a FRESH token (mirroring
backend/sections/api/email_verify.py: secrets.token_urlsafe(32), 24h expiry)
and builds the same landing URL the backend does:
{DASHBOARD_ORIGIN}/auth?mode=verify&token={raw}. KNOWN RESIDUAL: a retry
inside the 48h window but past the 24h token expiry re-sends the stored
(now-expired) link; the landing page's "resend" path covers that.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from sqlmodel import Session

import config
from utils import lifecycle_mail
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Optional founder-call stage (later phase). Guarded: everything in this
# module must work when the file does not exist yet. Expected interface when
# present: run_founder_call_pass(db, now, epoch, budget) -> counts dict, using
# the same claim/recheck helpers this module exports.
try:  # pragma: no cover - exercised only once the sibling module lands
    from services import lifecycle_founder_call as _founder_call
except ImportError:
    _founder_call = None

# Retry sub-pass bounds — same shape as subscription_emails (window bounds how
# late a nudge can arrive; grace spaces attempts across timer fires).
RETRY_WINDOW_HOURS = 48
RETRY_GRACE_MINUTES = 15
RETRY_CAP = 3

VERIFY_TOKEN_EXPIRY_HOURS = 24

_STAGES = ("verify_1", "verify_2", "no_key", "no_call", "stuck", "activated")

_STAGE_EMAIL_TYPE = {stage: f"lifecycle_{stage}" for stage in _STAGES}


def _aware(dt: datetime) -> datetime:
    """TIMESTAMPTZ comes back aware from psycopg2; create_all scratch DBs
    (bare TIMESTAMP) do not — treat naive as UTC, same as subscription_emails."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_epoch(value: Optional[str] = None) -> datetime:
    """Resolve the lifecycle epoch to an aware-UTC instant, or refuse.

    REQUIRED by contract: never default to a past date — an unset epoch on an
    enabled system would sweep the entire historical user base, so we raise
    instead (the runner turns that into exit 2)."""
    raw = value if value is not None else getattr(config, "LIFECYCLE_EPOCH", None)
    if not raw or not str(raw).strip():
        raise RuntimeError(
            "LIFECYCLE_EPOCH is not set; refusing to run the lifecycle pass "
            "(set it to the launch date, e.g. 2026-08-15)"
        )
    try:
        parsed = datetime.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError(f"LIFECYCLE_EPOCH is not ISO format: {raw!r}") from exc
    return _aware(parsed)


def stage_email_key(user_id: int, stage: str) -> str:
    return f"lifecycle:u{int(user_id)}:{stage}"


@dataclass(frozen=True)
class LifecycleCandidate:
    """One lifecycle email the pass wants to send; produced by pure builders."""

    user_id: int
    project_id: int
    stage: str
    email_type: str
    email_key: str
    # Everything the template needs, JSON-serializable: the retry sub-pass
    # rebuilds the email from this alone.
    context: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure candidate builders. Each takes a plain dict row (as returned by
# .mappings()), the aware-UTC now, and the aware-UTC epoch. EVERY builder
# re-asserts the epoch — the SQL already filters on it, but a builder must be
# safe against a query drifting (that is THE structural test).
# ---------------------------------------------------------------------------


def _base_checks(row: dict, epoch: datetime) -> bool:
    created_at = row.get("created_at")
    if created_at is None or _aware(created_at) < epoch:
        return False
    return True


def _candidate(row: dict, stage: str, context: dict) -> LifecycleCandidate:
    return LifecycleCandidate(
        user_id=int(row["user_id"]),
        project_id=int(row.get("project_id") or 0),
        stage=stage,
        email_type=_STAGE_EMAIL_TYPE[stage],
        email_key=stage_email_key(int(row["user_id"]), stage),
        context=context,
    )


def build_verify_1_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    if row.get("is_email_verified"):
        return None
    if _aware(row["created_at"]) > now - timedelta(hours=2):
        return None
    return _candidate(row, "verify_1", {"verify_url": row.get("verify_url")})


def build_verify_2_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    if row.get("is_email_verified"):
        return None
    if _aware(row["created_at"]) > now - timedelta(hours=26):
        return None
    if not row.get("verify_1_sent"):
        return None
    return _candidate(row, "verify_2", {"verify_url": row.get("verify_url")})


def build_no_key_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    verified_at = row.get("email_verified_at")
    if verified_at is None or _aware(verified_at) > now - timedelta(hours=24):
        return None
    return _candidate(row, "no_key", {})


def build_no_call_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    oldest = row.get("oldest_key_created_at")
    if oldest is None or _aware(oldest) > now - timedelta(hours=24):
        return None
    return _candidate(row, "no_call", {})


def build_stuck_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    first_used = row.get("first_used_at")
    if first_used is None or _aware(first_used) > now - timedelta(hours=4):
        return None
    failed_count = int(row.get("failed_count") or 0)
    if failed_count < 2:
        return None
    return _candidate(
        row,
        "stuck",
        {
            "error_type": row.get("error_type"),
            "error_message": row.get("error_message"),
            "failed_count": failed_count,
        },
    )


def build_activated_candidate(
    row: dict, now: datetime, epoch: datetime
) -> Optional[LifecycleCandidate]:
    if not _base_checks(row, epoch):
        return None
    if row.get("first_used_at") is None:
        return None
    return _candidate(row, "activated", {"surface": row.get("first_used_surface")})


# ---------------------------------------------------------------------------
# Candidate queries. Module-level text() constants (house pattern). Shared
# fragments are module-owned f-string constants, never user input.
# ---------------------------------------------------------------------------

# The standing suppression predicates + the epoch. Present in every query.
# idx_ch_users_lifecycle_candidates (partial, ON created_at WHERE opt-out/
# suppression/active are open) carries the scan.
_SUPPRESSED = """
      u.active IS TRUE
      AND u.banned_at IS NULL
      AND u.lifecycle_opt_out_at IS NULL
      AND u.email_suppressed_at IS NULL
      AND u.created_at >= :epoch
"""

# First owned project (users get a default project at signup). ch_email_log
# .project_id is NOT NULL, so a pathological owner-less user claims under 0.
_OWNER_PROJECT = """
    LEFT JOIN LATERAL (
        SELECT m.project_id
        FROM ch_project_members m
        WHERE m.user_id = u.id AND m.role = 'owner'
        ORDER BY m.project_id
        LIMIT 1
    ) op ON TRUE
"""

_BASE_COLS = """
    u.id AS user_id, u.email, u.full_name, u.created_at,
    u.is_email_verified, u.email_verified_at,
    COALESCE(op.project_id, 0) AS project_id
"""

# Anti-join on the stage's own email_key (unique index): keeps already-claimed
# users out of the scan so they never burn the per-tick claim budget. The
# claim's ON CONFLICT remains the authoritative dedup.
_NOT_CLAIMED = """
      NOT EXISTS (
          SELECT 1 FROM ch_email_log el
          WHERE el.email_key = 'lifecycle:u' || u.id || ':' || {stage}
      )
"""

_VERIFY_1_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}
    FROM ch_users u
    {_OWNER_PROJECT}
    WHERE u.is_email_verified = FALSE
      AND u.created_at <= :now - INTERVAL '2 hours'
      AND {_NOT_CLAIMED.format(stage="'verify_1'")}
      AND {_SUPPRESSED}
""")

_VERIFY_2_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}, TRUE AS verify_1_sent
    FROM ch_users u
    {_OWNER_PROJECT}
    WHERE u.is_email_verified = FALSE
      AND u.created_at <= :now - INTERVAL '26 hours'
      -- verify_2 escalates verify_1: only after the first nudge actually went
      AND EXISTS (
          SELECT 1 FROM ch_email_log el
          WHERE el.user_id = u.id
            AND el.email_type = 'lifecycle_verify_1'
            AND el.sent_ok = TRUE
      )
      AND {_NOT_CLAIMED.format(stage="'verify_2'")}
      AND {_SUPPRESSED}
""")

_NO_KEY_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}
    FROM ch_users u
    {_OWNER_PROJECT}
    WHERE u.is_email_verified = TRUE
      AND u.email_verified_at IS NOT NULL
      AND u.email_verified_at <= :now - INTERVAL '24 hours'
      AND NOT EXISTS (
          SELECT 1
          FROM ch_api_keys k
          JOIN ch_project_members mk
            ON mk.project_id = k.project_id
           AND mk.user_id = u.id
           AND mk.role = 'owner'
          WHERE k.key_type = 'private'
      )
      AND {_NOT_CLAIMED.format(stage="'no_key'")}
      AND {_SUPPRESSED}
""")

_NO_CALL_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}, ok.oldest_key_created_at
    FROM ch_users u
    {_OWNER_PROJECT}
    JOIN LATERAL (
        SELECT MIN(k.created_at) AS oldest_key_created_at
        FROM ch_api_keys k
        JOIN ch_project_members mk
          ON mk.project_id = k.project_id
         AND mk.user_id = u.id
         AND mk.role = 'owner'
        WHERE k.key_type = 'private'
    ) ok ON ok.oldest_key_created_at IS NOT NULL
    WHERE u.is_email_verified = TRUE
      AND ok.oldest_key_created_at <= :now - INTERVAL '24 hours'
      AND NOT EXISTS (
          SELECT 1
          FROM ch_api_keys k2
          JOIN ch_project_members mk2
            ON mk2.project_id = k2.project_id
           AND mk2.user_id = u.id
           AND mk2.role = 'owner'
          WHERE k2.first_used_at IS NOT NULL
      )
      AND {_NOT_CLAIMED.format(stage="'no_call'")}
      AND {_SUPPRESSED}
""")

# ch_activity.project_id is VARCHAR: ALWAYS a.project_id = p.id::text (cast
# the ch_projects side) so idx_ch_activity_project_status_ts stays usable.
_STUCK_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}, fk.first_used_at, fc.failed_count
    FROM ch_users u
    {_OWNER_PROJECT}
    JOIN LATERAL (
        SELECT MIN(k.first_used_at) AS first_used_at
        FROM ch_api_keys k
        JOIN ch_project_members mk
          ON mk.project_id = k.project_id
         AND mk.user_id = u.id
         AND mk.role = 'owner'
        WHERE k.first_used_at IS NOT NULL
    ) fk ON fk.first_used_at IS NOT NULL
    JOIN LATERAL (
        SELECT COUNT(*) AS failed_count
        FROM ch_activity a
        JOIN ch_projects p ON a.project_id = p.id::text
        JOIN ch_project_members mp
          ON mp.project_id = p.id
         AND mp.user_id = u.id
         AND mp.role = 'owner'
        WHERE a.status = 'Failed'
    ) fc ON TRUE
    WHERE fk.first_used_at <= :now - INTERVAL '4 hours'
      AND fc.failed_count >= 2
      AND NOT EXISTS (
          SELECT 1
          FROM ch_activity a2
          JOIN ch_projects p2 ON a2.project_id = p2.id::text
          JOIN ch_project_members mp2
            ON mp2.project_id = p2.id
           AND mp2.user_id = u.id
           AND mp2.role = 'owner'
          WHERE a2.status = 'Success'
      )
      AND {_NOT_CLAIMED.format(stage="'stuck'")}
      AND {_SUPPRESSED}
""")

_ACTIVATED_CANDIDATES = text(f"""
    SELECT {_BASE_COLS}, fk.first_used_at, fk.first_used_surface
    FROM ch_users u
    {_OWNER_PROJECT}
    JOIN LATERAL (
        SELECT k.first_used_at, k.first_used_surface
        FROM ch_api_keys k
        JOIN ch_project_members mk
          ON mk.project_id = k.project_id
         AND mk.user_id = u.id
         AND mk.role = 'owner'
        WHERE k.first_used_at IS NOT NULL
        ORDER BY k.first_used_at
        LIMIT 1
    ) fk ON TRUE
    WHERE {_NOT_CLAIMED.format(stage="'activated'")}
      AND {_SUPPRESSED}
""")

# Latest failure detail for the stuck email (best-effort enrichment).
_STUCK_LAST_ERROR = text("""
    SELECT a.error_type, a.error_message
    FROM ch_activity a
    JOIN ch_projects p ON a.project_id = p.id::text
    JOIN ch_project_members mp
      ON mp.project_id = p.id
     AND mp.user_id = :user_id
     AND mp.role = 'owner'
    WHERE a.status = 'Failed'
    ORDER BY a."timestamp" DESC
    LIMIT 1
""")

# ---------------------------------------------------------------------------
# Recheck queries: ONE indexed SELECT per stage re-asserting the condition
# between the committed claim and the send (and again on retry). All embed
# the suppression predicates so an opt-out that landed mid-pass also stops
# the send. Returning no row = condition cleared.
# ---------------------------------------------------------------------------

_RECHECK_SUPPRESSED = """
      u.active IS TRUE
      AND u.banned_at IS NULL
      AND u.lifecycle_opt_out_at IS NULL
      AND u.email_suppressed_at IS NULL
"""

_RECHECK_UNVERIFIED = text(f"""
    SELECT 1 FROM ch_users u
    WHERE u.id = :user_id
      AND u.is_email_verified = FALSE
      AND {_RECHECK_SUPPRESSED}
""")

_RECHECK_NO_KEY = text(f"""
    SELECT 1 FROM ch_users u
    WHERE u.id = :user_id
      AND u.is_email_verified = TRUE
      AND NOT EXISTS (
          SELECT 1
          FROM ch_api_keys k
          JOIN ch_project_members mk
            ON mk.project_id = k.project_id
           AND mk.user_id = u.id
           AND mk.role = 'owner'
          WHERE k.key_type = 'private'
      )
      AND {_RECHECK_SUPPRESSED}
""")

_RECHECK_NO_CALL = text(f"""
    SELECT 1 FROM ch_users u
    WHERE u.id = :user_id
      AND NOT EXISTS (
          SELECT 1
          FROM ch_api_keys k
          JOIN ch_project_members mk
            ON mk.project_id = k.project_id
           AND mk.user_id = u.id
           AND mk.role = 'owner'
          WHERE k.first_used_at IS NOT NULL
      )
      AND {_RECHECK_SUPPRESSED}
""")

_RECHECK_STUCK = text(f"""
    SELECT 1 FROM ch_users u
    WHERE u.id = :user_id
      AND NOT EXISTS (
          SELECT 1
          FROM ch_activity a
          JOIN ch_projects p ON a.project_id = p.id::text
          JOIN ch_project_members mp
            ON mp.project_id = p.id
           AND mp.user_id = u.id
           AND mp.role = 'owner'
          WHERE a.status = 'Success'
      )
      AND {_RECHECK_SUPPRESSED}
""")

# activated cannot un-happen; the recheck only re-asserts suppression.
_RECHECK_SUPPRESSION_ONLY = text(f"""
    SELECT 1 FROM ch_users u
    WHERE u.id = :user_id
      AND {_RECHECK_SUPPRESSED}
""")

_RECHECKS: dict[str, TextClause] = {
    "lifecycle_verify_1": _RECHECK_UNVERIFIED,
    "lifecycle_verify_2": _RECHECK_UNVERIFIED,
    "lifecycle_no_key": _RECHECK_NO_KEY,
    "lifecycle_no_call": _RECHECK_NO_CALL,
    "lifecycle_stuck": _RECHECK_STUCK,
    "lifecycle_activated": _RECHECK_SUPPRESSION_ONLY,
    "founder_call": _RECHECK_SUPPRESSION_ONLY,
}

# ---------------------------------------------------------------------------
# Claim / outcome SQL (ch_email_log with the migration-031 user_id column).
# ---------------------------------------------------------------------------

_RECIPIENT = text("""
    SELECT u.id, u.email, u.full_name,
           u.active, u.banned_at, u.lifecycle_opt_out_at, u.email_suppressed_at
    FROM ch_users u
    WHERE u.id = :user_id
""")

_CLAIM = text("""
    INSERT INTO ch_email_log
        (project_id, user_id, email_type, email_key, recipient, sent_ok,
         attempts, context, created_at)
    VALUES (:project_id, :user_id, :email_type, :email_key, :recipient, FALSE,
            0, CAST(:context AS JSONB), :claimed_at)
    ON CONFLICT (email_key) DO NOTHING
    RETURNING id
""")

_RECORD_OUTCOME = text("""
    UPDATE ch_email_log
       SET sent_ok = :ok,
           attempts = attempts + 1,
           sent_at = CASE WHEN :ok THEN CAST(:now AS timestamptz) END,
           last_error = :error
     WHERE id = :id
""")

# attempts pinned to the cap: 'closed', the retry pass never picks it up.
_MARK_CLEARED = text(f"""
    UPDATE ch_email_log
       SET attempts = {int(RETRY_CAP)}, last_error = 'condition_cleared'
     WHERE id = :id
""")

_MARK_DRY_RUN = text("""
    UPDATE ch_email_log
       SET last_error = 'dry_run'
     WHERE id = :id
""")

# Lifecycle-scoped retry scan; the mirror of subscription_emails._RETRYABLE_IDS
# (which now excludes these types). Dry-run claims are never retried — they
# are released via --purge-dry-run-claims instead.
_RETRYABLE_IDS = text(f"""
    SELECT id
    FROM ch_email_log
    WHERE sent_ok = FALSE
      AND (email_type LIKE 'lifecycle%' OR email_type = 'founder_call')
      AND COALESCE(last_error, '') <> 'dry_run'
      AND created_at > :now - INTERVAL '{int(RETRY_WINDOW_HOURS)} hours'
      AND created_at < :now - INTERVAL '{int(RETRY_GRACE_MINUTES)} minutes'
      AND attempts < :max_cap
    ORDER BY created_at
""")

_RETRY_CLAIM_ROW = text("""
    SELECT id, project_id, user_id, email_type, email_key, recipient, context,
           attempts
    FROM ch_email_log
    WHERE id = :id AND sent_ok = FALSE
    FOR UPDATE SKIP LOCKED
""")

_MINT_VERIFY_TOKEN = text("""
    INSERT INTO ch_email_verify_tokens
        (user_id, token_hash, expires_at, created_at)
    VALUES (:user_id, :token_hash, :expires_at, :now)
""")


# ---------------------------------------------------------------------------
# Send dispatch — email_type -> lifecycle_mail sender. Module attribute
# access so harnesses can monkeypatch lifecycle_mail functions directly.
# ---------------------------------------------------------------------------

_SENDERS: dict[str, Callable[[dict, dict, datetime], bool]] = {
    "lifecycle_verify_1": lifecycle_mail.unverified_nudge_1,
    "lifecycle_verify_2": lifecycle_mail.unverified_nudge_2,
    "lifecycle_no_key": lifecycle_mail.verified_no_key,
    "lifecycle_no_call": lifecycle_mail.key_never_used,
    "lifecycle_stuck": lifecycle_mail.stuck,
    "lifecycle_activated": lifecycle_mail.activated,
    "founder_call": lifecycle_mail.founder_call,
}

Builder = Callable[[dict, datetime, datetime], Optional[LifecycleCandidate]]

# stage -> (candidate query, builder, needs_verify_token, enrich)
_STAGE_SPECS: dict[str, tuple[TextClause, Builder, bool]] = {
    "verify_1": (_VERIFY_1_CANDIDATES, build_verify_1_candidate, True),
    "verify_2": (_VERIFY_2_CANDIDATES, build_verify_2_candidate, True),
    "no_key": (_NO_KEY_CANDIDATES, build_no_key_candidate, False),
    "no_call": (_NO_CALL_CANDIDATES, build_no_call_candidate, False),
    "stuck": (_STUCK_CANDIDATES, build_stuck_candidate, False),
    "activated": (_ACTIVATED_CANDIDATES, build_activated_candidate, False),
}


def _lifecycle_recipient(db: Session, user_id: int) -> Optional[dict]:
    """The suppression/opt-out gate, checked BEFORE the claim. Returns the
    recipient dict the senders take, or None when this user must not receive
    lifecycle mail (the key stays unclaimed so a later un-suppression can
    still send a still-relevant stage)."""
    row = db.execute(_RECIPIENT, {"user_id": user_id}).mappings().first()
    if row is None:
        return None
    if (
        not row["active"]
        or row["banned_at"] is not None
        or row["lifecycle_opt_out_at"] is not None
        or row["email_suppressed_at"] is not None
    ):
        return None
    return {"user_id": int(row["id"]), "email": row["email"], "name": row["full_name"]}


def _frontend_base() -> str:
    return lifecycle_mail._frontend_base()


def _mint_verify_url(db: Session, user_id: int, now: datetime) -> str:
    """Mint a fresh verify token (raw never stored; SHA-256 hex only) and
    return the frontend landing URL — the same shape the backend builds at
    signup ({FRONTEND}/auth?mode=verify&token={raw})."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.execute(
        _MINT_VERIFY_TOKEN,
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": now + timedelta(hours=VERIFY_TOKEN_EXPIRY_HOURS),
            "now": now,
        },
    )
    db.commit()
    return f"{_frontend_base()}/auth?mode=verify&token={raw}"


def _claim(db: Session, candidate: LifecycleCandidate, recipient_email: str) -> Optional[int]:
    row = db.execute(
        _CLAIM,
        {
            "project_id": candidate.project_id,
            "user_id": candidate.user_id,
            "email_type": candidate.email_type,
            "email_key": candidate.email_key,
            "recipient": recipient_email,
            "context": json.dumps(candidate.context),
            # Fresh clock read, NOT the pass moment (see subscription_emails).
            "claimed_at": datetime.now(timezone.utc),
        },
    ).first()
    db.commit()
    return row[0] if row else None


def _recheck_holds(db: Session, email_type: str, user_id: int) -> bool:
    recheck = _RECHECKS.get(email_type)
    if recheck is None:
        return True  # unknown type from a future build: do not block it
    return db.execute(recheck, {"user_id": user_id}).first() is not None


def _claim_and_send(
    db: Session,
    candidate: LifecycleCandidate,
    recipient: dict,
    now: datetime,
    dry_run: bool,
) -> str:
    """Durable claim BEFORE any send; returns one of
    sent | send_failed | already_claimed | dry_run | condition_cleared."""
    row_id = _claim(db, candidate, recipient["email"])
    if row_id is None:
        return "already_claimed"

    if dry_run:
        db.execute(_MARK_DRY_RUN, {"id": row_id})
        db.commit()
        logger.info(
            "[lifecycle] DRY RUN claim %s (user %s) — send skipped",
            candidate.email_key, candidate.user_id,
        )
        return "dry_run"

    # Recheck between claim and send: the world may have moved since the scan.
    if not _recheck_holds(db, candidate.email_type, candidate.user_id):
        db.execute(_MARK_CLEARED, {"id": row_id})
        db.commit()
        return "condition_cleared"

    ok = _SENDERS[candidate.email_type](recipient, candidate.context, now)
    db.execute(
        _RECORD_OUTCOME,
        {
            "ok": ok,
            "now": now,
            "error": None if ok else "send returned False",
            "id": row_id,
        },
    )
    db.commit()
    return "sent" if ok else "send_failed"


def _run_stage(
    db: Session,
    stage: str,
    now: datetime,
    epoch: datetime,
    dry_run: bool,
    budget: dict,
    deadline: Optional[float],
) -> dict:
    """One stage sweep: SELECT candidates, build, gate, claim, recheck, send."""
    query, builder, needs_verify = _STAGE_SPECS[stage]
    counts = {
        "candidates": 0, "claimed": 0, "sent": 0, "failed": 0,
        "skipped": 0, "already": 0, "cleared": 0, "dry_run": 0,
    }
    rows = db.execute(query, {"now": now, "epoch": epoch}).mappings().all()
    for raw in rows:
        row = dict(raw)
        if stage == "stuck":
            _enrich_stuck_row(db, row)
        candidate = builder(row, now, epoch)
        if candidate is None:
            continue
        counts["candidates"] += 1
        if budget["remaining"] <= 0:
            logger.info("[lifecycle] claim budget exhausted in stage %s", stage)
            break
        if deadline is not None and time.monotonic() > deadline:
            logger.info("[lifecycle] time budget exhausted in stage %s", stage)
            break
        # Suppression gate BEFORE the claim.
        recipient = _lifecycle_recipient(db, candidate.user_id)
        if recipient is None:
            counts["skipped"] += 1
            continue
        # Live verify nudges need a working link: mint a fresh token now so
        # the claimed context (which the retry pass rebuilds from) carries it.
        if needs_verify and not dry_run:
            candidate = replace(
                candidate,
                context={
                    **candidate.context,
                    "verify_url": _mint_verify_url(db, candidate.user_id, now),
                },
            )
        outcome = _claim_and_send(db, candidate, recipient, now, dry_run)
        if outcome == "already_claimed":
            counts["already"] += 1
            continue
        budget["remaining"] -= 1  # every durable claim consumes budget
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


def _enrich_stuck_row(db: Session, row: dict) -> None:
    """Best-effort: attach the latest failure's error_type/error_message."""
    row.setdefault("error_type", None)
    row.setdefault("error_message", None)
    hit = db.execute(_STUCK_LAST_ERROR, {"user_id": row["user_id"]}).first()
    if hit is not None:
        row["error_type"], row["error_message"] = hit[0], hit[1]


def _retry_unsent(db: Session, now: datetime, dry_run: bool) -> dict:
    """Bounded retry of claimed-but-unsent lifecycle rows, mirroring
    subscription_emails._retry_unsent (short per-row transactions, FOR UPDATE
    SKIP LOCKED). A failed stage recheck closes the row as condition_cleared.
    In dry-run mode the whole sub-pass is a no-op (nothing may send)."""
    counts = {"retried": 0, "sent": 0, "failed": 0, "cleared": 0}
    if dry_run:
        return counts
    ids = [
        r[0]
        for r in db.execute(_RETRYABLE_IDS, {"now": now, "max_cap": RETRY_CAP}).all()
    ]
    db.commit()  # end the scan snapshot before taking row locks
    for row_id in ids:
        row = db.execute(_RETRY_CLAIM_ROW, {"id": row_id}).mappings().first()
        if row is None:  # concurrent run holds or already finished it
            db.commit()
            continue
        sender = _SENDERS.get(row["email_type"])
        if row["attempts"] >= RETRY_CAP or sender is None or row["user_id"] is None:
            db.commit()
            continue
        if not _recheck_holds(db, row["email_type"], int(row["user_id"])):
            db.execute(_MARK_CLEARED, {"id": row["id"]})
            db.commit()
            counts["cleared"] += 1
            continue
        context = row["context"] or {}
        if isinstance(context, str):  # driver-dependent JSONB decoding
            context = json.loads(context)
        counts["retried"] += 1
        recipient = {
            "user_id": int(row["user_id"]),
            "email": row["recipient"],
            "name": "",
        }
        ok = sender(recipient, context, now)
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
        counts["sent" if ok else "failed"] += 1
    return counts


def list_candidates(
    now: Optional[datetime] = None,
    epoch_value: Optional[str] = None,
    stages: Optional[list[str]] = None,
) -> dict:
    """Zero-write preview: run the candidate queries + builders and return
    what WOULD be claimed. Never claims, never mints tokens, never sends."""
    moment = now or datetime.now(timezone.utc)
    epoch = resolve_epoch(epoch_value)
    wanted = [s for s in _STAGES if stages is None or s in stages]
    preview: dict[str, list[dict]] = {}
    db = get_db()
    try:
        for stage in wanted:
            query, builder, _needs_verify = _STAGE_SPECS[stage]
            out = []
            for raw in db.execute(query, {"now": moment, "epoch": epoch}).mappings():
                row = dict(raw)
                if stage == "stuck":
                    _enrich_stuck_row(db, row)
                candidate = builder(row, moment, epoch)
                if candidate is None:
                    continue
                out.append(
                    {
                        "user_id": candidate.user_id,
                        "email": row["email"],
                        "email_key": candidate.email_key,
                        "email_type": candidate.email_type,
                        "context": candidate.context,
                    }
                )
            preview[stage] = out
        # founder_call preview (sibling module; read-only, never claims).
        if (
            (stages is None or "founder_call" in stages)
            and _founder_call is not None
            and hasattr(_founder_call, "list_founder_call_candidates")
        ):
            preview["founder_call"] = _founder_call.list_founder_call_candidates(
                db, moment, epoch
            )
    finally:
        db.close()
    return preview


def run_lifecycle_pass(
    now: Optional[datetime] = None,
    *,
    dry_run: Optional[bool] = None,
    epoch_value: Optional[str] = None,
    only_stages: Optional[list[str]] = None,
    skip_stages: Optional[list[str]] = None,
    max_claims: Optional[int] = None,
    skip_retry: bool = False,
    time_budget_seconds: Optional[float] = None,
    force: bool = False,
) -> dict:
    """One full lifecycle sweep. Sync; script/systemd context.

    Fresh Session per stage so one poisoned transaction cannot sink the rest
    (mirrors subscription_emails.run_email_pass). Returns a summary the runner
    maps to exit codes."""
    if not getattr(config, "LIFECYCLE_EMAILS_ENABLED", False) and not force:
        return {"status": "disabled"}
    epoch = resolve_epoch(epoch_value)  # raises when unset — refuse, never default
    effective_dry_run = (
        dry_run if dry_run is not None else getattr(config, "LIFECYCLE_DRY_RUN", True)
    )
    moment = now or datetime.now(timezone.utc)
    budget = {
        "remaining": (
            max_claims
            if max_claims is not None
            else getattr(config, "MAX_LIFECYCLE_SENDS_PER_TICK", 25)
        )
    }
    deadline = (
        time.monotonic() + time_budget_seconds if time_budget_seconds else None
    )
    stages = [s for s in _STAGES if only_stages is None or s in only_stages]
    if skip_stages:
        stages = [s for s in stages if s not in skip_stages]

    results: dict[str, dict] = {}
    crashed: list[str] = []
    for stage in stages:
        db = get_db()
        try:
            results[stage] = _run_stage(
                db, stage, moment, epoch, effective_dry_run, budget, deadline
            )
        except Exception as exc:  # noqa: BLE001 — one broken stage must not block the rest
            logger.exception("[lifecycle] stage %s crashed", stage)
            results[stage] = {"error": str(exc)}
            crashed.append(stage)
        finally:
            db.close()

    # founder_call: owned by the (optional) sibling module; skips cleanly when
    # the module or FOUNDER_CALL_URL is absent. Honors --only-stage /
    # --skip-stage like the six built-in stages.
    founder_call_wanted = (
        (only_stages is None or "founder_call" in only_stages)
        and not (skip_stages and "founder_call" in skip_stages)
    )
    if (
        founder_call_wanted
        and _founder_call is not None
        and hasattr(_founder_call, "run_founder_call_pass")
        and getattr(config, "FOUNDER_CALL_URL", None)
    ):
        db = get_db()
        try:
            results["founder_call"] = _founder_call.run_founder_call_pass(
                db, moment, epoch, budget,
                dry_run=effective_dry_run, deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lifecycle] founder_call pass crashed")
            results["founder_call"] = {"error": str(exc)}
            crashed.append("founder_call")
        finally:
            db.close()

    retry: dict[str, Any] = {"retried": 0, "sent": 0, "failed": 0, "cleared": 0}
    if not skip_retry:
        db = get_db()
        try:
            retry = _retry_unsent(db, moment, effective_dry_run)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lifecycle] retry sub-pass crashed")
            retry = {"error": str(exc), "failed": 0}
            crashed.append("retry")
        finally:
            db.close()

    failed_sends = sum(
        r.get("failed", 0) for r in results.values() if isinstance(r, dict)
    ) + retry.get("failed", 0)
    return {
        "status": "ok" if not crashed and failed_sends == 0 else "partial",
        "dry_run": effective_dry_run,
        "epoch": epoch.isoformat(),
        "claims_remaining": budget["remaining"],
        "stages": results,
        "retry": retry,
        "crashed_stages": crashed,
        "failed_sends": failed_sends,
    }
