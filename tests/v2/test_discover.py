"""Sprint H.1 — /v2/discover endpoint + services/v2_engine/discover_flow.

Hermetic pytest (no live browser, no live DB, no network). The live
3-site sanity (static site, JS-SPA, sitemap site) plus the no-Chromium
`ps` assertion live in tests/v2/verify_h1_discover.py — same harness
pattern as F.1/F.2/F.5.

What this file proves:
  (a) Schemas: DiscoverRequest defaults + bounds per plan section 4
      (mode enum, max_urls/max_depth bounds, http-only url, invalid
      include/exclude regex rejected at validation time -> 422).
  (b) Plan gate: deps.check_v2_feature raises 402 on a disabled
      discover_enabled flag and bypasses for admin (V2 gate convention,
      F.5 verification d/e parity).
  (c) SSRF guard: discover_flow.run rejects private/loopback URLs
      before any fetch (NEW endpoint -> ships with the guard the project
      security rules require).
  (d) URL assembly is pure + correct: normalization-dedup,
      same_domain_only filtering, include/exclude regex filtering,
      respect_robots filtering, non-page asset filtering, and the
      max_urls cap with a truncated flag.
  (e) run() composes sitemap + crawl sources without touching a browser
      (gather helpers patched; asserts dedup across sources + response
      shape).
  (f) Handler contract: 200 + section-4 response on success, 402 on the
      plan gate.

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_discover.py -v
"""
import asyncio
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


# ─── Shared test data ────────────────────────────────────────────────────────

SUB_ENABLED = {"plan_slug": "starter", "discover_enabled": True}
SUB_DISABLED = {"plan_slug": "free-v1", "discover_enabled": False}


def make_user(sub: dict, user_id: str = "42") -> dict:
    return {"id": user_id, "key_type": "private", "subscription": dict(sub)}


# ─── (a) Schemas ─────────────────────────────────────────────────────────────


