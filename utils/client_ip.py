"""The visitor's IP for a gateway request: per-IP rate-limit buckets, the
Turnstile remoteip, and logs.

Every public hostname is proxied by Cloudflare to nginx on the droplet, which
proxies to uvicorn. The live nginx restores the visitor IP itself
(real_ip_header CF-Connecting-IP with Cloudflare's set_real_ip_from ranges, seen
in `nginx -T` on 2026-09-17), so request.client.host is normally the visitor
already and is returned unchanged. The Cloudflare branch covers a hop that did
not restore it (that nginx config removed, or a request from a Cloudflare
address straight to the port): the visitor IP is then read from
CF-Connecting-IP, but ONLY when the peer is inside Cloudflare's published
ranges: from any other peer that header is client-forgeable.

Ported from backend/utils/client_geo.py (the source of the range list below)
with one security difference. uvicorn runs with proxy_headers=True and trusts
X-Forwarded-For only from forwarded_allow_ips (127.0.0.1 by default), so when
local nginx sends X-Forwarded-For, request.client.host is already the hop nginx
saw. The peer is therefore request.client.host, and X-Real-IP / X-Forwarded-For
are read only when that peer is loopback (local nginx sent no X-Forwarded-For).
The gateway listens on 0.0.0.0, so a client can connect to it directly: its
peer is neither loopback nor Cloudflare, and every forwarding header it sends
(X-Real-IP, X-Forwarded-For, CF-Connecting-IP) is ignored, so it cannot pick
its own rate-limit bucket.
"""
import ipaddress

from fastapi import Request

# Copied from backend/utils/client_geo.py (https://www.cloudflare.com/ips/ as
# of 2026-09-11); update both together. A stale list degrades to keying on the
# edge IP for traffic from a new range, never to trusting a forged header.
CLOUDFLARE_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
))

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    # A dual-stack listener can report IPv4 peers as ::ffff:a.b.c.d.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _is_cloudflare(ip: IPAddress) -> bool:
    return any(ip in net for net in CLOUDFLARE_NETWORKS)


def _peer_ip(request: Request) -> IPAddress | None:
    """The address that connected to nginx. A loopback peer is local nginx
    without X-Forwarded-For: its X-Real-IP is $remote_addr (a client-sent
    value is overwritten), then the LAST X-Forwarded-For element is the hop
    our proxy appended (earlier ones are client-controlled). Any other peer
    is taken as-is, headers unread."""
    peer = _parse_ip(request.client.host if request.client else None)
    if peer is None or not peer.is_loopback:
        return peer
    candidates = (
        request.headers.get("x-real-ip"),
        request.headers.get("x-forwarded-for", "").split(",")[-1],
    )
    for candidate in candidates:
        ip = _parse_ip(candidate)
        if ip is not None:
            return ip
    return peer


def resolve_client_ip(request: Request) -> str:
    """The visitor's IP as a normalized string, or "" when it can't be trusted
    (a Cloudflare peer without a valid CF-Connecting-IP, or no peer)."""
    peer = _peer_ip(request)
    if peer is None:
        return ""
    if _is_cloudflare(peer):
        visitor = _parse_ip(request.headers.get("cf-connecting-ip"))
        return str(visitor) if visitor is not None else ""
    return str(peer)


def peer_address(request: Request) -> str:
    """The resolved peer (the nginx-restored hop when the socket is loopback)
    as a normalized string, or "" when there is no usable peer. The fallback
    bucket for a visitor resolve_client_ip can't trust: the raw socket would
    be 127.0.0.1 for every request behind local nginx."""
    return str(peer) if (peer := _peer_ip(request)) else ""


def is_proxy_address(value: str) -> bool:
    """Whether value is loopback, a Cloudflare edge, or not an IP at all: an
    address that stands for many visitors, so visitor IP restoration failed."""
    ip = _parse_ip(value)
    return ip is None or ip.is_loopback or _is_cloudflare(ip)
