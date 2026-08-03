
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from api.v1.router import router as v1_router
from api.v2.router import router as v2_router
from api.internal import router as internal_router
from middleware.cors import DynamicCORSMiddleware
from middleware.security_headers import SecurityHeadersMiddleware
from middleware.timeout import TimeoutMiddleware
from monitoring import posthog_client
from monitoring.posthog_client import PostHogContextMiddleware
from services import billing_rotation, retention_worker
from services.browser.converters.browser_manager import (
    BrowserManager,
    get_browser_manager,
)
from services.browser.converters.errors import ConversionError
from services.v2_engine import batch_worker, ingest_worker, watch_worker
from utils import memory

logger = logging.getLogger(__name__)


def _worker_enabled(flag: str) -> bool:
    """True unless the env flag explicitly disables the worker ("0"/"false"/"no").

    Droplet deployments set INPROCESS_BILLING_ROTATION=0, INPROCESS_WATCH_WORKER=0
    and INPROCESS_RETENTION_WORKER=0 so the systemd timers in deploy/systemd/ own
    those schedules exclusively (see deploy/systemd/README.md). Default is enabled
    so local dev keeps the in-process pollers with no .env changes.
    """
    return os.getenv(flag, "1").strip().lower() not in ("0", "false", "no")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for browser initialization and cleanup."""
    # Startup: initialize PostHog first so the droplet-local pollers below —
    # which run off-request and would otherwise report nothing — can capture
    # events and have their unhandled errors autocaptured through the shared
    # process singleton.
    posthog_client.init()
    logger.info("[Startup] PostHog analytics initialized")

    # Memory hygiene (2026-07-28 incident): periodic gc + malloc_trim sweep
    # so heap freed after large conversions is returned to the kernel instead
    # of sitting in glibc arenas (and then swap) forever. Pairs with the
    # post-conversion hooks in api/v1/convert.py and utils/processor.py, and
    # with MALLOC_ARENA_MAX=2 in the systemd unit.
    memory.start_periodic_trim()

    # Startup: Initialize the browser
    logger.info("[Startup] Initializing browser instance...")
    browser_manager = await get_browser_manager()
    logger.info("[Startup] Browser initialized successfully")

    # F.8: sweep operation rows orphaned by the previous process, then
    # start the droplet-local batch worker (no Cloud Tasks).
    await batch_worker.startup()
    logger.info("[Startup] V2 batch worker started")

    # H.7: resume any in-flight ingest jobs (durable per-job/page state),
    # then start the droplet-local ingest worker.
    await ingest_worker.startup()
    logger.info("[Startup] V2 ingest worker started")

    # I.1: start the droplet-local watcher poller (no Cloud Tasks). The
    # schedule lives in ch_watchers.next_check_at, so there is nothing to
    # resume — the first tick sweeps up everything overdue.
    if _worker_enabled("INPROCESS_WATCH_WORKER"):
        await watch_worker.startup()
        logger.info("[Startup] V2 watch worker started")
    else:
        logger.info("[Startup] V2 watch worker disabled (INPROCESS_WATCH_WORKER=0) - systemd timer owns the schedule")

    # Usage-period rotation poller (no Cloud Tasks): replaces the
    # GCP-triggered /internal/rotate-usage-period schedule, which was not
    # firing — periods lapsed, and usage went uncounted AND ungated. The
    # schedule lives in ch_subscriptions.current_period_end, so the first
    # tick catches up everything overdue (including multi-month backlogs).
    if _worker_enabled("INPROCESS_BILLING_ROTATION"):
        await billing_rotation.startup()
        logger.info("[Startup] Billing rotation worker started")
    else:
        logger.info("[Startup] Billing rotation worker disabled (INPROCESS_BILLING_ROTATION=0) - systemd timer owns the schedule")

    # Droplet-local file-retention poller (no Cloud Tasks): sweeps the durable
    # ch_scheduled_deletions schedule, replacing the GCP Cloud Tasks file-cleanup
    # trigger that was not firing (expired files were never deleted). The
    # schedule lives in ch_scheduled_deletions.delete_at, so the first tick
    # catches up everything already overdue.
    if _worker_enabled("INPROCESS_RETENTION_WORKER"):
        await retention_worker.startup()
        logger.info("[Startup] Retention worker started")
    else:
        logger.info("[Startup] Retention worker disabled (INPROCESS_RETENTION_WORKER=0) - systemd timer owns the schedule")

    yield

    # Shutdown: stop the workers first (they render through the browser),
    # then close the browser. The gated workers' shutdown() hooks run
    # UNCONDITIONALLY on purpose: they are None-guarded no-ops when the
    # worker never started, and re-reading the INPROCESS_* flags here would
    # leak a running poller if os.environ changed between startup and
    # shutdown (e.g. a test harness flipping env vars around a lifespan).
    await retention_worker.shutdown()
    logger.info("[Shutdown] Retention worker stopped")
    await billing_rotation.shutdown()
    logger.info("[Shutdown] Billing rotation worker stopped")
    await watch_worker.shutdown()
    logger.info("[Shutdown] V2 watch worker stopped")
    await ingest_worker.shutdown()
    logger.info("[Shutdown] V2 ingest worker stopped")
    await batch_worker.shutdown()
    logger.info("[Shutdown] V2 batch worker stopped")
    await memory.stop_periodic_trim()
    logger.info("[Shutdown] Periodic memory trim stopped")
    logger.info("[Shutdown] Closing browser instance...")
    await browser_manager.shutdown()
    logger.info("[Shutdown] Browser closed successfully")

    # Flush any buffered analytics before the process exits.
    posthog_client.shutdown()
    logger.info("[Shutdown] PostHog analytics flushed")


app = FastAPI(
    title="Conversion API",
    description="Convert files between formats",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(TimeoutMiddleware, timeout=300.0)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(DynamicCORSMiddleware)
# Outermost: open a PostHog context per request so exceptions autocaptured
# anywhere downstream inherit the project distinct-id that get_current_user
# stamps on it after auth.
app.add_middleware(PostHogContextMiddleware)

app.include_router(v1_router, prefix="/v1", tags=["v1"])
app.include_router(v2_router, prefix="/v2", tags=["v2"])
app.include_router(internal_router)


@app.get("/", tags=["root"])
def root():
    """API Information"""
    return {
        "service": "Conversion API",
        "version": "1.0.0",
        "documentation": "/docs",
        "available_versions": ["v1", "v2"],
        "current_version": "v1"
    }


def _check_database() -> bool:
    """Blocking DB liveness probe (run off the event loop via to_thread)."""
    from utils.postgres import get_db
    from sqlmodel import text
    try:
        db = get_db()
        db.exec(text("SELECT 1"))
        db.close()
        return True
    except Exception:
        return False


def _check_storage() -> bool:
    """Blocking object-storage liveness probe (run off the event loop).

    Uses the shared boto3 client: building a fresh client per probe (every
    LB health-check interval) was a steady allocation-churn source feeding
    glibc arena fragmentation in the long-lived process.
    """
    try:
        from utils.storage import DO_SPACES_BUCKET, get_s3_client
        get_s3_client().head_bucket(Bucket=DO_SPACES_BUCKET)
        return True
    except Exception:
        return False


@app.get("/health", tags=["system"])
async def health_check():
    """Health check endpoint.

    Probes the three critical subsystems — database, object storage, and the
    headless browser — and returns 503 if any is unhealthy so a load balancer
    drains/restarts the node. The browser check is a read-only CDP round-trip
    (BrowserManager.is_browser_healthy) that never triggers recovery, and the
    response carries saturation stats (in-use slots, queue depth, crash
    recoveries) for observability. The blocking DB/storage probes run off the
    event loop via to_thread so the async handler never stalls.
    """
    from datetime import datetime, timezone

    checks = {
        "database": await asyncio.to_thread(_check_database),
        "storage": await asyncio.to_thread(_check_storage),
    }

    browser_stats = None
    try:
        bm = BrowserManager._instance
        if bm is None:
            # Startup initializes the browser; a missing instance means it
            # never came up (or was reset) — report unhealthy.
            checks["browser"] = False
        else:
            checks["browser"] = await bm.is_browser_healthy()
            browser_stats = bm.stats()
    except Exception:
        checks["browser"] = False

    all_healthy = all(checks.values())

    content = {
        "status": "healthy" if all_healthy else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if browser_stats is not None:
        content["browser"] = browser_stats

    # In-gateway CPU-conversion load (image/document/WeasyPrint path) plus
    # process RSS: the ops memory-guard timer uses these to restart the
    # service only when it is bloated AND truly idle (browser slots free AND
    # no non-browser conversion running/queued).
    try:
        from api.v1 import convert as v1_convert
        from utils import memory as memory_utils
        content["load"] = {
            "pending_conversions": v1_convert._pending_conversions,
            "pending_bytes": v1_convert._pending_bytes,
            "rss_bytes": memory_utils.current_rss_bytes(),
        }
    except Exception:  # noqa: BLE001 — observability must not fail /health
        pass

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content=content,
    )


@app.exception_handler(ConversionError)
async def conversion_error_handler(request: Request, exc: ConversionError):
    """Return a structured envelope for typed conversion failures.

    Distinguishes soft failures (the target site or the input is at fault:
    4xx/502/504) from a generic 500 so clients and on-call can tell them
    apart. These are expected conversion outcomes already tracked via the
    conversion_failed analytics event in the processor, so they are NOT
    re-captured as exceptions here.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())