class TestDiscoverSchemas:
    def test_request_defaults_match_plan_section_4(self):
        from api.v2.schemas.discover import DiscoverRequest

        req = DiscoverRequest(url="https://example.com")
        assert req.mode == "hybrid"
        assert req.max_urls == 100
        assert req.max_depth == 2
        assert req.same_domain_only is True
        assert req.respect_robots is False
        assert req.include_patterns == []
        assert req.exclude_patterns == []

    def test_mode_enum_enforced(self):
        from api.v2.schemas.discover import DiscoverRequest

        for mode in ("sitemap", "crawl", "hybrid"):
            assert DiscoverRequest(url="https://example.com", mode=mode).mode == mode
        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", mode="bogus")

    def test_max_urls_bounds(self):
        from api.v2.schemas.discover import DiscoverRequest

        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", max_urls=0)
        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", max_urls=1001)

    def test_max_depth_bounds(self):
        from api.v2.schemas.discover import DiscoverRequest

        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", max_depth=0)
        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", max_depth=6)

    def test_non_http_scheme_rejected(self):
        from api.v2.schemas.discover import DiscoverRequest

        for url in ("file:///etc/passwd", "ftp://x.com", "javascript:alert(1)"):
            with pytest.raises(ValidationError):
                DiscoverRequest(url=url)

    def test_invalid_regex_rejected(self):
        from api.v2.schemas.discover import DiscoverRequest

        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", include_patterns=["([unclosed"])
        with pytest.raises(ValidationError):
            DiscoverRequest(url="https://example.com", exclude_patterns=["*bad"])

    def test_response_shape_has_expected_fields(self):
        from api.v2.schemas.discover import DiscoverResponse

        fields = set(DiscoverResponse.model_fields)
        for required in (
            "url",
            "mode",
            "total",
            "urls",
            "pages_crawled",
            "truncated",
            "robots_respected",
            "sources",
            "warnings",
        ):
            assert required in fields, f"DiscoverResponse missing {required}"


# ─── (b) Plan gate (402, F.5 verification parity) ───────────────────────────


class TestCheckV2Feature:
    def test_gate_disabled_raises_402(self):
        import api.deps as deps

        with pytest.raises(HTTPException) as exc:
            deps.check_v2_feature(make_user(SUB_DISABLED), "discover_enabled", "Discover")
        assert exc.value.status_code == 402

    def test_gate_enabled_passes(self):
        import api.deps as deps

        deps.check_v2_feature(make_user(SUB_ENABLED), "discover_enabled", "Discover")

    def test_admin_bypasses(self):
        import api.deps as deps

        deps.check_v2_feature(
            make_user({"plan_slug": "admin"}), "discover_enabled", "Discover"
        )


# ─── (c) SSRF guard ─────────────────────────────────────────────────────────


class TestDiscoverSSRF:
    def test_private_url_rejected_before_fetch(self):
        from api.v2.schemas.discover import DiscoverRequest
        from services.v2_engine import discover_flow

        req = DiscoverRequest(url="http://127.0.0.1/admin", mode="crawl")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(discover_flow.run(req, make_user(SUB_ENABLED)))
        assert exc.value.status_code == 400

    def test_followed_link_ssrf_filter_screens_private_hosts(self):
        """The BFS filter chain must reject internal hosts so a followed
        link (esp. when same_domain_only=False) cannot reach RFC1918 /
        loopback / metadata. The filter delegates to is_public_http_url."""
        from services.v2_engine.discover_flow import _ssrf_filter_chain
        from services.v2_engine.url_safety import is_public_http_url

        chain = _ssrf_filter_chain()
        # Filter is wired into the chain.
        assert any(
            type(f).__name__ == "_PublicHostFilter" for f in chain.filters
        )
        # Loopback / metadata are rejected without DNS; a public literal passes.
        assert asyncio.run(is_public_http_url("http://127.0.0.1/admin")) is False
        assert asyncio.run(is_public_http_url("http://169.254.169.254/")) is False
        assert asyncio.run(is_public_http_url("http://[::1]/")) is False
        assert asyncio.run(is_public_http_url("http://8.8.8.8/")) is True


# ─── (d) Pure URL assembly: dedup / filter / cap ────────────────────────────


def _permissive_robots():
    from utils.robots_parser import RobotsInfo

    return RobotsInfo()


class TestFinalizeUrls:
    def test_normalization_dedup(self):
        from services.v2_engine.discover_flow import _finalize_urls

        raw = [
            "https://example.com/page",
            "https://example.com/page#frag",
            "https://example.com/page?utm_source=x",
            "https://EXAMPLE.com/page/",  # host case + trailing slash differ
        ]
        urls, truncated = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[],
            exclude_res=[],
            respect_robots=False,
            robots_info=None,
            max_urls=100,
        )
        # /page and /page/ are distinct paths; fragment + utm collapse onto /page.
        assert "https://example.com/page" in urls
        assert truncated is False
        # The three /page variants (frag, utm, bare) collapse to one entry.
        assert urls.count("https://example.com/page") == 1

    def test_same_domain_filter(self):
        from services.v2_engine.discover_flow import _finalize_urls

        raw = [
            "https://example.com/a",
            "https://other.com/b",
            "https://sub.example.com/c",
        ]
        urls, _ = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[],
            exclude_res=[],
            respect_robots=False,
            robots_info=None,
            max_urls=100,
        )
        assert any("example.com/a" in u for u in urls)
        assert any("sub.example.com/c" in u for u in urls)  # subdomain == same
        assert not any("other.com" in u for u in urls)

    def test_same_domain_off_keeps_external(self):
        from services.v2_engine.discover_flow import _finalize_urls

        raw = ["https://example.com/a", "https://other.com/b"]
        urls, _ = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=False,
            include_res=[],
            exclude_res=[],
            respect_robots=False,
            robots_info=None,
            max_urls=100,
        )
        assert any("other.com/b" in u for u in urls)

    def test_include_exclude_regex(self):
        import re

        from services.v2_engine.discover_flow import _finalize_urls

        raw = [
            "https://example.com/blog/1",
            "https://example.com/blog/2",
            "https://example.com/shop/1",
            "https://example.com/blog/draft-x",
        ]
        urls, _ = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[re.compile(r"/blog/")],
            exclude_res=[re.compile(r"draft")],
            respect_robots=False,
            robots_info=None,
            max_urls=100,
        )
        assert sorted(urls) == [
            "https://example.com/blog/1",
            "https://example.com/blog/2",
        ]

    def test_respect_robots_filters_disallowed(self):
        from services.v2_engine.discover_flow import _finalize_urls
        from utils.robots_parser import RobotsInfo

        robots = RobotsInfo(can_fetch=lambda u: "/private" not in u)
        raw = ["https://example.com/ok", "https://example.com/private/secret"]
        urls, _ = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[],
            exclude_res=[],
            respect_robots=True,
            robots_info=robots,
            max_urls=100,
        )
        assert urls == ["https://example.com/ok"]

    def test_non_page_assets_filtered(self):
        from services.v2_engine.discover_flow import _finalize_urls

        raw = [
            "https://example.com/page",
            "https://example.com/logo.png",
            "https://example.com/app.js",
            "https://example.com/styles.css",
        ]
        urls, _ = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[],
            exclude_res=[],
            respect_robots=False,
            robots_info=None,
            max_urls=100,
        )
        assert urls == ["https://example.com/page"]

    def test_max_urls_cap_sets_truncated(self):
        from services.v2_engine.discover_flow import _finalize_urls

        raw = [f"https://example.com/p{i}" for i in range(20)]
        urls, truncated = _finalize_urls(
            raw,
            base_host="example.com",
            same_domain_only=True,
            include_res=[],
            exclude_res=[],
            respect_robots=False,
            robots_info=None,
            max_urls=5,
        )
        assert len(urls) == 5
        assert truncated is True


