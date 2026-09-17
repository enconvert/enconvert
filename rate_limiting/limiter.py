"""In-process per-key / per-IP request-rate limiting for the gateway.

Backed by the `limits` library with in-memory storage (``memory://``), which is
correct for the single-worker gateway. Scaling past one worker/droplet needs a
shared backend AND an async migration: switch to ``limits.aio`` (async storage
+ strategy, e.g. ``async+redis://``), make ``enforce`` async and await it in
deps.py, and add the redis package to requirements. Do NOT simply point
``RATE_LIMIT_STORAGE_URI`` at ``redis://`` — the sync client would do blocking
network I/O on the event loop for every request.

These are short-window FAIRNESS limits (HTTP 429), separate from the monthly
conversion quotas enforced in api/deps.py (HTTP 402). Enforcement is ON by
default; RATE_LIMITING_ENABLED=false switches it off (a load test, a local
dev box). The `limits` import is guarded so the gateway still
starts if the package is not yet installed, and a bad storage URI disables
limiting (with a logged error) instead of preventing boot.
"""
import ipaddress
import logging
import os
import time

from fastapi import HTTPException, Request

from config import RATE_LIMITS
from utils.client_ip import is_proxy_address, peer_address, resolve_client_ip

logger = logging.getLogger("conversion-api-gateway")

try:
    from limits import (
        RateLimitItemPerDay,
        RateLimitItemPerHour,
        RateLimitItemPerMinute,
    )
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    _LIMITS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _LIMITS_AVAILABLE = False

_ENABLED = os.getenv("RATE_LIMITING_ENABLED", "true").lower() == "true"
_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

# Per-IP backstop for PUBLIC-key traffic. A public (pk_) key is shared by every
# one of a customer's browser visitors, so the per-project limits below cannot
# single out one abusive visitor. 0 = auto: half the tier's public per-minute/
# per-hour window (min 3/min), so the backstop always engages BELOW the shared
# project window on every tier.
_PUBLIC_IP_PER_MINUTE = int(os.getenv("PUBLIC_IP_RATE_PER_MINUTE", "0"))
_PUBLIC_IP_PER_HOUR = int(os.getenv("PUBLIC_IP_RATE_PER_HOUR", "0"))

# Per-IP caps for the anonymous playground: a public (pk_) key on the admin
# default project. The admin plan has no tier windows, so this bucket is the
# only app-layer throttle between a visitor's 1 h JWT and the single
# Chromium slot (MAX_CONCURRENT_CONTEXTS=1).
_PLAYGROUND_IP_PER_MINUTE = int(os.getenv("PLAYGROUND_IP_PER_MINUTE", "10"))
_PLAYGROUND_IP_PER_HOUR = int(os.getenv("PLAYGROUND_IP_PER_HOUR", "60"))

# Token minting (pk_ -> JWT) gets its own per-IP bucket and does NOT consume
# the tier windows — otherwise every widget visitor costs 2 units (mint +
# convert) and free's 10/min public window would false-429 legitimate traffic.
_MINT_PER_MINUTE = int(os.getenv("TOKEN_MINT_PER_IP_PER_MINUTE", "2"))
_MINT_PER_HOUR = int(os.getenv("TOKEN_MINT_PER_IP_PER_HOUR", "20"))

# Only mutating/billable POSTs are throttled; GET status-polling, downloads,
# branding and health are never limited (clients poll them every few seconds).
_BILLABLE_PREFIXES = ("/v1/convert/", "/v1/extension/", "/v2/")

if _LIMITS_AVAILABLE:
    try:
        _storage = storage_from_string(_STORAGE_URI)
        _limiter = FixedWindowRateLimiter(_storage)
    except Exception:
        # A typo'd or unsupported URI must never prevent the gateway from
        # booting — fall back to disabled and make the failure loud in logs.
        _storage = None
        _limiter = None
        _LIMITS_AVAILABLE = False
        logger.exception(
            "Invalid RATE_LIMIT_STORAGE_URI %r — rate limiting DISABLED",
            _STORAGE_URI,
        )
else:
    _storage = None
    _limiter = None
    if _ENABLED:
        logger.warning(
            "RATE_LIMITING_ENABLED=true but the 'limits' package is not "
            "installed; rate limiting is inactive. Run: pip install limits"
        )


def _should_limit(method: str, path: str) -> bool:
    if method != "POST":
        return False
    if path.endswith("/auth/token"):
        return True  # pk_ -> JWT minting, throttled via its own mint bucket
    return path.startswith(_BILLABLE_PREFIXES)


def _reject(item, limit_value: int, *identifiers: str) -> None:
    reset_time, remaining = _limiter.get_window_stats(item, *identifiers)
    retry_after = max(1, int(reset_time - time.time()))
    raise HTTPException(
        status_code=429,
        detail="Rate limit exceeded. Please slow down and retry shortly.",
        headers={
            "RateLimit-Limit": str(limit_value),
            "RateLimit-Remaining": str(max(0, remaining)),
            "RateLimit-Reset": str(retry_after),
            "Retry-After": str(retry_after),
        },
    )


def _enforce_windows(windows: list, *identifiers: str) -> None:
    # Test every window first (no consume); reject if any is exhausted, then
    # consume one hit from each. Test-then-hit is atomic within the
    # single-worker async process (no await between the calls).
    for item, limit_value in windows:
        if not _limiter.test(item, *identifiers):
            _reject(item, limit_value, *identifiers)
    for item, _ in windows:
        _limiter.hit(item, *identifiers)


def _tier_windows(cfg: dict) -> list:
    return [
        (RateLimitItemPerMinute(cfg["per_minute"]), cfg["per_minute"]),
        (RateLimitItemPerHour(cfg["per_hour"]), cfg["per_hour"]),
        (RateLimitItemPerDay(cfg["per_day"]), cfg["per_day"]),
    ]


