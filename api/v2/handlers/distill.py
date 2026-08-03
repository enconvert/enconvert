"""POST /v2/distill (Task H.5).

Thin handler per the coding rules: auth (``get_current_user``), V2 quota
gate (``distill_operations`` -- 402 on a disabled plan or an exhausted
monthly quota), then delegate to ``services.v2_engine.distill_flow``.

The gate here is a fast ``units=1`` check that rejects a disabled plan or a
zero-headroom quota before any render. The flow re-checks the quota PER URL
(so ``discover_from``, whose URL count is unknown up front, and a large
``urls[]`` list both stop cleanly at the cap instead of over-spending) and
increments the counter only for URLs that complete.

V1's activity table is reused with ``count_usage=False`` for dashboard
visibility only (coexistence rule 3: a V2 operation never consumes V1
conversion quota).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.deps import check_v2_feature, check_v2_quota, get_current_user
from api.v2.schemas.distill import DistillRequest, DistillResponse
from monitoring import posthog_client
from monitoring.metrics import log_activity_start, update_activity_status
from services.v2_engine import distill_flow
from utils.error_capture import error_fields

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/distill"


@router.post(
    "/distill",
    response_model=DistillResponse,
    response_model_exclude_none=True,
)
async def distill(
    body: DistillRequest,
    user: dict = Depends(get_current_user),
) -> DistillResponse:
    """Schema-driven structured extraction (CSS-first, LLM fallback)."""
    # For an explicit urls[] list the count is known, so pre-reject an
    # over-quota request before any render; discover_from's count is
    # unknown up front, so it gates 1 unit here and the flow re-checks per
    # discovered URL.
    units = len(body.urls) if body.urls else 1
    check_v2_quota(user, "distill_operations", units=units)
    # discover_from drives discover_flow, so it must carry the discover
    # plan flag too — otherwise a distill-only plan could use discover for
    # free by routing through here.
    if body.discover_from is not None:
        check_v2_feature(user, "discover_enabled", "Discover")

    operation_id = f"dst_{uuid.uuid4().hex}"
    source = (
        str(body.discover_from.url)
        if body.discover_from is not None
        else (body.urls[0] if body.urls else "")
    )
    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=len(source),
        source_url=source,
    )
    start = datetime.now(timezone.utc)

    try:
        response = await distill_flow.run(body, operation_id, user)
    except HTTPException as exc:
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise
    except Exception as exc:
        # Full detail to server logs only; the client gets a generic
        # message + the operation_id for support correlation. Raw
        # exception text can leak internal paths / library internals
        # (Playwright, SQLAlchemy, etc.) — never echo it.
        logger.exception("/v2/distill failed (operation %s)", operation_id)
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise HTTPException(
            status_code=500,
            detail=f"Distillation failed. Reference operation_id "
            f"'{operation_id}' when contacting support.",
        ) from exc

    await _mark_activity(
        activity_id, "Success", start, output_file_size=response.completed
    )
    posthog_client.capture_project_event(user["id"], "v2_distill_completed", {
        "operation_id": operation_id,
        "completed": response.completed,
        "total": getattr(response, "total", None),
        "failed": getattr(response, "failed", None),
        "plan_tier": user.get("subscription", {}).get("plan_slug", user.get("plan_slug", "free")),
        "duration_seconds": (datetime.now(timezone.utc) - start).total_seconds(),
    }, source=posthog_client.source_from(user))
    return response


async def _mark_activity(
    activity_id: int,
    status: str,
    start: datetime,
    *,
    output_file_size: int = 0,
    error: BaseException | None = None,
    error_context: str | None = None,
) -> None:
    """Best-effort activity update; never masks the request outcome."""
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    try:
        await update_activity_status(
            activity_id,
            status,
            output_file_size=output_file_size,
            duration=duration,
            count_usage=False,  # coexistence rule 3: V2 never bills V1
            **error_fields(error, context=error_context),
        )
    except Exception:  # noqa: BLE001
        logger.warning("activity update failed", exc_info=True)
