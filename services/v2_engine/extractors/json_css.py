"""CSS-first structured extraction — open-source fallback (fully functional).

Pass 1 of the two-pass distill engine: run Crawl4AI's
``JsonCssExtractionStrategy`` over ALREADY-RENDERED HTML — no second
crawl, no browser slot held. ``extract(url, html)`` is a pure
BeautifulSoup parse (CPU-bound), so callers run it in a worker thread.

This is the FREE pass: it answers every field a caller can express as a
CSS selector at zero LLM cost. It never raises — a selector that matches
nothing yields an empty list, and any parse fault degrades to ``[]`` with
a warning.
"""

from __future__ import annotations

import logging
from typing import Any

from crawl4ai import JsonCssExtractionStrategy

logger = logging.getLogger(__name__)


def extract_records(
    html: str, url: str, css_schema: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract structured records from rendered HTML via CSS selectors.

    Returns one dict per ``baseSelector`` match (the strategy's native
    shape). ``css_schema`` is the Crawl4AI dict form
    (``{"baseSelector": ..., "fields": [...]}``). Defensive by contract:
    empty/whitespace HTML or any extraction fault returns ``[]`` so the
    caller can fall through without a try/except at every call site.
    """
    if not html or not html.strip():
        return []
    try:
        strategy = JsonCssExtractionStrategy(css_schema)
        records = strategy.extract(url, html)
    except Exception:  # noqa: BLE001 — CSS failure degrades to empty
        logger.warning(
            "json_css: extraction failed for %s", _safe(url), exc_info=True
        )
        return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _safe(url: str) -> str:
    """Truncate an attacker-chosen URL before logging."""
    return (url or "")[:256]
