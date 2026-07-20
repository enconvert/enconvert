"""URL to Markdown conversion — open-source fallback.

Plain headless-Chromium render + Crawl4AI's markdown generator. The
cloud build layers Readability article extraction, a tuned HTML->GFM
converter and rich frontmatter on top; the open fallback renders the
page, captures its HTML, and converts it with
``DefaultMarkdownGenerator`` — honest, dependency-light output with a
minimal frontmatter block (url, title, description). Non-HTML responses
(JSON / plain text) are surfaced verbatim inside a fenced code block.

``generate_fit_markdown`` (consumed by
``services.v2_engine.crawl4ai_processors``) is fully functional: it is a
thin wrapper over Crawl4AI's fit-markdown pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Optional

from bs4 import BeautifulSoup
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from playwright.async_api import Error as PlaywrightError

from .arun_flow import (
    CONTENT_JSON,
    RENDER_WATCHDOG_SECONDS,
    RenderWatchdogTimeout,
    apply_origin_scoped_auth,
    broadcast_headers,
    decode_response_body,
    detect_content_category,
    enforce_readiness_selector,
    settle_page,
)
from .browser_manager import get_browser_manager
from .content_capture import content_with_shadow_dom
from .errors import SelectorNotFoundError, classify_render_failure
from .user_agent import pick_user_agent
from models import PdfOptions

logger = logging.getLogger(__name__)


def _frontmatter(fields: dict[str, str]) -> str:
    """Minimal YAML frontmatter. JSON string scalars are valid YAML."""
    lines = [
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    ]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _code_fence(text: str) -> str:
    """Backtick fence longer than any backtick run in ``text`` (min 3).

    Guards against a body that itself contains ``` sequences breaking
    out of the fenced block.
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _non_html_markdown_bytes(
    raw_body: Optional[bytes],
    content_type: Optional[str],
    category: str,
    base_url: str,
) -> bytes:
    """Wrap a raw non-HTML response body in Markdown with frontmatter."""
    body_text = decode_response_body(raw_body, content_type)
    language = "json" if category == CONTENT_JSON else "text"
    fence = _code_fence(body_text)
    frontmatter = _frontmatter({
        "url": base_url,
        "content_type": (content_type or "").split(";")[0].strip(),
    })
    return (
        f"{frontmatter}{fence}{language}\n{body_text}\n{fence}\n"
    ).encode("utf-8")


def generate_fit_markdown(html: str, base_url: str) -> bytes:
    """Crawl4AI Fit Markdown over already-rendered HTML.

    Runs ``DefaultMarkdownGenerator(content_source="fit_html")`` with a
    ``PruningContentFilter`` standalone — no second crawl. Filter
    failures fall back to the unfiltered raw markdown; hard failures
    degrade to empty bytes and must never break a conversion.
    """
    if not html or not isinstance(html, str):
        return b""
    try:
        generator = DefaultMarkdownGenerator(
            content_source="fit_html",
            content_filter=PruningContentFilter(),
        )
        generated = generator.generate_markdown(
            input_html=html, base_url=base_url
        )
        fit_markdown = generated.fit_markdown or ""
        # crawl4ai 0.8.x swallows filter exceptions into this marker
        # string instead of raising — treat it as a failure, not content.
        if fit_markdown.startswith("Error generating fit markdown"):
            logger.warning(
                "fit markdown filter failed for %s: %s",
                base_url,
                fit_markdown,
            )
            fit_markdown = ""
        fit_markdown = fit_markdown or generated.raw_markdown or ""
        return fit_markdown.encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — enrichment, never fatal
        logger.warning(
            "fit markdown generation failed for %s: %s", base_url, exc
        )
        return b""


def _html_to_md_bytes(html: str, base_url: str) -> bytes:
    """Convert rendered HTML to a .md file with minimal frontmatter."""
    title = ""
    description = ""
    try:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        desc_tag = soup.find(
            "meta", attrs={"name": "description"}
        ) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag:
            description = (desc_tag.get("content") or "").strip()
    except Exception:  # noqa: BLE001 — metadata is best-effort
        logger.warning("metadata extraction failed for %s", base_url)

    markdown = ""
    try:
        generated = DefaultMarkdownGenerator().generate_markdown(
            input_html=html, base_url=base_url
        )
        markdown = generated.raw_markdown or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "markdown generation failed for %s: %s", base_url, exc
        )

    frontmatter = _frontmatter({
        "url": base_url,
        "title": title,
        "description": description,
    })
    return f"{frontmatter}{markdown}".encode("utf-8")


async def url_to_markdown(
    url: str,
    load_media: bool = True,
    enable_scroll: bool = True,
    handle_sticky_header: bool = True,
    handle_cookies: bool = True,
    wait_for_images: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    single_page: bool = True,
    auth: dict | None = None,
    cookies: list | None = None,
    headers: dict | None = None,
    pdf_options: "PdfOptions | None" = None,
    instrumentation: Any = None,
    wait_for_selector: str | None = None,
    wait_for_selector_timeout: int = 10000,
    block_ads: bool = False,
    block_media: bool = False,
) -> bytes:
    """Convert a URL to a Markdown file with minimal YAML frontmatter.

    Signature-compatible with the cloud converter. ``single_page`` /
    ``pdf_options`` are accepted for parity and unused;
    ``handle_sticky_header`` / ``handle_cookies`` / ``block_ads`` /
    ``block_media`` are accepted and ignored in the open build.

    Returns:
        bytes: UTF-8 encoded Markdown file.
    """
    del single_page, pdf_options
    del handle_sticky_header, handle_cookies, block_ads, block_media

    browser_manager = await get_browser_manager()

    async def _render() -> tuple[
        Optional[str], str, Optional[str], Optional[bytes], Optional[str]
    ]:
        """Return (category, final_url, html, raw_body, raw_content_type)."""
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
                    artifact="HTML",
                ) from exc

            # Non-HTML bodies (json/text) are surfaced verbatim — not run
            # through the markdown pipeline, which would mangle Chromium's
            # JSON viewer / text wrapper into garbage.
            category = detect_content_category(response)
            if category is not None:
                try:
                    raw_content_type = response.headers.get(
                        "content-type", ""
                    )
                except Exception:  # noqa: BLE001
                    raw_content_type = ""
                try:
                    raw_body = await response.body()
                except Exception:  # noqa: BLE001
                    raw_body = b""
                return category, page.url, None, raw_body, raw_content_type

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

            html = await content_with_shadow_dom(page)
            return None, page.url, html, None, None

    try:
        category, final_url, html, raw_body, raw_content_type = (
            await asyncio.wait_for(_render(), timeout=RENDER_WATCHDOG_SECONDS)
        )
    except asyncio.TimeoutError:
        raise RenderWatchdogTimeout(
            f"Render exceeded the {RENDER_WATCHDOG_SECONDS:.0f}s watchdog "
            f"for {url}."
        ) from None

    if category is not None:
        return _non_html_markdown_bytes(
            raw_body, raw_content_type, category, final_url or url
        )

    if not html:
        raise classify_render_failure(None, url, artifact="HTML")

    # CPU-bound conversion happens outside the browser slot.
    return _html_to_md_bytes(html, final_url or url)
