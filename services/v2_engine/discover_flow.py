"""/v2/discover orchestration (Task H.1, plan sections 4 + 8).

``/v2/discover`` (Firecrawl ``/map`` equivalent) enumerates a site's URLs
WITHOUT rendering each one. It NEVER touches the singleton Chromium: the
crawl path uses Crawl4AI's ``AsyncHTTPCrawlerStrategy`` (httpx + lxml link
extraction, ~30 MB transient, no browser process), and the sitemap path
reuses the existing pure-HTTP ``utils/sitemap`` + ``utils/url_sources``
helpers. Three modes:

* ``sitemap`` — robots.txt Sitemap: directives + sitemap.xml (+ index
  recursion) + RSS/Atom feeds, probed on BOTH the request host and its
  registrable/apex domain (roadmap #8), via
  ``utils.url_sources.discover_seed_urls`` + ``utils.sitemap.parse_sitemap``.
  Instant, no crawl.
* ``crawl`` — breadth-first HTTP-only crawl from the seed
  (``BFSDeepCrawlStrategy`` over the HTTP strategy). Each fetched page's
  ``<a href>`` links are harvested from the static markup.
* ``hybrid`` — union of both (default), de-duplicated.

JS-SPA behaviour (intended, not a bug — documented per the H.1 prompt)
-----------------------------------------------------------------------
HTTP-only crawling reads the RAW HTML that the server returns and parses
``<a href>`` links out of that static markup. A client-rendered SPA
(React / Vue / Angular / SvelteKit-CSR, etc.) ships a near-empty HTML
shell — typically a single ``<div id="root">`` plus a bundle ``<script>``
— and synthesises its routes in the browser at runtime. With no browser
in the loop, those JS-injected routes are invisible, so ``crawl`` mode on
a pure SPA returns 0-1 URLs (just the shell) on the HTTP-only path.

Roadmap #8 adds an OPT-IN browser fallback: ``render_js`` (``auto`` by
default) renders the seed in the singleton Chromium and harvests the
client-injected ``<a href>`` routes when the HTTP crawl comes back with only
a shell. ``auto`` fires it just for that shell case (<=1 crawl URL);
``always`` forces it; ``never`` (or ``DISCOVER_JS_ENABLED=0``) keeps discover
fully browser-free. It is bounded to ``DISCOVER_JS_MAX_PAGES`` renders under
the semaphore=1 slot. ``sitemap`` mode also remains a fast no-browser route
for SEO-friendly SPAs (Next.js, Nuxt, Astro publish a sitemap.xml).

Cost & safety
-------------
The seed is SSRF-screened (``assert_public_http_url``) before any fetch.
``crawl`` mode bounds the number of pages actually fetched to
``min(max_urls, _MAX_CRAWL_PAGES)`` so a large ``max_urls`` cannot turn a
synchronous request into a thousand-page HTTP sweep; link harvesting
still lets the RETURNED list reach ``max_urls`` because each fetched page
contributes many links. Residual SSRF gaps (DNS rebinding, redirects to
internal hosts) match the documented ``services/v2_engine/url_safety``
caveats; ``same_domain_only`` (default True) keeps the crawl on the
validated public host.
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

from crawl4ai import (
    AsyncWebCrawler,
    BFSDeepCrawlStrategy,
    CacheMode,
    CrawlerRunConfig,
    FilterChain,
    HTTPCrawlerConfig,
)
from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy
from crawl4ai.deep_crawling.filters import URLFilter

from api.v2.schemas.discover import DiscoverRequest, DiscoverResponse
from services.v2_engine.url_safety import assert_public_http_url, is_public_http_url
from utils.robots_parser import RobotsInfo, fetch_robots_info
from utils.sitemap import parse_sitemap
from utils.url_normalizer import is_page_url, is_same_domain, normalize_url
from utils.url_registrable import registered_domain
from utils.url_sources import discover_seed_urls

logger = logging.getLogger(__name__)

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default


# Ceiling on pages actually FETCHED in crawl mode. Crawl mode is HTTP-only
# (AsyncHTTPCrawlerStrategy = httpx + lxml, no browser), so it is cheap enough
# to honor the full request. By default this equals the schema's max_urls
# ceiling (1000), i.e. crawl mode is effectively uncapped — bounded only by the
# caller's max_urls. DISCOVER_CRAWL_MAX_PAGES remains as an operator safety
# valve to lower the ceiling if a large crawl ever pressures the droplet.
_MAX_CRAWL_PAGES = _env_int("DISCOVER_CRAWL_MAX_PAGES", 1000)


# JS-rendered discovery fallback (roadmap #8). This is the ONLY discover path
# that touches the singleton Chromium, so it is bounded hard: at most
# DISCOVER_JS_MAX_PAGES renders on the semaphore=1 slot, within
# DISCOVER_JS_BUDGET_SECONDS, well under the 300 s TimeoutMiddleware.
# DISCOVER_JS_ENABLED is the global kill switch (per-request via render_js).
_DISCOVER_JS_ENABLED = _env_bool("DISCOVER_JS_ENABLED", True)
_DISCOVER_JS_MAX_PAGES = _env_int("DISCOVER_JS_MAX_PAGES", 5)
_DISCOVER_JS_BUDGET_SECONDS = float(_env_int("DISCOVER_JS_BUDGET_SECONDS", 120))


class _PublicHostFilter(URLFilter):
    """Deep-crawl filter that rejects link targets resolving to a private,
    loopback, link-local or metadata host (SSRF guard for followed links).

    crawl4ai's ``FilterChain.apply`` awaits filters whose ``apply`` returns
    an awaitable, so returning the ``is_public_http_url`` coroutine here
    runs the full async screen (scheme, embedded creds, blocked
    hostnames, IP-literal + DNS resolution) on every BFS candidate before
    it is fetched. ``BFSDeepCrawlStrategy.can_process_url`` bypasses the
    chain for depth 0 — that seed is already screened in ``run`` — so no
    URL is ever fetched without a public-host check.
    """

    def apply(self, url: str):  # noqa: ANN201 — returns a coroutine by design
        return is_public_http_url(url)


def _ssrf_filter_chain() -> FilterChain:
    """FilterChain that screens every followed link for SSRF (always on,
    defence in depth even when same_domain_only confines the host)."""
    return FilterChain([_PublicHostFilter()])


def _finalize_urls(
    raw_urls: list[str],
    *,
    base_host: str,
    same_domain_only: bool,
    include_res: list[re.Pattern[str]],
    exclude_res: list[re.Pattern[str]],
    respect_robots: bool,
    robots_info: RobotsInfo | None,
    max_urls: int,
    include_assets: bool = False,
) -> tuple[list[str], bool]:
    """Normalize, filter and de-duplicate a raw URL list (pure).

    Order: scheme guard -> canonical normalization (strips fragments,
    tracking params, default ports; sorts query) -> same-domain ->
    non-page asset drop -> include allowlist -> exclude denylist ->
    robots.txt -> dedup on the canonical form -> max_urls cap.

    Returns ``(urls, truncated)`` where ``truncated`` is True when at
    least one more unique URL existed past the cap.
    """
    seen: set[str] = set()
    out: list[str] = []
    truncated = False

    for raw in raw_urls:
        if not raw or not isinstance(raw, str):
            continue
        candidate = raw.strip()
        lowered = candidate.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            continue
        try:
            norm = normalize_url(candidate, aggressive=True)
        except Exception:  # noqa: BLE001 — junk crawl link must not 500
            continue

        if same_domain_only and not is_same_domain(norm, base_host):
            continue
        if not include_assets and not is_page_url(norm):
            continue
        if include_res and not any(pattern.search(norm) for pattern in include_res):
            continue
        if exclude_res and any(pattern.search(norm) for pattern in exclude_res):
            continue
        if respect_robots and robots_info is not None and not robots_info.can_fetch(
            norm
        ):
            continue
        if norm in seen:
            continue

        seen.add(norm)
        if len(out) >= max_urls:
            truncated = True
            break
        out.append(norm)

    return out, truncated


async def _gather_sitemap_urls(
    url: str, origin: str, robots_info: RobotsInfo | None
) -> tuple[list[str], list[str], dict[str, int]]:
    """Collect page URLs from sitemaps + feeds (pure HTTP, no browser).

    Roadmap #8 hardening over the original H.1 behavior:

    * probes the request's ORIGIN (scheme://host), fixing the old
      seed-path-relative bug where ``https://x.com/blog`` looked for
      ``/blog/sitemap.xml``;
    * ALSO probes the registrable/apex domain (``https://<apex>/sitemap.xml``
      + its robots ``Sitemap:`` directives), so a sitemap published only on
      the apex is found when the caller passed a subdomain/deep URL;
    * PARSES the robots.txt ``Sitemap:`` directive URLs (previously
      discovered by ``discover_seed_urls`` and then DISCARDED) via the
      shared ``parse_sitemap`` helper.

    Every probe fails soft (a missing/broken sitemap becomes a warning, never
    an exception) so hybrid mode still returns whatever else was found.
    Returns ``(page_urls, warnings, sources)`` where ``sources`` carries
    per-provenance raw counts (``sitemap`` / ``sitemap_apex``).
    """
    urls: list[str] = []
    warnings: list[str] = []
    sources: dict[str, int] = {}

    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    host_origin = f"{scheme}://{parts.netloc}" if parts.netloc else origin
    apex = registered_domain(host) if host else None

    # (provenance_key, origin_url, robots_info_or_None). The apex origin fetches
    # its own robots.txt (robots_info None -> discover_seed_urls fetches it).
    origin_specs: list[tuple[str, str, RobotsInfo | None]] = [
        ("sitemap", host_origin, robots_info),
    ]
    if apex and apex != host:
        origin_specs.append(("sitemap_apex", f"{scheme}://{apex}", None))

    # Gather candidate sitemap-file URLs (robots directives + probed paths +
    # standard /sitemap.xml) and RSS/Atom feed page URLs across every origin.
    sitemap_files: list[tuple[str, str]] = []  # (provenance, file_url)
    seen_files: set[str] = set()
    for provenance, org, rinfo in origin_specs:
        try:
            files, feed_pages, _robots = await discover_seed_urls(org, rinfo)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"seed discovery failed for {org}: {exc}")
            continue
        if feed_pages:
            urls.extend(feed_pages)
            sources[provenance] = sources.get(provenance, 0) + len(feed_pages)
        for file_url in files:
            if file_url not in seen_files:
                seen_files.add(file_url)
                sitemap_files.append((provenance, file_url))

    # Parse each discovered sitemap file (urlset or sitemapindex). parse_sitemap
    # is fail-soft, so one dead candidate never aborts the gather.
    for provenance, file_url in sitemap_files:
        page_urls = await parse_sitemap(file_url)
        if page_urls:
            urls.extend(page_urls)
            sources[provenance] = sources.get(provenance, 0) + len(page_urls)

    return urls, warnings, sources


async def _gather_crawl_urls(
    url: str, max_depth: int, max_urls: int, same_domain_only: bool
) -> tuple[list[str], int, list[str]]:
    """BFS the site over HTTP only (no Chromium); harvest page + link URLs.

    NOTE (plan/library drift): the plan and the crawl4ai docs reference
    ``BFSDeepCrawlStrategy(url_only=True)`` and
    ``arun_many(seeds, strategy=...)``. Neither exists in the pinned
    crawl4ai 0.8.9: ``url_only`` is not a parameter anywhere in the
    package, and the deep-crawl strategy plugs into
    ``CrawlerRunConfig.deep_crawl_strategy`` (consumed by ``arun``), not
    into ``arun_many``. With the HTTP strategy there is no render to skip
    anyway — the per-page cost is one httpx GET + an lxml link parse,
    which is exactly the work BFS needs to find the next frontier.

    Latency bound: BFSDeepCrawlStrategy's own ``max_pages`` is a loose
    per-level cap — link_discovery limits each result's contribution but
    not the accumulated frontier, so on a high-fan-out site batch mode
    can overshoot the cap several-fold (observed 152 fetches for
    max_pages=50). We therefore drive it in STREAM mode and hard-break at
    ``max_pages`` ourselves, which bounds the HTTP fetch count exactly and
    keeps the synchronous request well under the 300 s timeout.

    Off-by-one trap (crawl4ai 0.8.9): ``_arun_stream`` increments its
    internal counter and ``break``s BEFORE ``yield result`` when the
    counter hits ``max_pages`` — the cap-hitting page is fetched but
    never yielded, so a consumer sees at most ``max_pages - 1`` results
    (pages_crawled maxed at 49 for the default cap of 50). We hand the
    strategy ``max_pages + 1`` of headroom so its swallowing break can
    never fire first, and keep the exact bound in OUR loop below.
    tests/v2/test_discover.py::TestCrawlStreamOffByOne pins the upstream
    behavior — if crawl4ai fixes it, that test fails and the +1 can go.

    Returns ``(raw_urls, pages_crawled, warnings)``. ``raw_urls`` is the
    union of every fetched page's own URL and the internal ``<a href>``
    links it exposed — de-dup/filter/cap happen later in ``_finalize_urls``.
    """
    urls: list[str] = []
    warnings: list[str] = []
    pages_crawled = 0

    max_pages = min(max_urls, _MAX_CRAWL_PAGES)
    strategy = AsyncHTTPCrawlerStrategy(
        browser_config=HTTPCrawlerConfig(
            method="GET", follow_redirects=True, verify_ssl=True
        )
    )
    deep_strategy = BFSDeepCrawlStrategy(
        max_depth=max_depth,
        # +1 headroom: the library's stream loop swallows the result
        # that hits its cap (see the off-by-one note above). Our
        # hard-break below enforces the real ``max_pages`` bound.
        max_pages=max_pages + 1,
        include_external=not same_domain_only,
        filter_chain=_ssrf_filter_chain(),
    )
    config = CrawlerRunConfig(
        deep_crawl_strategy=deep_strategy,
        cache_mode=CacheMode.BYPASS,
        stream=True,
        verbose=False,
    )

    try:
        async with AsyncWebCrawler(crawler_strategy=strategy) as crawler:
            stream = await crawler.arun(url=url, config=config)
            async for result in stream:
                pages_crawled += 1
                result_url = getattr(result, "url", None)
                if result_url:
                    urls.append(result_url)
                links = getattr(result, "links", None) or {}
                internal = (
                    links.get("internal", []) if isinstance(links, dict) else []
                )
                for entry in internal:
                    href = entry.get("href") if isinstance(entry, dict) else None
                    if href:
                        urls.append(href)
                if pages_crawled >= max_pages:
                    break  # hard fetch ceiling — see the latency note above
    except Exception as exc:  # noqa: BLE001 — a crawl fault degrades to a warning
        logger.warning("discover crawl failed for %s", url, exc_info=True)
        warnings.append(f"crawl failed: {exc}")

    return urls, pages_crawled, warnings


async def _gather_crawl_urls_js(
    url: str, same_domain_only: bool, max_pages: int
) -> tuple[list[str], int, list[str]]:
    """Render pages in the singleton Chromium and harvest client-injected links.

    Recovers the routes an HTTP-only crawl cannot see on a JS/SPA site: the
    server ships a near-empty shell, the browser synthesises ``<a href>`` links
    at runtime, and we read them off the RENDERED DOM. A shallow same-domain
    BFS bounded to ``max_pages`` renders — each acquiring the semaphore=1
    browser slot briefly — plus a wall-clock budget, so this can never turn a
    synchronous /v2/discover into a long browser sweep on the 1 GB droplet.

    Returns ``(raw_urls, pages_rendered, warnings)`` with the same contract as
    ``_gather_crawl_urls`` so the output flows into the shared ``_finalize_urls``
    pass. Every followed link is SSRF-screened before it is rendered.
    """
    from services.browser.converters.arun_flow import arun_with_watchdog
    from services.browser.converters.browser_manager import get_browser_manager

    urls: list[str] = []
    warnings: list[str] = []
    pages_rendered = 0
    if max_pages <= 0:
        return urls, pages_rendered, warnings

    base_host = urlsplit(url).hostname or ""
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="load",
        page_timeout=45000,
        verbose=False,
        stream=False,
    )
    frontier: list[str] = [url]
    visited: set[str] = set()
    start = time.monotonic()

    try:
        browser_manager = await get_browser_manager()
        while frontier and pages_rendered < max_pages:
            if time.monotonic() - start > _DISCOVER_JS_BUDGET_SECONDS:
                warnings.append("js discovery budget exhausted")
                break
            candidate = frontier.pop(0)
            key = candidate.rstrip("/")
            if key in visited:
                continue
            visited.add(key)
            # Screen every followed link (SSRF), exactly like the HTTP crawl's
            # filter chain does before a fetch.
            if not await is_public_http_url(candidate):
                continue
            try:
                async with browser_manager.crawler_slot() as crawler:
                    # Watchdog-bounded: a wedged render recovers the browser
                    # instead of holding the slot; the except below treats it
                    # like any other failed render (RenderWatchdogTimeout is
                    # a RuntimeError).
                    result = await arun_with_watchdog(
                        crawler, browser_manager, url=candidate, config=run_config
                    )
            except Exception as exc:  # noqa: BLE001 — one bad render != fatal
                warnings.append(f"js render failed for {candidate}: {exc}")
                continue

            pages_rendered += 1
            result_url = getattr(result, "url", None)
            if result_url:
                urls.append(result_url)
            links = getattr(result, "links", None) or {}
            internal = links.get("internal", []) if isinstance(links, dict) else []
            for entry in internal:
                href = entry.get("href") if isinstance(entry, dict) else None
                if not href:
                    continue
                urls.append(href)
                # Enqueue unseen same-domain links for the shallow BFS. Bound
                # the frontier so a high-fan-out page cannot balloon it.
                if len(visited) + len(frontier) < max_pages * 5:
                    if (not same_domain_only) or is_same_domain(href, base_host):
                        if href.rstrip("/") not in visited:
                            frontier.append(href)
    except Exception as exc:  # noqa: BLE001 — a JS-discovery fault degrades
        logger.warning("js discovery failed for %s", url, exc_info=True)
        warnings.append(f"js discovery failed: {exc}")

    return urls, pages_rendered, warnings


async def run(request: DiscoverRequest, user: dict) -> DiscoverResponse:
    """Execute one /v2/discover operation end-to-end (no browser).

    Stateless: no Spaces artifact, no ch_* row, no usage counter
    (``discover`` has no per-operation quota — gated only by the
    ``discover_enabled`` plan flag in the handler). ``user`` is accepted
    for signature parity with the other v2 flows and future per-plan
    tuning; it is not read here today.
    """
    url = request.url.strip()
    await assert_public_http_url(url)

    parts = urlsplit(url)
    base_host = parts.hostname or ""
    origin = f"{parts.scheme}://{parts.netloc}"

    warnings: list[str] = []
    sources: dict[str, int] = {}
    raw: list[str] = [url]  # the seed is part of its own map
    pages_crawled = 0

    # robots.txt is only fetched when respect_robots is on; sitemap mode
    # otherwise lets discover_seed_urls fetch its own (or not). This keeps
    # the common path to a single robots round-trip and lets the unit
    # tests stay fully offline when respect_robots is False.
    robots_info: RobotsInfo | None = None
    if request.respect_robots:
        try:
            robots_info = await fetch_robots_info(origin)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"robots.txt unavailable: {exc}")
            robots_info = RobotsInfo()

    if request.mode in ("sitemap", "hybrid"):
        sitemap_urls, sitemap_warnings, sitemap_sources = await _gather_sitemap_urls(
            url, origin, robots_info
        )
        for key, count in sitemap_sources.items():
            sources[key] = sources.get(key, 0) + count
        raw.extend(sitemap_urls)
        warnings.extend(sitemap_warnings)

    if request.mode in ("crawl", "hybrid"):
        crawl_urls, crawl_pages, crawl_warnings = await _gather_crawl_urls(
            url, request.max_depth, request.max_urls, request.same_domain_only
        )
        sources["crawl"] = len(crawl_urls)
        pages_crawled += crawl_pages
        raw.extend(crawl_urls)
        warnings.extend(crawl_warnings)

    # Roadmap #8: JS-rendered discovery fallback (crawl/hybrid only). 'auto'
    # fires only when the HTTP-only crawl returned a JS shell (<=1 URL);
    # 'always' forces it; 'never' or the global kill switch skips it.
    render_js = getattr(request, "render_js", "auto")
    if (
        _DISCOVER_JS_ENABLED
        and render_js != "never"
        and request.mode in ("crawl", "hybrid")
    ):
        should_render = render_js == "always" or (
            render_js == "auto" and sources.get("crawl", 0) <= 1
        )
        if should_render:
            js_urls, js_pages, js_warnings = await _gather_crawl_urls_js(
                url, request.same_domain_only, _DISCOVER_JS_MAX_PAGES
            )
            sources["crawl_js"] = len(js_urls)
            pages_crawled += js_pages
            raw.extend(js_urls)
            warnings.extend(js_warnings)

    include_res = [re.compile(pattern) for pattern in request.include_patterns]
    exclude_res = [re.compile(pattern) for pattern in request.exclude_patterns]

    urls, truncated = _finalize_urls(
        raw,
        base_host=base_host,
        same_domain_only=request.same_domain_only,
        include_res=include_res,
        exclude_res=exclude_res,
        respect_robots=request.respect_robots,
        robots_info=robots_info,
        max_urls=request.max_urls,
    )

    return DiscoverResponse(
        url=url,
        mode=request.mode,
        total=len(urls),
        urls=urls,
        pages_crawled=pages_crawled,
        truncated=truncated,
        robots_respected=request.respect_robots,
        sources=sources,
        warnings=warnings,
    )
