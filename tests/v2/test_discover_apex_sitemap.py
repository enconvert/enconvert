"""Apex/registrable-domain sitemap probing + robots-directive parsing (#8b).

Hermetic pytest — discover_seed_urls / parse_sitemap are stubbed, no network.

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_discover_apex_sitemap.py -v
"""

import asyncio
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

from services.v2_engine import discover_flow as df
from utils.robots_parser import RobotsInfo
from utils.url_registrable import registered_domain


def _run(coro):
    return asyncio.run(coro)


def test_registered_domain_cases():
    assert registered_domain("blog.example.com") == "example.com"
    assert registered_domain("www.example.co.uk") == "example.co.uk"
    assert registered_domain("a.b.c.example.org") == "example.org"
    assert registered_domain("example.com") == "example.com"
    assert registered_domain("localhost") == "localhost"


def test_gather_sitemap_probes_origin_and_apex(monkeypatch):
    seen_origins: list[str] = []

    async def fake_seed(origin, robots_info):
        seen_origins.append(origin)
        if origin == "https://blog.example.com":
            return (
                [
                    "https://blog.example.com/sitemap.xml",
                    "https://cdn.example.com/robots-sitemap.xml",  # robots Sitemap:
                ],
                ["https://blog.example.com/feed-post"],
                RobotsInfo(),
            )
        if origin == "https://example.com":
            return (["https://example.com/sitemap.xml"], [], RobotsInfo())
        return ([], [], RobotsInfo())

    async def fake_parse(file_url):
        stem = file_url.replace(".xml", "")
        return [f"{stem}-p1", f"{stem}-p2"]

    monkeypatch.setattr(df, "discover_seed_urls", fake_seed)
    monkeypatch.setattr(df, "parse_sitemap", fake_parse)

    urls, warnings, sources = _run(
        df._gather_sitemap_urls(
            "https://blog.example.com/section", "https://blog.example.com", None
        )
    )

    # (1) Probes the ORIGIN, never the seed PATH (the old bug).
    assert "https://blog.example.com" in seen_origins
    assert all("/section" not in origin for origin in seen_origins)
    # (2) Also probes the registrable/apex domain.
    assert "https://example.com" in seen_origins
    # (3) The robots.txt Sitemap: directive file was actually parsed.
    assert any("robots-sitemap" in u for u in urls)
    # (4) RSS/Atom feed pages are included.
    assert any("feed-post" in u for u in urls)
    # (5) Provenance is split host vs apex.
    assert "sitemap" in sources and "sitemap_apex" in sources
    assert sources["sitemap_apex"] >= 2


def test_apex_not_probed_when_host_is_apex(monkeypatch):
    seen_origins: list[str] = []

    async def fake_seed(origin, robots_info):
        seen_origins.append(origin)
        return (["https://example.com/sitemap.xml"], [], RobotsInfo())

    async def fake_parse(file_url):
        return ["https://example.com/a"]

    monkeypatch.setattr(df, "discover_seed_urls", fake_seed)
    monkeypatch.setattr(df, "parse_sitemap", fake_parse)

    _run(df._gather_sitemap_urls("https://example.com/", "https://example.com", None))
    # host == apex -> only one origin probed, no sitemap_apex duplicate.
    assert seen_origins == ["https://example.com"]
