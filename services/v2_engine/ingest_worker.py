"""/v2/ingest droplet-local durable worker (Task H.7).

Same in-process asyncio pattern as F.8's ``batch_worker`` (owner decision
2026-06-07: no Google services / no Cloud Tasks), but DURABLE: the queue
carries only ``job_id`` strings and every byte of job/page state lives in
ch_ingest_jobs / ch_ingest_pages. Because nothing is held in memory, a
restart RESUMES in-flight jobs instead of failing them —
``startup`` re-enqueues every non-terminal job (the migration's
``idx_ingest_jobs_active`` partial index), and ``ingest_flow.process_job``
picks up exactly the pages still owed.

A single global worker drains the FIFO and runs jobs one at a time; per-page
renders serialize on the browser semaphore anyway (plan A5), so one job at a
time keeps memory flat on the 1 GB droplet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from services.v2_engine import ingest_flow, ingest_store

logger = logging.getLogger(__name__)

_queue: Optional[asyncio.Queue[str]] = None
_worker_task: Optional[asyncio.Task] = None


def _ensure_queue() -> asyncio.Queue[str]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def submit(job_id: str) -> None:
    """Enqueue a job id for the worker (FIFO, unbounded — the real limiter is
    the per-plan ingest_pages quota enforced inside the flow)."""
    _ensure_queue().put_nowait(job_id)


def worker_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


async def _worker_loop() -> None:
    queue = _ensure_queue()
    while True:
        job_id = await queue.get()
        try:
            await ingest_flow.process_job(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad job must not kill the loop
            logger.exception("ingest worker: job %s crashed", job_id)
        finally:
            queue.task_done()


async def startup() -> None:
    """Lifespan hook: re-enqueue orphaned jobs, then start the worker.

    Single-process gateway (the browser singleton requires it), so any job
    still in queued/discovering/processing at boot was interrupted by the
    previous process — resume it rather than fail it (the H.7 durability
    contract; F.8's batch worker had to fail its equivalents because it had
    no per-job table).
    """
    global _worker_task
    try:
        active = await asyncio.to_thread(ingest_store.list_active_job_ids)
        for job_id in active:
            await submit(job_id)
        if active:
            logger.info(
                "ingest worker startup: resumed %d in-flight job(s)", len(active)
            )
    except Exception:  # noqa: BLE001 — a resume scan failure must not block boot
        logger.exception("ingest worker startup resume scan failed")
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop(), name="v2-ingest-worker")


async def shutdown() -> None:
    """Lifespan hook: stop the worker between jobs (in-flight job resumes on
    the next boot via the startup scan)."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None


async def drain_for_tests() -> None:
    """Synchronously process every queued job (test helper — the test app
    runs without the lifespan, so no worker task exists there)."""
    queue = _ensure_queue()
    while not queue.empty():
        job_id = queue.get_nowait()
        try:
            await ingest_flow.process_job(job_id)
        finally:
            queue.task_done()
