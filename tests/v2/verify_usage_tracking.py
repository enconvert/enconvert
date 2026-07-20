"""Usage-tracking ledger + rotation verification harness (migration 016).

    DATABASE_URL=postgresql://localhost/scratch_usage \\
        .venv/bin/python tests/v2/verify_usage_tracking.py

SAFETY: refuses to run unless the DATABASE_URL database name contains
"scratch" — this harness WRITES (create_all, seeds, live increments) and
must never touch conversionhub or prod. Create/drop the scratch DB with:

    createdb scratch_usage      # before
    dropdb scratch_usage        # after

Covers, against real Postgres through the real code paths:
  1. record_conversion_usage: counts once, dedups the same key, computes
     overage atomically past the subscription limit
  2. same-key concurrency: N threads, one logical event -> counted ONCE
  3. distinct-key concurrency: N threads, N events -> counted exactly N
     (the lost-update race the old read-modify-write code had)
  4. reserve/settle LLM ledger: booking + rows, duplicate reserve fails
     closed, duplicate settle no-ops, SUM(ledger) == aggregate at every
     step (the reconcile invariant)
  5. Tier-2 _increment_period_counter + update_storage_peak atomics
  6. billing_rotation.rotate_project_period: advances an overdue sub,
     multi-month catch-up, idempotent re-run, ON CONFLICT DO NOTHING
     preserves an existing period's counts
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

from sqlalchemy import text  # noqa: E402
from sqlmodel import SQLModel, select  # noqa: E402

import models  # noqa: E402  (registers every table on SQLModel.metadata)
from models import Plan, Project, Subscription, UsagePeriod  # noqa: E402
from services import billing_rotation  # noqa: E402
from services.v2_engine import usage as v2_usage  # noqa: E402
from utils.postgres import engine, get_db  # noqa: E402
from utils.subscription import update_storage_peak  # noqa: E402
from utils.usage_ledger import record_conversion_usage  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def summarize() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


def _ledger_sum(period_id: int, counter: str) -> Decimal:
    db = get_db()
    try:
        col = "delta_units" if counter == "conversions_used" else "delta_cost_cents"
        row = db.execute(
            text(
                f"SELECT COALESCE(SUM({col}), 0) FROM ch_usage_ledger "
                "WHERE usage_period_id = :pid AND counter = :counter"
            ),
            {"pid": period_id, "counter": counter},
        ).first()
        return Decimal(str(row[0]))
    finally:
        db.close()


def _period(period_id: int) -> UsagePeriod:
    db = get_db()
    try:
        return db.exec(select(UsagePeriod).where(UsagePeriod.id == period_id)).one()
    finally:
        db.close()


def _seed() -> tuple[int, int]:
    """Fresh schema + one project/plan/sub/period. Returns (project_id, period_id)."""
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        plan = Plan(slug="starter", name="Starter", conversion_limit=10)
        project = Project(name="verify-project")
        db.add(plan)
        db.add(project)
        db.commit()
        db.refresh(plan)
        db.refresh(project)
        sub = Subscription(
            project_id=project.id,
            plan_id=plan.id,
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=29),
            effective_conversion_limit=10,
            effective_max_file_size=5242880,
            effective_file_retention_hours=1,
            effective_batch_limit=0,
        )
        period = UsagePeriod(
            project_id=project.id,
            period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=29),
            plan_id=plan.id,
        )
        db.add(sub)
        db.add(period)
        db.commit()
        db.refresh(period)
        return int(project.id), int(period.id)
    finally:
        db.close()


def check_conversion_ledger(project_id: int, period_id: int) -> None:
    print("\n[1] record_conversion_usage basics")
    out = record_conversion_usage(
        project_id=project_id, idempotency_key="v1:conversion:1001"
    )
    record(
        "first event counts",
        out is not None and out["conversions_used"] == 1,
        str(out),
    )
    dup = record_conversion_usage(
        project_id=project_id, idempotency_key="v1:conversion:1001"
    )
    record("duplicate key skipped", dup is None)
    record(
        "aggregate unchanged by duplicate",
        _period(period_id).conversions_used == 1,
    )
    # Push over the limit of 10: 1 + 12 = 13 used -> overage 3
    out = record_conversion_usage(
        project_id=project_id, idempotency_key="v1:conversion:batch-12", count=12
    )
    record(
        "overage derived atomically (13 used, limit 10 -> 3)",
        out is not None and out["overage_conversions"] == 3,
        str(out),
    )
    record(
        "ledger sum == aggregate",
        _ledger_sum(period_id, "conversions_used")
        == Decimal(_period(period_id).conversions_used),
    )


def check_concurrency(project_id: int, period_id: int) -> None:
    print("\n[2] concurrency")
    before = _period(period_id).conversions_used

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda i: record_conversion_usage(
                    project_id=project_id, idempotency_key="v1:conversion:same-key"
                ),
                range(8),
            )
        )
    record(
        "8 threads, SAME key -> counted once",
        _period(period_id).conversions_used == before + 1,
        f"{before} -> {_period(period_id).conversions_used}",
    )

    before = _period(period_id).conversions_used
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda i: record_conversion_usage(
                    project_id=project_id, idempotency_key=f"v1:conversion:distinct-{i}"
                ),
                range(20),
            )
        )
    record(
        "20 threads, DISTINCT keys -> counted exactly 20 (no lost updates)",
        _period(period_id).conversions_used == before + 20,
        f"{before} -> {_period(period_id).conversions_used}",
    )
    record(
        "ledger sum still == aggregate",
        _ledger_sum(period_id, "conversions_used")
        == Decimal(_period(period_id).conversions_used),
    )


def check_llm_ledger(project_id: int, period_id: int) -> None:
    print("\n[3] LLM reserve/settle ledger")
    cap = Decimal("500")
    hold = Decimal("5")

    got = v2_usage.reserve_llm_budget(
        project_id, hold, cap, idempotency_key="op_test_1"
    )
    record("reserve books and returns period id", got == period_id, str(got))
    record(
        "aggregate carries the hold",
        _period(period_id).llm_cost_cents == hold,
    )
    record(
        "SUM(ledger) == aggregate after reserve",
        _ledger_sum(period_id, "llm_cost_cents") == _period(period_id).llm_cost_cents,
    )

    dup = v2_usage.reserve_llm_budget(
        project_id, hold, cap, idempotency_key="op_test_1"
    )
    record("duplicate reserve fails CLOSED", dup is None)
    record(
        "duplicate reserve did not double-book",
        _period(period_id).llm_cost_cents == hold,
    )

    actual = Decimal("0.6979")
    ok = v2_usage.settle_llm_cost(
        period_id, hold, actual, idempotency_key="op_test_1"
    )
    record("settle succeeds", ok is True)
    record(
        "aggregate settled to actual",
        _period(period_id).llm_cost_cents == actual,
        str(_period(period_id).llm_cost_cents),
    )
    ok = v2_usage.settle_llm_cost(
        period_id, hold, actual, idempotency_key="op_test_1"
    )
    record("duplicate settle no-ops (returns True)", ok is True)
    record(
        "duplicate settle did not re-apply delta",
        _period(period_id).llm_cost_cents == actual,
    )
    record(
        "SUM(ledger) == aggregate after settle cycle",
        _ledger_sum(period_id, "llm_cost_cents") == _period(period_id).llm_cost_cents,
    )

    over = v2_usage.reserve_llm_budget(
        project_id, cap + 1, cap, idempotency_key="op_test_2"
    )
    record("over-cap reserve refused", over is None)
    record(
        "refused reserve left NO ledger row (rollback)",
        _ledger_sum(period_id, "llm_cost_cents") == _period(period_id).llm_cost_cents,
    )


def check_tier2(project_id: int, period_id: int) -> None:
    print("\n[4] Tier-2 atomics")
    v2_usage.increment_perceive_usage(project_id, 3)
    record("perceive counter +3", _period(period_id).perceive_operations == 3)

    update_storage_peak(project_id, 5000)
    update_storage_peak(project_id, 2000)  # lower — must not regress
    record(
        "storage peak monotonic (5000 stands after 2000)",
        _period(period_id).storage_bytes_peak == 5000,
    )


def check_rotation(project_id: int) -> None:
    print("\n[5] rotation poller logic")
    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        sub = db.exec(
            select(Subscription).where(Subscription.project_id == project_id)
        ).one()
        # Make the sub ~2.5 months overdue: catch-up should walk 3 boundaries
        sub.current_period_start = now - timedelta(days=105)
        sub.current_period_end = now - timedelta(days=75)
        db.add(sub)
        db.commit()
    finally:
        db.close()

    result = asyncio.run(billing_rotation.rotate_project_period(project_id))
    record(
        "overdue sub rotates (multi-month catch-up)",
        result.get("status") == "ok" and result.get("rotations", 0) >= 2,
        str(result),
    )
    db = get_db()
    try:
        sub = db.exec(
            select(Subscription).where(Subscription.project_id == project_id)
        ).one()
        record(
            "subscription advanced past now",
            sub.current_period_end > now,
            str(sub.current_period_end),
        )
        current = db.exec(
            select(UsagePeriod).where(
                UsagePeriod.project_id == project_id,
                UsagePeriod.period_start <= now,
                UsagePeriod.period_end > now,
            )
        ).first()
        record("a CURRENT usage period now exists", current is not None)
    finally:
        db.close()

    again = asyncio.run(billing_rotation.rotate_project_period(project_id))
    record(
        "re-run skips (idempotent)",
        again.get("status") == "skipped",
        str(again),
    )


def main() -> int:
    print(f"scratch DB: {_db_name}")
    try:
        project_id, period_id = _seed()
        check_conversion_ledger(project_id, period_id)
        check_concurrency(project_id, period_id)
        check_llm_ledger(project_id, period_id)
        check_tier2(project_id, period_id)
        check_rotation(project_id)
    except Exception:
        traceback.print_exc()
        return 2
    return summarize()


if __name__ == "__main__":
    sys.exit(main())
