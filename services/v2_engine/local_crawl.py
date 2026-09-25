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
from services.v2_engine import ingest_flow
from services.v2_engine.chunking.semantic import chunk_markdown

# ingest_flow's discovery ceiling (its sitemap probe max and the schema cap).
MAX_PAGES = 1000


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


async def discover_site(
    seed_url: str, *, max_pages: int = MAX_PAGES, max_depth: int = 3
) -> list[str]:
    """Discover up to ``max_pages`` URLs (sitemap + crawl) from ``seed_url``."""
    job = SimpleNamespace(mode="hybrid", source_url=seed_url, source_urls=None)
    discovery = {"max_pages": min(max_pages, MAX_PAGES), "max_depth": max_depth}
    urls, _found, _truncated = await ingest_flow._discover_urls(job, discovery, {})
    return urls


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
                markdown, title, _src, final_url, quality = (
                    await ingest_flow._url_to_markdown(SimpleNamespace(url=url), {}, {})
                )
                if not markdown.strip():
                    raise ingest_flow.EmptyPageError(
                        "the page returned no extractable content"
                    )
            except ingest_flow.EmptyPageError as exc:
                yield CrawledPage(url=url, status="skipped", reason=str(exc))
                continue
            except Exception as exc:  # one bad page never sinks the crawl
                yield CrawledPage(
                    url=url, status="error", reason=f"{type(exc).__name__}: {exc}"
                )
                continue

            content_hash = ingest_flow.content_fingerprint(markdown, final_url, url)
            page = CrawledPage(
                url=url,
                status="ok",
                final_url=final_url,
                title=title,
                content_hash=content_hash,
                render_quality=quality.get("render_quality"),
            )
            if seen.contains(final_url, content_hash):
                page.status = "duplicate"
                yield page
                continue
            seen.add(final_url, content_hash)
            chunks = await asyncio.to_thread(
                chunk_markdown, markdown, max_words=max_words
            )
            page.chunks = [
                {"text": c.text, "headings": list(c.headings_path)} for c in chunks
            ]
            yield page
    finally:
        await BrowserManager.reset_instance()
