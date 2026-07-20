"""Singleton browser manager — open-source fallback.

Owns one long-lived headless Chromium through a Crawl4AI
``AsyncWebCrawler`` (no stealth, no engine ladder — those live in the
cloud build) and exposes the same public surface the open code uses:
``BrowserManager`` (``get_instance``, ``crawler_slot``, ``get_context``,
``get_page``, ``get_crawler``, ``force_recover``, ``stats``,
``is_browser_healthy``, ``shutdown``, ``reset_instance``, ``_instance``,
``MAX_CONCURRENT_CONTEXTS``), ``get_browser_manager()`` and
``CHROMIUM_MEMORY_FLAGS``.

A bounded semaphore keeps total Chromium pressure at one conversion at a
time by default (low-memory friendly); waiters fast-fail with HTTP 503
after ``BROWSER_SLOT_ACQUIRE_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from crawl4ai import AsyncWebCrawler, BrowserConfig
from fastapi import HTTPException
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
)

from .user_agent import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# Re-validate every navigated/subresource host at request time (SSRF guard).
# ON by default; set BROWSER_SSRF_ROUTE_GUARD=0 to disable.
_SSRF_ROUTE_GUARD = os.getenv(
    "BROWSER_SSRF_ROUTE_GUARD", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Chromium flags passed verbatim to Crawl4AI's BrowserConfig.extra_args —
# the memory-optimized set for <=1GB RAM servers.
CHROMIUM_MEMORY_FLAGS: list[str] = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    # Memory optimization flags
    "--js-flags=--max-old-space-size=256",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--aggressive-cache-discard",
    "--disk-cache-size=1",
    "--memory-pressure-off",
]

# Seconds a request may wait for a conversion slot before fast-failing 503.
_DEFAULT_SLOT_ACQUIRE_TIMEOUT = 240.0


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back (with a warning) on bad input."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    """Parse a float env var; empty/"0"/"none" disables (returns None)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("0", "none", "off", "disabled"):
        return None
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r; using default %r", name, raw, default)
        return default


async def _install_ssrf_route_guard(context: BrowserContext) -> None:
    """Best-effort per-request SSRF guard on ``context``.

    Uses the open ``services.v2_engine.url_safety`` route handler when
    available. Never breaks rendering: any failure lets requests proceed
    (callers already screen the seed URL before rendering).
    """
    if not _SSRF_ROUTE_GUARD:
        return
    try:
        from services.v2_engine.url_safety import make_ssrf_route_handler

        await context.route(
            "**/*", make_ssrf_route_handler(allow_action="continue")
        )
    except Exception:  # noqa: BLE001 — guard is best-effort
        logger.warning(
            "[BrowserManager] could not install SSRF route guard",
            exc_info=True,
        )


