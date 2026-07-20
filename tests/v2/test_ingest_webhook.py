"""Sprint H.8 — HMAC-signed /v2/ingest completion webhooks.

Hermetic pytest (no live DB, no live network): the HMAC primitives are pure,
delivery is exercised against an in-memory ``httpx.MockTransport``, and the
orchestration layer (``ingest_flow.deliver_ingest_webhook``) runs with its
store / secret / SSRF-screen / transport dependencies monkeypatched. Async
entry points are driven with ``asyncio.run`` (the ``_run`` helper), matching
test_ingest.py — no pytest-asyncio plugin required.

What this file proves (plan H.8 verification a-d):
  (a) A valid signature verifies (sign -> verify round trip).
  (b) A tampered body / wrong secret is rejected.
  (c) A replayed (stale-timestamp) delivery is rejected; the timestamp is
      bound into the MAC so it cannot be moved without breaking the signature.
  (d) Retry exhaustion leaves webhook_delivered False AND records an alert;
      a 2xx flips webhook_delivered True. Each retry is independently signed.
  (+) Delivery-time SSRF screening blocks a private/internal webhook URL.
"""
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

import asyncio

import httpx
import pytest

from utils import callback_notifier as cn


def _run(coro):
    return asyncio.run(coro)


async def _no_sleep(_seconds: float) -> None:
    """Drop-in for asyncio.sleep so retry back-off is instant in tests."""
    return None


# ════════════════════════════════════════════════════════════════════════════
# (a)(b)(c) HMAC signing + constant-time verification
# ════════════════════════════════════════════════════════════════════════════


