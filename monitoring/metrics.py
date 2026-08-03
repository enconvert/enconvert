from datetime import datetime, timezone
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlmodel import select
from models import Activity, Project, Alert
from utils.postgres import get_db
from utils.error_capture import MAX_ERROR_TYPE_CHARS, truncate_error
from utils.subscription import increment_conversion_usage, update_storage_peak, is_admin_default_project


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1)
    else:
        end = datetime(now.year, now.month + 1, 1)
    return start, end


def get_monthly_conversions(db, project_id: str, now: datetime | None = None) -> int:
    """Legacy fallback: count conversions from activity table.
    Prefer reading from ch_usage_periods.conversions_used instead."""
    now = now or datetime.now(timezone.utc)
    start, end = _month_bounds(now)
    row = db.exec(
        select(func.count(Activity.id))
        .where(Activity.project_id == str(project_id))
        .where(Activity.timestamp >= start)
        .where(Activity.timestamp < end)
    ).first()
    if row is None:
        return 0
    try:
        return int(row[0])
    except TypeError:
        return int(row)


async def log_activity_start(
    project_id: str, endpoint: str, input_file_size: int,
    batch_id: str | None = None, source_url: str | None = None,
) -> int:
    """Insert an In Progress activity row. Returns the activity ID."""
    db = get_db()
    activity = Activity(
        project_id=project_id,
        endpoint=endpoint,
        input_file_size=str(input_file_size),
        status="In Progress",
        batch_id=batch_id,
        source_url=source_url,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    activity_id = activity.id
    db.close()
    return activity_id


async def log_batch_activity_start(
    project_id: str, endpoint: str, urls: list[str], batch_id: str,
) -> list[int]:
    """Insert N In-Progress rows for a batch. Returns list of activity IDs."""
    db = get_db()
    activity_ids = []
    for url in urls:
        activity = Activity(
            project_id=project_id,
            endpoint=endpoint,
            input_file_size=str(len(url)),
            status="In Progress",
            batch_id=batch_id,
            source_url=url,
        )
        db.add(activity)
        db.commit()
        db.refresh(activity)
        activity_ids.append(activity.id)
    db.close()
    return activity_ids


async def update_activity_status(
    activity_id: int,
    status: str,
    output_file_size: int = 0,
    object_key: str = "",
    duration: float = 0,
    content_hash: str | None = None,
    rendered_html_key: str | None = None,
    console_error_count: int | None = None,
    page_load_time_ms: int | None = None,
    error_message: str | None = None,
    error_type: str | None = None,
    count_usage: bool = True,
):
    """Update an existing activity row. Only overwrites fields with non-default values.

    The four instrumentation kwargs (V2 Phase 0) follow the same rule:
    pass them only when you actually have a value, so a partial update
    from the conversion path doesn't clobber another writer's data.

    ``error_message`` / ``error_type`` (migration 025) record WHY a
    conversion failed, so the admin Activity table shows the root cause
    without anyone reading the droplet's journal. Pass them on every
    Failed transition — ``utils.error_capture.error_fields(exc)`` builds
    both. A Success transition clears them: a stale traceback on a row
    that ended up succeeding is worse than no traceback at all.

    ``count_usage=False`` (Sprint F.5) records the activity WITHOUT the
    V1 usage side effects (conversions_used increment, storage counter,
    retention scheduling). /v2/* endpoints use it: V2 quotas are
    separate counters and V2 storage/retention is accounted by
    services.v2_engine.usage (coexistence rule 3, V3 plan section 5).
    V1 callers never pass it, so V1 behavior is unchanged.
    """
    db = get_db()
    activity = db.exec(select(Activity).where(Activity.id == activity_id)).first()
    if not activity:
        db.close()
        return

    prev_status = activity.status
    activity.status = status
    if duration:
        activity.duration = str(duration)
    if output_file_size:
        activity.output_file_size = str(output_file_size)
    if object_key:
        activity.url = object_key  # stores object_key for presigned URL generation
    if content_hash is not None:
        activity.content_hash = content_hash
    if rendered_html_key is not None:
        activity.rendered_html_key = rendered_html_key
    if console_error_count is not None:
        activity.console_error_count = console_error_count
    if page_load_time_ms is not None:
        activity.page_load_time_ms = page_load_time_ms
    if error_message is not None:
        activity.error_message = truncate_error(error_message)
    if error_type is not None:
        activity.error_type = error_type[:MAX_ERROR_TYPE_CHARS]
    if status == "Success":
        # The row ended up succeeding — drop any detail from an earlier
        # failed attempt so the admin table never shows a stale cause.
        activity.error_message = None
        activity.error_type = None
    db.add(activity)
    db.commit()

    # Update usage tracking only on first transition to Success
    # (batch processing calls update_activity_status twice per file —
    #  once after conversion, once after ZIP to set object_key)
    try:
        project_id_int = int(activity.project_id)
    except (TypeError, ValueError):
        project_id_int = None

    first_success = status == "Success" and prev_status != "Success"
    if first_success and project_id_int is not None and count_usage:
        # Skip usage tracking for admin users' default projects
        if not is_admin_default_project(db, project_id_int):
            # Count the conversion exactly once: activity_id is stable
            # for this logical conversion across retries/redeliveries,
            # so the ledger (migration 016) dedups on it even if this
            # function is somehow re-entered past the first_success
            # guard (e.g. a status flap or replayed update).
            increment_conversion_usage(
                project_id_int,
                idempotency_key=f"v1:conversion:{activity_id}",
            )

            # Update project.storage_used (live counter kept on project)
            # atomically — the old read-modify-write lost bytes when two
            # conversions for one project finished concurrently.
            if output_file_size:
                new_total = db.execute(
                    sa_update(Project)
                    .where(Project.id == project_id_int)
                    .values(
                        storage_used=func.coalesce(Project.storage_used, 0)
                        + output_file_size
                    )
                    .returning(Project.storage_used)
                ).first()
                db.commit()
                if new_total is not None:
                    # Update storage peak in usage period
                    update_storage_peak(project_id_int, int(new_total[0]))

            # Schedule droplet-local retention cleanup if no storage plan
            if object_key:
                try:
                    from utils.subscription import get_subscription
                    from utils.retention import schedule_file_cleanup
                    sub = get_subscription(project_id_int)
                    if sub and sub.get("storage_bytes", 0) == 0:
                        retention_hours = sub.get("file_retention_hours", 1)
                        schedule_file_cleanup(object_key, project_id_int, retention_hours)
                except Exception:
                    pass  # Don't fail conversion on scheduling errors

    # Create alert for project on completion
    if status in ("Success", "Failed") and project_id_int is not None:
        sev = "success" if status == "Success" else "error"
        title = f"Conversion {status.lower()}: {activity.endpoint}"
        msg = activity.source_url or ""
        db.add(Alert(
            project_id=project_id_int, alert_type="activity",
            severity=sev, title=title, message=msg, link="/dashboard/activity",
        ))
        db.commit()

    db.close()