@app.exception_handler(Exception)
def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors gracefully"""
    from monitoring.logger import log_security_event

    log_security_event("error", {
        "path": request.url.path,
        "error": str(exc),
        "client_ip": request.client.host
    })

    # Report to PostHog with a stack trace; get_current_user stamps the
    # project distinct-id on request.state after auth, so a resolved request
    # is attributed to its project. The returned event_id lets the client
    # quote it to support.
    distinct_id = getattr(request.state, "posthog_distinct_id", None)
    event_id = posthog_client.capture_exception(
        exc,
        distinct_id=distinct_id,
        properties={"path": request.url.path},
        groups=getattr(request.state, "posthog_group", None),
    )

    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "event_id": event_id}
    )


if __name__ == "__main__":
    import asyncio
    import sys
    import uvicorn
    
    # Fix for Windows asyncio subprocess issue
    if sys.platform == 'win32':
        # Set the event loop policy before anything else
        from asyncio import WindowsProactorEventLoopPolicy, WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsProactorEventLoopPolicy())
    
    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=8010,
        reload=os.getenv("ENV", "production") == "development",
        loop="asyncio",
        # Trust X-Forwarded-For from the local nginx reverse proxy so
        # request.client.host is the real client IP (not 127.0.0.1) for
        # logging, widget abuse tracking, and per-IP rate limiting.
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