# ─── (e) run() composes sitemap + crawl without a browser ───────────────────


class TestRunComposition:
    def test_hybrid_unions_and_dedups(self, monkeypatch):
        from api.v2.schemas.discover import DiscoverRequest
        from services.v2_engine import discover_flow

        async def fake_assert(url: str) -> None:
            return None

        async def fake_sitemap(url, origin, robots_info):
            urls = ["https://example.com/from-sitemap", "https://example.com/shared"]
            return (urls, [], {"sitemap": len(urls)})

        async def fake_crawl(url, max_depth, max_urls, same_domain_only):
            return (
                ["https://example.com/from-crawl", "https://example.com/shared"],
                3,
                [],
            )

        async def fake_js(url, same_domain_only, max_pages):
            return ([], 0, [])

        monkeypatch.setattr(discover_flow, "assert_public_http_url", fake_assert)
        monkeypatch.setattr(discover_flow, "_gather_sitemap_urls", fake_sitemap)
        monkeypatch.setattr(discover_flow, "_gather_crawl_urls", fake_crawl)
        monkeypatch.setattr(discover_flow, "_gather_crawl_urls_js", fake_js)

        req = DiscoverRequest(url="https://example.com", mode="hybrid")
        resp = asyncio.run(discover_flow.run(req, make_user(SUB_ENABLED)))

        assert resp.mode == "hybrid"
        assert resp.pages_crawled == 3
        # shared URL appears once after dedup; seed + sitemap + crawl all present.
        assert resp.urls.count("https://example.com/shared") == 1
        assert "https://example.com/from-sitemap" in resp.urls
        assert "https://example.com/from-crawl" in resp.urls
        assert resp.total == len(resp.urls)
        assert resp.sources.get("sitemap") and resp.sources.get("crawl")

    def test_sitemap_mode_skips_crawl(self, monkeypatch):
        from api.v2.schemas.discover import DiscoverRequest
        from services.v2_engine import discover_flow

        async def fake_assert(url: str) -> None:
            return None

        async def fake_sitemap(url, origin, robots_info):
            return (["https://example.com/a"], [], {"sitemap": 1})

        called = {"crawl": False}

        async def fake_crawl(url, max_depth, max_urls, same_domain_only):
            called["crawl"] = True
            return ([], 0, [])

        monkeypatch.setattr(discover_flow, "assert_public_http_url", fake_assert)
        monkeypatch.setattr(discover_flow, "_gather_sitemap_urls", fake_sitemap)
        monkeypatch.setattr(discover_flow, "_gather_crawl_urls", fake_crawl)

        req = DiscoverRequest(url="https://example.com", mode="sitemap")
        resp = asyncio.run(discover_flow.run(req, make_user(SUB_ENABLED)))

        assert called["crawl"] is False
        assert resp.pages_crawled == 0
        assert "https://example.com/a" in resp.urls

    def test_same_domain_false_flag_reaches_crawl_and_keeps_external(
        self, monkeypatch
    ):
        from api.v2.schemas.discover import DiscoverRequest
        from services.v2_engine import discover_flow

        async def fake_assert(url: str) -> None:
            return None

        captured: dict = {}

        async def fake_crawl(url, max_depth, max_urls, same_domain_only):
            captured["same_domain_only"] = same_domain_only
            return (["https://other.com/external"], 1, [])

        # Keep hermetic: a 1-URL crawl yield would otherwise trip the new
        # 'auto' JS-render fallback (roadmap #8) into a real browser launch.
        async def fake_js(url, same_domain_only, max_pages):
            return ([], 0, [])

        monkeypatch.setattr(discover_flow, "assert_public_http_url", fake_assert)
        monkeypatch.setattr(discover_flow, "_gather_crawl_urls", fake_crawl)
        monkeypatch.setattr(discover_flow, "_gather_crawl_urls_js", fake_js)

        req = DiscoverRequest(
            url="https://example.com", mode="crawl", same_domain_only=False
        )
        resp = asyncio.run(discover_flow.run(req, make_user(SUB_ENABLED)))

        # run() must thread the flag down to the crawl helper unchanged...
        assert captured["same_domain_only"] is False
        # ...and _finalize_urls must keep the external URL when the flag is off.
        assert "https://other.com/external" in resp.urls


# ─── (f) Handler contract ───────────────────────────────────────────────────


def _build_app(user: dict):
    from api.deps import get_current_user
    from api.v2.handlers.discover import router as discover_router

    app = FastAPI()
    app.include_router(discover_router, prefix="/v2")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


