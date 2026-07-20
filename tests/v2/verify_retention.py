"""Verify harness for the droplet-local file-retention system (migration 017 +
utils/retention.py + services/retention_worker.py).

Requires a SCRATCH Postgres DB — never point it at conversionhub. Usage:

    createdb enconvert_retention_scratch
    DATABASE_URL="postgresql://$(whoami)@localhost/enconvert_retention_scratch?sslmode=disable" \
        PYTHONPATH=api/gateway api/gateway/.venv/bin/python api/gateway/tests/v2/verify_retention.py
    dropdb enconvert_retention_scratch

Exercises: create_all (partial indexes + timestamptz), schedule_file_cleanup,
ON CONFLICT idempotency, the worker sweep (past-due stamped, future untouched),
and attempts tracking. delete_from_storage runs credential-free and is best-
effort, so the S3 delete no-ops while the DB flow is fully verified.
"""
import asyncio
import os
import sys

if not os.getenv("DATABASE_URL"):
    sys.exit("refusing to run without an explicit scratch DATABASE_URL (never use conversionhub)")
if "conversionhub" in os.getenv("DATABASE_URL", ""):
    sys.exit("refusing to run against conversionhub")

from sqlmodel import SQLModel, select

from models import ScheduledDeletion
from utils.postgres import engine, get_db
from utils.retention import schedule_file_cleanup
import services.retention_worker as rw

KEY = "live/files/42/url-to-pdf/a.pdf"
FUTURE_KEY = "live/files/42/url-to-pdf/future.pdf"


def _pending(key=None) -> int:
    db = get_db()
    try:
        q = select(ScheduledDeletion).where(ScheduledDeletion.deleted_at.is_(None))
        if key:
            q = q.where(ScheduledDeletion.object_key == key)
        return len(db.exec(q).all())
    finally:
        db.close()


def main() -> None:
    SQLModel.metadata.create_all(engine)
    print("create_all OK")

    schedule_file_cleanup(KEY, 42, 0)
    assert _pending(KEY) == 1, "expected 1 pending after schedule"
    print("schedule: 1 pending  OK")

    schedule_file_cleanup(KEY, 42, 0)
    assert _pending(KEY) == 1, "idempotency broken (duplicate pending)"
    print("re-schedule same key: still 1 pending  OK")

    schedule_file_cleanup(FUTURE_KEY, 42, 720)
    assert _pending(FUTURE_KEY) == 1

    claimed = asyncio.run(rw.tick())
    print(f"tick() claimed {claimed} row(s)")

    assert _pending(KEY) == 0, "past-due row not swept"
    assert _pending(FUTURE_KEY) == 1, "future row wrongly swept"
    print("sweep: past-due stamped, future untouched  OK")

    db = get_db()
    try:
        row = db.exec(
            select(ScheduledDeletion).where(ScheduledDeletion.object_key == KEY)
        ).first()
        assert row.deleted_at is not None and row.attempts >= 1
        print(f"swept row: deleted_at set, attempts={row.attempts}  OK")
    finally:
        db.close()

    print("\nALL RETENTION VERIFY CHECKS PASSED")


if __name__ == "__main__":
    main()
