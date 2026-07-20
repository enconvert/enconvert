"""TimeoutMiddleware — real cancellation semantics (raw ASGI rewrite).

The old BaseHTTPMiddleware variant 504'd the HTTP response but never
cancelled the request task; a hung render kept the browser slot for days.
These tests pin the new contract:

* timeout with nothing sent -> 504 AND the request task is truly cancelled
  (held resources — e.g. the conversion-slot semaphore — are released);
* a response that STARTS within budget streams to completion unbounded
  (download-proxy semantics, identical to the old call_next behavior);
* fast requests and app exceptions pass through untouched.

Hermetic: hand-driven ASGI scope/receive/send, no server, no DB (the
activity/PostHog bookkeeping is stubbed out per test).
"""
import asyncio
from typing import Any, Optional

import pytest

from middleware.timeout import TimeoutMiddleware


def make_scope() -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v2/perceive",
        "raw_path": b"/v2/perceive",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "state": {},
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def run_through(
    app: Any, timeout: float, scope: Optional[dict] = None
) -> list[dict]:
    """Drive the middleware over a fake ASGI app; return sent messages."""
    middleware = TimeoutMiddleware(app, timeout=timeout)
    # The bookkeeping path imports monitoring modules (DB/PostHog); stub it —
    # its logic is exercised by the existing conversion tests, not here.
    fired: list[Any] = []

    async def fake_bookkeeping(request: Any) -> None:
        fired.append(request)

    middleware._fail_timed_out_activity = fake_bookkeeping  # type: ignore[method-assign]

    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(middleware(scope or make_scope(), _receive, send))
    messages_status = [
        m["status"] for m in messages if m["type"] == "http.response.start"
    ]
    assert len(messages_status) <= 1, "middleware must never double-respond"
    return messages


def status_of(messages: list[dict]) -> int:
    return next(
        m["status"] for m in messages if m["type"] == "http.response.start"
    )


class TestTimeoutMiddleware:
    def test_fast_request_passes_through(self):
        async def app(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b"ok"})

        messages = run_through(app, timeout=1.0)
        assert status_of(messages) == 200
        assert b"ok" in b"".join(
            m.get("body", b"") for m in messages
        )

    def test_timeout_returns_504(self):
        async def app(scope, receive, send):
            await asyncio.Event().wait()  # hangs forever, sends nothing

        messages = run_through(app, timeout=0.05)
        assert status_of(messages) == 504
        assert b"Request timeout" in b"".join(
            m.get("body", b"") for m in messages
        )

    def test_timeout_actually_cancels_the_request_task(self):
        """THE incident regression test: the hung task must be cancelled."""
        witness = {"cancelled": False}

        async def app(scope, receive, send):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                witness["cancelled"] = True
                raise

        messages = run_through(app, timeout=0.05)
        assert status_of(messages) == 504
        assert witness["cancelled"] is True

    def test_timeout_releases_held_semaphore_slot(self):
        """Cancellation must release resources held via async-with — the
        browser conversion slot is exactly this shape. Before the rewrite
        the slot stayed held for days after the 504."""
        slot = asyncio.Semaphore(1)

        async def app(scope, receive, send):
            async with slot:  # acquires the only slot, like crawler_slot()
                await asyncio.Event().wait()

        messages = run_through(app, timeout=0.05)
        assert status_of(messages) == 504
        assert slot._value == 1, "conversion slot must be released on timeout"

    def test_started_response_streams_past_the_ceiling(self):
        """Time-to-first-byte is bounded; body streaming is not (download
        proxy to a slow client). Identical to old call_next semantics."""
        async def app(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send(
                {"type": "http.response.body", "body": b"a", "more_body": True}
            )
            await asyncio.sleep(0.15)  # 3x the ceiling below
            await send(
                {"type": "http.response.body", "body": b"b", "more_body": False}
            )

        messages = run_through(app, timeout=0.05)
        assert status_of(messages) == 200
        assert b"".join(m.get("body", b"") for m in messages) == b"ab"

    def test_app_exception_propagates(self):
        async def app(scope, receive, send):
            raise ValueError("handler exploded")

        middleware = TimeoutMiddleware(app, timeout=1.0)

        async def send(message: dict) -> None:  # pragma: no cover - unused
            pass

        with pytest.raises(ValueError):
            asyncio.run(middleware(make_scope(), _receive, send))

    def test_non_http_scope_passes_through(self):
        seen = {"called": False}

        async def app(scope, receive, send):
            seen["called"] = True

        middleware = TimeoutMiddleware(app, timeout=0.01)
        asyncio.run(middleware({"type": "lifespan"}, _receive, lambda m: None))
        assert seen["called"] is True

    def test_bookkeeping_runs_on_timeout_only(self):
        calls: list[Any] = []

        async def slow_app(scope, receive, send):
            await asyncio.Event().wait()

        async def fast_app(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})

        for app, timeout in ((slow_app, 0.05), (fast_app, 1.0)):
            middleware = TimeoutMiddleware(app, timeout=timeout)

            async def bookkeeping(request: Any, _app=app) -> None:
                calls.append(_app)

            middleware._fail_timed_out_activity = bookkeeping  # type: ignore[method-assign]

            async def send(message: dict) -> None:
                pass

            asyncio.run(middleware(make_scope(), _receive, send))

        assert calls == [slow_app]
