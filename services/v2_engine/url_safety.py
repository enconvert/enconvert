"""SSRF guard for V2 endpoints that fetch caller-supplied URLs.

The gateway's Chromium runs on the droplet with the database and the
backend service, so a caller-supplied URL pointing at loopback, RFC1918
space, or the cloud metadata endpoint must be rejected before it ever
reaches the browser (project security rules: "block private/internal
IPs" on URL conversion).

DNS rebinding + redirects: the guard resolves the hostname here, but a
fetcher that resolves it AGAIN (or follows a 30x to an internal address)
could diverge. The Chromium render path runs behind a loopback egress proxy
(services/browser/egress_guard.py) that re-validates EVERY connection the
browser makes — documents, iframes, native redirect hops, subresources,
WebSockets — with ``pinned_public_ips`` and connects to the validated
address itself; the TLS/HTTP rung pins the validated IP too. A bare
``assert_public_http_url`` caller that does its own fetch without those
guards still carries the original TOCTOU. (``make_ssrf_route_handler``
below is kept for the open-source mirror build, which has no egress proxy.)

A configurable local threat policy (services/v2_engine/threat_policy.py)
is enforced at this same choke point — a denylist of domains/TLDs/host
patterns with an audit trail, using no external hosted dependency.

V1's url-to-* converters predate this module; retrofitting V1 is a
separate task because its public behavior is frozen.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

from fastapi import HTTPException

from services.v2_engine import threat_policy

# A label that a permissive C resolver reads as decimal but a WHATWG URL
# parser (Chromium) reads as octal/hex — or a bare packed-integer host.
# 0177.0.0.1 -> getaddrinfo 177.0.0.1 (public) but Chromium 127.0.0.1
# (loopback): the guard and the browser disagree on the destination. We
# only apply this when EVERY label is numeric/hex (a clear IP attempt),
# so real hostnames like "007.example.com" are never false-positived.
_NUMERIC_LABEL_RE = re.compile(r"0[xX][0-9a-fA-F]+|[0-9]+")

# Hostnames that must never be fetched regardless of what they resolve
# to (cloud metadata services answer on well-known names too).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when the address is not publicly routable.

    ``not is_global`` also catches ranges the named predicates miss, e.g.
    CGNAT 100.64.0.0/10 (used by Tailscale / carrier NAT) and the IETF
    documentation / benchmarking blocks.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


async def _resolve_host(hostname: str) -> list[str]:
    """Resolve a hostname to all its addresses without blocking the loop."""

    def _resolve() -> list[str]:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        return [info[4][0] for info in infos]

    return await asyncio.to_thread(_resolve)


async def assert_public_http_url(url: str) -> None:
    """Raise HTTPException(400) unless ``url`` is a public http(s) URL.

    Rejects: non-http(s) schemes, URLs with embedded credentials, blocked
    hostnames, IP literals or DNS results in private/loopback/link-local/
    reserved/multicast/unspecified ranges, and unresolvable hosts.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=400,
            detail="Only http:// and https:// URLs are supported.",
        )

    if parts.username is not None or parts.password is not None:
        raise HTTPException(
            status_code=400,
            detail="URLs with embedded credentials are not allowed. "
            "Use the 'auth' field for HTTP Basic Auth.",
        )

    hostname = parts.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL has no hostname.")

    if hostname.lower().rstrip(".") in _BLOCKED_HOSTNAMES:
        raise HTTPException(
            status_code=400,
            detail="This hostname is not allowed.",
        )

    # Local threat policy (denylist of domains/TLDs/host patterns + audit).
    # No network call — safe to run before DNS resolution.
    threat_policy.assert_allowed(url, hostname)

    # Canonical IP literal: check directly, no DNS involved.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_forbidden_ip(literal):
            raise HTTPException(
                status_code=400,
                detail="URLs resolving to private or internal addresses "
                "are not allowed.",
            )
        return

    # Not a canonical literal. If every label is numeric/hex, this is a
    # non-standard IP notation (octal/hex per-octet or packed integer)
    # that libc and Chromium can parse to DIFFERENT addresses — reject
    # before it can produce a validate-vs-navigate split (see module note
    # on residual gaps; this closes the dotted-octal/hex/packed class).
    labels = hostname.split(".")
    if labels and all(
        _NUMERIC_LABEL_RE.fullmatch(label) for label in labels
    ):
        raise HTTPException(
            status_code=400,
            detail="Non-standard IP address notation is not allowed.",
        )

    try:
        addresses = await _resolve_host(hostname)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve hostname '{hostname}'.",
        ) from exc

    if not addresses:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve hostname '{hostname}'.",
        )

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            continue
        if _is_forbidden_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="URLs resolving to private or internal addresses "
                "are not allowed.",
            )


