"""
Pre-crawl supplementary URL discovery from fast, non-browser sources.
These URLs seed the Crawlee SitemapRequestLoader and RequestQueue.

Sources probed (in order):
1. robots.txt → Sitemap: directives (via robots_parser)
2. sitemap.xml → existing parser + robots.txt sitemap URLs
3. Common sitemap paths (if no sitemap found)
4. RSS/Atom feeds → discovered from homepage or probed at common paths
"""
import logging
import xml.etree.ElementTree as ET

import httpx

from utils.robots_parser import fetch_robots_info, RobotsInfo

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0  # seconds per HTTP request

# Common sitemap paths to probe if no sitemap found via robots.txt
COMMON_SITEMAP_PATHS = [
    "/sitemap_index.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/sitemap1.xml",
]

# Common RSS/Atom feed paths to probe
COMMON_FEED_PATHS = [
    "/feed",
    "/rss",
    "/rss.xml",
    "/atom.xml",
]


async def discover_seed_urls(
    base_url: str,
    robots_info: RobotsInfo | None = None,
) -> tuple[list[str], list[str], RobotsInfo]:
    """
    Discover seed URLs from non-browser sources.

    Returns:
        (sitemap_xml_urls, additional_page_urls, robots_info)
        - sitemap_xml_urls: URLs of sitemap XML files (to feed SitemapRequestLoader)
        - additional_page_urls: Directly discovered page URLs (to add as initial requests)
        - robots_info: The RobotsInfo used (fetched if not provided)
    """
    base = base_url.rstrip("/")

    # 1. Fetch robots.txt if not provided
    if robots_info is None:
        robots_info = await fetch_robots_info(base_url)

    sitemap_xml_urls: list[str] = []
    page_urls: list[str] = []

    # 2. Collect sitemap URLs from robots.txt
    sitemap_xml_urls.extend(robots_info.sitemap_urls)

    # 3. Check standard /sitemap.xml
    standard_sitemap = f"{base}/sitemap.xml"
    if standard_sitemap not in sitemap_xml_urls:
        if await _url_exists(standard_sitemap):
            sitemap_xml_urls.append(standard_sitemap)

    # 4. If no sitemaps found, probe common paths
    if not sitemap_xml_urls:
        logger.info("no_sitemap_found_probing", extra={"base_url": base_url})
        for path in COMMON_SITEMAP_PATHS:
            url = f"{base}{path}"
            if await _url_exists(url):
                sitemap_xml_urls.append(url)
                logger.info("sitemap_probed_found", extra={"url": url})
                break  # Use the first one found

    # 5. Discover RSS/Atom feed URLs for additional page URLs
    feed_urls = await _discover_feeds(base)
    for feed_url in feed_urls:
        urls = await _parse_feed(feed_url)
        page_urls.extend(urls)

    logger.info(
        "seed_urls_discovered",
        extra={
            "base_url": base_url,
            "sitemap_count": len(sitemap_xml_urls),
            "feed_page_count": len(page_urls),
        },
    )

    return sitemap_xml_urls, page_urls, robots_info


async def _url_exists(url: str) -> bool:
    """Check if a URL returns 200 with a HEAD request, falling back to GET."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
            response = await client.head(url)
            if response.status_code == 405:  # Method not allowed, try GET
                response = await client.get(url)
            return response.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        return False


async def _discover_feeds(base: str) -> list[str]:
    """
    Discover RSS/Atom feeds from the homepage <link> tags,
    or probe common feed paths.
    """
    feeds: list[str] = []

    # Try to find feeds from homepage <link> tags
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(base)
            if response.status_code == 200:
                text = response.text[:50_000]  # Only scan first 50KB
                feeds.extend(_extract_feed_links(text, base))
    except (httpx.TimeoutException, httpx.RequestError):
        pass

    # If no feeds found from homepage, probe common paths
    if not feeds:
        for path in COMMON_FEED_PATHS:
            url = f"{base}{path}"
            if await _url_exists(url):
                feeds.append(url)
                break  # Use the first one found

    return feeds


def _extract_feed_links(html: str, base: str) -> list[str]:
    """Extract RSS/Atom feed URLs from HTML <link> tags."""
    import re

    feeds = []
    # Match <link rel="alternate" type="application/rss+xml" href="...">
    # and <link rel="alternate" type="application/atom+xml" href="...">
    pattern = r'<link[^>]+type=["\']application/(rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']'
    for match in re.finditer(pattern, html, re.IGNORECASE):
        href = match.group(2)
        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            href = base + "/" + href
        feeds.append(href)

    # Also check reverse attribute order
    pattern_rev = r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/(rss|atom)\+xml["\']'
    for match in re.finditer(pattern_rev, html, re.IGNORECASE):
        href = match.group(1)
        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            href = base + "/" + href
        if href not in feeds:
            feeds.append(href)

    return feeds


async def _parse_feed(feed_url: str) -> list[str]:
    """Parse an RSS or Atom feed and extract page URLs."""
    urls: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(feed_url)
            if response.status_code != 200:
                return urls

        root = ET.fromstring(response.text)

        # RSS 2.0: <channel><item><link>
        for item in root.iter("item"):
            link = item.find("link")
            if link is not None and link.text:
                urls.append(link.text.strip())

        # Atom: <entry><link href="...">
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            link = entry.find("atom:link[@rel='alternate']", ns)
            if link is None:
                link = entry.find("atom:link", ns)
            if link is not None:
                href = link.get("href")
                if href:
                    urls.append(href.strip())

    except (ET.ParseError, httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("feed_parse_failed", extra={"url": feed_url, "error": str(e)})

    return urls