class TestDiscoverHandler:
    def test_disabled_plan_returns_402(self):
        app = _build_app(make_user(SUB_DISABLED))
        client = TestClient(app)
        resp = client.post("/v2/discover", json={"url": "https://example.com"})
        assert resp.status_code == 402

    def test_success_returns_section_4_shape(self, monkeypatch):
        from api.v2.schemas.discover import DiscoverResponse
        import api.v2.handlers.discover as handler

        async def fake_run(request, user):
            return DiscoverResponse(
                url=str(request.url),
                mode=request.mode,
                total=2,
                urls=["https://example.com/", "https://example.com/about"],
                pages_crawled=1,
                truncated=False,
                robots_respected=False,
                sources={"sitemap": 2, "crawl": 0},
                warnings=[],
            )

        async def fake_log_start(**kwargs):
            return 777

        async def fake_update(*args, **kwargs):
            fake_update.calls.append((args, kwargs))

        fake_update.calls = []
        monkeypatch.setattr(handler.discover_flow, "run", fake_run)
        monkeypatch.setattr(handler, "log_activity_start", fake_log_start)
        monkeypatch.setattr(handler, "update_activity_status", fake_update)

        app = _build_app(make_user(SUB_ENABLED))
        client = TestClient(app)
        resp = client.post(
            "/v2/discover", json={"url": "https://example.com", "mode": "sitemap"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["urls"] == [
            "https://example.com/",
            "https://example.com/about",
        ]
        assert body["mode"] == "sitemap"


# ─── (g) crawl4ai 0.8.9 stream off-by-one (pages_crawled maxed at 49) ────────


class TestCrawlStreamOffByOne:
    """BFSDeepCrawlStrategy._arun_stream increments its page counter and
    breaks BEFORE yielding the result that hits ``max_pages``, so a
    consumer sees at most ``max_pages - 1`` results — the user-visible
    symptom was pages_crawled stuck at 49 with the 50-page cap.
    _gather_crawl_urls compensates with +1 headroom and enforces the
    real bound itself."""

    def test_upstream_stream_swallows_cap_hitting_result(self):
        """Pins the upstream bug. If this fails with 5 results yielded,
        crawl4ai fixed the off-by-one: remove the +1 headroom in
        discover_flow._gather_crawl_urls."""
        import itertools
        from types import SimpleNamespace

        from crawl4ai import CrawlerRunConfig
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

        counter = itertools.count(1)

        class FakeCrawler:
            async def arun_many(self, urls, config):
                async def gen():
                    for u in urls:
                        links = [
                            {"href": f"http://site.test/p{next(counter)}"}
                            for _ in range(10)
                        ]
                        yield SimpleNamespace(
                            url=u,
                            success=True,
                            links={"internal": links},
                            metadata={},
                        )

                return gen()

        strategy = BFSDeepCrawlStrategy(max_depth=5, max_pages=5)
        config = CrawlerRunConfig(stream=True)

        async def consume():
            results = []
            async for result in strategy._arun_stream(
                "http://site.test/", FakeCrawler(), config
            ):
                results.append(result)
            return results

        results = asyncio.run(consume())
        # The consumer sees max_pages - 1: the cap-hitting result is
        # swallowed by the pre-yield break.
        assert len(results) == 4
        # Fetch count overshoots the cap too: stream mode (unlike batch
        # mode) has no cap check at the top of its level loop, so after
        # the swallow-break it dispatches the already-built next level
        # and its first result bumps the counter past max_pages before
        # breaking again (seed + 4 at level 1 + 1 stale = 6).
        assert strategy._pages_crawled == 6

    def test_gather_crawl_urls_reaches_full_cap(self, monkeypatch):
        """With the +1 headroom, pages_crawled reaches max_pages exactly."""
        from types import SimpleNamespace

        import services.v2_engine.discover_flow as discover_flow

        captured: dict = {}
        real_strategy = discover_flow.BFSDeepCrawlStrategy

        class SpyStrategy(real_strategy):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**kwargs)

        class FakeCrawler:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def arun(self, url, config):
                async def gen():
                    n = 0
                    while True:  # endless stream — the flow must break
                        n += 1
                        yield SimpleNamespace(
                            url=f"http://site.test/p{n}",
                            links={"internal": []},
                        )

                return gen()

        monkeypatch.setattr(discover_flow, "BFSDeepCrawlStrategy", SpyStrategy)
        monkeypatch.setattr(discover_flow, "AsyncWebCrawler", FakeCrawler)

        urls, pages_crawled, warnings = asyncio.run(
            discover_flow._gather_crawl_urls(
                "http://site.test/",
                max_depth=2,
                max_urls=7,
                same_domain_only=True,
            )
        )
        assert captured["max_pages"] == 8  # 7 requested + 1 headroom
        assert pages_crawled == 7  # exact cap — not 6
        assert len(urls) == 7
        assert warnings == []
