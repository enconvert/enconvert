"""/v2/watch droplet-local scheduler (Task I.1, no-GCP design).

Owner decision 2026-06-07: NO Google services. The plan's per-watcher Cloud
Task self-rescheduling is replaced by a single in-process asyncio poller,
the same lifecycle shape as F.8's ``batch_worker`` and H.7's ``ingest_worker``
(started/stopped from ``main.py`` lifespan). The difference is what drives it:
batch/ingest drain a FIFO of submitted jobs, whereas the watcher is TIME-driven
— it wakes on a fixed interval and asks the database which watchers are due.

Each tick:

* ``watch_store.claim_due_watchers`` atomically claims the active rows whose
  ``next_check_at`` has passed (``FOR UPDATE SKIP LOCKED``) and advances each
  one's schedule a full interval — so a slow render is never double-claimed.
* every claimed watcher is rendered + diffed + rescheduled by
  ``watch_flow.run_check`` (which self-reschedules: writes the next
  ``next_check_at``, or pauses + emails after three consecutive failures).

Durability is free and stronger than Cloud Tasks: the schedule lives in
``next_check_at``, so downtime just means the first tick after boot sweeps up
everything overdue — there is nothing to resume. Renders serialize on the
browser semaphore anyway (plan A5), so the poller processes due watchers one at
a time, keeping memory flat on the 1 GB droplet. Firing is poll-bounded (up to
one interval late); irrelevant under the hourly floor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from services.v2_engine import watch_flow, watch_store

logger = logging.getLogger(__name__)

# How often the poller scans for due watchers. Well under the hourly floor, so
# a check fires within a minute of its scheduled time.
POLL_INTERVAL_SECONDS = 60
# Max watchers handled per tick. The browser semaphore serializes the renders
# regardless; this just bounds one scan so a large overdue backlog drains in
# steady chunks instead of one giant pass.
CLAIM_BATCH = 25

_worker_task: Optional[asyncio.Task] = None


def worker_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


async def tick(now: Optional[datetime] = None) -> int:
    """Run one scan: claim due watchers and check each. Returns the count.

    Shared by the poll loop and the test harness (the test app runs without the
    lifespan, so no loop exists there — a test arms ``next_check_at`` in the
    past and calls ``tick`` directly). A per-watcher failure is isolated so one
    bad watcher never sinks the rest of the batch.
    """
    moment = now or datetime.now(timezone.utc)
    claimed = await asyncio.to_thread(
        watch_store.claim_due_watchers, moment, CLAIM_BATCH
    )
    for watcher in claimed:
        try:
            await watch_flow.run_check(watcher)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad watcher must not stop the tick
            logger.exception(
                "watch worker: check for %s crashed", watcher.watcher_id
            )
    return len(claimed)


async def _poll_loop() -> None:
    """Scan immediately, then every ``POLL_INTERVAL_SECONDS``.

    The first scan runs before the initial sleep so watchers overdue at boot
    fire promptly rather than after a full interval.
    """
    while True:
        try:
            processed = await tick()
            if processed:
                logger.info("watch worker: checked %d due watcher(s)", processed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.exception("watch worker: poll tick crashed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def startup() -> None:
    """Lifespan hook: start the poller.

    No resume scan is needed (unlike ingest): the schedule lives in
    ``next_check_at``, so the first tick naturally sweeps everything overdue.
    """
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_poll_loop(), name="v2-watch-worker")


async def shutdown() -> None:
    """Lifespan hook: stop the poller between ticks.

    Whatever was due but unprocessed (and whatever was provisionally rescheduled
    mid-render) is picked up by the first tick on the next boot.
    """
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
