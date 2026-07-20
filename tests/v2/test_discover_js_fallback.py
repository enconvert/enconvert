"""JS-rendered discovery fallback gating (roadmap #8a).

Hermetic pytest — every gather helper + the SSRF screen are stubbed, so this
proves ONLY the render_js trigger logic in discover_flow.run (no browser, no
network, no DB).

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_discover_js_fallback.py -v
"""

import asyncio
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

from api.v2.schemas.discover import DiscoverRequest
from services.v2_engine import discover_flow as df


def _run(coro):
    return asyncio.run(coro)


def _wire(monkeypatch, crawl_urls, js_calls, js_enabled=True):
    async def fake_ssrf(url):
        return None

    async def fake_sitemap(url, origin, robots_info):
        return ([], [], {})

    async def fake_crawl(url, max_depth, max_urls, same_domain_only):
        return (list(crawl_urls), 1, [])

    async def fake_js(url, same_domain_only, max_pages):
        js_calls.append(url)
        return (["https://x.com/js-route"], 1, [])

    monkeypatch.setattr(df, "assert_public_http_url", fake_ssrf)
    monkeypatch.setattr(df, "_gather_sitemap_urls", fake_sitemap)
    monkeypatch.setattr(df, "_gather_crawl_urls", fake_crawl)
    monkeypatch.setattr(df, "_gather_crawl_urls_js", fake_js)
    monkeypatch.setattr(df, "_DISCOVER_JS_ENABLED", js_enabled)


def _req(**over):
    base = dict(url="https://x.com", mode="crawl", render_js="auto")
    base.update(over)
    return DiscoverRequest(**base)


def test_auto_fires_on_js_shell(monkeypatch):
    js_calls: list[str] = []
    _wire(monkeypatch, crawl_urls=["https://x.com"], js_calls=js_calls)
    resp = _run(df.run(_req(render_js="auto"), {}))
    assert js_calls == ["https://x.com"]
    assert "crawl_js" in resp.sources


def test_auto_skips_when_crawl_found_many(monkeypatch):
    js_calls: list[str] = []
    _wire(
        monkeypatch,
        crawl_urls=["https://x.com/a", "https://x.com/b", "https://x.com/c"],
        js_calls=js_calls,
    )
    _run(df.run(_req(render_js="auto"), {}))
    assert js_calls == []


def test_never_disables(monkeypatch):
    js_calls: list[str] = []
    _wire(monkeypatch, crawl_urls=["https://x.com"], js_calls=js_calls)
    _run(df.run(_req(render_js="never"), {}))
    assert js_calls == []


def test_always_forces_even_with_many(monkeypatch):
    js_calls: list[str] = []
    _wire(
        monkeypatch,
        crawl_urls=["https://x.com/a", "https://x.com/b"],
        js_calls=js_calls,
    )
    _run(df.run(_req(render_js="always"), {}))
    assert js_calls == ["https://x.com"]


def test_global_kill_switch(monkeypatch):
    js_calls: list[str] = []
    _wire(monkeypatch, crawl_urls=["https://x.com"], js_calls=js_calls, js_enabled=False)
    _run(df.run(_req(render_js="always"), {}))
    assert js_calls == []


def test_sitemap_mode_never_renders(monkeypatch):
    js_calls: list[str] = []
    _wire(monkeypatch, crawl_urls=["https://x.com"], js_calls=js_calls)
    _run(df.run(_req(mode="sitemap", render_js="always"), {}))
    assert js_calls == []  # crawl/hybrid only