def _public_ip_caps(cfg: dict) -> tuple:
    per_minute = _PUBLIC_IP_PER_MINUTE or max(3, cfg["per_minute"] // 2)
    per_hour = _PUBLIC_IP_PER_HOUR or max(30, cfg["per_hour"] // 2)
    return per_minute, per_hour


_warned_collapsed_ip = False


def _client_ip(request: Request) -> str:
    """Visitor IP for the per-IP buckets, warning once if it is not a visitor.

    An unresolvable visitor (a Cloudflare peer without a valid
    CF-Connecting-IP) is bucketed on its resolved peer (the edge nginx saw,
    not the loopback socket) rather than "", which would put every such
    request everywhere into one bucket. If the key is still loopback or a
    Cloudflare edge, restoration failed and every visitor behind that address
    shares one bucket, so the 2/min mint bucket becomes a site-wide cap: make
    that visible in the logs instead of as a 429 storm.

    IPv6 keys are the visitor's /64: one ordinary allocation is a whole /64,
    so a per-address bucket could be rotated around without limit.
    """
    global _warned_collapsed_ip
    client_ip = resolve_client_ip(request) or peer_address(request) or "unknown"
    if not _warned_collapsed_ip and is_proxy_address(client_ip):
        _warned_collapsed_ip = True
        logger.warning(
            "Rate limiter saw client IP %r: per-IP buckets are collapsed "
            "onto a proxy address; check the Cloudflare -> nginx -> uvicorn "
            "chain (CF-Connecting-IP, X-Real-IP / X-Forwarded-For, "
            "forwarded_allow_ips)",
            client_ip,
        )
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return client_ip


def enforce(request: Request, user: dict) -> None:
    """Raise HTTP 429 if the caller exceeded its plan's request-rate limit.

    No-op unless rate limiting is enabled (and the `limits` package is present)
    and the request is a billable POST. The admin plan bypasses the tier
    windows, but its PUBLIC key (the anonymous playground JWT) still gets a
    per-IP mint bucket and a per-IP backstop, both at the playground caps.
    Safe to call on every authenticated
    request.
    """
    if not (_ENABLED and _LIMITS_AVAILABLE):
        return
    if not _should_limit(request.method, request.url.path):
        return

    sub = user.get("subscription", {})
    plan = sub.get("plan_slug", "free")
    key_type = "public" if user.get("key_type") == "public" else "private"
    if plan == "admin" and key_type != "public":
        return  # the founder's own sk_ key

    tier_cfg = RATE_LIMITS.get(plan, RATE_LIMITS["free"])
    limits_cfg = tier_cfg.get(key_type, tier_cfg["private"])

    project_id = str(user.get("id", "anonymous"))
    client_ip = _client_ip(request)

    # Token minting: own per-IP bucket, never charged to the tier windows.
    if request.url.path.endswith("/auth/token"):
        # The playground (/playground, /convert/*, /check) mints one JWT per
        # conversion, so the 2/min customer-widget mint cap would 429 a person
        # converting their third file. Its billable POSTs are already capped
        # per IP below, whatever the number of tokens, so its mint bucket
        # matches those caps instead.
        # ponytail: Turnstile is only verified when Origin == WIDGET_ORIGIN
        # (api/v1/auth.py); requiring it on every playground mint would let
        # this bucket go, once no caller of the playground key lacks Turnstile.
        mint_minute, mint_hour = (
            (_PLAYGROUND_IP_PER_MINUTE, _PLAYGROUND_IP_PER_HOUR)
            if plan == "admin"
            else (_MINT_PER_MINUTE, _MINT_PER_HOUR)
        )
        _enforce_windows(
            [
                (RateLimitItemPerMinute(mint_minute), mint_minute),
                (RateLimitItemPerHour(mint_hour), mint_hour),
            ],
            "mint",
            project_id,
            client_ip,
        )
        return

    # Per-IP backstop for shared public keys (keyed by project + client IP).
    if key_type == "public":
        ip_minute, ip_hour = (
            (_PLAYGROUND_IP_PER_MINUTE, _PLAYGROUND_IP_PER_HOUR)
            if plan == "admin"
            else _public_ip_caps(limits_cfg)
        )
        _enforce_windows(
            [
                (RateLimitItemPerMinute(ip_minute), ip_minute),
                (RateLimitItemPerHour(ip_hour), ip_hour),
            ],
            "pubip",
            project_id,
            client_ip,
        )
    if plan == "admin":
        return  # playground: per-IP backstop only, the admin plan has no tier

    # Per-project tier limits, namespaced by key_type so public and private
    # traffic do not share a bucket.
    _enforce_windows(_tier_windows(limits_cfg), "proj", project_id, key_type)


def enforce_ip(
    request: Request,
    scope: str,
    per_minute: int = None,
    per_hour: int = None,
) -> None:
    """Per-IP rate limit for endpoints with NO authenticated user — the widget
    token mint/refresh and the auth refresh-cookie flow, which bypass
    get_current_user and would otherwise be unlimited token mills.

    Self-gates like enforce(). Defaults to the token-mint env caps.
    """
    if not (_ENABLED and _LIMITS_AVAILABLE):
        return

    per_minute = per_minute or _MINT_PER_MINUTE
    per_hour = per_hour or _MINT_PER_HOUR
    client_ip = _client_ip(request)
    _enforce_windows(
        [
            (RateLimitItemPerMinute(per_minute), per_minute),
            (RateLimitItemPerHour(per_hour), per_hour),
        ],
        scope,
        client_ip,
    )
