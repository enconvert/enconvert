"""Local (non-server) crawl entry point: site -> pages -> heading-aware chunks.

A thin, stable wrapper over ``ingest_flow``'s discovery and per-page render
helpers for tools that run OUTSIDE the gateway process (the demo-bot
generator in the separate ``enconvert-demo-bots`` repo, run on a laptop).
It touches no database, billing, storage or PostHog: callers import this
module instead of reaching into ``ingest_flow`` privates, so the ingest
internals can move without breaking them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import AsyncIterator, Iterable, Optional

from services.browser.converters.browser_manager import BrowserManager
from services.v2_engine import ingest_flow, perceive_flow
from services.v2_engine.chunking.semantic import chunk_markdown
from services.v2_engine.quality import QUALITY_FLOOR

# ingest_flow's discovery ceiling (its sitemap probe max and the schema cap).
MAX_PAGES = 1000


# Before/after samples on the demo page show at most this much of a page.
SAMPLE_CHARS = 12_000


@dataclass
class CrawledPage:
    url: str
    status: str  # "ok" | "skipped" | "duplicate" | "error"
    final_url: str = ""
    title: str = ""
    content_hash: str = ""
    render_quality: Optional[float] = None
    reason: str = ""
    chunks: list[dict] = field(default_factory=list)
    # Raw HTML as fetched vs the clean Markdown ingest keeps: the demo's
    # "what went in / what came out" panels and its noise-removed figure.
    raw_chars: int = 0
    clean_chars: int = 0
    raw_sample: str = ""
    clean_sample: str = ""


async def discover_site(
    seed_url: str, *, max_pages: int = MAX_PAGES, max_depth: int = 3
) -> list[str]:
    """Discover up to ``max_pages`` URLs (sitemap + crawl) from ``seed_url``."""
    job = SimpleNamespace(mode="hybrid", source_url=seed_url, source_urls=None)
    discovery = {"max_pages": min(max_pages, MAX_PAGES), "max_depth": max_depth}
    urls, _found, _truncated = await ingest_flow._discover_urls(job, discovery, {})
    return urls


async def _render_page(url: str) -> tuple[str, str, str, str, float, str]:
    """(html, final_url, markdown, title, render_quality, blocked_reason).

    Mirrors ingest_flow._url_to_markdown step for step (browser render, the
    QUALITY_FLOOR gate, the same Markdown pass) but also hands back the raw
    HTML, which ingest discards. A blocked page returns its HTML with an
    empty Markdown and the reason, instead of raising, so the demo can show
    what the crawler was served.
    """
    rendered = await perceive_flow.render_html(url, allow_tls=False)
    html = rendered.html or ""
    final_url = rendered.final_url or url
    quality = rendered.render_quality
    if rendered.is_blocked or quality < QUALITY_FLOOR:
        names = ", ".join(sorted(getattr(rendered, "deductions", None) or {}))
        reason = (
            f"the page could not be read (render_quality {quality:.2f}; "
            f"{names or 'below quality floor'})"
        )
        return html, final_url, "", "", quality, reason
    markdown = await asyncio.to_thread(
        ingest_flow._markdown_for,
        html,
        final_url,
        content_category=rendered.content_category,
        content_type=rendered.content_type,
    )
    title = await asyncio.to_thread(ingest_flow._extract_title, html)
    return html, final_url, markdown, title, quality, ""


async def crawl_pages(
    urls: Iterable[str],
    *,
    max_words: int = 300,
    known_hashes: Iterable[str] = (),
) -> AsyncIterator[CrawledPage]:
    """Render and chunk each URL in order, yielding one CrawledPage per URL.

    ``known_hashes`` are content hashes a previous (resumed) run already
    produced, so a page identical to one of them is reported ``duplicate``.
    One bad page never stops the crawl. The shared Chromium is shut down at
    the end because a local process owns it outright.
    """
    # PageIdentitySet seeds from completed page rows; duck-type them.
    seen = ingest_flow.PageIdentitySet(
        [SimpleNamespace(status="completed", content_hash=h) for h in known_hashes]
    )
    try:
        for url in urls:
            try:
                html, final_url, markdown, title, quality, blocked = await _render_page(url)
            except Exception as exc:  # one bad page never sinks the crawl
                yield CrawledPage(
                    url=url, status="error", reason=f"{type(exc).__name__}: {exc}"
                )
                continue

            page = CrawledPage(
                url=url,
                status="ok",
                final_url=final_url,
                title=title,
                render_quality=quality,
                raw_chars=len(html),
                raw_sample=html[:SAMPLE_CHARS],
            )
            if blocked or not markdown.strip():
                page.status = "skipped"
                page.reason = blocked or "the page returned no extractable content"
                yield page
                continue

            page.clean_chars = len(markdown)
            page.clean_sample = markdown[:SAMPLE_CHARS]
            page.content_hash = ingest_flow.content_fingerprint(markdown, final_url, url)
            if seen.contains(final_url, page.content_hash):
                page.status = "duplicate"
                yield page
                continue
            seen.add(final_url, page.content_hash)
            chunks = await asyncio.to_thread(
                chunk_markdown, markdown, max_words=max_words
            )
            page.chunks = [
                {"text": c.text, "headings": list(c.headings_path)} for c in chunks
            ]
            yield page
    finally:
        await BrowserManager.reset_instance()
