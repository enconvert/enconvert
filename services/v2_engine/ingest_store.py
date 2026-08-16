"""ch_ingest_jobs / ch_ingest_pages persistence (Task H.7).

One module owns every read/write of the ingest tables so the handler, the
ingest flow and the durable worker share identical row semantics — the same
arrangement F.5's ``operations.py`` uses for perceive. Sessions follow the
gateway convention (sync SQLModel sessions opened per call via ``get_db()``;
the worker offloads them with ``asyncio.to_thread``). Timestamps are written
timezone-aware UTC.

Durability model (the H.7 difference from F.8's in-memory batch worker):
job + per-page state lives in these tables, so a process restart RESUMES
in-flight jobs instead of failing them. ``list_active_job_ids`` (backed by
the migration's ``idx_ingest_jobs_active`` partial index) is the resume
scan; ``pages_to_process`` returns only the work a resumed job still owes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import update
from sqlmodel import select

from models import Alert, IngestJob, IngestPage
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Job statuses that are NOT terminal — a row in one of these at boot was
# orphaned by a restart and is resumed (mirrors the migration's partial
# index predicate).
ACTIVE_JOB_STATUSES = ("queued", "discovering", "processing")
# Per-page statuses the flow still owes work on (pending = never started,
# processing = crashed mid-render and must be redone).
RESUMABLE_PAGE_STATUSES = ("pending", "processing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Jobs ─────────────────────────────────────────────────────────────────────


def create_job(
    *,
    job_id: str,
    project_id: int,
    mode: str,
    source_url: Optional[str],
    source_urls: Optional[List[str]],
    chunk_options: dict[str, Any],
    webhook_url: Optional[str] = None,
) -> None:
    """Insert the 'queued' job row at submit time."""
    db = get_db()
    try:
        db.add(
            IngestJob(
                job_id=job_id,
                project_id=project_id,
                mode=mode,
                source_url=source_url,
                source_urls=source_urls,
                chunk_options=chunk_options,
                webhook_url=webhook_url,
                status="queued",
                created_at=_utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def get_job(job_id: str) -> Optional[IngestJob]:
    """Fetch one job row by its public id (no tenant scope — worker use)."""
    db = get_db()
    try:
        return db.exec(
            select(IngestJob).where(IngestJob.job_id == job_id)
        ).first()
    finally:
        db.close()


def get_job_for_project(job_id: str, project_id: int) -> Optional[IngestJob]:
    """Fetch a job scoped to its owner; foreign/unknown ids yield None so the
    handler 404s without leaking existence across tenants."""
    db = get_db()
    try:
        return db.exec(
            select(IngestJob).where(
                IngestJob.job_id == job_id,
                IngestJob.project_id == project_id,
            )
        ).first()
    finally:
        db.close()


def list_jobs_for_project(
    project_id: int, *, skip: int = 0, limit: int = 20
) -> List[IngestJob]:
    """Newest-first page of a project's ingest jobs (dashboard list, H.8).

    Ordered by ``created_at DESC`` to ride the ``idx_ingest_jobs_project_created``
    index. ``skip``/``limit`` are bounded by the handler. The store applies
    whatever ``limit`` it receives verbatim — the handler passes ``limit + 1`` so
    it can detect a further page (``has_more``) without a COUNT.
    """
    db = get_db()
    try:
        return list(
            db.exec(
                select(IngestJob)
                .where(IngestJob.project_id == project_id)
                .order_by(IngestJob.created_at.desc())  # type: ignore[attr-defined]
                .offset(max(skip, 0))
                .limit(max(limit, 0))
            ).all()
        )
    finally:
        db.close()


def set_webhook_delivered(job_id: str, delivered: bool) -> None:
    """Record the completion-webhook delivery outcome (H.8).

    Unconditional by status: a job is only ever delivered after it reaches
    'completed', and a manual retry must be able to flip a previously-failed
    delivery to True (or back to False) regardless of terminal status.
    """
    db = get_db()
    try:
        db.execute(
            update(IngestJob)
            .where(IngestJob.job_id == job_id)
            .values(webhook_delivered=delivered, updated_at=_utcnow())
        )
        db.commit()
    finally:
        db.close()


def record_webhook_failure_alert(
    job: IngestJob, attempts: int, *, reason: Optional[str] = None
) -> None:
    """Raise a dashboard alert when completion-webhook delivery fails (H.8
    verification (d)). ``reason`` overrides the default retry-exhaustion message
    for terminal failures that never reach the network (e.g. an SSRF-blocked
    URL). Best-effort: an alert-write failure must never propagate into the
    worker / retry path."""
    if reason:
        message = (
            f"The completion webhook for ingest job {job.job_id} could not be "
            f"delivered: {reason}"
        )
    else:
        message = (
            f"The completion webhook for ingest job {job.job_id} could not be "
            f"delivered after {attempts} attempt(s). Verify your endpoint and "
            f"retry from the Ingest page."
        )
    db = get_db()
    try:
        db.add(
            Alert(
                project_id=job.project_id,
                alert_type="ingest_webhook",
                severity="error",
                title="Webhook delivery failed",
                message=message,
                link="/dashboard/ingest",
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 — alerting is best-effort
        logger.warning(
            "ingest %s: failed to record webhook-failure alert",
            job.job_id,
            exc_info=True,
        )
    finally:
        db.close()


def list_active_job_ids() -> List[str]:
    """Job ids in a non-terminal status (the startup resume scan)."""
    db = get_db()
    try:
        rows = db.exec(
            select(IngestJob.job_id)
            .where(IngestJob.status.in_(ACTIVE_JOB_STATUSES))  # type: ignore[attr-defined]
            .order_by(IngestJob.id)  # type: ignore[arg-type]
        ).all()
        return list(rows)
    finally:
        db.close()


def transition_status(
    job_id: str,
    new_status: str,
    *,
    allowed_from: tuple[str, ...] = ACTIVE_JOB_STATUSES,
    error_message: Optional[str] = None,
    completed: bool = False,
) -> bool:
    """Atomically advance a job's status ONLY if it is still in ``allowed_from``.

    Returns True when the transition committed. A single conditional UPDATE
    makes cancellation race-proof: once DELETE has set 'canceled', every
    worker transition returns False (the row is no longer in ``allowed_from``)
    and the flow aborts without resurrecting a canceled job.
    """
    values: dict[str, Any] = {"status": new_status, "updated_at": _utcnow()}
    if error_message is not None:
        values["error_message"] = error_message[:2000]
    if completed:
        values["completed_at"] = _utcnow()
    db = get_db()
    try:
        result = db.execute(
            update(IngestJob)
            .where(
                IngestJob.job_id == job_id,
                IngestJob.status.in_(allowed_from),  # type: ignore[attr-defined]
            )
            .values(**values)
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


def fail_job(job_id: str, error_message: str) -> bool:
    """Mark an active job 'failed' (no-op if already terminal/canceled)."""
    return transition_status(
        job_id, "failed", error_message=error_message, completed=True
    )


def set_pages_discovered(
    job_id: str,
    count: int,
    *,
    pages_found: Optional[int] = None,
    truncated: Optional[bool] = None,
) -> None:
    """Record how many URLs the discovery phase enqueued.

    ``pages_found`` / ``truncated`` (migration 026) carry the PRE-cap
    discovery stats; omitted (None) they are left untouched, so the resume
    path — which re-derives ``count`` from the page rows — never wipes the
    stats the original discovery pass wrote.

    Guarded to active jobs (conditional UPDATE) so a cancel committed during
    discovery is never mutated back — audit counters on a terminal row stay
    frozen.
    """
    values: dict[str, Any] = {
        "pages_discovered": count,
        "updated_at": _utcnow(),
    }
    if pages_found is not None:
        values["pages_found"] = pages_found
    if truncated is not None:
        values["discovery_truncated"] = truncated
    db = get_db()
    try:
        db.execute(
            update(IngestJob)
            .where(
                IngestJob.job_id == job_id,
                IngestJob.status.in_(ACTIVE_JOB_STATUSES),  # type: ignore[attr-defined]
            )
            .values(**values)
        )
        db.commit()
    finally:
        db.close()


def update_job_progress(
    job_id: str,
    *,
    pages_processed: int,
    pages_failed: int,
    total_chunks: int,
) -> None:
    """Overwrite the running counters (recomputed from page rows each pass).

    Guarded to active jobs (conditional UPDATE) like set_pages_discovered —
    a cancel committed mid-loop wins, and the terminal row's audit counters
    stay frozen even if the worker races one last progress write in.
    """
    db = get_db()
    try:
        db.execute(
            update(IngestJob)
            .where(
                IngestJob.job_id == job_id,
                IngestJob.status.in_(ACTIVE_JOB_STATUSES),  # type: ignore[attr-defined]
            )
            .values(
                pages_processed=pages_processed,
                pages_failed=pages_failed,
                total_chunks=total_chunks,
                updated_at=_utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def complete_job(
    job_id: str,
    *,
    output_key: str,
    total_chunks: int,
    pages_processed: int,
    pages_failed: int,
) -> bool:
    """Mark an ACTIVE job completed with its final JSONL key and counts.

    Conditional on the job still being active so a cancel committed during
    assembly wins — a canceled job is never flipped to completed. Returns
    True when the completion committed.
    """
    db = get_db()
    try:
        result = db.execute(
            update(IngestJob)
            .where(
                IngestJob.job_id == job_id,
                IngestJob.status.in_(ACTIVE_JOB_STATUSES),  # type: ignore[attr-defined]
            )
            .values(
                status="completed",
                output_key=output_key,
                total_chunks=total_chunks,
                pages_processed=pages_processed,
                pages_failed=pages_failed,
                updated_at=_utcnow(),
                completed_at=_utcnow(),
            )
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


def cancel_job(job_id: str, project_id: int) -> Optional[IngestJob]:
    """Atomically set a job 'canceled' if still active; return the row (or None).

    Project-scoped. A SINGLE conditional UPDATE (status IN active) makes this
    race-proof against a concurrent ``complete_job``: if assembly committed
    first, the UPDATE matches nothing and the re-fetched row is returned
    unchanged (completed), so a finished job is never clobbered back to
    'canceled'. A terminal job is returned as-is (idempotent DELETE); an
    unknown/foreign id returns None so the handler 404s. The worker reads the
    canceled status between pages and stops without assembling output.
    """
    now = _utcnow()
    db = get_db()
    try:
        db.execute(
            update(IngestJob)
            .where(
                IngestJob.job_id == job_id,
                IngestJob.project_id == project_id,
                IngestJob.status.in_(ACTIVE_JOB_STATUSES),  # type: ignore[attr-defined]
            )
            .values(status="canceled", updated_at=now, completed_at=now)
        )
        db.commit()
        # Re-fetch the current row: 'canceled' if we won the race, the terminal
        # status (e.g. 'completed') if a concurrent writer won, None if absent.
        return db.exec(
            select(IngestJob).where(
                IngestJob.job_id == job_id,
                IngestJob.project_id == project_id,
            )
        ).first()
    finally:
        db.close()


# ── Pages ────────────────────────────────────────────────────────────────────


def create_pages(job_id: str, urls: List[str]) -> int:
    """Insert one 'pending' page row per URL; returns the number inserted.

    Idempotent: URLs already present for this job (resume, or a duplicate in
    the source list) are skipped, so re-discovery never double-inserts. The
    DB unique index on (job_id, md5(url)) is the hard backstop.
    """
    db = get_db()
    try:
        existing = set(
            db.exec(
                select(IngestPage.url).where(IngestPage.job_id == job_id)
            ).all()
        )
        now = _utcnow()
        inserted = 0
        for url in urls:
            if url in existing:
                continue
            existing.add(url)
            db.add(
                IngestPage(
                    job_id=job_id,
                    url=url,
                    status="pending",
                    created_at=now,
                )
            )
            inserted += 1
        if inserted:
            db.commit()
        return inserted
    finally:
        db.close()


def create_file_pages(job_id: str, files: List[tuple[str, str]]) -> int:
    """Insert one 'pending' file page per (object_key, filename); returns count.

    ``files`` is a list of (uploaded Spaces object key, original filename). The
    object key is stored in ``url`` — unique per file — so the (job_id, md5(url))
    uniqueness index and the per-page staging-key derivation work unchanged.
    Idempotent: an object key already present for this job (resume) is skipped.
    """
    db = get_db()
    try:
        existing = set(
            db.exec(
                select(IngestPage.url).where(IngestPage.job_id == job_id)
            ).all()
        )
        now = _utcnow()
        inserted = 0
        for object_key, filename in files:
            if object_key in existing:
                continue
            existing.add(object_key)
            db.add(
                IngestPage(
                    job_id=job_id,
                    url=object_key,
                    source_type="file",
                    filename=filename,
                    status="pending",
                    created_at=now,
                )
            )
            inserted += 1
        if inserted:
            db.commit()
        return inserted
    finally:
        db.close()


def list_pages(job_id: str) -> List[IngestPage]:
    """Every page row of a job, in insertion (discovery) order."""
    db = get_db()
    try:
        return list(
            db.exec(
                select(IngestPage)
                .where(IngestPage.job_id == job_id)
                .order_by(IngestPage.id)  # type: ignore[arg-type]
            ).all()
        )
    finally:
        db.close()


def mark_page_processing(page_id: int) -> None:
    """Mark a page row in-progress (called before each render attempt)."""
    db = get_db()
    try:
        page = db.exec(
            select(IngestPage).where(IngestPage.id == page_id)
        ).first()
        if page is None:
            return
        page.status = "processing"
        db.add(page)
        db.commit()
    finally:
        db.close()


def complete_page(
    page_id: int,
    *,
    chunk_count: int,
    word_count: int,
    content_hash: Optional[str],
    note: Optional[str] = None,
) -> None:
    """Mark a page done. ``note`` records WHY a completed page contributed
    nothing (today: it duplicated an earlier page). It reuses error_message
    as the row's free-text field — the status stays 'completed', so nothing
    that classifies by status reads it as a failure."""
    db = get_db()
    try:
        page = db.exec(
            select(IngestPage).where(IngestPage.id == page_id)
        ).first()
        if page is None:
            return
        page.status = "completed"
        page.chunk_count = chunk_count
        page.word_count = word_count
        page.content_hash = content_hash
        page.error_message = note[:2000] if note else None
        page.processed_at = _utcnow()
        db.add(page)
        db.commit()
    finally:
        db.close()


def fail_page(page_id: int, error_message: str) -> None:
    db = get_db()
    try:
        page = db.exec(
            select(IngestPage).where(IngestPage.id == page_id)
        ).first()
        if page is None:
            return
        page.status = "failed"
        page.error_message = error_message[:2000]
        page.processed_at = _utcnow()
        db.add(page)
        db.commit()
    finally:
        db.close()


def skip_page(page_id: int, reason: str) -> None:
    """Mark a page 'skipped' (e.g. quota exhausted mid-job)."""
    db = get_db()
    try:
        page = db.exec(
            select(IngestPage).where(IngestPage.id == page_id)
        ).first()
        if page is None:
            return
        page.status = "skipped"
        page.error_message = reason[:2000]
        page.processed_at = _utcnow()
        db.add(page)
        db.commit()
    finally:
        db.close()
