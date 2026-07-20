"""
Crawlee-based URL discovery service for full website crawling.
Creates an ephemeral AdaptivePlaywrightCrawler per job with MemoryStorageClient for isolation.

Uses existing system limits (batch_limit, MAX_CONCURRENT_CONTEXTS, RATE_LIMITS) —
does not define its own limits.
"""
import asyncio
import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlparse

from crawlee import ConcurrencySettings, Configuration
from crawlee.crawlers import AdaptivePlaywrightCrawler
from crawlee.request_loaders import SitemapRequestLoader
from crawlee.http_clients import HttpxHttpClient
from crawlee.storage_clients import MemoryStorageClient
from crawlee.browsers import BrowserPool, PlaywrightBrowserPlugin

from config import RATE_LIMITS
from services.browser.converters.browser_manager import BrowserManager
from utils.url_normalizer import normalize_url, is_same_domain, is_page_url
from utils.url_sources import discover_seed_urls

logger = logging.getLogger(__name__)

# Safety caps
GLOBAL_TIMEOUT = 600  # 10 minutes
MAX_CRAWL_DEPTH = 10
MEMORY_MBYTES = 512
TRAP_THRESHOLD = 20  # max URLs per pattern fingerprint
PER_PAGE_TIMEOUT = 30  # seconds

# Default exclude patterns for link enqueueing
DEFAULT_EXCLUDE_PATTERNS = [
    r'.*\.(pdf|zip|jpg|jpeg|png|gif|svg|css|js|xml|json|mp4|webm|woff|woff2)(\?.*)?$',
    r'.*/login.*', r'.*/admin.*', r'.*/cart.*', r'.*/checkout.*',
    r'.*[\?&](sort|filter|page=\d{3,}).*',
]