class BrowserManager:
    """Singleton manager for one long-lived headless Chromium (open build).

    The Chromium process is owned by a Crawl4AI ``AsyncWebCrawler``; the
    raw Playwright ``Browser`` is re-exposed on ``self._browser`` for the
    ``get_context()`` / ``get_page()`` flows. ``crawler_slot()`` hands the
    Crawl4AI pipeline to callers that want ``arun()``. A semaphore bounds
    concurrent conversions (default 1) — hook registration on the shared
    crawler strategy is only race-free at 1.
    """

    _instance: Optional['BrowserManager'] = None
    _lock = asyncio.Lock()

    # Default max concurrent browser contexts (1 for <=1GB RAM servers).
    MAX_CONCURRENT_CONTEXTS = 1

    def __init__(self) -> None:
        self._crawler: Optional[AsyncWebCrawler] = None
        self._browser: Optional[Browser] = None
        self._initialization_lock = asyncio.Lock()
        self._is_initialized = False
        self._shutdown = False

        self.max_concurrent_contexts = _env_int(
            "BROWSER_MAX_CONCURRENT_CONTEXTS", self.MAX_CONCURRENT_CONTEXTS
        )
        if self.max_concurrent_contexts < 1:
            logger.warning(
                "BROWSER_MAX_CONCURRENT_CONTEXTS=%d is invalid; clamping to 1",
                self.max_concurrent_contexts,
            )
            self.max_concurrent_contexts = 1

        self._semaphore = asyncio.Semaphore(self.max_concurrent_contexts)
        self._acquire_timeout = _env_float(
            "BROWSER_SLOT_ACQUIRE_TIMEOUT_SECONDS",
            _DEFAULT_SLOT_ACQUIRE_TIMEOUT,
        )

        # Saturation / health counters (observability).
        self._active_slots = 0
        self._waiters = 0
        self._reinit_count = 0

    @classmethod
    async def get_instance(cls) -> 'BrowserManager':
        """Get or create the singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance.initialize()
        return cls._instance

    async def initialize(self) -> None:
        """Initialize the Crawl4AI crawler. Safe to call multiple times."""
        if self._is_initialized and not self._shutdown:
            return

        async with self._initialization_lock:
            if self._is_initialized and not self._shutdown:
                return

            try:
                browser_config = BrowserConfig(
                    browser_type="chromium",
                    headless=True,
                    extra_args=list(CHROMIUM_MEMORY_FLAGS),
                    text_mode=False,
                    verbose=False,
                )
                self._crawler = AsyncWebCrawler(config=browser_config)
                await self._crawler.start()

                # crawl4ai 0.8.x keeps the Playwright Browser on the
                # strategy's internal browser manager.
                strategy = getattr(self._crawler, "crawler_strategy", None)
                internal = getattr(strategy, "browser_manager", None)
                self._browser = getattr(internal, "browser", None)
                if self._browser is None:
                    raise RuntimeError(
                        "Crawl4AI started without exposing a Playwright "
                        "Browser (unexpected launch mode)"
                    )

                self._is_initialized = True
                self._shutdown = False
                logger.info("[BrowserManager] Crawl4AI browser initialized")

            except Exception as e:
                logger.error(
                    "[BrowserManager] Failed to initialize browser: %s", e
                )
                await self._dispose_crawler()
                self._is_initialized = False
                raise

    async def _dispose_crawler(self) -> None:
        """Best-effort close of the wrapped crawler. Never raises."""
        if self._crawler is not None:
            try:
                await self._crawler.close()
            except Exception as e:  # noqa: BLE001 — disposal is best-effort
                logger.warning("[BrowserManager] Error closing crawler: %s", e)
        self._crawler = None
        self._browser = None

    async def _reinitialize(self) -> None:
        """Tear down the crawler and start a fresh one (crash recovery)."""
        self._reinit_count += 1
        await self._dispose_crawler()
        self._is_initialized = False
        await self.initialize()

    async def force_recover(self, reason: str) -> None:
        """Tear down a wedged browser and relaunch it (render watchdog).

        ``close()`` is bounded — on a wedged Chromium the close itself can
        hang, which is exactly the failure mode being recovered from. A
        failed relaunch is non-fatal: ``ensure_browser_ready()`` retries on
        the next slot acquisition.
        """
        logger.warning("[BrowserManager] Force recovery: %s", reason)
        self._reinit_count += 1
        if self._crawler is not None:
            try:
                await asyncio.wait_for(self._crawler.close(), timeout=10.0)
            except Exception as e:  # noqa: BLE001 — disposal is best-effort
                logger.warning(
                    "[BrowserManager] Error closing wedged crawler: %s", e
                )
        self._crawler = None
        self._browser = None
        self._is_initialized = False
        try:
            await self.initialize()
        except Exception:  # noqa: BLE001 — next request retries
            logger.exception(
                "[BrowserManager] Relaunch after force recovery failed; "
                "the next request will retry"
            )

    async def ensure_browser_ready(self) -> None:
        """Ensure the browser is connected, reinitializing if it is not."""
        if self._browser is None or not self._browser.is_connected():
            logger.warning(
                "[BrowserManager] Browser not connected, reinitializing..."
            )
            await self._reinitialize()

    async def is_browser_healthy(self) -> bool:
        """Read-only liveness probe for /health. Never reinitializes."""
        if self._shutdown or not self._is_initialized or self._browser is None:
            return False
        return self._browser.is_connected()

    def stats(self) -> dict[str, Any]:
        """Saturation / recovery counters for observability."""
        return {
            "max_concurrent_contexts": self.max_concurrent_contexts,
            "active_slots": self._active_slots,
            "waiting": self._waiters,
            "reinitializations": self._reinit_count,
            "acquire_timeout_seconds": self._acquire_timeout,
        }

    @asynccontextmanager
    async def _acquire_slot(self) -> AsyncIterator[None]:
        """Acquire a conversion slot, bounded by ``_acquire_timeout``."""
        self._waiters += 1
        try:
            if self._acquire_timeout is None:
                await self._semaphore.acquire()
            else:
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(),
                        timeout=self._acquire_timeout,
                    )
                except asyncio.TimeoutError:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "The conversion service is at capacity. Please "
                            "retry shortly."
                        ),
                        headers={"Retry-After": "30"},
                    )
        finally:
            self._waiters -= 1

        self._active_slots += 1
        try:
            yield
        finally:
            self._active_slots -= 1
            self._semaphore.release()

    @asynccontextmanager
    async def get_context(
        self, **context_options: Any
    ) -> AsyncIterator[BrowserContext]:
        """Yield a fresh browser context inside a bounded conversion slot.

        The context is automatically closed on exit. Retries once through a
        reinitialize when Chromium died between the health check and
        ``new_context()``.
        """
        async with self._acquire_slot():
            await self.ensure_browser_ready()

            if 'user_agent' not in context_options:
                context_options['user_agent'] = DEFAULT_USER_AGENT
            # CSP is respected by default (security hardening).
            if 'bypass_csp' not in context_options:
                context_options['bypass_csp'] = False

            browser = self._browser
            if browser is None:
                await self._reinitialize()
                browser = self._browser

            context: Optional[BrowserContext] = None
            try:
                try:
                    context = await browser.new_context(**context_options)
                except PlaywrightError as e:
                    logger.warning(
                        "[BrowserManager] new_context() failed (%s: %s), "
                        "reinitializing once and retrying...",
                        type(e).__name__, e,
                    )
                    await self._reinitialize()
                    context = await self._browser.new_context(
                        **context_options
                    )
                await _install_ssrf_route_guard(context)
                yield context
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "[BrowserManager] Error closing context: %s", e
                        )

    @asynccontextmanager
    async def get_page(self, **context_options: Any) -> AsyncIterator[Page]:
        """Yield a new page in a new context; both closed on exit."""
        async with self.get_context(**context_options) as context:
            page: Optional[Page] = None
            try:
                page = await context.new_page()
                yield page
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "[BrowserManager] Error closing page: %s", e
                        )

    def _check_initialized(self) -> AsyncWebCrawler:
        """Return the live crawler or raise if not initialized."""
        if self._crawler is None or not self._is_initialized:
            raise RuntimeError(
                "BrowserManager is not initialized; await "
                "BrowserManager.get_instance() (or initialize()) first"
            )
        return self._crawler

    def get_crawler(self) -> AsyncWebCrawler:
        """Return the wrapped Crawl4AI crawler for ``arun()`` callers."""
        return self._check_initialized()

    @asynccontextmanager
    async def crawler_slot(self) -> AsyncIterator[AsyncWebCrawler]:
        """Acquire a conversion slot and yield the ready Crawl4AI crawler.

        Every ``arun()`` caller queues on the SAME semaphore as the
        ``get_context()`` flow so total Chromium pressure stays bounded,
        and per-request hook registration on the shared crawler strategy
        stays serialized at the default concurrency of 1.
        """
        async with self._acquire_slot():
            await self.ensure_browser_ready()
            yield self._check_initialized()

    async def shutdown(self) -> None:
        """Shutdown the crawler. Called on application shutdown."""
        async with self._initialization_lock:
            if self._crawler is not None and not self._shutdown:
                try:
                    await self._crawler.close()
                    logger.info(
                        "[BrowserManager] Crawler closed successfully"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[BrowserManager] Error closing crawler: %s", e
                    )
                finally:
                    self._crawler = None
                    self._browser = None
                    self._shutdown = True
                    self._is_initialized = False

    @classmethod
    async def reset_instance(cls) -> None:
        """Reset the singleton instance. Useful for testing."""
        if cls._instance:
            await cls._instance.shutdown()
            cls._instance = None


async def get_browser_manager() -> BrowserManager:
    """Get the singleton browser manager instance."""
    return await BrowserManager.get_instance()
