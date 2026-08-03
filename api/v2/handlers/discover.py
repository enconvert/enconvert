"""POST /v2/discover (Task H.1).

Thin handler per the coding rules: auth (``get_current_user``), V2 plan
gate (``discover_enabled``, 402 — H.1 How step 2), then delegate to
``services.v2_engine.discover_flow``. Discover is stateless and
browser-free (HTTP-only Crawl4AI + sitemap), so there is no operation
row, no Spaces artifact and no usage counter. V1's activity table is
reused with ``count_usage=False`` purely for dashboard visibility
(coexistence rule 3: a V2 operation never consumes V1 conversion quota).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException

from api.deps import check_v2_feature, get_current_user
from api.v2.schemas.discover import DiscoverRequest, DiscoverResponse
from monitoring import posthog_client
from monitoring.metrics import log_activity_start, update_activity_status
from services.v2_engine import discover_flow
from utils.error_capture import error_fields

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/discover"


@router.post("/discover", response_model=DiscoverResponse)
async def discover(
    body: DiscoverRequest,
    user: dict = Depends(get_current_user),
) -> DiscoverResponse:
    """List a site's URLs with no browser (sitemap + HTTP-only crawl)."""
    check_v2_feature(user, "discover_enabled", "Discover")

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=ENDPOINT,
        input_file_size=len(str(body.url)),
        source_url=str(body.url),
    )
    start = datetime.now(timezone.utc)

    try:
        response = await discover_flow.run(body, user)
    except HTTPException as exc:
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise
    except Exception as exc:
        # Full detail to server logs only; the client gets a generic
        # message. Raw exception text can leak internal paths / library
        # internals (httpx, crawl4ai) — never echo it.
        logger.exception("/v2/discover failed for %s", body.url)
        await _mark_activity(activity_id, "Failed", start, error=exc)
        raise HTTPException(
            status_code=500,
            detail="URL discovery failed. Please try again or contact support.",
        ) from exc

    await _mark_activity(
        activity_id, "Success", start, output_file_size=response.total
    )
    posthog_client.capture_project_event(user["id"], "v2_discover_completed", {
        "url_domain": urlsplit(str(body.url)).netloc,
        "total": response.total,
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
