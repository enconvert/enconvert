"""Shared per-request render plumbing — open-source fallback.

The cloud build layers a page-quality chain, CSP stripping, resource
blocking and stealth hooks onto Crawl4AI's ``arun()`` pipeline here. The
open fallback keeps the same public surface (``build_run_config``,
``arun_with_watchdog``, ``RequestPageState``, ``hook_session``, the hook
factories and the pure request helpers) with plain, honest behaviour:
no stealth, no quality chain, no CSP manipulation. ``block_ads`` /
``block_media`` are accepted for signature parity and ignored.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Optional
from urllib.parse import urlsplit

from crawl4ai import CacheMode, CrawlerRunConfig
from playwright.async_api import BrowserContext, Page

from .user_agent import DEFAULT_USER_AGENT, pick_user_agent

logger = logging.getLogger(__name__)

# Per-request SSRF re-validation on the render path. ON by default;
# BROWSER_SSRF_ROUTE_GUARD=0 off.
_SSRF_ROUTE_GUARD = os.getenv(
    "BROWSER_SSRF_ROUTE_GUARD", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Backwards-compatible alias kept for existing importers.
V1_USER_AGENT = DEFAULT_USER_AGENT

# Request headers that carry credentials and must NEVER be broadcast
# page-wide via set_extra_http_headers (they would leak to third-party
# subresources). A caller-supplied Authorization is applied origin-scoped.
_NON_BROADCAST_HEADERS = frozenset({"authorization", "proxy-authorization"})

# Non-HTML content categories detected from the navigation response.
CONTENT_JSON = "json"
CONTENT_TEXT = "text"


@dataclass
class RequestPageState:
    """Per-request mutable state shared between hooks and the caller.

    Field names match the cloud build so any consumer reading them keeps
    working unchanged.
    """

    context: Optional[BrowserContext] = None
    popup_pages: list[Page] = field(default_factory=list)
    popup_listener: Optional[Callable[[Page], None]] = None
    cookies_injected: bool = False
    # Set when the navigation response is a non-HTML content type
    # ("json"/"text"). The converter decides what to do with it.
    content_category: Optional[str] = None
    raw_body: Optional[bytes] = None
    raw_content_type: Optional[str] = None
    raw_final_url: Optional[str] = None
    # Set True when a caller-supplied wait_for_selector never appeared.
    selector_timed_out: bool = False


# Hard ceiling on a single render: a hook stall or a wedged CDP connection
# is otherwise unbounded.
RENDER_WATCHDOG_SECONDS = float(os.getenv("RENDER_WATCHDOG_SECONDS", "300"))


class RenderWatchdogTimeout(RuntimeError):
    """A render exceeded the hard watchdog; the browser was recycled.

    Subclasses RuntimeError so every existing render-failure handler
    treats it like any other failed render.
    """


async def arun_with_watchdog(
    crawler: Any,
    browser_manager: Any,
    *,
    url: str,
    config: CrawlerRunConfig,
    timeout: Optional[float] = None,
) -> Any:
    """Run ``crawler.arun`` bounded by the render watchdog.

    On expiry the arun task is cancelled and the browser is
    force-recovered BEFORE the exception propagates, so cleanup on the
    way out talks to a freshly closed browser instead of hanging on the
    wedged one.
    """
    limit = RENDER_WATCHDOG_SECONDS if timeout is None else timeout
    try:
        return await asyncio.wait_for(
            crawler.arun(url=url, config=config), timeout=limit
        )
    except asyncio.TimeoutError:
        await browser_manager.force_recover(
            f"render watchdog fired after {limit:.0f}s for {url}"
        )
        raise RenderWatchdogTimeout(
            f"Render exceeded the {limit:.0f}s watchdog for {url}; "
            "the browser was recycled."
        ) from None


def build_run_config(user_agent: Optional[str] = None) -> CrawlerRunConfig:
    """A plain CrawlerRunConfig for basic hook-based renders.

    No stealth/anti-bot flags in the open build: just a normal load with
    a rotated, complete desktop UA, cache bypassed, and retries pinned to
    0 so a converter's in-hook work can never run twice.
    """
    return CrawlerRunConfig(
        pdf=False,
        screenshot=False,
        cache_mode=CacheMode.BYPASS,
        wait_until="load",
        page_timeout=60000,
        max_retries=0,
        verbose=False,
        user_agent=user_agent or pick_user_agent(),
    )


def compose_extra_headers(
    browser_config: Any,
    headers: Optional[dict],
) -> Dict[str, str]:
    """Merge the caller's custom headers with the browser's own header set.

    Playwright's ``set_extra_http_headers`` REPLACES previous extra
    headers, so the browser-level UA / client hints are re-included when
    anything custom is sent. Returns an empty dict when there is nothing
    custom, in which case the caller must NOT call
    ``set_extra_http_headers``. Credential headers are never broadcast.
    """
    if not headers:
        return {}

    merged: Dict[str, str] = {}
    user_agent = getattr(browser_config, "user_agent", None)
    if user_agent:
        merged["User-Agent"] = user_agent
    browser_hint = getattr(browser_config, "browser_hint", None)
    if browser_hint:
        merged["sec-ch-ua"] = browser_hint
    base_headers = getattr(browser_config, "headers", None)
    if isinstance(base_headers, dict):
        merged.update({str(k): str(v) for k, v in base_headers.items()})

    merged.update({
        str(k): str(v)
        for k, v in headers.items()
        if k.lower() not in _NON_BROADCAST_HEADERS
    })
    return merged


def extract_authorization(headers: Optional[dict]) -> Optional[str]:
    """Return the caller's ``Authorization`` header value, or None.

    Case-insensitive. The value is applied origin-scoped rather than
    broadcast (see ``make_before_goto`` / the converter setup helpers).
    """
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "authorization":
            return str(value)
    return None


def detect_content_category(response: Any) -> Optional[str]:
    """Classify a navigation response as non-HTML, or None for HTML/other.

    Returns ``CONTENT_JSON`` for ``application/json`` (and ``*+json``) or
    ``CONTENT_TEXT`` for ``text/plain``. Everything else returns None so
    the normal render path runs. Never raises.
    """
    if response is None:
        return None
    try:
        content_type = response.headers.get("content-type", "")
    except Exception:  # noqa: BLE001
        return None
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        return CONTENT_JSON
    if ct == "text/plain":
        return CONTENT_TEXT
    return None


def decode_response_body(
    body: Optional[bytes], content_type: Optional[str]
) -> str:
    """Decode raw response bytes using the charset in ``content_type``.

    Falls back to UTF-8 (with replacement) when no charset is declared or
    the declared one is unknown.
    """
    if not body:
        return ""
    charset = "utf-8"
    for part in (content_type or "").split(";")[1:]:
        piece = part.strip()
        if piece.lower().startswith("charset="):
            charset = piece.split("=", 1)[1].strip().strip("\"'") or "utf-8"
            break
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", errors="replace")


async def enforce_readiness_selector(
    page: Page, selector: Optional[str], timeout_ms: int
) -> bool:
    """Wait for a caller-supplied ``selector`` before capture.

    Returns True when the selector appears (or none was requested), and
    False when it never appears within ``timeout_ms``. Never raises.
    """
    if not selector:
        return True
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001 — timeout or otherwise: not ready
        return False


def basic_auth_header(auth: Optional[dict]) -> Optional[str]:
    """Return the ``Authorization: Basic`` value for ``auth``, or None."""
    if not (auth and auth.get("username") and auth.get("password")):
        return None
    credentials = f"{auth['username']}:{auth['password']}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


async def apply_origin_scoped_auth(
    page: Page,
    request_url: str,
    auth: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> None:
    """Attach an Authorization header to same-origin requests only.

    Value is either structured Basic Auth (``auth``) or a caller-supplied
    ``Authorization`` header pulled off ``headers`` — never broadcast to
    third-party subresources.
    """
    auth_value = basic_auth_header(auth) or extract_authorization(headers)
    origin_parts = urlsplit(request_url)
    if not (auth_value and origin_parts.scheme and origin_parts.netloc):
        return
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}"

    async def _add_auth_header(route: Any) -> None:
        merged = {
            **route.request.headers,
            "Authorization": auth_value,
        }
        await route.fallback(headers=merged)

    await page.route(f"{origin}/**", _add_auth_header)


def broadcast_headers(headers: Optional[dict]) -> Dict[str, str]:
    """The caller's custom headers minus credential headers.

    Safe to pass to ``page.set_extra_http_headers`` — Authorization /
    Proxy-Authorization never reach third-party subresources this way
    (they are applied origin-scoped via ``apply_origin_scoped_auth``).
    """
    if not headers:
        return {}
    return {
        str(k): str(v)
        for k, v in headers.items()
        if str(k).lower() not in _NON_BROADCAST_HEADERS
    }


async def settle_page(
    page: Page,
    *,
    enable_scroll: bool = True,
    load_media: bool = True,
    wait_for_images: bool = True,
) -> None:
    """Basic open-build settle pass before capture.

    Scrolls through the page to trigger lazy-loading, then gives the
    network a moment to go idle. The cloud build's cookie-banner, modal,
    sticky-header and animation heuristics are not part of the open
    fallback. Best-effort and never raises.
    """
    if enable_scroll:
        try:
            await page.evaluate(
                """
                async () => {
                    const step = window.innerHeight || 800;
                    const total = Math.min(
                        document.body ? document.body.scrollHeight : 0,
                        40000
                    );
                    for (let y = 0; y <= total; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 60));
                    }
                    window.scrollTo(0, 0);
                }
                """
            )
        except Exception:  # noqa: BLE001 — settling is best-effort
            pass
    if load_media and wait_for_images:
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001 — networkidle may never arrive
            pass
    try:
        await page.wait_for_timeout(250)
    except Exception:  # noqa: BLE001
        pass


async def cleanup_context_state(state: RequestPageState) -> None:
    """Undo per-request mutations on a cached/shared browser context.

    Removes the popup listener, closes leftover popups and clears ALL
    cookies so nothing leaks into the next request. Best-effort and never
    raises.
    """
    context = state.context
    if context is None:
        return

    if state.popup_listener is not None:
        try:
            context.remove_listener("page", state.popup_listener)
        except Exception:  # noqa: BLE001
            pass
        state.popup_listener = None

    for popup in state.popup_pages:
        try:
            if not popup.is_closed():
                await popup.close()
        except Exception:  # noqa: BLE001
            pass
    state.popup_pages.clear()

    try:
        await context.clear_cookies()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to clear per-request cookies: %s", exc)


def make_on_page_context_created(
    state: RequestPageState,
    *,
    viewport_width: int,
    viewport_height: int,
    cookies: Optional[list],
) -> Callable[..., Any]:
    """Hook factory: viewport resize + cookie injection + popup listener."""

    async def _on_page_context_created(
        page: Page,
        context: BrowserContext = None,
        config: CrawlerRunConfig = None,
        **kwargs: Any,
    ) -> Page:
        state.context = context
        await page.set_viewport_size(
            {"width": viewport_width, "height": viewport_height}
        )
        if cookies:
            await context.add_cookies(cookies)
            state.cookies_injected = True

        def _handle_popup(popup: Page) -> None:
            state.popup_pages.append(popup)

        context.on("page", _handle_popup)
        state.popup_listener = _handle_popup
        return page

    return _on_page_context_created


def make_before_goto(
    *,
    browser_config: Any,
    request_url: str,
    headers: Optional[dict],
    auth: Optional[dict],
    instrumentation: Optional[Any],
    block_ads: bool = False,
    block_media: bool = False,
) -> Callable[..., Any]:
    """Hook factory: instrumentation attach + headers + origin-scoped auth.

    Open-build simplifications: no CSP stripping and no ad/media resource
    blocking (``block_ads`` / ``block_media`` are accepted and ignored).
    The SSRF route guard is installed when the open ``url_safety`` module
    is available.
    """
    if block_ads or block_media:
        logger.debug(
            "block_ads/block_media are ignored in the open build"
        )

    async def _before_goto(
        page: Page,
        context: BrowserContext = None,
        url: str = None,
        config: CrawlerRunConfig = None,
        **kwargs: Any,
    ) -> Page:
        # Instrumentation listeners must attach BEFORE navigation.
        if instrumentation is not None:
            try:
                instrumentation.attach(page)
            except Exception:  # noqa: BLE001 — observability, never fatal
                logger.warning("instrumentation attach failed", exc_info=True)

        # SSRF re-validation guard (best-effort, open url_safety module).
        if _SSRF_ROUTE_GUARD:
            try:
                from services.v2_engine.url_safety import (
                    make_ssrf_route_handler,
                )

                await page.route(
                    "**/*", make_ssrf_route_handler(allow_action="fallback")
                )
            except Exception:  # noqa: BLE001 — guard is best-effort
                logger.warning(
                    "could not install SSRF route guard", exc_info=True
                )

        extra_headers = compose_extra_headers(browser_config, headers)
        if extra_headers:
            await page.set_extra_http_headers(extra_headers)

        await apply_origin_scoped_auth(
            page, request_url, auth=auth, headers=headers
        )
        return page

    return _before_goto


@asynccontextmanager
async def hook_session(
    strategy: Any,
    hooks: Dict[str, Callable[..., Any]],
    state: RequestPageState,
) -> AsyncIterator[None]:
    """Register per-request hooks; restore them and clean the context on exit.

    Restores pre-existing hooks even when ``arun()`` raises, so a failed
    request can never leak its closures into the next one.
    """
    previous_hooks = {name: strategy.hooks.get(name) for name in hooks}
    for name, hook in hooks.items():
        strategy.set_hook(name, hook)
    try:
        yield
    finally:
        for name, hook in previous_hooks.items():
            strategy.hooks[name] = hook
        await cleanup_context_state(state)
