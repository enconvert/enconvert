"""Unit tests — subscription email pass pure logic (services/subscription_emails).

Covers the DB-free candidate builders and helpers:
  * _key_ts: canonical UTC key fragments for aware non-UTC and naive inputs;
  * trial reminder: staleness cutoff, days_left floor of 1, key format;
  * storage lapse: staleness, byte humanization, key format;
  * overage receipt: marker parsing (good/malformed), verbatim marker key;
  * renewal: key derived from the final committed period_start;
  * upcoming plan charge: trial-boundary exclusion (trial_end == period_end),
    overage line-item guards (disabled / zero rate / zero accrued / sub-cent
    floor), passed-charge-date suppression;
  * upcoming storage charge: key format and amount formatting;
  * is_expired: event_at semantics per type.

House conventions (tests/v2/test_diff.py): synchronous ``def test_*`` (no
pytest-asyncio in this venv), pure inputs, no DB, no mocks.

Run: .venv/bin/python -m pytest tests/v2/test_subscription_emails_pure.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

# services.subscription_emails pulls in utils.postgres, whose engine is built
# at import time from DATABASE_URL. The pure tests never touch the DB, so a
# dummy URL satisfies create_engine (house pattern: test_endpoint_allowlist).
os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test_dummy"
)

from services.subscription_emails import (  # noqa: E402
    _humanize_bytes,
    _key_ts,
    build_overage_receipt_candidate,
    build_renewal_candidate,
    build_storage_lapse_candidate,
    build_trial_reminder_candidate,
    build_upcoming_plan_charge_candidate,
    build_upcoming_storage_charge_candidate,
    is_expired,
    parse_overage_marker,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _key_ts
# ---------------------------------------------------------------------------


def test_key_ts_normalizes_non_utc_aware_input():
    ist = timezone(timedelta(hours=5, minutes=30))
    aware_ist = datetime(2026, 8, 1, 5, 30, 0, tzinfo=ist)  # == 2026-08-01T00:00Z
    assert _key_ts(aware_ist) == "20260801000000"


def test_key_ts_treats_naive_as_utc():
    naive = datetime(2026, 8, 1, 0, 0, 0)
    assert _key_ts(naive) == "20260801000000"


# ---------------------------------------------------------------------------
# trial reminder
# ---------------------------------------------------------------------------


def _trial_row(**overrides):
    row = {
        "project_id": 7,
        "trial_end": NOW + timedelta(days=2),
        "plan_name": "Pro",
    }
    row.update(overrides)
    return row


def test_trial_candidate_key_and_context():
    cand = build_trial_reminder_candidate(_trial_row(), NOW)
    assert cand is not None
    assert cand.email_type == "trial_reminder"
    assert cand.email_key == f"trial_reminder:7:{_key_ts(NOW + timedelta(days=2))}"
    assert cand.context["plan_name"] == "Pro"
    assert cand.context["event_at"] == (NOW + timedelta(days=2)).isoformat()


def test_trial_days_left_floors_at_one():
    # 6 hours out: timedelta.days == 0, the email must still say "1 day"
    cand = build_trial_reminder_candidate(
        _trial_row(trial_end=NOW + timedelta(hours=6)), NOW
    )
    assert cand is not None
    assert cand.context["days_left"] == 1


def test_trial_already_ended_produces_no_candidate():
    assert build_trial_reminder_candidate(
        _trial_row(trial_end=NOW - timedelta(minutes=1)), NOW
    ) is None


# ---------------------------------------------------------------------------
# storage lapse
# ---------------------------------------------------------------------------


def test_storage_lapse_candidate_and_humanized_bytes():
    row = {
        "project_id": 9,
        "storage_period_end": NOW + timedelta(days=1),
        "project_name": "My Project",
        "storage_used": 123456789,
    }
    cand = build_storage_lapse_candidate(row, NOW)
    assert cand is not None
    assert cand.email_key == f"storage_lapse:9:{_key_ts(NOW + timedelta(days=1))}"
    assert cand.context["storage_used_str"] == "117.7 MB"


def test_storage_lapse_past_end_produces_no_candidate():
    row = {
        "project_id": 9,
        "storage_period_end": NOW - timedelta(seconds=1),
        "project_name": "P",
        "storage_used": 1,
    }
    assert build_storage_lapse_candidate(row, NOW) is None


def test_humanize_bytes_units():
    assert _humanize_bytes(0) == "0 B"
    assert _humanize_bytes(512) == "512 B"
    assert _humanize_bytes(2048) == "2.0 KB"
    assert _humanize_bytes(5 * 1024**3) == "5.0 GB"


# ---------------------------------------------------------------------------
# overage receipt
# ---------------------------------------------------------------------------


def test_parse_overage_marker_roundtrip():
    period_start = datetime(2026, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
    marker = f"overage:42:{period_start.strftime('%Y%m%d%H%M%S')}"
    assert parse_overage_marker(marker) == period_start


def test_parse_overage_marker_rejects_malformed():
    assert parse_overage_marker("PAYPAL-TXN-8FX01") is None
    assert parse_overage_marker("overage:42") is None
    assert parse_overage_marker("overage:42:not-a-ts") is None


def test_overage_receipt_key_reuses_marker_verbatim():
    marker = "overage:42:20260603000000"
    row = {
        "project_id": 42,
        "paypal_transaction_id": marker,
        "amount_value": "1.85",
        "amount_currency": "USD",
        "payment_time": NOW - timedelta(hours=1),
        "plan_name": "Pro",
        "overage_conversions": 37,
    }
    cand = build_overage_receipt_candidate(row, NOW)
    assert cand is not None
    assert cand.email_key == f"overage_receipt:{marker}"
    assert cand.context["amount"] == "1.85"
    assert cand.context["overage_conversions"] == 37
    # receipts never expire inside the retry window
    assert "event_at" not in cand.context


# ---------------------------------------------------------------------------
# renewal
# ---------------------------------------------------------------------------


def test_renewal_candidate_key_uses_period_start():
    start = NOW - timedelta(days=1)
    row = {
        "project_id": 5,
        "current_period_start": start,
        "current_period_end": start + timedelta(days=30),
        "effective_conversion_limit": 10000,
        "plan_name": "Pro",
    }
    cand = build_renewal_candidate(row, NOW)
    assert cand is not None
    assert cand.email_key == f"renewal:5:{_key_ts(start)}"
    assert cand.context["conversion_limit"] == 10000


# ---------------------------------------------------------------------------
# upcoming plan charge
# ---------------------------------------------------------------------------


def _upcoming_row(**overrides):
    row = {
        "project_id": 11,
        "current_period_end": NOW + timedelta(days=1),
        "trial_end": None,
        "overage_enabled": True,
        "plan_name": "Pro",
        "price_monthly": 2900,
        "overage_rate_cents": 5.0,
        "overage_conversions": 37,
    }
    row.update(overrides)
    return row


def test_upcoming_charge_includes_plan_and_overage_lines():
    cand = build_upcoming_plan_charge_candidate(_upcoming_row(), NOW)
    assert cand is not None
    items = cand.context["line_items"]
    assert len(items) == 2
    assert items[0]["label"] == "Pro plan renewal"
    assert items[0]["amount"] == "29.00"
    assert "37 conversions" in items[1]["label"]
    assert items[1]["amount"] == "1.85"


def test_upcoming_charge_trial_boundary_is_excluded():
    # During a trial, trial_end == the first current_period_end: the trial
    # reminder already announces that charge, so this builder must bail.
    end = NOW + timedelta(days=1)
    assert build_upcoming_plan_charge_candidate(
        _upcoming_row(current_period_end=end, trial_end=end), NOW
    ) is None
    # A CONVERTED trial (trial_end strictly before the boundary) still emails.
    cand = build_upcoming_plan_charge_candidate(
        _upcoming_row(current_period_end=end, trial_end=end - timedelta(days=30)), NOW
    )
    assert cand is not None


def test_upcoming_charge_overage_guards():
    for overrides in (
        {"overage_enabled": False},
        {"overage_rate_cents": 0},
        {"overage_conversions": 0},
    ):
        cand = build_upcoming_plan_charge_candidate(_upcoming_row(**overrides), NOW)
        assert cand is not None
        assert len(cand.context["line_items"]) == 1, overrides


def test_upcoming_charge_sub_cent_overage_is_dropped():
    # 1 conversion at 0.9 cents < $0.01: rotation's capture skips it, so the
    # reminder must not predict it either.
    cand = build_upcoming_plan_charge_candidate(
        _upcoming_row(overage_conversions=1, overage_rate_cents=0.9), NOW
    )
    assert cand is not None
    assert len(cand.context["line_items"]) == 1


def test_upcoming_charge_past_date_suppressed():
    assert build_upcoming_plan_charge_candidate(
        _upcoming_row(current_period_end=NOW - timedelta(minutes=5)), NOW
    ) is None


# ---------------------------------------------------------------------------
# upcoming storage charge
# ---------------------------------------------------------------------------


def test_upcoming_storage_charge_candidate():
    row = {
        "project_id": 13,
        "storage_period_end": NOW + timedelta(days=2),
        "storage_plan_name": "Storage 50GB",
        "price_monthly": 500,
    }
    cand = build_upcoming_storage_charge_candidate(row, NOW)
    assert cand is not None
    assert cand.email_key == f"upcoming_charge:storage:13:{_key_ts(NOW + timedelta(days=2))}"
    assert cand.context["line_items"][0]["amount"] == "5.00"


# ---------------------------------------------------------------------------
# retry expiry semantics
# ---------------------------------------------------------------------------


def test_is_expired_reminder_types_expire_after_event():
    past = (NOW - timedelta(hours=1)).isoformat()
    future = (NOW + timedelta(hours=1)).isoformat()
    assert is_expired("trial_reminder", {"event_at": past}, NOW) is True
    assert is_expired("trial_reminder", {"event_at": future}, NOW) is False
    assert is_expired("upcoming_charge", {"event_at": past}, NOW) is True


def test_is_expired_receipts_and_renewals_never_expire():
    # No event_at in their context — a late receipt/notice is still correct.
    assert is_expired("overage_receipt", {"amount": "1.85"}, NOW) is False
    assert is_expired("renewal", {"plan_name": "Pro"}, NOW) is False
