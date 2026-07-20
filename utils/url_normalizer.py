"""
URL normalization utilities for crawl deduplication.
Prevents duplicate crawling by normalizing URLs to canonical forms.
Uses only urllib.parse from stdlib — no new dependencies.
"""
from urllib.parse import urlparse, urlunparse, unquote, urlencode, parse_qs
import re

# Tracking and session parameters to strip during aggressive normalization
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "ysclid", "mc_cid", "mc_eid",
})

SESSION_PARAMS = frozenset({
    "phpsessid", "jsessionid", "sessionid", "sid",
})

STRIP_PARAMS = TRACKING_PARAMS | SESSION_PARAMS

# File extensions that are not HTML pages
NON_PAGE_EXTENSIONS = frozenset({
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff",
    ".mp4", ".webm", ".avi", ".mov", ".mkv", ".flv",
    ".mp3", ".ogg", ".wav", ".flac", ".aac",
    ".css", ".js", ".mjs", ".map",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".xml", ".json", ".rss", ".atom",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dmg", ".apk", ".deb", ".rpm",
})

# Default ports to strip
DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str, aggressive: bool = True) -> str:
    """
    Normalize a URL to a canonical form for deduplication.

    Always-safe normalizations:
    - Lowercase scheme and host
    - Remove default port (:80 for http, :443 for https)
    - Decode unreserved percent-encoded characters
    - Resolve dot segments (/../, /./)
    - Add trailing slash to bare domains

    Aggressive normalizations (when aggressive=True):
    - Remove URL fragments (#section)
    - Remove tracking parameters (utm_*, fbclid, gclid, etc.)
    - Remove session ID parameters (PHPSESSID, jsessionid, etc.)
    - Sort remaining query parameters alphabetically
    """
    parsed = urlparse(url)

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""

    # Remove default port
    port = parsed.port
    if port and DEFAULT_PORTS.get(scheme) == port:
        port = None

    netloc = host
    if port:
        netloc = f"{host}:{port}"

    # Decode unreserved percent-encoded characters and resolve dot segments
    path = _resolve_dot_segments(unquote(parsed.path))

    # Add trailing slash to bare domains (no path or just /)
    if not path:
        path = "/"

    # Handle query parameters
    query = parsed.query
    fragment = parsed.fragment

    if aggressive:
        # Remove fragments
        fragment = ""

        # Filter and sort query parameters
        if query:
            params = parse_qs(query, keep_blank_values=True)
            filtered = {
                k: v for k, v in params.items()
                if k.lower() not in STRIP_PARAMS
            }
            # Sort and rebuild — parse_qs returns lists, flatten single values
            sorted_params = sorted(filtered.items())
            query = urlencode(
                [(k, v[0] if len(v) == 1 else v) for k, v in sorted_params],
                doseq=True,
            )

    return urlunparse((scheme, netloc, path, "", query, fragment if not aggressive else ""))


def is_same_domain(url: str, base_domain: str) -> bool:
    """Check if a URL belongs to the same domain (including subdomains)."""
    parsed = urlparse(url)
    url_host = (parsed.hostname or "").lower()
    base = base_domain.lower()

    # Strip port from base_domain if present
    if ":" in base:
        base = base.split(":")[0]

    return url_host == base or url_host.endswith(f".{base}")


def is_page_url(url: str) -> bool:
    """
    Filter out non-page URLs (binary files, stylesheets, scripts, etc.).
    Returns True if the URL likely points to an HTML page.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Strip query string from path for extension check
    # Check if path ends with a known non-page extension
    for ext in NON_PAGE_EXTENSIONS:
        if path.endswith(ext):
            return False

    return True


def _resolve_dot_segments(path: str) -> str:
    """Resolve . and .. segments in a URL path (RFC 3986 Section 5.2.4)."""
    segments = path.split("/")
    resolved = []
    for segment in segments:
        if segment == ".":
            continue
        elif segment == "..":
            if resolved and resolved[-1] != "":
                resolved.pop()
        else:
            resolved.append(segment)

    resolved_path = "/".join(resolved)
    # Preserve leading slash
    if path.startswith("/") and not resolved_path.startswith("/"):
        resolved_path = "/" + resolved_path

    return resolved_path
