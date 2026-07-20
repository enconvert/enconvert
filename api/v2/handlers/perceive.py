"""POST /v2/perceive (Task F.5) + /v2/perceive/batch (Task F.8).

Thin handlers per the coding rules: auth, plan gate + quota (both 402,
F.5 verification d/e), request validation, activity logging — then they
delegate to services.v2_engine (perceive_flow / batch_worker). V1's
activity table is reused for dashboard visibility, but with
``count_usage=False`` so a V2 operation can never consume V1 conversion
quota (coexistence rule 3); the V2 counter is bumped inside the flow.

F.8 (no-GCP revision): batches of <= 10 URLs run inline and answer 200
with full results; larger batches answer 202 with a job_id and are
drained one URL at a time by the in-process worker
(services/v2_engine/batch_worker.py). No Cloud Tasks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError

from api.deps import check_batch_limit, check_v2_quota, get_current_user
from monitoring import posthog_client
from api.v2.schemas.perceive import (
    OutputArtifact,
    PerceiveBatchRequest,
    PerceiveBatchResponse,
    PerceiveOptionsBase,
    PerceiveRequest,
    PerceiveResponse,
)
from api.v2.handlers.perceive_status import _response_from_operation
from monitoring.metrics import log_activity_start, update_activity_status
from services.v2_engine import batch_store, batch_worker, operations, perceive_flow
from utils.processor import validate_auth_cookies_headers
from utils.storage import generate_presigned_url

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/perceive"


def _reject_unsupported(body: PerceiveOptionsBase) -> None:
    """proxy_url / geolocation / action_chain ship in later sprints;
    rejecting explicitly beats silently ignoring them."""
    unsupported = [
        name
        for name, value in (
            ("proxy_url", body.proxy_url),
            ("geolocation", body.geolocation),
            ("action_chain", body.action_chain),
        )
        if value is not None
    ]
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=f"Not yet supported: {', '.join(unsupported)}. "
            "These parameters arrive in an upcoming release.",
        )


@router.post("/perceive", response_model=PerceiveResponse)
async def perceive(
    body: PerceiveRequest,
    user: dict = Depends(get_current_user),
) -> PerceiveResponse:
    """Single-URL perception: render once, return every requested output."""
    check_v2_quota(user, "perceive_operations")
    _reject_unsupported(body)
    validate_auth_cookies_headers(
        {
            "auth": body.auth.model_dump() if body.auth else None,
            "cookies": body.cookies,
            "headers": body.headers,
        }
    )

    operation_id = f"per_{uuid.uuid4().hex}"
    url_domain = urlsplit(str(body.url)).netloc
    plan_tier = user.get("subscription", {}).get("plan_slug", user.get("plan_slug", "free"))
    posthog_client.capture_project_event(user["id"], "v2_perceive_requested", {
        "operation_id": operation_id,
        "url_domain": url_domain,
        "plan_tier": plan_tier,
    }, source=posthog_client.source_from(user))

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=len(str(body.url)),
        source_url=str(body.url),
    )
    start = datetime.now(timezone.utc)

    try:
        response = await perceive_flow.run(body, operation_id, user)
    except HTTPException:
        await _mark_activity(activity_id, "Failed", start)
        raise
    except Exception as exc:
        # Full detail goes to server logs only; the client gets a generic
        # message + the operation_id for support correlation. Raw
        # exception text can carry internal paths / library internals
        # (Playwright CDP, boto3, SQLAlchemy) — never echo it.
        logger.exception("/v2/perceive failed for %s", body.url)
        await _mark_activity(activity_id, "Failed", start)
        posthog_client.capture_project_event(user["id"], "v2_perceive_failed", {
            "operation_id": operation_id,
            "url_domain": url_domain,
            "plan_tier": plan_tier,
            "duration_seconds": (datetime.now(timezone.utc) - start).total_seconds(),
            "error_type": type(exc).__name__,
        }, source=posthog_client.source_from(user))
        raise HTTPException(
            status_code=500,
            detail=f"Perception failed. Reference operation_id "
            f"'{operation_id}' when contacting support.",
        ) from exc

    primary_key = next(
        (artifact.object_key for artifact in response.outputs.values()), ""
    )
    total_bytes = sum(
        artifact.size_bytes for artifact in response.outputs.values()
    )
    await _mark_activity(
        activity_id,
        "Success",
        start,
        output_file_size=total_bytes,
        object_key=primary_key,
        content_hash=response.content_hash,
    )
    posthog_client.capture_project_event(user["id"], "v2_perceive_completed", {
        "operation_id": operation_id,
        "url_domain": url_domain,
        "plan_tier": plan_tier,
        "extraction_tier": response.extraction_tier,
        "duration_seconds": response.duration_ms / 1000 if response.duration_ms else None,
        "llm_cost_cents": response.cost_cents,
        "render_quality_score": response.render_quality,
        "cache_hit": response.cache_hit,
        "output_file_size_bytes": total_bytes,
    }, source=posthog_client.source_from(user))
    return response


@router.post(
    "/perceive/batch",
    response_model=PerceiveBatchResponse,
    response_model_exclude_none=True,
)
async def perceive_batch(
    body: PerceiveBatchRequest,
    response: Response,
    user: dict = Depends(get_current_user),
) -> PerceiveBatchResponse:
    """Bulk perception: <= 10 URLs inline (200), larger queued (202).

    Gate order matters: nothing is persisted until every gate passed —
    a 403/402/422 must leave zero rows behind.
    """
    try:
        requests = batch_worker.build_requests(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {"loc": list(error.get("loc", ())), "msg": error.get("msg", "")}
                for error in exc.errors()
            ],
        ) from exc

    check_batch_limit(user, len(requests))
    check_v2_quota(user, "perceive_operations", units=len(requests))
    _reject_unsupported(body.options)
    validate_auth_cookies_headers(
        {
            "auth": body.options.auth.model_dump() if body.options.auth else None,
            "cookies": body.options.cookies,
            "headers": body.options.headers,
        }
    )
    await batch_worker.assert_urls_public([str(r.url) for r in requests])

    job = batch_worker.make_job(body, user, requests)

    if len(requests) <= batch_worker.INLINE_THRESHOLD:
        finished = await batch_worker.process_inline(
            job, batch_worker.INLINE_WAIT_BUDGET_S
        )
        if finished:
            return _batch_response(
                job.batch_id, body.output_mode, job.items, job.zip_artifact
            )
        # The job outlived the inline window (slow pages / busy worker):
        # degrade to async semantics — renders continue in the worker.
        response.status_code = 202
        return PerceiveBatchResponse(
            job_id=job.batch_id,
            status="processing",
            output_mode=body.output_mode,
            total=len(requests),
            pending=len(requests),
            warnings=[
                "batch exceeded the inline response window; "
                "poll GET /v2/perceive/batch/{job_id} for results."
            ],
        )

    await batch_worker.submit(job)
    response.status_code = 202
    return PerceiveBatchResponse(
        job_id=job.batch_id,
        status="queued",
        output_mode=body.output_mode,
        total=len(requests),
        pending=len(requests),
    )


@router.get(
    "/perceive/batch/{job_id}",
    response_model=PerceiveBatchResponse,
    response_model_exclude_none=True,
)
async def perceive_batch_status(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> PerceiveBatchResponse:
    """Aggregate one batch from its operation rows (project-scoped 404)."""
    project_id = int(user["id"])
    rows = operations.list_batch_operations(job_id, project_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Batch not found")

    items = [_response_from_operation(row) for row in rows]
    zip_artifact = _zip_artifact_from_rows(rows, user["id"])
    # The durable batch row is authoritative for output_mode and for the
    # 'canceled' terminal state (which the per-URL rows cannot express).
    batch = batch_store.get_batch_for_project(job_id, project_id)
    if batch is not None:
        output_mode = batch.output_mode
    else:
        output_mode = "zip" if zip_artifact is not None else "manifest"
    override = "canceled" if (batch is not None and batch.status == "canceled") else None
    return _batch_response(
        job_id, output_mode, items, zip_artifact, override_status=override
    )


@router.delete(
    "/perceive/batch/{job_id}",
    response_model=PerceiveBatchResponse,
    response_model_exclude_none=True,
)
async def perceive_batch_cancel(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> PerceiveBatchResponse:
    """Cancel a batch (idempotent). The worker stops between URLs; already
    completed URLs keep their artifacts. Project-scoped 404."""
    project_id = int(user["id"])
    batch = batch_store.cancel_batch(job_id, project_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    rows = operations.list_batch_operations(job_id, project_id)
    items = [_response_from_operation(row) for row in rows]
    zip_artifact = _zip_artifact_from_rows(rows, user["id"])
    override = "canceled" if batch.status == "canceled" else None
    return _batch_response(
        job_id, batch.output_mode, items, zip_artifact, override_status=override
    )


def _batch_response(
    job_id: str,
    output_mode: str,
    items: list[PerceiveResponse],
    zip_artifact: OutputArtifact | None,
    override_status: str | None = None,
) -> PerceiveBatchResponse:
    completed = sum(1 for item in items if item.status == "completed")
    failed = sum(1 for item in items if item.status == "failed")
    pending = len(items) - completed - failed
    if override_status is not None:
        status = override_status
    elif pending == len(items):
        status = "queued"
    elif pending > 0:
        status = "processing"
    elif failed == len(items):
        status = "failed"
    elif failed > 0:
        status = "partial"
    else:
        status = "completed"
    return PerceiveBatchResponse(
        job_id=job_id,
        status=status,  # type: ignore[arg-type]
        output_mode=output_mode,  # type: ignore[arg-type]
        total=len(items),
        completed=completed,
        failed=failed,
        pending=pending,
        zip=zip_artifact,
        items=items,
    )


def _zip_artifact_from_rows(
    rows: list, project_id: object
) -> OutputArtifact | None:
    """Lift the batch ZIP entry (operations.BATCH_ZIP_KEY) off any row."""
    for row in rows:
        entry = (row.output_keys or {}).get(operations.BATCH_ZIP_KEY)
        if not entry:
            continue
        key = entry.get("key")
        if not key:
            return None
        try:
            url = generate_presigned_url(key, str(project_id))
        except Exception:  # noqa: BLE001 — a stale key must not 500 the GET
            logger.warning("batch zip presign failed for %s", key, exc_info=True)
            url = None
        return OutputArtifact(
            url=url,
            object_key=key,
            size_bytes=int(entry.get("size_bytes", 0) or 0),
            content_type="application/zip",
        )
    return None


async def _mark_activity(
    activity_id: int,
    status: str,
    start: datetime,
    *,
    output_file_size: int = 0,
    object_key: str = "",
    content_hash: str | None = None,
) -> None:
    """Best-effort activity update; never masks the request outcome."""
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    try:
        await update_activity_status(
            activity_id,
            status,
            output_file_size=output_file_size,
            object_key=object_key,
            duration=duration,
            content_hash=content_hash,
            count_usage=False,  # coexistence rule 3: V2 never bills V1
        )
    except Exception:  # noqa: BLE001
        logger.warning("activity update failed", exc_info=True)
