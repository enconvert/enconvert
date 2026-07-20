"""Subscription email pass verification harness (migration 019).

    DATABASE_URL=postgresql://localhost/scratch_emailpass \\
        .venv/bin/python tests/v2/verify_subscription_emails.py

SAFETY: refuses to run unless the DATABASE_URL database name contains
"scratch" — this harness WRITES (drop_all/create_all, seeds, live pass runs)
and must never touch conversionhub or prod. Create/drop the scratch DB with:

    createdb scratch_emailpass      # before
    dropdb scratch_emailpass        # after

Covers, against real Postgres through the real code paths (the five Brevo
senders are replaced with recorders; nothing leaves the machine):
  1. Full pass over seeded subscriptions: exactly the expected sends fire
     (trial reminder, renewal notice, upcoming plan + storage charges,
     storage-lapse warning, overage receipt with conversion count), the
     excluded neighbors stay silent (cancelled trial, first activation,
     free plan, active storage add-on, trial-boundary upcoming charge),
     ch_email_log rows land with sent_ok, and the legacy dedup columns
     (trial_reminder_sent_at / storage_lapse_warned_at) are stamped.
  2. Idempotent rerun with the same clock: zero new sends (claim conflicts).
  3. Inactive project owner: candidate skipped with NO claim row, so the
     email can still go out once the owner is fixed.
  4. Failed send -> claimed-but-unsent row -> retry sub-pass resends once the
     sender recovers; legacy stamp lands on the retry success.
  5. Persistent failure hits the attempts cap and stops retrying.
  6. Expired reminder (event already passed) is marked expired, not resent.
  7. Pre-claimed key (overlapping run simulation) is skipped without a send.
  8. Multi-month catch-up: real billing_rotation.rotate_project_period walks
     three boundaries, then the pass sends exactly ONE renewal notice.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

_db_url = os.environ.get("DATABASE_URL", "")
_db_name = _db_url.rsplit("/", 1)[-1].split("?")[0] if _db_url else ""
if "scratch" not in _db_name:
    print(
        "REFUSING TO RUN: DATABASE_URL must point at a scratch database "
        f"(got {_db_name or '<unset>'}). This harness writes."
    )
    sys.exit(2)

from dateutil.relativedelta import relativedelta  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import SQLModel, select  # noqa: E402

import models  # noqa: E402  (registers every table on SQLModel.metadata)
from models import (  # noqa: E402
    EmailLog,
    PaymentHistory,
    Plan,
    Project,
    ProjectMember,
    StoragePlan,
    Subscription,
    UsagePeriod,
    User,
)
from services import billing_rotation, subscription_emails  # noqa: E402
from services.subscription_emails import _key_ts, run_email_pass  # noqa: E402
from utils import email_notifier  # noqa: E402
from utils.postgres import engine, get_db  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
NOW = datetime.now(timezone.utc)


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def summarize() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


class SendRecorder:
    """Replaces the five Brevo senders; records calls, fails on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fail_types: set[str] = set()

    def make(self, email_type: str):
        def _send(*args, **kwargs) -> bool:
            self.calls.append((email_type, args))
            return email_type not in self.fail_types

        return _send

    def sent(self, email_type: str, recipient: str) -> list[tuple]:
        return [
            args for etype, args in self.calls
            if etype == email_type and args and args[0] == recipient
        ]


RECORDER = SendRecorder()
for _fn_name, _etype in (
    ("send_trial_ending_email", "trial_reminder"),
    ("send_storage_lapse_warning_email", "storage_lapse"),
    ("send_overage_receipt_email", "overage_receipt"),
    ("send_renewal_notice_email", "renewal"),
    ("send_upcoming_charge_email", "upcoming_charge"),
):
    # subscription_emails calls these through the module attribute, so
    # rebinding here intercepts every send.
    setattr(email_notifier, _fn_name, RECORDER.make(_etype))


_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


