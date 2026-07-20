"""POST / GET / PATCH / DELETE /v2/watch (Task I.2).

Thin handlers per the coding rules: auth (``get_current_user``), the watcher
plan gate (``check_watcher_quota`` — 402 on a watch-disabled plan or when the
project already holds ``max_watchers`` active monitors), then delegate to
``services.v2_engine.watch_flow`` / ``watch_store``.

Lifecycle: POST creates an active watcher (SSRF-screened) scheduled to fire on
the poller's next tick and answers 201. GET lists / shows. PATCH updates the
cadence, diff settings or active/paused status. DELETE soft-deletes (tombstone,
schedule cleared). The recurring checks themselves run out of band in the
droplet-local ``watch_worker`` — there is no Cloud Tasks (owner decision
2026-06-07: no Google services).

No V1 activity row is written for watch CRUD: a watcher is not a conversion, and
its checks are audited through ch_watcher_snapshots, not ch_activity.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from api.deps import check_watcher_quota, get_current_user
from api.v2.schemas.watch import (
    WatchCreateRequest,
    WatcherListResponse,
    WatcherResponse,
    WatcherSnapshotListResponse,
    WatchUpdateRequest,
)
from monitoring import posthog_client
from services.v2_engine import watch_flow, watch_store


def _notification_channel(watcher) -> str:
    """Which channels a watcher notifies on: webhook / email / both / none."""
    has_webhook = bool(getattr(watcher, "webhook_url", None))
    has_email = bool(getattr(watcher, "notify_email", None))
    if has_webhook and has_email:
        return "both"
    if has_webhook:
        return "webhook"
    if has_email:
        return "email"
    return "none"

logger = logging.getLogger(__name__)

router = APIRouter()

ENDPOINT = "/v2/watch"

# Dashboard list paging bounds (Task I.4).
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
# Snapshot timeline page bounds (Task I.4).
DEFAULT_SNAPSHOT_LIMIT = 20
MAX_SNAPSHOT_LIMIT = 100


@router.post("/watch", response_model=WatcherResponse, status_code=201)
async def create_watch(
    body: WatchCreateRequest,
    user: dict = Depends(get_current_user),
) -> WatcherResponse:
    """Create a watcher (201). Gates first: nothing is persisted until the plan
    gate AND the SSRF screen pass, so a 402/4xx leaves zero rows behind."""
    project_id = int(user["id"])

    active = await asyncio.to_thread(
        watch_store.count_active_for_project, project_id
    )
    check_watcher_quota(user, active)

    try:
        watcher = await watch_flow.create_watcher(body, project_id)
    except HTTPException:
        raise  # SSRF / validation rejection — surfaced as-is
    except Exception as exc:
        logger.exception("/v2/watch create failed for %s", body.url)
        raise HTTPException(
            status_code=500,
            detail="Could not create the watcher. Please try again.",
        ) from exc

    posthog_client.capture_project_event(user["id"], "v2_watch_created", {
        "watcher_id": watcher.watcher_id,
        "frequency_minutes": watcher.frequency_minutes,
        "diff_mode": watcher.diff_mode,
        "notification_channel": _notification_channel(watcher),
        "plan_tier": user.get("subscription", {}).get("plan_slug", user.get("plan_slug", "free")),
    }, source=posthog_client.source_from(user))
    return watch_flow.watcher_response(watcher)


@router.get("/watch", response_model=WatcherListResponse)
async def list_watch(
    skip: int = 0,
    limit: int = DEFAULT_LIST_LIMIT,
    user: dict = Depends(get_current_user),
) -> WatcherListResponse:
    """Newest-first page of this project's live watchers (read-only)."""
    skip = max(skip, 0)
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    watchers = await asyncio.to_thread(
        watch_store.list_watchers_for_project,
        int(user["id"]),
        skip=skip,
        limit=limit + 1,
    )
    has_more = len(watchers) > limit
    return WatcherListResponse(
        watchers=[watch_flow.watcher_summary(w) for w in watchers[:limit]],
        skip=skip,
        limit=limit,
        has_more=has_more,
    )


