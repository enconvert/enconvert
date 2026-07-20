"""URL to PDF conversion — open-source fallback.

Plain headless-Chromium render: ``page.goto`` + ``page.pdf`` through the
shared ``BrowserManager``. The cloud build's page-quality chain, stealth
render ladder and header/footer template translation are not part of the
open build; ``handle_sticky_header`` / ``handle_cookies`` / ``block_ads``
/ ``block_media`` are accepted for signature parity and ignored. Header
and footer content from ``pdf_options`` is passed to Playwright verbatim.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Optional

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
from models import PdfOptions
from utils.pdf_postprocess import convert_to_grayscale

logger = logging.getLogger(__name__)

# CSS-pixel per millimetre at 96 DPI.
_PX_PER_MM = 3.7795

# Simple full-document height probe (light version of the cloud measurer).
_PAGE_HEIGHT_JS = (
    "() => Math.max("
    "document.documentElement.scrollHeight,"
    "document.body ? document.body.scrollHeight : 0,"
    "document.documentElement.offsetHeight,"
    "document.documentElement.clientHeight) + 8"
)


async def _generate_pdf_bytes(
    page: Page,
    *,
    single_page: bool,
    viewport_width: int,
    viewport_height: int,
    pdf_options: Optional[PdfOptions],
) -> bytes:
    """Derive ``page.pdf`` kwargs from the request and capture the PDF."""
    if single_page:
        pdf_height = await page.evaluate(_PAGE_HEIGHT_JS)
        if pdf_options:
            page_w_mm, _ = pdf_options.get_dimensions_mm()
            effective_width = int(page_w_mm * _PX_PER_MM)
            margin_kwargs: Optional[dict] = {
                "top": f"{pdf_options.margins.top}mm",
                "bottom": f"{pdf_options.margins.bottom}mm",
                "left": f"{pdf_options.margins.left}mm",
                "right": f"{pdf_options.margins.right}mm",
            }
        else:
            effective_width = viewport_width
            margin_kwargs = None

        pdf_call_kwargs: dict[str, Any] = {
            "print_background": True,
            "width": f"{effective_width}px",
            "height": f"{pdf_height}px",
            "scale": 1,
            "prefer_css_page_size": False,
            "tagged": True,
        }
        if margin_kwargs:
            pdf_call_kwargs["margin"] = margin_kwargs
        return await page.pdf(**pdf_call_kwargs)

    if pdf_options:
        has_hf = bool(pdf_options.header or pdf_options.footer)
        pdf_call_kwargs = {
            "print_background": True,
            "scale": pdf_options.scale,
            "landscape": pdf_options.orientation == "landscape",
            "margin": {
                "top": f"{pdf_options.margins.top}mm",
                "bottom": f"{pdf_options.margins.bottom}mm",
                "left": f"{pdf_options.margins.left}mm",
                "right": f"{pdf_options.margins.right}mm",
            },
            "display_header_footer": has_hf,
            "tagged": True,
            "prefer_css_page_size": False,
        }

        if (
            pdf_options.page_width is not None
            and pdf_options.page_height is not None
        ):
            w, h = pdf_options.get_dimensions_mm()
            pdf_call_kwargs["width"] = f"{w}mm"
            pdf_call_kwargs["height"] = f"{h}mm"
        else:
            pdf_call_kwargs["format"] = pdf_options.page_size

        if has_hf:
            # Open build: templates are passed to Playwright verbatim (no
            # placeholder translation — that is a cloud-engine feature).
            if pdf_options.header and pdf_options.header.content:
                pdf_call_kwargs["header_template"] = pdf_options.header.content
                header_margin = (
                    pdf_options.margins.top + pdf_options.header.height
                )
                pdf_call_kwargs["margin"]["top"] = f"{header_margin}mm"
            else:
                pdf_call_kwargs["header_template"] = "<span></span>"

            if pdf_options.footer and pdf_options.footer.content:
                pdf_call_kwargs["footer_template"] = pdf_options.footer.content
                footer_margin = (
                    pdf_options.margins.bottom + pdf_options.footer.height
                )
                pdf_call_kwargs["margin"]["bottom"] = f"{footer_margin}mm"
            else:
                pdf_call_kwargs["footer_template"] = "<span></span>"

        return await page.pdf(**pdf_call_kwargs)

    return await page.pdf(
        print_background=True,
        width=f"{viewport_width}px",
        height=f"{viewport_height}px",
        scale=1,
        prefer_css_page_size=False,
        tagged=True,
    )


async def url_to_pdf(
    url: str,
    load_media: bool = True,
    enable_scroll: bool = True,
    handle_sticky_header: bool = True,
    handle_cookies: bool = True,
    wait_for_images: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    single_page: bool = True,
    auth: dict = None,
    cookies: list = None,
    headers: dict = None,
    pdf_options: "PdfOptions | None" = None,
    instrumentation: Any = None,
    wait_for_selector: str | None = None,
    wait_for_selector_timeout: int = 10000,
    block_ads: bool = False,
    block_media: bool = False,
):
    """Convert a URL to PDF with a plain headless-browser render.

    Signature-compatible with the cloud converter. ``handle_sticky_header``
    / ``handle_cookies`` / ``block_ads`` / ``block_media`` are accepted and
    ignored in the open build.

    Returns:
        bytes: The generated PDF content.
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
            # A caller-supplied Authorization header reaches ONLY the
            # target origin (Basic auth goes through http_credentials).
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
                    artifact="PDF",
                ) from exc

            # A JSON response renders as Chromium's interactive JSON
            # viewer — a poor PDF. Steer the caller to url-to-markdown.
            if detect_content_category(response) == CONTENT_JSON:
                raise UnsupportedContentError(
                    f"{url} returned JSON, which cannot be rendered as a "
                    f"PDF. Use the url-to-markdown endpoint to capture "
                    f"JSON content."
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

            return await _generate_pdf_bytes(
                page,
                single_page=single_page,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                pdf_options=pdf_options,
            )

    try:
        pdf_content = await asyncio.wait_for(
            _render(), timeout=RENDER_WATCHDOG_SECONDS
        )
    except asyncio.TimeoutError:
        raise RenderWatchdogTimeout(
            f"Render exceeded the {RENDER_WATCHDOG_SECONDS:.0f}s watchdog "
            f"for {url}."
        ) from None

    if pdf_options and pdf_options.grayscale:
        pdf_content = await convert_to_grayscale(pdf_content)

    return pdf_content