def seed_project(db, label: str, owner_active: bool = True) -> tuple[int, str]:
    """Project + active-or-not owner; returns (project_id, owner_email)."""
    email = f"owner-{label}-{_next()}@example.test"
    user = User(
        email=email, full_name=f"Owner {label}", password_hash="x",
        active=owner_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    project = Project(name=f"proj-{label}")
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    db.commit()
    return project.id, email


def seed_sub(db, project_id: int, plan: Plan, **overrides) -> Subscription:
    fields = dict(
        project_id=project_id,
        plan_id=plan.id,
        status="active",
        current_period_start=NOW - timedelta(days=15),
        current_period_end=NOW + timedelta(days=15),
        effective_conversion_limit=plan.conversion_limit,
        effective_max_file_size=plan.max_file_size,
        effective_file_retention_hours=plan.file_retention_hours,
        effective_batch_limit=plan.batch_limit,
    )
    fields.update(overrides)
    sub = Subscription(**fields)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def seed_period(db, project_id: int, plan_id: int, start, end, **overrides) -> UsagePeriod:
    period = UsagePeriod(
        project_id=project_id, plan_id=plan_id,
        period_start=start, period_end=end, **overrides,
    )
    db.add(period)
    db.commit()
    return period


def email_log_row(db, email_key: str) -> EmailLog | None:
    return db.exec(select(EmailLog).where(EmailLog.email_key == email_key)).first()


def main() -> int:
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    db = get_db()
    paid = Plan(
        slug="pro-test", name="Pro", price_monthly=2900, conversion_limit=10000,
        overage_rate_cents=5.0, overage_allowed=True,
    )
    free = Plan(slug="free-test", name="Free", price_monthly=0)
    # 1 GB, not 50: the ORM's plain-int storage_bytes becomes INT4 under
    # create_all (prod DDL is BIGINT), and 50 GB overflows it in scratch DBs.
    storage_plan = StoragePlan(slug="stor-test", name="Storage 1GB",
                               storage_bytes=1024**3, price_monthly=500)
    db.add(paid)
    db.add(free)
    db.add(storage_plan)
    db.commit()
    db.refresh(paid)
    db.refresh(free)
    db.refresh(storage_plan)

    # --- Scenario seeds -----------------------------------------------------
    # A: trial ending in 2 days -> trial reminder (and ONLY that: its
    #    current_period_end equals trial_end, so the upcoming-charge builder
    #    must bail on the trial boundary).
    pid_a, mail_a = seed_project(db, "trial")
    sub_a = seed_sub(
        db, pid_a, paid,
        payment_provider="paypal", payment_subscription_id="I-TRIAL-A",
        trial_end=NOW + timedelta(days=2),
        current_period_start=NOW - timedelta(days=12),
        current_period_end=NOW + timedelta(days=2),
    )

    # B: cancelled trial (payment refs nulled) -> silent.
    pid_b, mail_b = seed_project(db, "trial-cancelled")
    seed_sub(db, pid_b, paid, trial_end=NOW + timedelta(days=1),
             current_period_end=NOW + timedelta(days=1))

    # C: rotated boundary yesterday -> renewal notice (prior period seeded
    #    contiguously so the EXISTS guard passes).
    pid_c, mail_c = seed_project(db, "renewal")
    c_start = NOW - timedelta(days=1)
    seed_sub(db, pid_c, paid,
             payment_provider="paypal", payment_subscription_id="I-REN-C",
             current_period_start=c_start,
             current_period_end=c_start + timedelta(days=30))
    seed_period(db, pid_c, paid.id, c_start - timedelta(days=30), c_start)
    seed_period(db, pid_c, paid.id, c_start, c_start + timedelta(days=30))

    # D: same window but FIRST activation (no contiguous prior period) -> silent.
    pid_d, mail_d = seed_project(db, "activation")
    d_start = NOW - timedelta(days=1)
    seed_sub(db, pid_d, paid,
             payment_provider="paypal", payment_subscription_id="I-ACT-D",
             current_period_start=d_start,
             current_period_end=d_start + timedelta(days=30))
    seed_period(db, pid_d, paid.id, d_start, d_start + timedelta(days=30))

    # E: period ends tomorrow with accrued overage -> upcoming charge with two
    #    line items; plus a captured overage marker row -> receipt with count.
    pid_e, mail_e = seed_project(db, "upcoming")
    e_start = NOW - timedelta(days=29)
    seed_sub(db, pid_e, paid,
             payment_provider="paypal", payment_subscription_id="I-UPC-E",
             overage_enabled=True,
             current_period_start=e_start,
             current_period_end=NOW + timedelta(days=1))
    seed_period(db, pid_e, paid.id, e_start, NOW + timedelta(days=1),
                overage_conversions=37)
    marker = f"overage:{pid_e}:{_key_ts(e_start)}"
    db.add(PaymentHistory(
        project_id=pid_e, paypal_transaction_id=marker,
        subscription_type="overage", status="CAPTURED",
        amount_value="1.85", amount_currency="USD",
        payment_time=NOW - timedelta(hours=1),
    ))
    db.commit()

    # F: free plan rotated yesterday -> silent everywhere (price gate).
    pid_f, mail_f = seed_project(db, "free")
    f_start = NOW - timedelta(days=1)
    seed_sub(db, pid_f, free, current_period_start=f_start,
             current_period_end=f_start + timedelta(days=30))
    seed_period(db, pid_f, free.id, f_start - timedelta(days=30), f_start)

    # G: cancelled storage add-on lapsing in 2 days with stored bytes ->
    #    storage-lapse warning (free plan side keeps it out of other passes).
    pid_g, mail_g = seed_project(db, "storage-lapse")
    sub_g = seed_sub(db, pid_g, free, storage_plan_id=storage_plan.id,
                     storage_payment_subscription_id=None,
                     storage_period_end=NOW + timedelta(days=2))
    db.execute(text("UPDATE ch_projects SET storage_used = 123456789 WHERE id = :p"),
               {"p": pid_g})
    db.commit()

    # H: ACTIVE storage add-on renewing tomorrow -> upcoming storage charge,
    #    and NOT a lapse warning.
    pid_h, mail_h = seed_project(db, "storage-active")
    seed_sub(db, pid_h, free, storage_plan_id=storage_plan.id,
             storage_payment_subscription_id="I-STOR-H",
             storage_period_end=NOW + timedelta(days=1))

    # I: trial window but the owner account is suspended -> skipped, NO claim.
    pid_i, mail_i = seed_project(db, "inactive-owner", owner_active=False)
    seed_sub(db, pid_i, paid,
             payment_provider="paypal", payment_subscription_id="I-INA-I",
             trial_end=NOW + timedelta(days=1),
             current_period_end=NOW + timedelta(days=1))

    # --- 1. Full pass -------------------------------------------------------
    print("\n[1] full pass over seeded subscriptions")
    summary = run_email_pass(now=NOW)
    record("pass status ok", summary["status"] == "ok", str(summary))
    record("trial reminder sent to A", len(RECORDER.sent("trial_reminder", mail_a)) == 1)
    record("renewal notice sent to C", len(RECORDER.sent("renewal", mail_c)) == 1)
    upcoming_e = RECORDER.sent("upcoming_charge", mail_e)
    record("upcoming plan charge sent to E", len(upcoming_e) == 1)
    record(
        "E upcoming charge has plan + overage line items",
        len(upcoming_e) == 1 and len(upcoming_e[0][2]) == 2,
        str(upcoming_e[0][2]) if upcoming_e else "no call",
    )
    receipt_e = RECORDER.sent("overage_receipt", mail_e)
    record("overage receipt sent to E", len(receipt_e) == 1)
    record(
        "receipt carries conversion count 37",
        len(receipt_e) == 1 and receipt_e[0][5] == 37,
        str(receipt_e[0]) if receipt_e else "no call",
    )
    record("storage lapse warning sent to G", len(RECORDER.sent("storage_lapse", mail_g)) == 1)
    record("upcoming storage charge sent to H", len(RECORDER.sent("upcoming_charge", mail_h)) == 1)
    for label, mail in (("B cancelled trial", mail_b), ("D first activation", mail_d),
                        ("F free plan", mail_f), ("I inactive owner", mail_i)):
        got = [c for c in RECORDER.calls if c[1] and c[1][0] == mail]
        record(f"{label} stays silent", not got, str(got))
    record("H got no lapse warning", not RECORDER.sent("storage_lapse", mail_h))

    key_a = f"trial_reminder:{pid_a}:{_key_ts(sub_a.trial_end)}"
    row_a = email_log_row(db, key_a)
    record("A email_log row sent_ok", row_a is not None and row_a.sent_ok
           and row_a.attempts == 1, str(row_a))
    key_i = f"trial_reminder:{pid_i}:{_key_ts(NOW + timedelta(days=1))}"
    record("I skipped WITHOUT a claim row", email_log_row(db, key_i) is None)

    db.refresh(sub_a)
    record("A trial_reminder_sent_at stamped", sub_a.trial_reminder_sent_at is not None)
    db.refresh(sub_g)
    record("G storage_lapse_warned_at stamped", sub_g.storage_lapse_warned_at is not None)

    # --- 2. Idempotent rerun ------------------------------------------------
    print("\n[2] idempotent rerun, same clock")
    calls_before = len(RECORDER.calls)
    summary2 = run_email_pass(now=NOW)
    record("zero new sends on rerun", len(RECORDER.calls) == calls_before,
           f"{len(RECORDER.calls) - calls_before} new")
    already = sum(t.get("already", 0) for t in summary2["types"].values())
    record("rerun reports claim conflicts", already >= 6, f"already={already}")

    # --- 4. Failed send -> retry recovers on a LATER run ---------------------
    # Retry semantics: a failed first send is NOT retried in the same run or
    # by an immediate re-run (the grace-period age floor defers it to a later
    # timer fire, so one Brevo outage cannot burn the whole attempt budget).
    print("\n[4] failed send, then retry recovery on a later run")
    pid_k, mail_k = seed_project(db, "retry")
    sub_k = seed_sub(db, pid_k, paid,
                     payment_provider="paypal", payment_subscription_id="I-RET-K",
                     trial_end=NOW + timedelta(days=2),
                     current_period_end=NOW + timedelta(days=2))
    key_k = f"trial_reminder:{pid_k}:{_key_ts(sub_k.trial_end)}"
    RECORDER.fail_types.add("trial_reminder")
    run_email_pass(now=NOW)
    row_k = email_log_row(db, key_k)
    db.refresh(row_k)
    record("K claimed, ONE attempt only (no same-run retry)",
           row_k is not None and not row_k.sent_ok and row_k.attempts == 1,
           f"attempts={getattr(row_k, 'attempts', None)}")
    run_email_pass(now=NOW)  # immediate re-run: still inside the grace period
    db.refresh(row_k)
    record("K not re-attempted inside the grace period", row_k.attempts == 1,
           f"attempts={row_k.attempts}")
    RECORDER.fail_types.discard("trial_reminder")
    run_email_pass(now=NOW + timedelta(minutes=20))  # next timer fire
    db.refresh(row_k)
    record("K resent by a later run's retry sub-pass",
           row_k.sent_ok and row_k.attempts == 2, f"attempts={row_k.attempts}")
    db.refresh(sub_k)
    record("K legacy stamp lands on retry success", sub_k.trial_reminder_sent_at is not None)

    # --- 5. Attempts cap across successive runs ------------------------------
    print("\n[5] persistent failure hits the attempts cap")
    pid_l, mail_l = seed_project(db, "cap")
    sub_l = seed_sub(db, pid_l, paid,
                     payment_provider="paypal", payment_subscription_id="I-CAP-L",
                     trial_end=NOW + timedelta(days=2),
                     current_period_end=NOW + timedelta(days=2))
    key_l = f"trial_reminder:{pid_l}:{_key_ts(sub_l.trial_end)}"
    RECORDER.fail_types.add("trial_reminder")
    run_email_pass(now=NOW)                             # attempt 1 (claim)
    run_email_pass(now=NOW + timedelta(minutes=20))     # attempt 2 (retry)
    run_email_pass(now=NOW + timedelta(minutes=40))     # attempt 3 (retry, = cap)
    row_l = email_log_row(db, key_l)
    db.refresh(row_l)
    record("L attempts capped at 3", not row_l.sent_ok and row_l.attempts == 3,
           f"attempts={row_l.attempts}")
    calls_before = len(RECORDER.calls)
    run_email_pass(now=NOW + timedelta(minutes=60))
    row_l_sends = [c for c in RECORDER.calls[calls_before:] if c[1][0] == mail_l]
    record("L not retried past the cap", not row_l_sends)
    RECORDER.fail_types.discard("trial_reminder")

    # --- 6. Expired reminder ------------------------------------------------
    print("\n[6] expired reminder is not resent")
    expired_row = EmailLog(
        project_id=999999, email_type="upcoming_charge",
        email_key="upcoming_charge:plan:999999:19990101000000",
        recipient="ghost@example.test", sent_ok=False, attempts=1,
        context={"charge_date": (NOW - timedelta(days=1)).isoformat(),
                 "line_items": [],
                 "event_at": (NOW - timedelta(days=1)).isoformat()},
        created_at=NOW - timedelta(minutes=16),  # past the retry age floor
    )
    db.add(expired_row)
    db.commit()
    calls_before = len(RECORDER.calls)
    run_email_pass(now=NOW)
    db.refresh(expired_row)
    ghost_sends = [c for c in RECORDER.calls[calls_before:]
                   if c[1][0] == "ghost@example.test"]
    record("expired row marked, not sent",
           expired_row.last_error == "expired" and not ghost_sends,
           f"last_error={expired_row.last_error}")

    # --- 7. Pre-claimed key (overlapping run) --------------------------------
    print("\n[7] pre-claimed key is skipped")
    pid_m, mail_m = seed_project(db, "preclaimed")
    sub_m = seed_sub(db, pid_m, paid,
                     payment_provider="paypal", payment_subscription_id="I-PRE-M",
                     trial_end=NOW + timedelta(days=2),
                     current_period_end=NOW + timedelta(days=2))
    key_m = f"trial_reminder:{pid_m}:{_key_ts(sub_m.trial_end)}"
    db.add(EmailLog(project_id=pid_m, email_type="trial_reminder",
                    email_key=key_m, recipient=mail_m, sent_ok=True,
                    attempts=1, created_at=NOW, sent_at=NOW))
    db.commit()
    calls_before = len(RECORDER.calls)
    run_email_pass(now=NOW)
    m_sends = [c for c in RECORDER.calls[calls_before:] if c[1][0] == mail_m]
    record("M skipped (concurrent claim won)", not m_sends)

    # --- 8. Multi-month catch-up rotation -> ONE renewal notice --------------
    print("\n[8] catch-up rotation then renewal notice")
    pid_j, mail_j = seed_project(db, "catchup")
    j_end0 = NOW - relativedelta(months=2) - timedelta(days=1)
    seed_sub(db, pid_j, paid,
             payment_provider="paypal", payment_subscription_id="I-CAT-J",
             current_period_start=j_end0 - relativedelta(months=1),
             current_period_end=j_end0)
    seed_period(db, pid_j, paid.id, j_end0 - relativedelta(months=1), j_end0)
    result = asyncio.run(billing_rotation.rotate_project_period(pid_j))
    record("rotation walked 3 boundaries",
           result.get("status") == "ok" and result.get("rotations") == 3,
           str(result))
    calls_before = len(RECORDER.calls)
    run_email_pass(now=NOW)
    j_renewals = [c for c in RECORDER.calls[calls_before:]
                  if c[0] == "renewal" and c[1][0] == mail_j]
    record("exactly ONE renewal notice after catch-up", len(j_renewals) == 1,
           f"got {len(j_renewals)}")

    db.close()
    return summarize()


if __name__ == "__main__":
    sys.exit(main())