async def discover_urls(
    base_url: str,
    user: dict,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """
    Discover all linked pages on a website using Crawlee's AdaptivePlaywrightCrawler.

    Two-phase discovery:
    1. Pre-crawl: gather seed URLs from robots.txt, sitemaps, RSS feeds
    2. BFS link crawl: follow links to discover additional pages

    Args:
        base_url: The website root URL
        user: Full user dict with subscription data
        include_patterns: Regex patterns to whitelist URLs
        exclude_patterns: Regex patterns to blacklist URLs

    Returns:
        Deduplicated list of discovered page URLs
    """
    sub = user.get("subscription", {})
    start_time = time.monotonic()

    # --- Derive limits from existing system config ---
    max_urls = sub.get("batch_limit", 100)
    max_concurrency = BrowserManager.MAX_CONCURRENT_CONTEXTS
    tier = sub.get("plan_slug", "free")
    key_type = user.get("key_type", "private")
    tier_limits = RATE_LIMITS.get(tier, RATE_LIMITS.get("free", {}))
    per_minute = tier_limits.get(key_type, {}).get("per_minute", 60)

    logger.info(
        "crawl_start",
        extra={
            "base_url": base_url,
            "max_urls": max_urls,
            "concurrency": max_concurrency,
            "rate_limit": per_minute,
            "user_id": user.get("id"),
        },
    )

    # Phase 1: Pre-crawl URL seeding
    sitemap_xml_urls, extra_seed_urls, robots_info = await discover_seed_urls(base_url)

    # Phase 2: Configure Crawlee
    config = Configuration(
        storage_client=MemoryStorageClient(),
        purge_on_start=False,
        memory_mbytes=MEMORY_MBYTES,
    )

    # Set up SitemapRequestLoader if sitemaps found
    request_manager = None
    sitemap_loader = None
    if sitemap_xml_urls:
        sitemap_loader = SitemapRequestLoader(
            sitemap_urls=sitemap_xml_urls,
            http_client=HttpxHttpClient(),
            max_buffer_size=500,
        )
        request_manager = await sitemap_loader.to_tandem()

    # Configure Playwright browser plugin — matches BrowserManager's memory-optimized
    # launch args since the server has very limited memory
    plugin = PlaywrightBrowserPlugin(
        browser_type='chromium',
        headless=True,
        launch_options={
            'args': [
                '--no-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-setuid-sandbox',
                # Memory optimization flags (matching BrowserManager)
                '--js-flags=--max-old-space-size=256',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--aggressive-cache-discard',
                '--disk-cache-size=1',
                '--memory-pressure-off',
            ],
        },
        browser_new_context_options={
            'user_agent': 'ConvertHub-Crawler/1.0 (+https://enconvert.com/crawler-info)',
            'viewport': {'width': 1280, 'height': 720},
        },
    )
    pool = BrowserPool(plugins=[plugin])

    # Create crawler with system-derived limits
    crawler_kwargs = dict(
        max_requests_per_crawl=max_urls,
        request_handler_timeout=timedelta(seconds=PER_PAGE_TIMEOUT),
        concurrency_settings=ConcurrencySettings(
            max_concurrency=max_concurrency,
            max_tasks_per_minute=per_minute,
        ),
        configuration=config,
        browser_pool=pool,
        max_crawl_depth=MAX_CRAWL_DEPTH,
    )
    if request_manager:
        crawler_kwargs["request_manager"] = request_manager

    crawler = AdaptivePlaywrightCrawler.with_parsel_static_parser(**crawler_kwargs)

    # Discovered URL collection
    discovered_urls: list[str] = []
    url_patterns: dict[str, int] = {}  # for trap detection
    base_domain = urlparse(base_url).netloc

    # Block resources during discovery (Playwright pages only)
    @crawler.pre_navigation_hook(playwright_only=True)
    async def block_resources(context):
        await context.block_requests(
            extra_url_patterns=[
                '.mp4', '.webm', '.mp3', '.ogg',
                '.woff2', '.ttf', '.eot',
                'analytics', 'tracking', 'adsbygoogle',
                'google-analytics', 'googletagmanager',
                'facebook.com/tr',
            ]
        )

    # Request handler
    @crawler.router.default_handler
    async def handler(context):
        url = context.request.url
        normalized = normalize_url(url)
        parsed = urlparse(normalized)

        # robots.txt check
        if not robots_info.can_fetch(url):
            logger.warning("crawl_blocked_robots", extra={"url": url})
            return

        # Same-domain HTML page filter
        if not is_same_domain(url, base_domain) or not is_page_url(url):
            return

        # Infinite trap detection: pattern fingerprinting
        pattern = _fingerprint_url_pattern(parsed.path)
        url_patterns[pattern] = url_patterns.get(pattern, 0) + 1
        if url_patterns[pattern] > TRAP_THRESHOLD:
            logger.warning(
                "crawl_trap_detected",
                extra={"url": url, "pattern": pattern, "count": url_patterns[pattern]},
            )
            return

        # Repeating path segment detection
        segments = [s for s in parsed.path.split('/') if s]
        if len(segments) != len(set(segments)) and len(segments) > 4:
            return

        discovered_urls.append(url)

        links_before = len(discovered_urls)

        # Enqueue links for further crawling
        await context.enqueue_links(
            strategy='same-domain',
            include=include_patterns,
            exclude=exclude_patterns or DEFAULT_EXCLUDE_PATTERNS,
        )

        logger.info(
            "crawl_page_discovered",
            extra={
                "url": url,
                "depth": getattr(context.request, 'crawl_depth', None),
                "links_found": len(discovered_urls) - links_before,
            },
        )

    # Custom 429 error handler
    @crawler.error_handler
    async def handle_errors(context):
        resp = getattr(context, 'http_response', None)
        if resp and resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', 10))
            wait_time = min(retry_after, 30)
            logger.warning(
                "crawl_429_backoff",
                extra={"url": context.request.url, "retry_after": wait_time},
            )
            await asyncio.sleep(wait_time)

    # Run with global timeout
    seed_requests = [base_url] + extra_seed_urls

    async def run_crawl():
        if sitemap_loader:
            async with sitemap_loader:
                await crawler.run(seed_requests)
        else:
            await crawler.run(seed_requests)

    crawl_task = asyncio.create_task(run_crawl())
    try:
        await asyncio.wait_for(asyncio.shield(crawl_task), timeout=GLOBAL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("crawl_timeout", extra={"base_url": base_url, "timeout": GLOBAL_TIMEOUT})
        await crawler.stop(reason="Global timeout reached")

    # Deduplicate (normalized) while preserving discovery order
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in discovered_urls:
        norm = normalize_url(url)
        if norm not in seen:
            seen.add(norm)
            unique_urls.append(url)

    duration = time.monotonic() - start_time
    logger.info(
        "crawl_complete",
        extra={
            "base_url": base_url,
            "total_discovered": len(unique_urls),
            "duration_sec": round(duration, 2),
            "sitemaps_used": len(sitemap_xml_urls),
            "seeds_used": len(seed_requests),
        },
    )

    return unique_urls


def _fingerprint_url_pattern(path: str) -> str:
    """Replace numeric and date-like segments with placeholders for trap detection."""
    segments = path.strip('/').split('/')
    fingerprinted = []
    for seg in segments:
        if re.match(r'^\d+$', seg):
            fingerprinted.append('{N}')
        elif re.match(r'^\d{4}-\d{2}(-\d{2})?$', seg):
            fingerprinted.append('{DATE}')
        else:
            fingerprinted.append(seg)
    return '/'.join(fingerprinted)
