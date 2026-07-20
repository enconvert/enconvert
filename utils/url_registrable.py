"""Registrable-domain (approximate eTLD+1) helper.

A deliberately small public-suffix approximation shared by the F.7 render
quality scorer (so www/accounts/consent subdomain hops do not look like
cross-domain redirects) and /v2/discover (so it can probe a site's apex
domain for sitemaps, not only the exact host it was handed). Kept dependency
free — no tldextract / publicsuffix — matching the codebase's stdlib-only URL
utilities.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

# Second-level labels under which a two-label suffix is NOT the registered
# domain (example.co.uk, example.ac.jp, ...). This is the exact set the F.7
# scorer has used since its inception; it now lives here as the single source
# of truth so the scorer and discover cannot drift.
SHARED_SLDS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})


def registered_domain(host: Optional[str]) -> Optional[str]:
    """Approximate eTLD+1 for an already-parsed hostname (no scheme/port)."""
    if not host:
        return None
    host = host.lower().rstrip(".")
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in SHARED_SLDS and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def registered_domain_from_url(url: Optional[str]) -> Optional[str]:
    """Approximate eTLD+1 for a full URL; None on empty/unparseable input."""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return registered_domain(host) if host else None
