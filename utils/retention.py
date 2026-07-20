"""Droplet-local file retention (no-GCP design).

Replaces utils/cloud_tasks.schedule_file_cleanup (Google Cloud Tasks) with a
durable, DB-backed schedule swept by services/retention_worker.py — the same
poller shape as the batch/ingest/watch/billing workers. Owner decision
(Group I onward): NO Google services on the droplet.

Time-based cleanup has two sources, both routed here:
  * converted OUTPUT files on non-storage plans (monitoring/metrics.py,
    services/v2_engine/usage.py), retained for the plan's file_retention_hours;
  * rendered HTML captured for V2 quality scoring (utils/processor.py),
    retained 90 days.

``schedule_file_cleanup`` records one PENDING row per object_key (idempotent
via the partial UNIQUE index). ``_delete_and_reconcile`` is the single deletion
primitive shared by the worker and the legacy /internal/cleanup-file route, so
both paths do identical Spaces-delete + storage-bookkeeping.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from models import Activity, Project, ScheduledDeletion
from utils.postgres import get_db
from utils.storage import delete_from_storage

logger = logging.getLogger(__name__)

# Insert a pending deletion. ON CONFLICT against the partial UNIQUE index
# (object_key WHERE deleted_at IS NULL) makes a re-schedule of a still-pending
# key a no-op; a previously-deleted row for the same key does not block it.
# attempts is written explicitly (not left to a DDL default): the prod schema
# has DEFAULT 0 from migration 017, but create_all-bootstrapped dev DBs do not,
# and a raw INSERT bypasses SQLModel's Python-side default (same discipline as
# billing_rotation._INSERT_PERIOD_IF_ABSENT).
_INSERT_SCHEDULED_DELETION = text(
    """
    INSERT INTO ch_scheduled_deletions (object_key, project_id, delete_at, created_at, attempts)
    VALUES (:object_key, :project_id, :delete_at, :now, 0)
    ON CONFLICT (object_key) WHERE deleted_at IS NULL DO NOTHING
    """
)


def schedule_file_cleanup(
    object_key: str, project_id: Optional[int], retention_hours: int
) -> None:
    """Record a pending deletion of ``object_key`` after ``retention_hours``.

    Fire-and-forget and idempotent. Scheduling failures are swallowed —
    retention must never fail a conversion (same contract as the old Cloud
    Tasks call it replaces).
    """
    if not object_key:
        return
    now = datetime.now(timezone.utc)
    delete_at = now + timedelta(hours=max(0, int(retention_hours)))
    db = get_db()
    try:
        db.execute(
            _INSERT_SCHEDULED_DELETION,
            {
                "object_key": object_key,
                "project_id": project_id,
                "delete_at": delete_at,
                "now": now,
            },
        )
        db.commit()
    except Exception:  # noqa: BLE001 — scheduling must not fail the request
        logger.warning(
            "failed to schedule deletion for %s", object_key, exc_info=True
        )
    finally:
        db.close()


def _delete_and_reconcile(
    db: Session, object_key: str, project_id: Optional[int]
) -> None:
    """Delete one object from Spaces and reconcile DB bookkeeping.

    Shared by the retention worker and /internal/cleanup-file so both behave
    identically: remove the object, and
      * if it was a converted OUTPUT file (matched by Activity.url), decrement
        the owning project's storage_used and clear the key;
      * if it was a rendered-HTML capture (matched by Activity.rendered_html_key),
        clear that pointer (HTML is not counted in storage_used).

    Does NOT commit — the caller owns the transaction. delete_from_storage is
    best-effort and idempotent (deleting a missing key is a no-op), matching the
    prior Cloud Tasks behavior.
    """
    delete_from_storage(object_key)

    act = db.exec(select(Activity).where(Activity.url == object_key)).first()
    if act is not None:
        size = 0
        if act.output_file_size:
            try:
                size = int(act.output_file_size)
            except (TypeError, ValueError):
                size = 0
        if size and project_id is not None:
            proj = db.exec(
                select(Project).where(Project.id == project_id)
            ).first()
            if proj is not None:
                proj.storage_used = max(0, (proj.storage_used or 0) - size)
                db.add(proj)
        act.url = ""
        db.add(act)
        return

    html_act = db.exec(
        select(Activity).where(Activity.rendered_html_key == object_key)
    ).first()
    if html_act is not None:
        html_act.rendered_html_key = None
        db.add(html_act)
