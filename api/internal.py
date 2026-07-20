"""Internal endpoints (manual / legacy-Cloud-Tasks triggers). Not exposed to public API."""
import hmac
import os
import logging
from fastapi import APIRouter, Request, HTTPException

from utils.postgres import get_db
from utils.retention import _delete_and_reconcile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal")

INTERNAL_AUTH_TOKEN = os.getenv("INTERNAL_AUTH_TOKEN", "")


def _verify_internal_auth(request: Request):
    """Authorize an internal-endpoint call. FAILS CLOSED: an unset
    INTERNAL_AUTH_TOKEN disables these endpoints entirely rather than leaving
    them open (they can delete arbitrary objects / rotate billing). Constant-time
    comparison to avoid leaking the token via timing."""
    token = request.headers.get("X-Internal-Auth", "")
    if not INTERNAL_AUTH_TOKEN or not hmac.compare_digest(token, INTERNAL_AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")


@router.post("/cleanup-file")
async def cleanup_file(request: Request):
    """Delete one expired file (manual / straggler Cloud-Task trigger).

    Routine retention now runs in-process via services/retention_worker.py; this
    route stays for manual ops and any legacy Cloud Task still pointed at it, and
    shares the same deletion + storage-reconciliation primitive so both paths
    behave identically."""
    _verify_internal_auth(request)
    data = await request.json()
    object_key = data["object_key"]
    project_id = data.get("project_id")

    db = get_db()
    try:
        _delete_and_reconcile(db, object_key, project_id)
        db.commit()
    finally:
        db.close()

    return {"status": "ok"}


# NOTE: overage capture moved to services/billing_rotation.py (sync, no
# mid-transaction commit, DB-unique dedup marker) — it must only ever run
# inside the rotation's FOR-UPDATE-locked single transaction.


@router.post("/rotate-usage-period")
async def rotate_usage_period(request: Request):
    """Rotate one project's usage period on demand.

    Rotation is now driven by the droplet-local poller
    (services/billing_rotation.py, started from main.py's lifespan) —
    the no-GCP replacement for the Cloud Tasks trigger, which was not
    firing and left periods unrotated (usage uncounted AND ungated once
    the first period lapsed). This route stays for manual triggering and
    any straggler Cloud Task still pointed at it; both paths share the
    same idempotent, FOR-UPDATE-guarded implementation, so a double
    trigger is a harmless skip. The old per-call Cloud Tasks
    re-scheduling is gone.
    """
    _verify_internal_auth(request)
    data = await request.json()
    project_id = data["project_id"]

    from services.billing_rotation import rotate_project_period

    return await rotate_project_period(project_id)
