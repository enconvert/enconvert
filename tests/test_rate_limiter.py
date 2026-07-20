"""Unit tests for rate_limiting.limiter.

These exercise the in-memory limiter directly with a fresh MemoryStorage per
test, a tiny limit config, and the feature flag forced on. A lightweight
SimpleNamespace stands in for the FastAPI Request (the limiter only reads
.method, .url.path and .client.host).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rate_limiting import limiter

# Small limits so tests exhaust a window in a couple of calls.
TINY_LIMITS = {
    "free": {
        "private": {"per_minute": 2, "per_hour": 100, "per_day": 1000},
        "public": {"per_minute": 2, "per_hour": 100, "per_day": 1000},
    },
}


def _request(method: str = "POST", path: str = "/v1/convert/html-to-pdf", ip: str = "1.2.3.4"):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host=ip),
    )


def _user(plan: str = "free", key_type: str = "private", pid: str = "42") -> dict:
    return {"id": pid, "key_type": key_type, "subscription": {"plan_slug": plan}}


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    if not limiter._LIMITS_AVAILABLE:
        pytest.skip("limits package not installed")
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    storage = storage_from_string("memory://")
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "_limiter", FixedWindowRateLimiter(storage))
    monkeypatch.setattr(limiter, "_ENABLED", True)
    monkeypatch.setattr(limiter, "RATE_LIMITS", TINY_LIMITS)


def _drain(user: dict, request, n: int) -> None:
    for _ in range(n):
        limiter.enforce(request, user)


def test_disabled_never_raises(monkeypatch):
    monkeypatch.setattr(limiter, "_ENABLED", False)
    req, user = _request(), _user()
    for _ in range(50):
        limiter.enforce(req, user)  # must not raise even far past the limit


def test_private_free_blocks_after_limit():
    req, user = _request(), _user(plan="free", key_type="private")
    _drain(user, req, 2)  # 2/min allowed
    with pytest.raises(HTTPException) as exc:
        limiter.enforce(req, user)
    assert exc.value.status_code == 429
    assert exc.value.headers["RateLimit-Limit"] == "2"
    assert exc.value.headers["RateLimit-Remaining"] == "0"
    assert int(exc.value.headers["RateLimit-Reset"]) >= 1
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_get_status_polling_is_exempt():
    req, user = _request(method="GET", path="/v1/convert/status/job_1"), _user()
    for _ in range(50):
        limiter.enforce(req, user)  # polling never throttled


def test_non_billable_post_is_exempt():
    req, user = _request(method="POST", path="/v1/whoami"), _user()
    for _ in range(50):
        limiter.enforce(req, user)


def test_extension_capture_is_throttled():
    req = _request(path="/v1/extension/capture")
    user = _user(plan="free", key_type="private")
    _drain(user, req, 2)
    with pytest.raises(HTTPException):
        limiter.enforce(req, user)


def test_admin_bypasses():
    req, user = _request(), _user(plan="admin")
    for _ in range(50):
        limiter.enforce(req, user)


def test_public_and_private_are_separate_buckets():
    req = _request()
    pub = _user(plan="free", key_type="public", pid="7")
    priv = _user(plan="free", key_type="private", pid="7")
    _drain(pub, req, 2)
    with pytest.raises(HTTPException):
        limiter.enforce(req, pub)
    # Same project id, but the private bucket is namespaced independently.
    limiter.enforce(req, priv)


def test_project_ceiling_applies_across_public_ips():
    # A public key's per-project window (2/min here) caps total usage even when
    # requests come from different visitor IPs.
    user = _user(plan="free", key_type="public", pid="9")
    _drain(user, _request(ip="1.1.1.1"), 2)
    with pytest.raises(HTTPException):
        limiter.enforce(_request(ip="2.2.2.2"), user)


def test_public_ip_backstop_engages_per_ip(monkeypatch):
    # Explicit env-style override: one IP capped at 2/min while the project
    # window (100/hr tier here) stays open — a second IP still passes.
    monkeypatch.setattr(limiter, "_PUBLIC_IP_PER_MINUTE", 2)
    monkeypatch.setattr(
        limiter, "RATE_LIMITS",
        {"free": {
            "private": {"per_minute": 100, "per_hour": 1000, "per_day": 10000},
            "public": {"per_minute": 100, "per_hour": 1000, "per_day": 10000},
        }},
    )
    user = _user(plan="free", key_type="public", pid="11")
    _drain(user, _request(ip="3.3.3.3"), 2)
    with pytest.raises(HTTPException):
        limiter.enforce(_request(ip="3.3.3.3"), user)
    limiter.enforce(_request(ip="4.4.4.4"), user)  # other visitors unaffected


def test_token_minting_uses_own_bucket(monkeypatch):
    # Mints are throttled per IP in their own bucket and must NOT consume the
    # tier windows — otherwise each visitor costs 2 units (mint + convert).
    monkeypatch.setattr(limiter, "_MINT_PER_MINUTE", 2)
    mint_req = _request(method="POST", path="/v1/auth/token")
    user = _user(plan="free", key_type="public")
    _drain(user, mint_req, 2)
    with pytest.raises(HTTPException):
        limiter.enforce(mint_req, user)
    # Tier windows untouched by the mints: both conversion slots still free.
    _drain(user, _request(), 2)
    with pytest.raises(HTTPException):
        limiter.enforce(_request(), user)


def test_enforce_ip_scoped_windows():
    req = _request(method="POST", path="/v1/widget/abc/token", ip="5.5.5.5")
    limiter.enforce_ip(req, "widget_mint", per_minute=2, per_hour=100)
    limiter.enforce_ip(req, "widget_mint", per_minute=2, per_hour=100)
    with pytest.raises(HTTPException) as exc:
        limiter.enforce_ip(req, "widget_mint", per_minute=2, per_hour=100)
    assert exc.value.status_code == 429
    # Different scope from the same IP is an independent bucket.
    limiter.enforce_ip(req, "widget_refresh", per_minute=2, per_hour=100)


def test_unknown_plan_falls_back_to_free():
    req = _request()
    user = _user(plan="enterprise_typo", key_type="private")
    _drain(user, req, 2)  # falls back to free's 2/min
    with pytest.raises(HTTPException):
        limiter.enforce(req, user)
