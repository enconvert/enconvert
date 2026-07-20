"""In-process per-key / per-IP request-rate limiting for the gateway.

Backed by the `limits` library with in-memory storage (``memory://``), which is
correct for the single-worker gateway. Scaling past one worker/droplet needs a
shared backend AND an async migration: switch to ``limits.aio`` (async storage
+ strategy, e.g. ``async+redis://``), make ``enforce`` async and await it in
deps.py, and add the redis package to requirements. Do NOT simply point
``RATE_LIMIT_STORAGE_URI`` at ``redis://`` — the sync client would do blocking
network I/O on the event loop for every request.

These are short-window FAIRNESS limits (HTTP 429), separate from the monthly
conversion quotas enforced in api/deps.py (HTTP 402). Enforcement self-gates on
RATE_LIMITING_ENABLED so the wiring can ship inert and be switched on in
production when ready. The `limits` import is guarded so the gateway still
starts if the package is not yet installed, and a bad storage URI disables
limiting (with a logged error) instead of preventing boot.
"""
import logging
import os
import time

from fastapi import HTTPException, Request

from config import RATE_LIMITS

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

_ENABLED = os.getenv("RATE_LIMITING_ENABLED", "false").lower() == "true"
_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

# Per-IP backstop for PUBLIC-key traffic. A public (pk_) key is shared by every
# one of a customer's browser visitors, so the per-project limits below cannot
# single out one abusive visitor. 0 = auto: half the tier's public per-minute/
# per-hour window (min 3/min), so the backstop always engages BELOW the shared
# project window on every tier.
_PUBLIC_IP_PER_MINUTE = int(os.getenv("PUBLIC_IP_RATE_PER_MINUTE", "0"))
_PUBLIC_IP_PER_HOUR = int(os.getenv("PUBLIC_IP_RATE_PER_HOUR", "0"))

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


def enforce(request: Request, user: dict) -> None:
    """Raise HTTP 429 if the caller exceeded its plan's request-rate limit.

    No-op unless rate limiting is enabled (and the `limits` package is present)
    and the request is a billable POST. Admin projects bypass. Safe to call on
    every authenticated request.
    """
    if not (_ENABLED and _LIMITS_AVAILABLE):
        return
    if not _should_limit(request.method, request.url.path):
        return

    sub = user.get("subscription", {})
    plan = sub.get("plan_slug", "free")
    if plan == "admin":
        return

    key_type = "public" if user.get("key_type") == "public" else "private"
    tier_cfg = RATE_LIMITS.get(plan, RATE_LIMITS["free"])
    limits_cfg = tier_cfg.get(key_type, tier_cfg["private"])

    project_id = str(user.get("id", "anonymous"))
    client_ip = request.client.host if request.client else "unknown"

    # Token minting: own per-IP bucket, never charged to the tier windows.
    if request.url.path.endswith("/auth/token"):
        _enforce_windows(
            [
                (RateLimitItemPerMinute(_MINT_PER_MINUTE), _MINT_PER_MINUTE),
                (RateLimitItemPerHour(_MINT_PER_HOUR), _MINT_PER_HOUR),
            ],
            "mint",
            project_id,
            client_ip,
        )
        return

    # Per-IP backstop for shared public keys (keyed by project + client IP).
    if key_type == "public":
        ip_minute, ip_hour = _public_ip_caps(limits_cfg)
        _enforce_windows(
            [
                (RateLimitItemPerMinute(ip_minute), ip_minute),
                (RateLimitItemPerHour(ip_hour), ip_hour),
            ],
            "pubip",
            project_id,
            client_ip,
        )

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
    client_ip = request.client.host if request.client else "unknown"
    _enforce_windows(
        [
            (RateLimitItemPerMinute(per_minute), per_minute),
            (RateLimitItemPerHour(per_hour), per_hour),
        ],
        scope,
        client_ip,
    )