class TestSigning:
    SECRET = "whsec_unit_test_secret"
    BODY = b'{"job_id":"ing_abc","status":"completed","total_chunks":7}'

    def test_sign_is_deterministic_and_hex(self):
        ts = "1717000000"
        a = cn.sign_payload(self.BODY, self.SECRET, ts)
        b = cn.sign_payload(self.BODY, self.SECRET, ts)
        assert a == b
        assert len(a) == 64
        int(a, 16)  # raises if not hex

    def test_headers_then_verify_roundtrip(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert headers[cn.SIGNATURE_HEADER].startswith("sha256=")
        assert headers[cn.TIMESTAMP_HEADER] == "1717000000"
        assert cn.verify_signature(
            self.BODY,
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            now=1717000000,
        )

    def test_tampered_body_rejected(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert not cn.verify_signature(
            self.BODY + b"x",
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            now=1717000000,
        )

    def test_wrong_secret_rejected(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert not cn.verify_signature(
            self.BODY,
            "whsec_attacker",
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            now=1717000000,
        )

    def test_replay_outside_window_rejected(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        # 301 s later: just past the 300 s default tolerance.
        assert not cn.verify_signature(
            self.BODY,
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            now=1717000000 + cn.DEFAULT_REPLAY_TOLERANCE_SECONDS + 1,
        )

    def test_within_window_accepted(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert cn.verify_signature(
            self.BODY,
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            now=1717000000 + cn.DEFAULT_REPLAY_TOLERANCE_SECONDS - 1,
        )

    def test_moved_timestamp_breaks_signature(self):
        # The signature is over "<ts>.<body>"; changing only the timestamp
        # header (a naive replay) invalidates the MAC even inside the window.
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert not cn.verify_signature(
            self.BODY,
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            "1717000050",  # different ts, original signature
            now=1717000050,
        )

    def test_malformed_headers_rejected(self):
        good = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1717000000)
        assert not cn.verify_signature(self.BODY, self.SECRET, None, "1717000000")
        assert not cn.verify_signature(self.BODY, self.SECRET, good[cn.SIGNATURE_HEADER], None)
        assert not cn.verify_signature(
            self.BODY, self.SECRET, good[cn.SIGNATURE_HEADER], "not-an-int"
        )

    def test_tolerance_none_skips_freshness(self):
        headers = cn.build_signature_headers(self.BODY, self.SECRET, timestamp=1000)
        assert cn.verify_signature(
            self.BODY,
            self.SECRET,
            headers[cn.SIGNATURE_HEADER],
            headers[cn.TIMESTAMP_HEADER],
            tolerance_seconds=None,
            now=10_000_000,
        )


# ════════════════════════════════════════════════════════════════════════════
# (d) deliver_signed_webhook — retry / back-off / per-attempt signing
# ════════════════════════════════════════════════════════════════════════════


def _capturing_transport(status_sequence):
    """MockTransport returning the given status codes in order (last repeats);
    records each request's body + signature headers for assertions."""
    seen = []
    seq = list(status_sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "body": request.content,
                "signature": request.headers.get(cn.SIGNATURE_HEADER),
                "timestamp": request.headers.get(cn.TIMESTAMP_HEADER),
                "content_type": request.headers.get("Content-Type"),
            }
        )
        code = seq[min(len(seen) - 1, len(seq) - 1)]
        return httpx.Response(code, json={"ok": code < 300})

    return httpx.MockTransport(handler), seen


class TestDelivery:
    SECRET = "whsec_delivery"
    BODY = b'{"job_id":"ing_d","status":"completed"}'

    def _deliver(self, transport, **kwargs):
        async def _go():
            async with httpx.AsyncClient(transport=transport) as client:
                return await cn.deliver_signed_webhook(
                    "https://hooks.example.com/ingest",
                    self.BODY,
                    self.SECRET,
                    client=client,
                    sleep=_no_sleep,
                    **kwargs,
                )

        return _run(_go())

    def test_first_attempt_success(self):
        transport, seen = _capturing_transport([200])
        result = self._deliver(transport)
        assert result.delivered is True
        assert result.status_code == 200
        assert result.attempts == 1
        assert len(seen) == 1
        assert seen[0]["content_type"] == "application/json"

    def test_retries_then_succeeds(self):
        transport, seen = _capturing_transport([500, 503, 200])
        result = self._deliver(transport)
        assert result.delivered is True
        assert result.status_code == 200
        assert result.attempts == 3
        assert len(seen) == 3

    def test_exhaustion_after_all_retries(self):
        # Default back-off has 3 entries -> 1 initial + 3 retries = 4 attempts.
        transport, seen = _capturing_transport([500])
        result = self._deliver(transport)
        assert result.delivered is False
        assert result.status_code == 500
        assert result.attempts == 1 + len(cn.WEBHOOK_RETRY_BACKOFF_SECONDS)
        assert result.error == "http_500"
        assert len(seen) == result.attempts

    def test_each_attempt_independently_signed(self):
        transport, seen = _capturing_transport([500, 200])
        result = self._deliver(transport)
        assert result.delivered is True
        # Every POST carries a signature valid for the exact body+ts it sent,
        # so a slow retry chain never ships a stale signature.
        for record in seen:
            assert cn.verify_signature(
                record["body"],
                self.SECRET,
                record["signature"],
                record["timestamp"],
                tolerance_seconds=None,
            )

    def test_network_error_is_retried_and_reported(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        transport = httpx.MockTransport(handler)
        result = self._deliver(transport)
        assert result.delivered is False
        assert result.status_code is None
        assert result.error and result.error.startswith("request_error")
        assert result.attempts == 1 + len(cn.WEBHOOK_RETRY_BACKOFF_SECONDS)

    def test_empty_secret_short_circuits(self):
        transport, seen = _capturing_transport([200])

        async def _go():
            async with httpx.AsyncClient(transport=transport) as client:
                return await cn.deliver_signed_webhook(
                    "https://hooks.example.com/x", self.BODY, "", client=client
                )

        result = _run(_go())
        assert result.delivered is False
        assert result.error == "no_secret"
        assert result.attempts == 0
        assert seen == []


# ════════════════════════════════════════════════════════════════════════════
# (d)(+) ingest_flow.deliver_ingest_webhook — bookkeeping + SSRF screen
# ════════════════════════════════════════════════════════════════════════════


def _make_job(**overrides):
    from models import IngestJob

    fields = dict(
        job_id="ing_flow",
        project_id=77,
        mode="urls",
        status="completed",
        pages_processed=3,
        pages_failed=0,
        total_chunks=11,
        output_key="proj/77/v2-ingest/ing_flow.jsonl",
        webhook_url="https://hooks.example.com/done",
        webhook_delivered=False,
    )
    fields.update(overrides)
    return IngestJob(**fields)


class _FlowSpies:
    """Captures the store mutations deliver_ingest_webhook performs."""

    def __init__(self):
        self.delivered_calls = []
        self.alert_calls = []

    def install(self, monkeypatch):
        from services.v2_engine import ingest_flow

        monkeypatch.setattr(
            ingest_flow.ingest_store,
            "set_webhook_delivered",
            lambda job_id, delivered: self.delivered_calls.append((job_id, delivered)),
        )
        monkeypatch.setattr(
            ingest_flow.ingest_store,
            "record_webhook_failure_alert",
            lambda job, attempts, reason=None: self.alert_calls.append(
                (job.job_id, attempts)
            ),
        )
        monkeypatch.setattr(
            ingest_flow,
            "generate_presigned_url",
            lambda key, project: f"https://signed.example/{project}/{key}",
        )
        monkeypatch.setattr(
            ingest_flow.webhook_secret,
            "get_or_create_webhook_secret",
            lambda project_id: "whsec_flow_secret",
        )


class TestIngestWebhookFlow:
    def test_delivered_marks_true_no_alert(self, monkeypatch):
        from services.v2_engine import ingest_flow

        spies = _FlowSpies()
        spies.install(monkeypatch)

        async def _ok_public(_url):
            return None

        captured = {}

        async def _deliver(url, body, secret, **kwargs):
            captured["url"] = url
            captured["body"] = body
            captured["secret"] = secret
            return cn.WebhookDeliveryResult(True, 200, 1, None)

        monkeypatch.setattr(ingest_flow, "assert_public_http_url", _ok_public)
        monkeypatch.setattr(ingest_flow, "deliver_signed_webhook", _deliver)

        result = _run(ingest_flow.deliver_ingest_webhook(_make_job()))

        assert result.delivered is True
        assert spies.delivered_calls == [("ing_flow", True)]
        assert spies.alert_calls == []
        # Payload carries the H.8 fields + a signed output URL.
        import json

        payload = json.loads(captured["body"])
        assert payload["job_id"] == "ing_flow"
        assert payload["status"] == "completed"
        assert payload["pages_processed"] == 3
        assert payload["total_chunks"] == 11
        assert payload["output_url"].startswith("https://signed.example/")
        assert captured["secret"] == "whsec_flow_secret"

    def test_exhaustion_marks_false_and_alerts(self, monkeypatch):
        from services.v2_engine import ingest_flow

        spies = _FlowSpies()
        spies.install(monkeypatch)

        async def _ok_public(_url):
            return None

        async def _deliver(url, body, secret, **kwargs):
            return cn.WebhookDeliveryResult(False, 500, 4, "http_500")

        monkeypatch.setattr(ingest_flow, "assert_public_http_url", _ok_public)
        monkeypatch.setattr(ingest_flow, "deliver_signed_webhook", _deliver)

        result = _run(ingest_flow.deliver_ingest_webhook(_make_job()))

        assert result.delivered is False
        assert spies.delivered_calls == [("ing_flow", False)]
        assert spies.alert_calls == [("ing_flow", 4)]

    def test_ssrf_screen_blocks_internal_url(self, monkeypatch):
        # Uses the REAL assert_public_http_url against a loopback literal — no
        # DNS, deterministic reject. Delivery must never be attempted.
        from services.v2_engine import ingest_flow

        spies = _FlowSpies()
        spies.install(monkeypatch)

        called = {"deliver": False}

        async def _deliver(*a, **k):
            called["deliver"] = True
            return cn.WebhookDeliveryResult(True, 200, 1, None)

        monkeypatch.setattr(ingest_flow, "deliver_signed_webhook", _deliver)

        job = _make_job(webhook_url="http://127.0.0.1:9000/hook")
        result = _run(ingest_flow.deliver_ingest_webhook(job))

        assert result.delivered is False
        assert result.error == "blocked_url"
        assert called["deliver"] is False
        assert spies.delivered_calls == [("ing_flow", False)]
        # A blocked (internal) URL is a permanent misconfiguration — alert it.
        assert spies.alert_calls == [("ing_flow", 0)]

    def test_no_webhook_url_is_noop(self, monkeypatch):
        from services.v2_engine import ingest_flow

        spies = _FlowSpies()
        spies.install(monkeypatch)

        result = _run(ingest_flow.deliver_ingest_webhook(_make_job(webhook_url=None)))
        assert result.delivered is False
        assert result.error == "no_webhook"
        assert spies.delivered_calls == []
