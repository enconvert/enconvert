"""Thin wrappers around Crawl4AI's CPU-bound processing layer (Task F.5).

These run standalone over already-rendered HTML — no second crawl, no
browser slot held. Plan section A5 classifies them as unbounded pure
functions; perceive_flow calls them AFTER ``arun()`` returns, exactly
like the F.2 markdown converter did.

The fit-markdown generator is NOT duplicated here: F.2 shipped
``generate_fit_markdown`` in url_markdown.py and this module re-exports
it so V1 and V2 can never drift apart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bs4 import BeautifulSoup
from crawl4ai.content_scraping_strategy import WebScrapingStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.models import ScrapingResult
from crawl4ai.table_extraction import DefaultTableExtraction

from services.browser.converters.url_markdown import generate_fit_markdown

__all__ = [
    "extract_headings",
    "extract_json_ld",
    "generate_fit_markdown",
    "generate_markdown_bytes",
    "scrap_html",
    "serialize_images",
    "serialize_links",
    "serialize_tables",
]

logger = logging.getLogger(__name__)

# Link/media fields exposed to API consumers. Crawl4AI's Link model
# carries internal scoring fields that are not part of our contract.
_LINK_FIELDS = ("href", "text", "title", "base_domain")
_IMAGE_FIELDS = ("src", "alt", "desc", "width", "format")


def scrap_html(url: str, html: str) -> ScrapingResult:
    """Run Crawl4AI's content scraping over rendered HTML.

    ``WebScrapingStrategy`` is crawl4ai 0.8.9's default scraping
    strategy inside ``arun()``; using the same class standalone keeps
    /v2/perceive output consistent with the library's own pipeline.

    Table extraction is kwarg-gated inside ``scrap()``: ``arun()``
    injects ``CrawlerRunConfig.table_extraction`` (which defaults to
    ``DefaultTableExtraction(table_score_threshold=7)``), but a bare
    ``scrap()`` call runs no table strategy at all and ``media.tables``
    is always empty. Pass the same default explicitly so
    ``structured.tables`` (perceive) and table diffs (watch) match
    what ``arun()`` would produce. Instantiated per call because the
    strategy is mutated (logger) and this runs in worker threads.
    """
    return WebScrapingStrategy().scrap(
        url,
        html,
        table_extraction=DefaultTableExtraction(table_score_threshold=7),
    )


def generate_markdown_bytes(html: str, base_url: str) -> bytes:
    """Standard Markdown for the ``markdown`` output.

    Uses ``DefaultMarkdownGenerator`` over the (cleaned) HTML the caller
    provides. ``content_source`` is irrelevant in standalone mode — the
    ``input_html`` argument IS the source.
    """
    if not html or not isinstance(html, str):
        return b""
    try:
        generated = DefaultMarkdownGenerator().generate_markdown(
            input_html=html, base_url=base_url
        )
        return (generated.raw_markdown or "").encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — output must degrade, not 500
        logger.warning("markdown generation failed for %s: %s", base_url, exc)
        return b""


def extract_headings(html: str) -> list[dict[str, Any]]:
    """Document outline: ``[{"level": 1, "text": "..."}]`` in DOM order."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    headings: list[dict[str, Any]] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = el.get_text(" ", strip=True)
        if text:
            headings.append({"level": int(el.name[1]), "text": text})
    return headings


def extract_json_ld(html: str) -> list[dict[str, Any]]:
    """Parse every ``<script type="application/ld+json">`` block.

    Malformed blocks are skipped (real-world JSON-LD is frequently
    broken); top-level arrays are flattened so the result is always a
    list of objects.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            blocks.append(data)
        elif isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
    return blocks


def _slim(model: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: getattr(model, name, None)
        for name in fields
        if getattr(model, name, None) is not None
    }


def serialize_links(result: ScrapingResult) -> dict[str, list[dict[str, Any]]]:
    """JSON-safe ``{"internal": [...], "external": [...]}`` link map."""
    links = result.links
    return {
        "internal": [_slim(link, _LINK_FIELDS) for link in links.internal],
        "external": [_slim(link, _LINK_FIELDS) for link in links.external],
    }


def serialize_images(result: ScrapingResult) -> list[dict[str, Any]]:
    """JSON-safe image list from the scraping result's media block."""
    return [_slim(image, _IMAGE_FIELDS) for image in result.media.images]


def serialize_tables(result: ScrapingResult) -> list[dict[str, Any]]:
    """Tables detected by the scraping strategy (already plain dicts)."""
    tables = result.media.tables or []
    return [table for table in tables if isinstance(table, dict)]
