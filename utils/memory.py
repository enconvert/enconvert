"""Process-level memory hygiene for the 1GB droplet.

Why this exists (2026-07-28 memory incident): the gateway is a single
long-lived Python process. A medium/large conversion allocates hundreds of
MB (upload bytes + decoded intermediates + output bytes); when it finishes,
Python frees the objects but glibc's allocator RETAINS the freed pages in
its arenas instead of returning them to the kernel. The droplet showed the
gateway holding 537MB in swap + 150MB RSS at idle — memory that "never
comes down" after conversions complete or time out. ``malloc_trim(0)``
walks the arenas and releases every free page back to the OS in a few
milliseconds, which is exactly the missing step.

Two hooks are exposed:

* ``schedule_release(payload_size)`` — fire-and-forget, called at the end
  of a conversion. Waits ~1s (so the response frame has exited and the
  request's buffers are actually freed, not still referenced by the
  caller's locals), then runs ``gc.collect()`` + ``malloc_trim(0)``.
  Throttled and payload-gated so small conversions pay nothing.
* ``start_periodic_trim()`` / ``stop_periodic_trim()`` — lifespan-owned
  background task that trims whenever RSS is above a threshold, catching
  anything the per-conversion hook missed (browser flows, workers).

Pair with ``MALLOC_ARENA_MAX=2`` in the systemd unit: to_thread runs
conversions on pool threads, and without the cap each thread gets its own
arena, multiplying retained-but-free heap. Both knobs together are the
standard fix for "RSS only ever grows" in long-lived CPython services.

No-ops safely on non-Linux (dev Macs) and when libc lacks malloc_trim.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import logging
import os
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("MEMORY_TRIM_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# Conversions smaller than this skip the post-conversion trim (the periodic
# task still covers slow accumulation). 4MB keeps thumbnail-sized traffic
# free of any overhead.
_MIN_PAYLOAD_BYTES = int(os.getenv("MEMORY_TRIM_MIN_PAYLOAD_BYTES", str(4 * 1024 * 1024)))
# At most one trim per this many seconds — a burst of large conversions
# pays for one full gc pass, not one per request.
_MIN_INTERVAL_SECONDS = float(os.getenv("MEMORY_TRIM_MIN_INTERVAL_SECONDS", "15"))
# Periodic sweep cadence and the RSS floor below which it does nothing.
_PERIODIC_SECONDS = float(os.getenv("MEMORY_TRIM_PERIODIC_SECONDS", "120"))
_RSS_THRESHOLD_MB = int(os.getenv("MEMORY_TRIM_RSS_THRESHOLD_MB", "250"))
# Delay before a scheduled release runs, letting the request frame (which
# still references the payload bytes in its locals) exit first.
_RELEASE_DELAY_SECONDS = float(os.getenv("MEMORY_TRIM_RELEASE_DELAY_SECONDS", "1.0"))

_last_trim_monotonic: float = 0.0
_periodic_task: Optional[asyncio.Task] = None

_PAGE_SIZE = 4096


def _load_malloc_trim():
    """Resolve glibc's malloc_trim, or None where unavailable (macOS/musl)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name)
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except (OSError, AttributeError):
        return None


_malloc_trim = _load_malloc_trim()


def trim_supported() -> bool:
    """True when malloc_trim is available on this platform."""
    return _malloc_trim is not None


def current_rss_bytes() -> int:
    """Resident set size of this process, 0 where /proc is unavailable."""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return 0


def release_memory(reason: str = "") -> None:
    """Collect garbage and hand freed heap pages back to the kernel.

    Synchronous and cheap relative to any conversion (single-digit ms for
    the trim; the gc pass dominates and is throttled by callers). Never
    raises.
    """
    if not _ENABLED:
        return
    try:
        started = time.monotonic()
        collected = gc.collect()
        trimmed = _malloc_trim(0) if _malloc_trim is not None else -1
        logger.debug(
            "[memory] release (%s): gc collected %d, malloc_trim=%s, %.1fms",
            reason or "unspecified", collected, trimmed,
            (time.monotonic() - started) * 1000.0,
        )
    except Exception:  # noqa: BLE001 — hygiene must never break a request
        logger.debug("[memory] release failed", exc_info=True)


def schedule_release(payload_size: int, reason: str = "conversion") -> None:
    """Fire-and-forget post-conversion release.

    Called from a request handler's ``finally``. Skips small payloads and
    throttles bursts; the actual release runs ~1s later on the event loop,
    after the handler frame (and its payload references) is gone. Safe to
    call from contexts without a running loop (tests): silently no-ops.
    """
    global _last_trim_monotonic
    if not _ENABLED or payload_size < _MIN_PAYLOAD_BYTES:
        return
    now = time.monotonic()
    if now - _last_trim_monotonic < _MIN_INTERVAL_SECONDS:
        return
    _last_trim_monotonic = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(_RELEASE_DELAY_SECONDS, release_memory, reason)


async def _periodic_loop() -> None:
    threshold = _RSS_THRESHOLD_MB * 1024 * 1024
    while True:
        await asyncio.sleep(_PERIODIC_SECONDS)
        try:
            rss = current_rss_bytes()
            if rss and rss >= threshold:
                release_memory(f"periodic rss={rss // (1024 * 1024)}MB")
        except Exception:  # noqa: BLE001 — the sweep must survive anything
            logger.debug("[memory] periodic sweep failed", exc_info=True)


def start_periodic_trim() -> None:
    """Start the lifespan-owned periodic trim task (idempotent)."""
    global _periodic_task
    if not _ENABLED or not trim_supported():
        logger.info(
            "[memory] periodic trim disabled (enabled=%s, malloc_trim=%s)",
            _ENABLED, trim_supported(),
        )
        return
    if _periodic_task is None or _periodic_task.done():
        _periodic_task = asyncio.get_running_loop().create_task(
            _periodic_loop(), name="memory-periodic-trim"
        )
        logger.info(
            "[memory] periodic trim started (every %.0fs, rss>=%dMB)",
            _PERIODIC_SECONDS, _RSS_THRESHOLD_MB,
        )


async def stop_periodic_trim() -> None:
    """Cancel the periodic trim task on shutdown (idempotent)."""
    global _periodic_task
    task, _periodic_task = _periodic_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