# NOTE: any future static sub-route (e.g. /watch/webhook-secret for I.4) MUST be
# declared BEFORE these "/watch/{watcher_id}" routes — Starlette matches in
# definition order, so a dynamic {watcher_id} declared first would capture it.


@router.get("/watch/{watcher_id}", response_model=WatcherResponse)
async def get_watch(
    watcher_id: str,
    user: dict = Depends(get_current_user),
) -> WatcherResponse:
    """Show one watcher (project-scoped 404; a tombstone reads as gone)."""
    watcher = await asyncio.to_thread(
        watch_store.get_watcher_for_project, watcher_id, int(user["id"])
    )
    if watcher is None or watcher.status == "deleted":
        raise HTTPException(status_code=404, detail="Watcher not found")
    return watch_flow.watcher_response(watcher)


@router.get("/watch/{watcher_id}/snapshots", response_model=WatcherSnapshotListResponse)
async def list_watch_snapshots(
    watcher_id: str,
    limit: int = DEFAULT_SNAPSHOT_LIMIT,
    user: dict = Depends(get_current_user),
) -> WatcherSnapshotListResponse:
    """Newest-first snapshot timeline for one watcher (project-scoped 404).

    Ownership is verified here (``get_watcher_for_project``) before the
    snapshot read, per the I.3 store-tenancy contract.
    """
    project_id = int(user["id"])
    watcher = await asyncio.to_thread(
        watch_store.get_watcher_for_project, watcher_id, project_id
    )
    if watcher is None or watcher.status == "deleted":
        raise HTTPException(status_code=404, detail="Watcher not found")

    limit = max(1, min(limit, MAX_SNAPSHOT_LIMIT))
    snapshots = await asyncio.to_thread(
        watch_store.list_snapshots, watcher_id, limit
    )
    return WatcherSnapshotListResponse(
        watcher_id=watcher_id,
        snapshots=[watch_flow.snapshot_response(s) for s in snapshots],
        limit=limit,
    )


@router.patch("/watch/{watcher_id}", response_model=WatcherResponse)
async def update_watch(
    watcher_id: str,
    body: WatchUpdateRequest,
    user: dict = Depends(get_current_user),
) -> WatcherResponse:
    """Update cadence / diff settings / active-paused status (project-scoped)."""
    project_id = int(user["id"])

    # Resuming a paused watcher adds an active monitor, so it must clear the
    # same max_watchers cap as POST — otherwise pause/resume cycling lets a
    # project hold more active watchers than its plan allows. Only a genuine
    # paused->active transition is gated (re-activating an already-active row,
    # or any other field change, is exempt).
    if body.status == "active":
        current = await asyncio.to_thread(
            watch_store.get_watcher_for_project, watcher_id, project_id
        )
        if current is not None and current.status != "active":
            active = await asyncio.to_thread(
                watch_store.count_active_for_project, project_id
            )
            check_watcher_quota(user, active)

    watcher = await watch_flow.update_watcher(watcher_id, project_id, body)
    if watcher is None:
        raise HTTPException(status_code=404, detail="Watcher not found")
    posthog_client.capture_project_event(user["id"], "v2_watch_updated", {
        "watcher_id": watcher.watcher_id,
        "frequency_minutes": watcher.frequency_minutes,
        "diff_mode": watcher.diff_mode,
        "status": watcher.status,
        "notification_channel": _notification_channel(watcher),
    }, source=posthog_client.source_from(user))
    return watch_flow.watcher_response(watcher)


@router.delete("/watch/{watcher_id}", response_model=WatcherResponse)
async def delete_watch(
    watcher_id: str,
    user: dict = Depends(get_current_user),
) -> WatcherResponse:
    """Soft-delete a watcher (idempotent; returns the tombstoned row)."""
    watcher = await asyncio.to_thread(
        watch_store.delete_watcher, watcher_id, int(user["id"])
    )
    if watcher is None:
        raise HTTPException(status_code=404, detail="Watcher not found")
    posthog_client.capture_project_event(user["id"], "v2_watch_deleted", {
        "watcher_id": watcher.watcher_id,
        "checks_count": getattr(watcher, "checks_count", None),
    }, source=posthog_client.source_from(user))
    return watch_flow.watcher_response(watcher)
