"""V2 API router (Task F.5).

All V2 endpoints mount under /v2 via this single router (plan section
4): same get_current_user, same key types, same allowed_endpoints
allowlist, same middleware stack as V1. Later sprints add discover /
lookup / distill / ingest / watch / feedback sub-routers here.
"""

from fastapi import APIRouter

from .handlers.discover import router as discover_router
from .handlers.distill import router as distill_router
from .handlers.ingest import router as ingest_router
from .handlers.lookup import router as lookup_router
from .handlers.perceive import router as perceive_router
from .handlers.perceive_status import router as perceive_status_router
from .handlers.watch import router as watch_router

router = APIRouter()

router.include_router(perceive_router, tags=["v2-perceive"])
router.include_router(perceive_status_router, tags=["v2-perceive"])
router.include_router(discover_router, tags=["v2-discover"])
router.include_router(lookup_router, tags=["v2-lookup"])
router.include_router(distill_router, tags=["v2-distill"])
router.include_router(ingest_router, tags=["v2-ingest"])
router.include_router(watch_router, tags=["v2-watch"])


@router.get("/", tags=["v2-info"])
def v2_info() -> dict:
    """V2 API information."""
    return {
        "version": "v2",
        "status": "beta",
        "endpoints": {
            "perceive": "POST /v2/perceive",
            "perceive_status": "GET /v2/perceive/{operation_id}",
            "perceive_batch": "POST /v2/perceive/batch",
            "perceive_batch_status": "GET /v2/perceive/batch/{job_id}",
            "discover": "POST /v2/discover",
            "lookup": "POST /v2/lookup",
            "distill": "POST /v2/distill",
            "ingest": "POST /v2/ingest",
            "ingest_files": "POST /v2/ingest/files",
            "ingest_list": "GET /v2/ingest",
            "ingest_status": "GET /v2/ingest/{job_id}",
            "ingest_cancel": "DELETE /v2/ingest/{job_id}",
            "ingest_retry_webhook": "POST /v2/ingest/{job_id}/retry-webhook",
            "ingest_webhook_secret": "GET /v2/ingest/webhook-secret",
            "ingest_webhook_secret_rotate": "POST /v2/ingest/webhook-secret/rotate",
            "watch_create": "POST /v2/watch",
            "watch_list": "GET /v2/watch",
            "watch_get": "GET /v2/watch/{watcher_id}",
            "watch_snapshots": "GET /v2/watch/{watcher_id}/snapshots",
            "watch_update": "PATCH /v2/watch/{watcher_id}",
            "watch_delete": "DELETE /v2/watch/{watcher_id}",
        },
    }
