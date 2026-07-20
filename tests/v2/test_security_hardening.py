"""Security hardening: local threat policy, SSRF threat wiring, IP-pin helper,
DNS-rebind revalidation primitive.

Hermetic pytest — no network (DNS is monkeypatched).

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_security_hardening.py -v
"""

import asyncio
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

import pytest
from fastapi import HTTPException

from services.v2_engine import threat_policy, url_safety


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_policy():
    """Ensure a clean, disabled-by-default-empty policy around each test."""
    threat_policy.reload()
    yield
    threat_policy.reload()


def _apply_policy(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    threat_policy.reload()


# ── threat_policy ────────────────────────────────────────────────────────


class TestThreatPolicy:
    def test_blocks_exact_domain_and_subdomain(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_DOMAINS="evil.com, bad.example.org")
        assert threat_policy.check_host("evil.com") is not None
        assert threat_policy.check_host("a.b.evil.com") is not None
        assert threat_policy.check_host("notevil.com") is None
        assert threat_policy.check_host("evil.com.good.net") is None

    def test_blocks_tld(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_TLDS="zip,internal")
        assert threat_policy.check_host("foo.zip") is not None
        assert threat_policy.check_host("host.internal") is not None
        assert threat_policy.check_host("foo.com") is None

    def test_disabled_blocks_nothing(self, monkeypatch):
        _apply_policy(
            monkeypatch, THREAT_POLICY_ENABLED="0", THREAT_BLOCKED_DOMAINS="evil.com"
        )
        assert threat_policy.check_host("evil.com") is None

    def test_assert_allowed_raises_and_audits(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_DOMAINS="evil.com")
        before = len(threat_policy.audit_log())
        with pytest.raises(HTTPException) as exc:
            threat_policy.assert_allowed("https://evil.com/x?secret=1", "evil.com")
        assert exc.value.status_code == 400
        audit = threat_policy.audit_log()
        assert len(audit) == before + 1
        assert audit[-1]["host"] == "evil.com"
        assert audit[-1]["rule"] == "domain"
        # The audit records the host only — never the full URL (query secret).
        assert "secret" not in str(audit[-1])

    def test_empty_policy_allows(self, monkeypatch):
        _apply_policy(monkeypatch)  # no rules
        assert threat_policy.check_host("anything.com") is None


# ── url_safety wiring ────────────────────────────────────────────────────


class TestSsrfThreatWiring:
    def test_threat_policy_blocks_in_assert_public(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_DOMAINS="blocked.example.com")
        # No DNS needed — the threat check runs before resolution.
        with pytest.raises(HTTPException) as exc:
            _run(url_safety.assert_public_http_url("https://blocked.example.com/p"))
        assert exc.value.status_code == 400

    def test_clean_url_passes_threat_then_dns(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_DOMAINS="other.com")

        async def fake_resolve(host):
            return ["93.184.216.34"]  # public

        monkeypatch.setattr(url_safety, "_resolve_host", fake_resolve)
        # Should not raise.
        _run(url_safety.assert_public_http_url("https://example.com/p"))


# ── IP-pin helper ────────────────────────────────────────────────────────


class TestPublicIpForHost:
    def test_public_literal_returned(self):
        assert _run(url_safety.public_ip_for_host("8.8.8.8")) == "8.8.8.8"

    def test_private_literal_raises(self):
        with pytest.raises(HTTPException):
            _run(url_safety.public_ip_for_host("127.0.0.1"))
        with pytest.raises(HTTPException):
            _run(url_safety.public_ip_for_host("10.1.2.3"))

    def test_hostname_returns_first_public(self, monkeypatch):
        async def fake_resolve(host):
            return ["93.184.216.34", "93.184.216.35"]

        monkeypatch.setattr(url_safety, "_resolve_host", fake_resolve)
        assert _run(url_safety.public_ip_for_host("example.com")) == "93.184.216.34"

    def test_hostname_resolving_private_raises(self, monkeypatch):
        async def fake_resolve(host):
            return ["192.168.0.5"]

        monkeypatch.setattr(url_safety, "_resolve_host", fake_resolve)
        with pytest.raises(HTTPException):
            _run(url_safety.public_ip_for_host("rebind.example.com"))


# ── DNS-rebind revalidation primitive (browser route guard) ──────────────


class TestIsHostPublic:
    def test_public_host_allowed(self, monkeypatch):
        async def fake_resolve(host):
            return ["93.184.216.34"]

        monkeypatch.setattr(url_safety, "_resolve_host", fake_resolve)
        assert _run(url_safety.is_host_public("example.com")) is True

    def test_private_literal_blocked(self):
        # A host that rebinds to a private literal is rejected at request time.
        assert _run(url_safety.is_host_public("169.254.169.254")) is False
        assert _run(url_safety.is_host_public("10.0.0.1")) is False

    def test_empty_host_blocked(self):
        assert _run(url_safety.is_host_public("")) is False

    def test_threat_blocked_host_denied(self, monkeypatch):
        _apply_policy(monkeypatch, THREAT_BLOCKED_DOMAINS="evil.com")
        assert _run(url_safety.is_host_public("evil.com")) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
