"""
robots.txt parser with Sitemap: directive extraction.
Wraps Python's urllib.robotparser with additional functionality
not supported by stdlib (Sitemap: directives, Crawl-Delay).
Uses urllib.robotparser (stdlib) + httpx (already installed).
"""
import logging
from dataclasses import dataclass, field
from typing import Callable
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "ConvertHub-Crawler/1.0"
ROBOTS_TIMEOUT = 10.0  # seconds


@dataclass
class RobotsInfo:
    """Parsed robots.txt information."""
    sitemap_urls: list[str] = field(default_factory=list)
    can_fetch: Callable[[str], bool] = field(default=lambda url: True)
    crawl_delay: float | None = None


async def fetch_robots_info(base_url: str) -> RobotsInfo:
    """
    Fetch and parse robots.txt from target site.

    Extracts:
    - Sitemap: directives (not supported by stdlib robotparser)
    - can_fetch(url) check for our User-Agent
    - Crawl-Delay directive

    Fails gracefully — if robots.txt is unavailable, returns permissive
    defaults (allow all, no sitemaps, no delay).
    """
    robots_url = base_url.rstrip("/") + "/robots.txt"

    try:
        async with httpx.AsyncClient(timeout=ROBOTS_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(robots_url)

        if response.status_code != 200:
            logger.info("robots_txt_not_found", extra={"url": robots_url, "status": response.status_code})
            return RobotsInfo()

        text = response.text

    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("robots_txt_fetch_failed", extra={"url": robots_url, "error": str(e)})
        return RobotsInfo()

    # Parse with stdlib RobotFileParser
    parser = RobotFileParser()
    parser.parse(text.splitlines())

    # Extract Sitemap: directives (not supported by stdlib)
    sitemap_urls = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            sitemap_url = line.split(":", 1)[1].strip()
            if sitemap_url:
                sitemap_urls.append(sitemap_url)

    # Extract Crawl-Delay for our user agent
    crawl_delay = _extract_crawl_delay(text, USER_AGENT)

    # Build can_fetch callable
    def can_fetch(url: str) -> bool:
        return parser.can_fetch(USER_AGENT, url)

    logger.info(
        "robots_txt_parsed",
        extra={
            "url": robots_url,
            "sitemap_count": len(sitemap_urls),
            "crawl_delay": crawl_delay,
        },
    )

    return RobotsInfo(
        sitemap_urls=sitemap_urls,
        can_fetch=can_fetch,
        crawl_delay=crawl_delay,
    )


def _extract_crawl_delay(text: str, user_agent: str) -> float | None:
    """
    Extract Crawl-Delay directive for a specific user agent.
    Falls back to * user agent if specific one not found.
    """
    current_agent = None
    specific_delay = None
    wildcard_delay = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.lower().startswith("user-agent:"):
            current_agent = line.split(":", 1)[1].strip().lower()
        elif line.lower().startswith("crawl-delay:"):
            try:
                delay = float(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                continue

            if current_agent == user_agent.lower():
                specific_delay = delay
            elif current_agent == "*":
                wildcard_delay = delay

    return specific_delay if specific_delay is not None else wildcard_delay
