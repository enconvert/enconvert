"""/v2/perceive/batch droplet-local worker (Task F.8 + durable-resume revision).

Owner decision 2026-06-07: NO Google services — the plan's Cloud Tasks
dispatch is an in-process asyncio worker draining a FIFO and processing URLs
strictly ONE AT A TIME through ``perceive_flow.run()`` (which serializes on the
browser semaphore anyway, plan A5).

DURABILITY (the revision): the batch ENVELOPE — the single shared render
``options`` block and ``output_mode`` — now lives in ch_perceive_batches
(migration 023), the same way ch_ingest_jobs makes /v2/ingest restart-safe. The
per-URL work already lived in ch_perceive_operations (grouped by batch_id). So:

* The queue carries batch_id STRINGS, not in-memory job objects.
* ``startup()`` re-enqueues every non-terminal batch (batch_store.
  list_active_batch_ids) and the worker rebuilds each URL's PerceiveRequest
  from the persisted options, re-rendering ONLY the still-pending
  (queued/processing) operation rows — an interrupted batch RESUMES instead of
  being failed ("resubmit the batch" is gone).
* Cancellation is observed between URLs via the batch status (a DELETE flips it
  to 'canceled'; the conditional-UPDATE store methods make that race-proof).
* Terminal status + counters + the zip key are computed from the FULL set of
  operation rows, so a batch finished across two process lifetimes is correct.

An in-process ``_jobs`` registry keeps the live BatchJob (with its inline
completion event) for batches submitted THIS process, so the inline fast path
still waits on the real job; a resumed batch (empty registry) is reconstructed
from the DB.

ZIP mode reuses the V1 plumbing: artifacts are pulled back from Spaces, bundled
with zipfile, uploaded, and the key stamped on the completed operation rows
(operations.BATCH_ZIP_KEY) and the batch row (zip_object_key).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import IO, Optional

from api.v2.schemas.perceive import (
    OutputArtifact,
    PerceiveBatchRequest,
    PerceiveRequest,
    PerceiveResponse,
)
from models import PerceiveOperation
from monitoring.metrics import log_batch_activity_start, update_activity_status
from services.v2_engine import batch_store, operations, perceive_flow
from services.v2_engine.url_safety import assert_public_http_url
from utils.error_capture import error_fields
from utils.storage import (
    DO_SPACES_BUCKET,
    generate_presigned_url,
    get_s3_client,
    upload_fileobj_to_gcs,
)

logger = logging.getLogger(__name__)

# Plan F.8: batches up to this size run inline in the request;
# anything larger answers 202 and goes through the worker queue.
INLINE_THRESHOLD = 10

# How long the inline path may hold the HTTP request open. The gateway
# TimeoutMiddleware kills requests at 300 s; renders run 10-30 s each
# (F.1 perf data), so 10 inline URLs can overrun it. Inline batches
# therefore ALSO run in the worker and the request merely waits on the
# job's completion event up to this budget — on expiry the handler
# degrades to 202 + job_id and the renders continue undisturbed.
INLINE_WAIT_BUDGET_S = 240.0

ENDPOINT_BATCH = "/v2/perceive/batch"

# Spaces path segment for the bundled archive.
ZIP_ENDPOINT_SEGMENT = "v2-perceive-batch"

# Reason single-URL (non-batch) operations are swept at boot. Batch rows are
# NOT swept — they resume (operations.fail_stale_operations skips batch rows).
_ORPHAN_SWEEP_REASON = (
    "interrupted by server restart (single operation, not resumable)"
)

_queue: Optional[asyncio.Queue[str]] = None
_worker_task: Optional[asyncio.Task] = None
# batch_id -> live BatchJob for batches submitted in THIS process (carries the
# inline completion event). A resumed batch is absent here and rebuilt from DB.
_jobs: dict[str, "BatchJob"] = {}


@dataclass
class BatchJob:
    """Everything the worker needs to process one batch (this process)."""

    batch_id: str
    user: dict
    requests: list[tuple[str, PerceiveRequest]]  # (operation_id, request)
    output_mode: str = "manifest"
    activity_ids: Optional[list[int]] = None
    zip_artifact: Optional[OutputArtifact] = None
    items: list[PerceiveResponse] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


def build_requests(body: PerceiveBatchRequest) -> list[PerceiveRequest]:
    """One fully validated PerceiveRequest per unique URL, in order.

    Duplicates are dropped order-preserving (a batch never bills the
    same URL twice); an invalid URL raises pydantic.ValidationError so
    the handler can answer 422 before anything is created.
    """
    options = body.options.model_dump()
    seen: dict[str, PerceiveRequest] = {}
    for raw_url in body.urls:
        request = PerceiveRequest(**{**options, "url": raw_url})
        seen.setdefault(str(request.url), request)
    return list(seen.values())


def make_job(
    body: PerceiveBatchRequest,
    user: dict,
    requests: Optional[list[PerceiveRequest]] = None,
) -> BatchJob:
    """Create the durable batch envelope + its 'queued' operation rows."""
    if requests is None:
        requests = build_requests(body)
    batch_id = f"batch_{uuid.uuid4().hex}"
    entries = [(f"per_{uuid.uuid4().hex}", request) for request in requests]
    project_id = int(user["id"])
    # Envelope FIRST, children second. An operation row whose envelope is
    # missing can never be resumed (list_active_batch_ids only walks
    # ch_perceive_batches) nor swept, so it consumes quota and sits 'queued'
    # forever — exactly what happened on 2026-07-31 when create_batch raised
    # UndefinedTable after the operation rows had already been committed.
    # Persist the envelope so a restart can rebuild each PerceiveRequest from
    # the shared options + each operation row's url (JSON-mode dump for JSONB).
    batch_store.create_batch(
        batch_id,
        project_id,
        output_mode=body.output_mode,
        options=body.options.model_dump(mode="json"),
        total=len(entries),
    )
    operations.create_queued_operations(
        batch_id=batch_id,
        project_id=project_id,
        entries=[(op_id, str(request.url)) for op_id, request in entries],
        outputs_requested=list(dict.fromkeys(body.options.outputs)),
    )
    return BatchJob(
        batch_id=batch_id,
        user=user,
        requests=entries,
        output_mode=body.output_mode,
    )


def _load_job(batch_id: str) -> Optional[BatchJob]:
    """Reconstruct a resumable BatchJob from the DB (worker/resume path).

    Returns None if the batch is gone or already terminal. Rebuilds the
    per-request user (subscription) from project_id — like ingest_flow — and
    re-queues ONLY the still-pending (queued/processing) operation rows.
    """
    from utils.subscription import get_effective_subscription

    row = batch_store.get_batch(batch_id)
    if row is None or row.status not in batch_store.ACTIVE_BATCH_STATUSES:
        return None
    subscription = get_effective_subscription(row.project_id)
    user = {"id": row.project_id, "subscription": subscription}
    options = dict(row.options or {})
    # Credentials are deliberately not persisted (batch_store._redact). Resuming
    # such a batch without them would silently render login walls and bill the
    # caller for them, so fail the pending rows and tell them to resubmit.
    redacted = options.pop("_redacted", None)
    requests: list[tuple[str, PerceiveRequest]] = []
    for op in operations.list_batch_operations(batch_id, row.project_id):
        if op.status not in ("queued", "processing"):
            continue  # completed/failed rows are done — never re-rendered
        if redacted:
            operations.fail_operation(
                operation_id=op.operation_id,
                error_message=(
                    "batch used credentials that are not stored; resubmit the batch"
                ),
            )
            continue
        try:
            request = PerceiveRequest(**{**options, "url": op.url})
        except Exception:  # noqa: BLE001 — a row we cannot rebuild is failed
            logger.exception(
                "batch resume: could not rebuild request for %s", op.operation_id
            )
            operations.fail_operation(
                operation_id=op.operation_id,
                error_message="could not rebuild request on resume",
            )
            continue
        requests.append((op.operation_id, request))
    return BatchJob(
        batch_id=batch_id,
        user=user,
        requests=requests,
        output_mode=row.output_mode,
    )


async def assert_urls_public(urls: list[str]) -> None:
    """SSRF guard over the whole batch before any row is created."""
    for url in urls:
        await assert_public_http_url(url)


def _failed_item(
    operation_id: str, url: str, error: str
) -> PerceiveResponse:
    return PerceiveResponse(
        operation_id=operation_id,
        status="failed",
        url=url,
        error=error,
    )


async def process_batch(job: BatchJob) -> list[PerceiveResponse]:
    """Process one batch's still-pending URLs, strictly sequentially.

    Shared by the inline path (handler awaits directly) and the worker. A
    per-URL failure marks that operation failed and the loop continues
    (partial results beat all-or-nothing). Cancellation is observed between
    URLs via the batch status; a shutdown (CancelledError) leaves the batch
    'processing' so the next boot resumes it.
    """
    project_id = int(job.user["id"])
    # Claim the batch in place (queued|processing -> processing). Idempotent on
    # resume; harmless if the batch row is absent (tests without a store).
    batch_store.transition_status(
        job.batch_id, "processing", allowed_from=("queued", "processing")
    )

    urls = [str(request.url) for _, request in job.requests]
    if job.activity_ids is None:
        try:
            job.activity_ids = await log_batch_activity_start(
                project_id=job.user["id"],
                endpoint=ENDPOINT_BATCH,
                urls=urls,
                batch_id=job.batch_id,
            )
        except Exception:  # noqa: BLE001 — dashboard rows are best-effort
            logger.warning("batch activity start failed", exc_info=True)
            job.activity_ids = []

    items: list[PerceiveResponse] = []
    stopped_for_cancel = False
    try:
        for index, (operation_id, request) in enumerate(job.requests):
            # DB-observed cancellation between URLs (race-proof: DELETE flipped
            # the row to 'canceled', which the conditional transitions honor).
            current = batch_store.get_batch(job.batch_id)
            if current is not None and current.status == "canceled":
                stopped_for_cancel = True
                break
            start = datetime.now(timezone.utc)
            try:
                response = await perceive_flow.run(
                    request, operation_id, job.user, batch_id=job.batch_id
                )
                items.append(response)
                await _mark_activity(job, index, "Success", start, response)
            except asyncio.CancelledError:
                # Shutdown mid-batch: leave rows pending + the batch
                # 'processing'; the next boot resumes exactly this remainder.
                raise
            except Exception as exc:  # noqa: BLE001 — isolate per-URL failures
                logger.exception(
                    "batch %s: %s failed for %s",
                    job.batch_id,
                    operation_id,
                    request.url,
                )
                operations.fail_operation(
                    operation_id=operation_id, error_message=str(exc)
                )
                items.append(
                    _failed_item(operation_id, str(request.url), str(exc))
                )
                await _mark_activity(
                    job,
                    index,
                    "Failed",
                    start,
                    None,
                    error=exc,
                    error_context=f"url={request.url}",
                )

        job.items = items
        if not stopped_for_cancel:
            await _finalize_batch(job, project_id)
    finally:
        # Wake any inline request waiting on this job — including on
        # cancellation, so a shutdown never strands an open request.
        job.done.set()
    return items


async def _finalize_batch(job: BatchJob, project_id: int) -> None:
    """Compute terminal status/counters + bundle zip from ALL operation rows.

    DB-driven (not just this run's ``job.items``) so a batch completed across
    two process lifetimes finalizes correctly. All writes are guarded to an
    active batch, so a cancel that lands during the zip bundle still wins.
    """
    rows = operations.list_batch_operations(job.batch_id, project_id)
    completed = [r for r in rows if r.status == "completed"]
    failed = [r for r in rows if r.status == "failed"]

    zip_key: Optional[str] = None
    if job.output_mode == "zip" and completed:
        zip_key = await _bundle_zip(job, project_id, completed)

    if failed and not completed:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "completed"
    batch_store.finalize(
        job.batch_id,
        status=status,
        completed=len(completed),
        failed=len(failed),
        zip_object_key=zip_key,
    )


async def _mark_activity(
    job: BatchJob,
    index: int,
    status: str,
    start: datetime,
    response: Optional[PerceiveResponse],
    *,
    error: BaseException | None = None,
    error_context: str | None = None,
) -> None:
    """Best-effort per-URL Activity update (V1 dashboard parity)."""
    if not job.activity_ids or index >= len(job.activity_ids):
        return
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    output_bytes = 0
    object_key = ""
    if response is not None:
        output_bytes = sum(a.size_bytes for a in response.outputs.values())
        object_key = next(
            (a.object_key for a in response.outputs.values()), ""
        )
    try:
        await update_activity_status(
            job.activity_ids[index],
            status,
            output_file_size=output_bytes,
            object_key=object_key,
            duration=duration,
            count_usage=False,  # coexistence rule 3: V2 never bills V1
            **error_fields(error, context=error_context),
        )
    except Exception:  # noqa: BLE001
        logger.warning("batch activity update failed", exc_info=True)


def _zip_member_name(index: int, url: str, output: str, object_key: str) -> str:
    """Collision-free archive member name: 001_host-path_markdown.md."""
    slug = re.sub(r"https?://", "", url)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-")[:80]
    suffix = "." + object_key.rsplit(".", 1)[-1] if "." in object_key else ""
    return f"{index + 1:03d}_{slug}_{output}{suffix}"


def _spool_artifacts_to_zip(
    spool: IO[bytes], completed_rows: list[PerceiveOperation]
) -> None:
    """Stream every completed row's artifacts from Spaces into a zip on disk.

    Blocking (boto3) — run via asyncio.to_thread. Each S3 body is streamed in
    64KB chunks straight into its archive member, so no artifact is ever fully
    resident: the old path buffered every artifact's bytes AND doubled the
    finished archive via BytesIO.getvalue() — too much for the 1GB droplet.
    """
    client = get_s3_client()
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, row in enumerate(completed_rows):
            # Read object keys straight off the durable row (name ->
            # {key, size_bytes, content_type}); skip reserved underscore
            # keys (_fingerprint, _batch_zip). No presign needed — we
            # download by object key.
            for output_name, entry in (row.output_keys or {}).items():
                if output_name.startswith("_") or not isinstance(entry, dict):
                    continue
                object_key = entry.get("key")
                if not object_key:
                    continue
                body = client.get_object(
                    Bucket=DO_SPACES_BUCKET, Key=object_key
                )["Body"]
                member_name = _zip_member_name(
                    index, str(row.url), output_name, object_key
                )
                with archive.open(member_name, mode="w") as member:
                    for chunk in body.iter_chunks(65536):
                        member.write(chunk)
    spool.flush()


async def _bundle_zip(job: BatchJob, project_id: int, completed_rows) -> Optional[str]:
    """Bundle every completed URL's artifacts into one archive (DB-driven).

    Reads the completed ch_perceive_operations rows (works across a resume,
    where this run's ``job.items`` holds only the newly-rendered URLs),
    rebuilds each row's artifacts from output_keys, streams the bytes into a
    disk-spooled zip, uploads it, and stamps the key on the operation rows
    (V2 status GET). Returns the zip object key, or None on a bundling
    failure (artifacts stay individually reachable — the batch downgrades to
    manifest semantics).
    """
    spool = tempfile.NamedTemporaryFile(
        prefix="v2_batch_zip_", suffix=".zip", delete=False
    )
    try:
        await asyncio.to_thread(_spool_artifacts_to_zip, spool, completed_rows)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]
        spool.seek(0)
        upload = await asyncio.to_thread(
            upload_fileobj_to_gcs,
            spool,
            str(project_id),
            ZIP_ENDPOINT_SEGMENT,
            f"{job.batch_id}_{timestamp}.zip",
            file_size=os.path.getsize(spool.name),
        )
        zip_key = upload["object_key"]
        job.zip_artifact = OutputArtifact(
            url=generate_presigned_url(zip_key, str(project_id)),
            object_key=zip_key,
            size_bytes=upload["file_size"],
            content_type="application/zip",
        )
        operations.attach_batch_zip(
            job.batch_id,
            project_id,
            {"key": zip_key, "size_bytes": upload["file_size"]},
        )
        return zip_key
    except Exception:  # noqa: BLE001 — artifacts stay individually reachable
        logger.exception("batch %s: zip bundling failed", job.batch_id)
        return None
    finally:
        spool.close()
        try:
            os.unlink(spool.name)
        except OSError:
            pass


def _ensure_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def submit(job: BatchJob) -> None:
    """Register a live job and enqueue its batch_id for the worker (FIFO).

    Never blocks: the queue is unbounded and the real limiter is the plan's
    batch limit. The job object stays in ``_jobs`` so the worker uses the SAME
    instance (and its inline completion event) rather than reloading from DB.
    """
    _jobs[job.batch_id] = job
    _ensure_queue().put_nowait(job.batch_id)


def cancel(batch_id: str, project_id: int):
    """Cancel a batch (DELETE endpoint). Returns the current batch row.

    The worker observes the 'canceled' status between URLs and stops; rows
    already completed keep their artifacts.
    """
    return batch_store.cancel_batch(batch_id, project_id)


def worker_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


async def process_inline(job: BatchJob, timeout_s: float) -> bool:
    """Inline-path execution with a bounded wait (TimeoutMiddleware guard).

    With the worker running (production), the job goes through the queue and
    this merely WAITS on its completion event — a timeout cancels the wait,
    never the renders, and the handler degrades to 202. Without a worker
    (tests / a crashed worker) the job processes directly. Returns True when
    the job finished within the budget.
    """
    if worker_running():
        await submit(job)
        try:
            await asyncio.wait_for(job.done.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False
    await process_batch(job)
    return True


async def _run_batch_id(batch_id: str) -> None:
    """Resolve a batch_id to a job (live registry or DB resume) and run it."""
    job = _jobs.pop(batch_id, None)
    if job is None:
        job = await asyncio.to_thread(_load_job, batch_id)
    if job is None:
        return  # gone or already terminal
    await process_batch(job)


async def _worker_loop() -> None:
    queue = _ensure_queue()
    while True:
        batch_id = await queue.get()
        try:
            await _run_batch_id(batch_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad batch must not kill the loop
            logger.exception("batch worker: batch %s crashed", batch_id)
        finally:
            queue.task_done()


async def startup() -> None:
    """Lifespan hook: sweep orphaned single ops, resume batches, start worker."""
    global _worker_task
    # Single-URL (non-batch) operations cannot resume — fail the orphaned ones.
    try:
        swept = await asyncio.to_thread(
            operations.fail_stale_operations, _ORPHAN_SWEEP_REASON
        )
        if swept:
            logger.warning(
                "batch worker startup: failed %d orphaned single-operation rows",
                swept,
            )
    except Exception:  # noqa: BLE001 — a sweep failure must not block boot
        logger.exception("batch worker startup sweep failed")
    # Durable resume: re-enqueue every non-terminal batch (rebuilt from DB).
    try:
        active = await asyncio.to_thread(batch_store.list_active_batch_ids)
        for batch_id in active:
            _ensure_queue().put_nowait(batch_id)
        if active:
            logger.info("batch worker startup: resuming %d batches", len(active))
    except Exception:  # noqa: BLE001 — resume failure must not block boot
        logger.exception("batch worker startup resume scan failed")
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(
            _worker_loop(), name="v2-perceive-batch-worker"
        )


async def shutdown() -> None:
    """Lifespan hook: stop the worker between URLs."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


async def drain_for_tests() -> None:
    """Synchronously process every queued batch (test helper — the test app
    runs without the lifespan, so no worker task exists there)."""
    queue = _ensure_queue()
    while not queue.empty():
        batch_id = queue.get_nowait()
        try:
            await _run_batch_id(batch_id)
        finally:
            queue.task_done()
