"""
Sitemap parser utility for full website capture.
Fetches and parses sitemap.xml, with support for sitemap index recursion.
"""
import gzip
import xml.etree.ElementTree as ET
import httpx
from fastapi import HTTPException
import logging

logger = logging.getLogger(__name__)

SITEMAP_TIMEOUT = 30.0  # seconds per HTTP request

# Gzip magic number: a .xml.gz body (or any Content-Encoding: gzip that httpx
# did not transparently inflate) starts with these two bytes.
_GZIP_MAGIC = b"\x1f\x8b"

# Realistic browser-like headers. A bare httpx client with no User-Agent is a
# trivial WAF signal; many sites 403/503 the default python-httpx UA. These make
# sitemap fetches survive common bot-walls. (True TLS-fingerprint evasion needs
# the curl_cffi engine — tracked separately.)
_SITEMAP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _decode_sitemap_body(url: str, response: httpx.Response) -> str:
    """Decode a sitemap response body, transparently gunzipping when needed.

    httpx auto-inflates only responses carrying ``Content-Encoding: gzip``. A
    sitemap served as a ``.xml.gz`` file (``Content-Type: application/gzip``) or
    any body whose raw bytes begin with the gzip magic number is delivered
    un-inflated, and decoding it as text yields mojibake that fails XML parsing.
    Detect gzip by URL suffix, content-type, or magic bytes and decompress
    before decoding.
    """
    body = response.content
    content_type = response.headers.get("content-type", "").lower()
    looks_gzip = (
        url.lower().endswith(".gz")
        or "gzip" in content_type
        or "application/x-gzip" in content_type
        or body[:2] == _GZIP_MAGIC
    )
    if looks_gzip and body[:2] == _GZIP_MAGIC:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not decompress gzipped sitemap: {url} ({exc})",
            )
    # Prefer the charset httpx negotiated; fall back to utf-8, replacing any
    # stray undecodable bytes rather than raising.
    encoding = response.encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", errors="replace")


async def fetch_sitemap_urls(base_url: str) -> list[str]:
    """
    Fetch sitemap.xml from base_url and extract all page URLs.
    Supports both flat <urlset> sitemaps and <sitemapindex> with recursion.

    Args:
        base_url: The website base URL (e.g. "https://example.com")

    Returns:
        List of page URLs found in the sitemap.

    Raises:
        HTTPException(400) if sitemap cannot be fetched, parsed, or contains no URLs.
    """
    sitemap_url = base_url.rstrip("/") + "/sitemap.xml"
    urls = await _parse_sitemap(sitemap_url)

    if not urls:
        raise HTTPException(
            status_code=400,
            detail=f"No URLs found in sitemap: {sitemap_url}"
        )

    return urls


async def parse_sitemap(url: str) -> list[str]:
    """Parse an EXACT sitemap URL (urlset or sitemapindex), fail-soft.

    Unlike ``fetch_sitemap_urls`` (which appends ``/sitemap.xml`` to a base and
    raises on any problem), this parses the URL as given and returns ``[]`` on a
    missing / unfetchable / invalid sitemap instead of raising. That lets
    /v2/discover probe MANY candidate sitemap URLs (robots ``Sitemap:``
    directives, apex-domain sitemaps, common paths) where one dead candidate
    must not abort the whole gather.
    """
    try:
        return await _parse_sitemap(url)
    except HTTPException:
        return []


async def _fetch_xml(url: str) -> str:
    """Fetch XML content from a URL (gunzipping .xml.gz bodies, WAF-hardened headers)."""
    try:
        async with httpx.AsyncClient(
            timeout=SITEMAP_TIMEOUT,
            follow_redirects=True,
            headers=_SITEMAP_HEADERS,
        ) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=400,
            detail=f"Timeout fetching sitemap: {url}"
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not fetch sitemap: {url} ({e})"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Could not fetch sitemap: {url} returned {response.status_code}"
        )

    return _decode_sitemap_body(url, response)


async def _parse_sitemap(url: str) -> list[str]:
    """Parse a sitemap URL. Handles both <urlset> and <sitemapindex>."""
    xml_text = await _fetch_xml(url)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid XML in sitemap: {url}"
        )

    # Strip namespace for easier tag matching
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    ns = root.tag.replace(tag, "") if "}" in root.tag else ""

    if tag == "sitemapindex":
        # Sitemap index: recursively fetch each child sitemap
        urls = []
        for sitemap_el in root.findall(f"{ns}sitemap"):
            loc_el = sitemap_el.find(f"{ns}loc")
            if loc_el is not None and loc_el.text:
                child_urls = await _parse_sitemap(loc_el.text.strip())
                urls.extend(child_urls)
        return urls

    if tag == "urlset":
        # Flat sitemap: extract all <url><loc> entries
        urls = []
        for url_el in root.findall(f"{ns}url"):
            loc_el = url_el.find(f"{ns}loc")
            if loc_el is not None and loc_el.text:
                urls.append(loc_el.text.strip())
        return urls

    raise HTTPException(
        status_code=400,
        detail=f"Unrecognized sitemap format at {url}: root element is <{tag}>"
    )
