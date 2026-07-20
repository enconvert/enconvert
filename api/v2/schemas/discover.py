"""Pydantic models for POST /v2/discover (Task H.1).

Mirrors the section-4 request surface: ``url``, ``mode``, ``max_urls``,
``max_depth``, ``include_patterns[]``, ``exclude_patterns[]``,
``same_domain_only``, ``respect_robots``. ``/v2/discover`` enumerates a
site's URLs WITHOUT rendering each (HTTP-only Crawl4AI + sitemap), so the
response is a flat URL list plus provenance counters — no Spaces
artifacts, no operation row.

``include_patterns`` / ``exclude_patterns`` are Python regular
expressions (``re.search`` semantics, not glob): they are compiled at
validation time so a malformed pattern is a 422 at the edge instead of a
500 inside the flow.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DiscoverMode = Literal["sitemap", "crawl", "hybrid"]


class DiscoverRequest(BaseModel):
    """One /v2/discover request (plan section 4)."""

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(max_length=2048)
    mode: DiscoverMode = "hybrid"
    max_urls: int = Field(default=100, ge=1, le=1000)
    max_depth: int = Field(default=2, ge=1, le=5)
    include_patterns: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Regex (re.search) allowlist; a URL must match at "
        "least one to be returned. Empty = allow all.",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Regex (re.search) denylist; a URL matching any is "
        "dropped. Applied after include_patterns.",
    )
    same_domain_only: bool = True
    respect_robots: bool = Field(
        default=False,
        description="When true, robots.txt Disallow rules are applied to "
        "the RETURNED list (disallowed URLs are dropped). NOTE: crawl4ai "
        "0.8.9 has no native robots gate, so in crawl/hybrid mode pages "
        "may still be fetched over HTTP and then discarded — robots is "
        "enforced at output-filter time, not fetch time.",
    )
    render_js: Literal["auto", "never", "always"] = Field(
        default="auto",
        description="Browser-rendered discovery for JS/SPA sites (crawl/hybrid "
        "modes only). 'auto' renders only when the HTTP-only crawl returns a "
        "JS shell (<=1 URL); 'always' forces a bounded browser crawl; 'never' "
        "disables it. Uses the singleton Chromium, so it is capped to a few "
        "pages (DISCOVER_JS_MAX_PAGES).",
    )

    @field_validator("url")
    @classmethod
    def http_scheme_only(cls, v: str) -> str:
        v = v.strip()
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("include_patterns", "exclude_patterns")
    @classmethod
    def patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}")
        return patterns


class DiscoverResponse(BaseModel):
    """Flat URL list + provenance for one /v2/discover call."""

    url: str
    mode: DiscoverMode
    total: int
    urls: list[str] = Field(default_factory=list)
    pages_crawled: int = 0
    truncated: bool = Field(
        default=False,
        description="True when more URLs were discovered than max_urls "
        "allowed; the list was capped.",
    )
    robots_respected: bool = False
    sources: dict[str, int] = Field(
        default_factory=dict,
        description="Raw URL count contributed by each source before "
        "dedup/filter, e.g. {'sitemap': 42, 'crawl': 30}.",
    )
    warnings: list[str] = Field(default_factory=list)
