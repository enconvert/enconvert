"""POST / GET / DELETE /v2/ingest (Task H.7).

Thin handlers per the coding rules: auth (``get_current_user``), V2 quota
gate (``ingest_pages`` -- 402 on a disabled plan or an exhausted monthly
quota), then delegate to the durable ingest pipeline.

``/v2/ingest`` is ALWAYS asynchronous: POST validates + gates + creates the
ch_ingest_jobs row + enqueues the job, and answers 202 with a ``job_id``;
the droplet-local ``ingest_worker`` discovers, renders, chunks and assembles
the JSONL out of band. GET reports lifecycle status (with a signed output URL
once completed); DELETE cancels (sets status='canceled'; the worker observes
it between pages and stops without assembling).

The submit-time gate is a fast ``units=1`` check (plan has ingest enabled and
some monthly headroom). The worker re-checks the quota PER PAGE — so a
sitemap/crawl job whose page count is unknown up front stops cleanly at the
cap (remaining pages marked 'skipped') instead of over-spending — and
increments ``ingest_pages`` only for pages that complete.

V1's activity table is reused with ``count_usage=False`` for dashboard
visibility only (coexistence rule 3: a V2 operation never consumes V1 quota).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from api.deps import check_v2_quota, get_current_user
from api.v2.schemas.ingest import (
    IngestJobListResponse,
    IngestJobResponse,
    IngestRequest,
    WebhookRetryResponse,
    WebhookSecretResponse,
)
from monitoring.metrics import log_activity_start, update_activity_status
from services.markdown import SUPPORTED_EXTENSIONS
from services.v2_engine import ingest_flow, ingest_store, ingest_worker
from services.v2_engine.chunking.semantic import (
    DEFAULT_MAX_WORDS,
    DEFAULT_SENTENCE_OVERLAP,
    MAX_MAX_WORDS,
    MAX_SENTENCE_OVERLAP,
    MIN_MAX_WORDS,
)
from utils import webhook_secret
from utils.callback_notifier import WebhookDeliveryResult
from utils.error_capture import error_fields
from utils.retention import schedule_file_cleanup
from utils.storage import delete_from_storage, sanitize_filename, upload_to_gcs
from utils.validators import validate_file_content

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/ingest"

# Dashboard list paging bounds (Task H.8).
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100

# POST /v2/ingest/files (file ingestion): per-request file cap. The monthly
# ingest_pages quota is the real spend limiter (one unit per file, enforced per
# page in ingest_flow); this just bounds a single request.
MAX_FILES_PER_JOB = 200
# NAME_MAX on every mainstream filesystem. A legitimate upload never exceeds
# this; rejecting here keeps the DB row, the Spaces key and the per-chunk JSONL
# label bounded (the label is duplicated into EVERY chunk record).
MAX_FILENAME_LENGTH = 255
# Transient Spaces sub-path for uploaded source files (deleted after assembly).
FILE_UPLOAD_ENDPOINT = "v2-ingest-uploads"
# Durable backstop TTL for staged source uploads. The happy path deletes them
# eagerly at assembly; this guarantees a canceled/failed/crashed job's uploads
# are still swept by services/retention_worker. Generous enough that a large
# in-flight job is never robbed of its own sources mid-resume.
UPLOAD_BACKSTOP_TTL_HOURS = 24


@router.post(
    "/ingest",
    response_model=IngestJobResponse,
    response_model_exclude_none=True,
    status_code=202,
)
async def ingest(
    body: IngestRequest,
    user: dict = Depends(get_current_user),
) -> IngestJobResponse:
    """Create an ingest job and enqueue it (202 + job_id).

    Gate order matters: nothing is persisted until the quota gate passes — a
    402 leaves zero rows behind. SSRF screening runs in the worker (the seed
    via discover_flow, each page via render_html), so submit stays instant.
    """
    check_v2_quota(user, "ingest_pages", units=1)

    project_id = int(user["id"])
    job_id = f"ing_{uuid.uuid4().hex}"
    source = body.url if body.url else (body.urls[0] if body.urls else "")

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=len(source),
        source_url=source,
    )
    start = datetime.now(timezone.utc)

    try:
        await _create_and_enqueue(job_id, project_id, body)
    except Exception as exc:
        logger.exception("/v2/ingest submit failed (job %s)", job_id)
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise HTTPException(
            status_code=500,
            detail=f"Could not start ingest job. Reference job_id "
            f"'{job_id}' when contacting support.",
        ) from exc

    await _mark_activity(activity_id, "Success", start)

    job = await asyncio.to_thread(ingest_store.get_job, job_id)
    if job is None:  # defensive: the row we just created must exist
        raise HTTPException(status_code=500, detail="Ingest job not persisted")
    return ingest_flow.job_response(job)


async def _create_and_enqueue(
    job_id: str, project_id: int, body: IngestRequest
) -> None:
    """Persist the queued job row, then hand its id to the worker."""
    config = ingest_flow.build_job_config(body)
    # Sync SQLModel write offloaded so the event loop is never blocked.
    await asyncio.to_thread(
        ingest_store.create_job,
        job_id=job_id,
        project_id=project_id,
        mode=body.mode,
        source_url=body.url,
        source_urls=body.urls,
        chunk_options=config,
        webhook_url=body.webhook_url,
    )
    # Always enqueue: with the worker running (production) it processes now;
    # in tests (no lifespan, no worker) it waits for ingest_worker.drain.
    await ingest_worker.submit(job_id)


@router.post(
    "/ingest/files",
    response_model=IngestJobResponse,
    response_model_exclude_none=True,
    status_code=202,
)
async def ingest_files(
    files: list[UploadFile] = File(..., description="Documents to ingest."),
    max_words: int = Form(DEFAULT_MAX_WORDS),
    sentence_overlap: int = Form(DEFAULT_SENTENCE_OVERLAP),
    webhook_url: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
) -> IngestJobResponse:
    """Ingest uploaded FILES into RAG-ready chunks (202 + job_id).

    The file counterpart of ``POST /v2/ingest``: each uploaded document (PDF,
    DOCX, PPTX, XLSX, CSV, HTML, EPUB, TXT/MD, and legacy/ODF office) is converted
    to Markdown, split by the SAME heading-aware chunker, and assembled into the
    same single JSONL deliverable — one pipeline for both web and file RAG
    ingestion. Always asynchronous: the durable ingest worker drains it, and
    ``GET`` / ``DELETE /v2/ingest/{job_id}`` report status and cancel it.

    Files are stored to object storage at submit (so the durable resume can
    re-read them) and deleted once the JSONL is assembled. Bills one
    ``ingest_pages`` unit per file, re-checked per file in the worker.
    """
    check_v2_quota(user, "ingest_pages", units=1)

    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files ({len(files)}); the maximum is "
            f"{MAX_FILES_PER_JOB} per job.",
        )

    # Clamp chunker knobs to the shared bounds (mirrors ChunkOptions).
    max_words = max(MIN_MAX_WORDS, min(int(max_words), MAX_MAX_WORDS))
    sentence_overlap = max(0, min(int(sentence_overlap), MAX_SENTENCE_OVERLAP))

    if webhook_url:
        lowered = webhook_url.strip().lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise HTTPException(
                status_code=400,
                detail="webhook_url must start with http:// or https://",
            )
        webhook_url = webhook_url.strip()

    project_id = int(user["id"])
    job_id = f"ing_{uuid.uuid4().hex}"
    max_size = int(user.get("subscription", {}).get("max_file_size", 5242880))

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=0,
        source_url=f"{len(files)} file(s)",
    )
    start = datetime.now(timezone.utc)

    staged: list[str] = []
    try:
        uploaded = await _stage_uploaded_files(
            files, job_id, project_id, max_size, staged
        )
        config = {
            "chunk": {"max_words": max_words, "sentence_overlap": sentence_overlap},
            "render": {},
            "discovery": {},
        }
        # Sync SQLModel writes offloaded so the event loop is never blocked.
        await asyncio.to_thread(
            ingest_store.create_job,
            job_id=job_id,
            project_id=project_id,
            mode="files",
            source_url=None,
            source_urls=None,
            chunk_options=config,
            webhook_url=webhook_url,
        )
        await asyncio.to_thread(ingest_store.create_file_pages, job_id, uploaded)
        await ingest_worker.submit(job_id)
    except HTTPException as exc:
        await _discard_staged(staged)
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise
    except Exception as exc:  # noqa: BLE001
        await _discard_staged(staged)
        logger.exception("/v2/ingest/files submit failed (job %s)", job_id)
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise HTTPException(
            status_code=500,
            detail=f"Could not start ingest job. Reference job_id '{job_id}' "
            "when contacting support.",
        ) from exc

    await _mark_activity(activity_id, "Success", start)

    job = await asyncio.to_thread(ingest_store.get_job, job_id)
    if job is None:  # defensive: the row we just created must exist
        raise HTTPException(status_code=500, detail="Ingest job not persisted")
    return ingest_flow.job_response(job)


def _stored_name(job_id: str, index: int, filename: str) -> str:
    """Per-file staging name; the ``{job}_{index}_`` prefix makes it unique.

    Sanitize BEFORE prefixing: build_object_key -> sanitize_filename runs
    os.path.basename, which would otherwise strip the job_id+index prefix off a
    filename containing a path separator and collide two uploads onto one key.
    """
    return f"{job_id}_{index}_{sanitize_filename(filename)}"


async def _validate_uploads(
    files: list[UploadFile], max_size: int
) -> list[tuple[UploadFile, str]]:
    """PASS 1 — gate EVERY file before ANY object is created.

    Returns ``[(upload, filename)]`` with each spool rewound, ready to stage.
    Gating the whole batch first means a rejected request stages nothing at all:
    there is no orphan window to clean up, because nothing was written. Peak
    memory is unchanged (one file's bytes at a time): Starlette has already
    buffered the whole multipart body into per-file spool files (>1MB spills to
    disk), so this pass only re-reads what is already there.
    """
    validated: list[tuple[UploadFile, str]] = []
    for index, upload in enumerate(files):
        filename = upload.filename or f"file_{index}"
        if len(filename) > MAX_FILENAME_LENGTH:
            # Do not echo the raw filename back — that is the one case where
            # echoing is itself the amplification.
            raise HTTPException(
                status_code=400,
                detail=f"Filename exceeds the {MAX_FILENAME_LENGTH}-character limit.",
            )
        if "\x00" in filename:
            # Postgres TEXT cannot hold NUL; without this the insert raises
            # ValueError deep in create_file_pages and surfaces as an opaque 500.
            raise HTTPException(
                status_code=400,
                detail="Filename contains an invalid null byte.",
            )
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext or 'unknown'}' for '{filename}'.",
            )
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail=f"File '{filename}' is empty.")
        if len(data) > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"File '{filename}' exceeds the {max_size}-byte limit.",
            )
        mismatch = validate_file_content(ENDPOINT, filename, data)
        if mismatch:
            raise HTTPException(
                status_code=400,
                detail=f"File '{filename}' content does not match its '{ext}' type.",
            )
        del data  # release before the next file's bytes are read
        await upload.seek(0)  # rewind the spool for the staging pass
        validated.append((upload, filename))
    return validated


async def _stage_uploaded_files(
    files: list[UploadFile],
    job_id: str,
    project_id: int,
    max_size: int,
    staged: list[str],
) -> list[tuple[str, str]]:
    """Gate the whole batch (pass 1), then stage it (pass 2).

    Returns ``[(object_key, filename)]``. Every key that reaches Spaces is
    appended to ``staged`` BEFORE the next upload starts, so the caller can clean
    up a partial batch, and is immediately registered with the durable
    ch_scheduled_deletions backstop so it is swept even if this process dies.
    Files are read and uploaded ONE AT A TIME so peak memory is a single file,
    not the whole batch.
    """
    validated = await _validate_uploads(files, max_size)

    uploaded: list[tuple[str, str]] = []
    for index, (upload, filename) in enumerate(validated):
        data = await upload.read()
        stored_name = _stored_name(job_id, index, filename)
        result = await asyncio.to_thread(
            upload_to_gcs, data, str(project_id), FILE_UPLOAD_ENDPOINT, stored_name
        )
        object_key = result["object_key"]
        staged.append(object_key)
        # Durable backstop: covers a cancel before assembly, a worker crash, and
        # any terminal path that never reaches _assemble's eager delete.
        # Idempotent + never raises (utils/retention).
        await asyncio.to_thread(
            schedule_file_cleanup, object_key, project_id, UPLOAD_BACKSTOP_TTL_HOURS
        )
        uploaded.append((object_key, filename))
    return uploaded


async def _discard_staged(staged: list[str]) -> None:
    """Best-effort immediate removal of a partially-staged batch.

    With pass-1 validation this is normally unreachable for a gate rejection
    (nothing is staged when a gate trips) — it covers a Spaces/DB fault DURING
    staging. The ch_scheduled_deletions backstop already guarantees eventual
    removal; this just makes it prompt. delete_from_storage is idempotent, so
    this never masks the real failure being raised.
    """
    for object_key in staged:
        try:
            await asyncio.to_thread(delete_from_storage, object_key)
        except Exception:  # noqa: BLE001 — cleanup must not mask the fault
            logger.warning(
                "ingest: failed to discard staged upload %s",
                object_key,
                exc_info=True,
            )


@router.get(
    "/ingest",
    response_model=IngestJobListResponse,
    response_model_exclude_none=True,
)
async def ingest_list(
    skip: int = 0,
    limit: int = DEFAULT_LIST_LIMIT,
    user: dict = Depends(get_current_user),
) -> IngestJobListResponse:
    """Newest-first page of this project's ingest jobs (dashboard, H.8).

    Fetches ``limit + 1`` rows to set ``has_more`` without a COUNT. Read-only —
    it consumes no quota and creates no activity row.
    """
    skip = max(skip, 0)
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    jobs = await asyncio.to_thread(
        ingest_store.list_jobs_for_project,
        int(user["id"]),
        skip=skip,
        limit=limit + 1,
    )
    has_more = len(jobs) > limit
    return IngestJobListResponse(
        jobs=[ingest_flow.job_summary(job) for job in jobs[:limit]],
        skip=skip,
        limit=limit,
        has_more=has_more,
    )


# NOTE: the two static "/ingest/webhook-secret" routes MUST be declared before
# "/ingest/{job_id}" — Starlette matches in definition order, so a dynamic
# {job_id} route declared first would capture "webhook-secret" as an id.


@router.get("/ingest/webhook-secret", response_model=WebhookSecretResponse)
async def ingest_webhook_secret(
    user: dict = Depends(get_current_user),
) -> WebhookSecretResponse:
    """Reveal the project's webhook signing secret, creating it on first call.

    SENSITIVE: this is the only surface that exposes the secret, and only over
    the authenticated dashboard channel, so a customer can configure HMAC
    verification on their endpoint.
    """
    secret = await asyncio.to_thread(
        webhook_secret.get_or_create_webhook_secret, int(user["id"])
    )
    if not secret:
        raise HTTPException(status_code=404, detail="Project not found")
    return WebhookSecretResponse(secret=secret)


@router.post("/ingest/webhook-secret/rotate", response_model=WebhookSecretResponse)
async def ingest_webhook_secret_rotate(
    user: dict = Depends(get_current_user),
) -> WebhookSecretResponse:
    """Rotate the project's webhook signing secret.

    Every signature computed with the previous secret stops verifying the moment
    this commits — use after a suspected leak, then update the consumer.
    """
    secret = await asyncio.to_thread(
        webhook_secret.rotate_webhook_secret, int(user["id"])
    )
    if not secret:
        raise HTTPException(status_code=404, detail="Project not found")
    return WebhookSecretResponse(secret=secret, rotated=True)


@router.get("/ingest/{job_id}", response_model=IngestJobResponse)
async def ingest_status(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> IngestJobResponse:
    """Lifecycle status of one ingest job (project-scoped 404)."""
    job = await asyncio.to_thread(
        ingest_store.get_job_for_project, job_id, int(user["id"])
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return ingest_flow.job_response(job)


@router.delete("/ingest/{job_id}", response_model=IngestJobResponse)
async def ingest_cancel(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> IngestJobResponse:
    """Cancel an ingest job (idempotent; terminal jobs return unchanged)."""
    job = await asyncio.to_thread(ingest_store.cancel_job, job_id, int(user["id"]))
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return ingest_flow.job_response(job)


@router.post("/ingest/{job_id}/retry-webhook", response_model=WebhookRetryResponse)
async def ingest_retry_webhook(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> WebhookRetryResponse:
    """Manually redeliver a completed job's signed completion webhook (H.8).

    Project-scoped 404. 400 when no webhook is configured (or the stored URL now
    resolves to a private/internal address), 409 when the job has not completed.
    Otherwise re-signs and re-POSTs with the same retry/back-off policy as the
    auto-fire path, then reports the outcome.
    """
    job = await asyncio.to_thread(
        ingest_store.get_job_for_project, job_id, int(user["id"])
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    if not job.webhook_url:
        raise HTTPException(
            status_code=400,
            detail="No webhook URL is configured for this job.",
        )
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="A completion webhook is only delivered for completed jobs.",
        )

    result = await ingest_flow.deliver_ingest_webhook(job)
    detail = _retry_detail(result)
    if result.error == "blocked_url":
        # The stored URL now resolves to a private/internal address.
        raise HTTPException(status_code=400, detail=detail)

    return WebhookRetryResponse(
        job_id=job.job_id,
        delivered=result.delivered,
        attempts=result.attempts,
        status_code=result.status_code,
        detail=detail,
    )


def _retry_detail(result: WebhookDeliveryResult) -> str:
    """Human-readable outcome for a manual webhook redelivery."""
    if result.delivered:
        return f"Delivered (HTTP {result.status_code})."
    messages = {
        "blocked_url": "The webhook URL resolves to a private or internal "
        "address and was blocked.",
        "no_secret": "The project signing secret could not be loaded.",
        "no_webhook": "No webhook URL is configured for this job.",
    }
    if result.error in messages:
        return messages[result.error]
    if result.status_code is not None:
        return (
            f"Endpoint returned HTTP {result.status_code} after "
            f"{result.attempts} attempt(s)."
        )
    return f"Delivery failed after {result.attempts} attempt(s)."


async def _mark_activity(
    activity_id: int,
    status: str,
    start: datetime,
    *,
    error: BaseException | None = None,
    error_context: str | None = None,
) -> None:
    """Best-effort activity update; never masks the request outcome."""
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    try:
        await update_activity_status(
            activity_id,
            status,
            duration=duration,
            count_usage=False,  # coexistence rule 3: V2 never bills V1
            **error_fields(error, context=error_context),
        )
    except Exception:  # noqa: BLE001
        logger.warning("activity update failed", exc_info=True)
