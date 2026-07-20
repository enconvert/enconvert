"""POST /v2/lookup (Task H.3).

Thin handler per the coding rules: auth (``get_current_user``), V2 quota
gate (``lookup_queries`` -- 402 on a disabled plan or an exhausted
monthly quota, H.3 verification d), then delegate to
``services.v2_engine.lookup_flow``. V1's activity table is reused with
``count_usage=False`` for dashboard visibility only (coexistence rule 3:
a V2 operation never consumes V1 conversion quota).

The lookup quota is checked HERE, before the flow runs, so nothing is
billed when the gate rejects. The flow itself bumps the counter and
writes the audit row only after Serper actually answers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.deps import check_v2_quota, get_current_user
from api.v2.schemas.lookup import LookupRequest, LookupResponse
from monitoring import posthog_client
from monitoring.metrics import log_activity_start, update_activity_status
from services.v2_engine import lookup_flow

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/lookup"


@router.post(
    "/lookup",
    response_model=LookupResponse,
    response_model_exclude_none=True,
)
async def lookup(
    body: LookupRequest,
    user: dict = Depends(get_current_user),
) -> LookupResponse:
    """Search the web via Serper, optionally auto-perceiving top results."""
    check_v2_quota(user, "lookup_queries")

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=len(body.query),
    )
    start = datetime.now(timezone.utc)

    try:
        response = await lookup_flow.run(body, user)
    except HTTPException:
        await _mark_activity(activity_id, "Failed", start)
        raise
    except Exception as exc:
        # Full detail to server logs only; the client gets a generic
        # message. Raw exception text can leak internal paths / library
        # internals (httpx, SQLAlchemy) — never echo it.
        logger.exception("/v2/lookup failed for query %r", body.query)
        await _mark_activity(activity_id, "Failed", start)
        raise HTTPException(
            status_code=500,
            detail="Lookup failed. Please try again or contact support.",
        ) from exc

    await _mark_activity(
        activity_id, "Success", start, output_file_size=response.total
    )
    posthog_client.capture_project_event(user["id"], "v2_lookup_performed", {
        "result_count": response.total,
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
        )
    except Exception:  # noqa: BLE001
        logger.warning("activity update failed", exc_info=True)
