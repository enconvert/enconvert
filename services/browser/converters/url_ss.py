"""URL to PNG screenshot conversion — open-source fallback.

Plain headless-Chromium render: ``page.goto`` + full-page
``page.screenshot`` through the shared ``BrowserManager``. The cloud
build's page-quality chain and stealth ladder are not part of the open
build; ``handle_sticky_header`` / ``handle_cookies`` / ``block_ads`` /
``block_media`` are accepted for signature parity and ignored.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

from .arun_flow import (
    CONTENT_JSON,
    RENDER_WATCHDOG_SECONDS,
    RenderWatchdogTimeout,
    apply_origin_scoped_auth,
    broadcast_headers,
    detect_content_category,
    enforce_readiness_selector,
    settle_page,
)
from .browser_manager import get_browser_manager
from .errors import (
    SelectorNotFoundError,
    UnsupportedContentError,
    classify_render_failure,
)
from .user_agent import pick_user_agent

logger = logging.getLogger(__name__)

# Simple full-document height probe (light version of the cloud measurer).
_PAGE_HEIGHT_JS = (
    "() => Math.max("
    "document.documentElement.scrollHeight,"
    "document.body ? document.body.scrollHeight : 0,"
    "document.documentElement.offsetHeight,"
    "document.documentElement.clientHeight) + 8"
)


async def _capture_full_page_screenshot(
    page: Page,
    *,
    viewport_width: int,
) -> bytes:
    """Measure the document, resize the viewport, capture a full-page PNG."""
    page_height = await page.evaluate(_PAGE_HEIGHT_JS)
    try:
        await page.set_viewport_size(
            {"width": viewport_width, "height": int(page_height)}
        )
    except Exception:  # noqa: BLE001 — extreme heights: full_page still works
        logger.warning(
            "viewport resize to %spx failed; capturing with full_page only",
            page_height,
        )
    return await page.screenshot(full_page=True, type="png")


async def url_to_png(
    url: str,
    load_media: bool = True,
    enable_scroll: bool = True,
    handle_sticky_header: bool = True,
    handle_cookies: bool = True,
    wait_for_images: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    auth: dict = None,
    cookies: list = None,
    headers: dict = None,
    instrumentation: Any = None,
    wait_for_selector: str | None = None,
    wait_for_selector_timeout: int = 10000,
    block_ads: bool = False,
    block_media: bool = False,
):
    """Convert a URL to a full-page PNG with a plain headless render.

    Signature-compatible with the cloud converter. ``handle_sticky_header``
    / ``handle_cookies`` / ``block_ads`` / ``block_media`` are accepted and
    ignored in the open build.

    Returns:
        bytes: The generated PNG screenshot content.
    """
    del handle_sticky_header, handle_cookies, block_ads, block_media

    browser_manager = await get_browser_manager()

    async def _render() -> bytes:
        context_options: dict[str, Any] = {
            "viewport": {"width": viewport_width, "height": viewport_height},
            "user_agent": pick_user_agent(),
        }
        if auth and auth.get("username") and auth.get("password"):
            context_options["http_credentials"] = {
                "username": str(auth["username"]),
                "password": str(auth["password"]),
            }

        async with browser_manager.get_context(**context_options) as context:
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()

            extra = broadcast_headers(headers)
            if extra:
                await page.set_extra_http_headers(extra)
            await apply_origin_scoped_auth(page, url, headers=headers)

            if instrumentation is not None:
                try:
                    instrumentation.attach(page)
                except Exception:  # noqa: BLE001 — observability only
                    logger.warning(
                        "instrumentation attach failed", exc_info=True
                    )

            try:
                response = await page.goto(
                    url, wait_until="load", timeout=60000
                )
            except PlaywrightError as exc:
                raise classify_render_failure(
                    SimpleNamespace(error_message=str(exc)),
                    url,
                    artifact="screenshot",
                ) from exc

            # A JSON response renders as Chromium's JSON viewer — not a
            # useful screenshot. Steer the caller to url-to-markdown.
            if detect_content_category(response) == CONTENT_JSON:
                raise UnsupportedContentError(
                    f"{url} returned JSON, which cannot be rendered as a "
                    f"screenshot. Use the url-to-markdown endpoint to "
                    f"capture JSON content."
                )

            if not await enforce_readiness_selector(
                page, wait_for_selector, wait_for_selector_timeout
            ):
                raise SelectorNotFoundError(
                    f"The wait_for_selector {wait_for_selector!r} never "
                    f"appeared on {url}."
                )

            if instrumentation is not None:
                try:
                    await instrumentation.capture(page)
                except Exception:  # noqa: BLE001 — observability only
                    logger.warning(
                        "instrumentation capture failed", exc_info=True
                    )

            await settle_page(
                page,
                enable_scroll=enable_scroll,
                load_media=load_media,
                wait_for_images=wait_for_images,
            )
            await page.emulate_media(media="screen")

            return await _capture_full_page_screenshot(
                page, viewport_width=viewport_width
            )

    try:
        return await asyncio.wait_for(
            _render(), timeout=RENDER_WATCHDOG_SECONDS
        )
    except asyncio.TimeoutError:
        raise RenderWatchdogTimeout(
            f"Render exceeded the {RENDER_WATCHDOG_SECONDS:.0f}s watchdog "
            f"for {url}."
        ) from None