async def pinned_public_ips(host: str) -> list[str]:
    """Every address ``host`` resolves to, if ALL are safe to connect to.

    The browser egress guard's check (services/browser/egress_guard.py):
    the same rules as ``assert_public_http_url`` — blocked hostnames, the
    local threat policy, non-standard numeric notation, and every resolved
    address public — but it returns the validated addresses (IPv4 first) so
    the caller connects to exactly these and never resolves again (pinning,
    which closes the DNS-rebind window). Returns [] for anything that must
    not be fetched; never raises.
    """
    hostname = host.strip().strip("[]").rstrip(".").lower()
    if not hostname or hostname in _BLOCKED_HOSTNAMES:
        return []
    try:
        threat_policy.assert_allowed(f"http://{hostname}/", hostname)
    except HTTPException:
        return []
    try:
        ips = [ipaddress.ip_address(hostname)]
    except ValueError:
        labels = hostname.split(".")
        if all(_NUMERIC_LABEL_RE.fullmatch(label) for label in labels):
            return []
        try:
            addresses = await _resolve_host(hostname)
        except OSError:
            return []
        ips = []
        for address in addresses:
            try:
                ips.append(ipaddress.ip_address(address.split("%", 1)[0]))
            except ValueError:
                return []  # unparseable answer: fail closed
    if not ips or any(_is_forbidden_ip(ip) for ip in ips):
        return []
    # Return an IPv4-mapped IPv6 answer as the IPv4 it denotes, so the caller
    # compares and connects to exactly the address that was checked.
    canonical = {
        ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        for ip in ips
    }
    return [str(ip) for ip in sorted(canonical, key=lambda ip: ip.version)]


async def public_ip_for_host(host: str) -> str | None:
    """First publicly-routable IP for ``host`` (for connection PINNING).

    An IP literal is validated and returned as-is. A hostname is resolved and
    EVERY address validated; the first public address is returned. Raises
    HTTPException(400) if any resolved address is forbidden (SSRF), or returns
    None if the host cannot be resolved. Pinning the TLS/HTTP connection to
    this IP closes the resolve-vs-connect DNS-rebind window for that rung.
    """
    if not host:
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_forbidden_ip(literal):
            raise HTTPException(
                status_code=400,
                detail="URLs resolving to private or internal addresses "
                "are not allowed.",
            )
        return host
    try:
        addresses = await _resolve_host(host)
    except OSError:
        return None
    public: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            continue
        if _is_forbidden_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="URLs resolving to private or internal addresses "
                "are not allowed.",
            )
        public.append(str(ip))
    return public[0] if public else None


async def is_public_http_url(url: str) -> bool:
    """Boolean form of ``assert_public_http_url`` for per-link filtering.

    ``/v2/discover``'s BFS follows links it finds in fetched pages; when
    ``same_domain_only`` is off those links can point at arbitrary hosts,
    including ``http://192.168.x.x`` or the cloud metadata endpoint
    embedded in an attacker-controlled page. The deep-crawl filter chain
    screens every candidate through this predicate BEFORE it is fetched,
    so the SSRF guard covers followed links and not just the caller's
    seed. Same residual gaps as the assert form (DNS rebinding between
    this resolve and the crawler's own fetch).
    """
    try:
        await assert_public_http_url(url)
        return True
    except HTTPException:
        return False


async def is_host_public(host: str) -> bool:
    """True when a bare host (optionally host:port) passes every SSRF + threat
    check. Used by the browser route guard to RE-VALIDATE each navigated and
    subresource host at request time, closing the DNS-rebind / redirect TOCTOU
    for the Chromium render path (the second resolution is screened, not just
    the first). Reuses ``assert_public_http_url`` so the blocked-hostname,
    IP-literal, non-standard-notation, DNS-resolution and threat-policy rules
    all apply identically.
    """
    if not host:
        return False
    return await is_public_http_url(f"http://{host}")


def make_ssrf_route_handler(*, allow_action: str = "continue"):
    """Build a Playwright route handler that re-validates every request host.

    Re-checks each navigated/subresource host against the SSRF + threat rules
    at request time (verdicts cached per host so DNS resolves at most once per
    host), aborting requests to private/blocked hosts — this is what closes the
    DNS-rebind / redirect-to-internal window that pre-navigation validation
    alone cannot. On an allowed host it either ``continue_()``s (raw Playwright
    contexts) or ``fallback()``s (chained-route contexts like the crawl4ai
    render hooks) per ``allow_action``. Never breaks rendering: a guard fault
    lets the request proceed.
    """
    verdicts: dict[str, bool] = {}

    async def _handler(route: object) -> None:
        async def _allow() -> None:
            if allow_action == "fallback":
                await route.fallback()  # type: ignore[attr-defined]
            else:
                await route.continue_()  # type: ignore[attr-defined]

        try:
            host = (urlsplit(route.request.url).hostname or "").lower()  # type: ignore[attr-defined]
            allowed = verdicts.get(host)
            if allowed is None:
                allowed = await is_host_public(host)
                verdicts[host] = allowed
            if allowed:
                await _allow()
            else:
                await route.abort("addressunreachable")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — never break rendering on a guard fault
            try:
                await _allow()
            except Exception:  # noqa: BLE001
                pass

    return _handler
