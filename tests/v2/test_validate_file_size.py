"""Unit tests for api.deps.validate_file_size.

Proves the per-plan ceiling is enforced from the bytes actually received rather
than the Content-Length header — a chunked / HTTP2 client can omit that header
entirely, which previously skipped the size check altogether.

Uploads are built by driving Starlette's REAL MultiPartParser with a chunked
feed and NO content-length, so the tests exercise the same code path that
populates ``UploadFile.size`` in production.

Pure pytest (no pytest-asyncio, per the repo convention) — the async parse is
driven with ``asyncio.run``.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_validate_file_size.py -q
"""

from __future__ import annotations

import asyncio
import os

# api.deps pulls in the DB module, which builds an engine at import time from
# DATABASE_URL. These tests never touch the DB, so a dummy URL keeps the import
# working (create_engine is lazy and never connects). Same pattern as
# tests/v2/test_endpoint_allowlist.py.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_dummy")

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.requests import Request

from api.deps import validate_file_size

_BOUNDARY = "testboundary1234"


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/convert/anything-to-pdf",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _upload(nbytes: int, name: str = "a.bin") -> UploadFile:
    """Parse a real multipart body of `nbytes`, fed in chunks with no length."""

    async def _build() -> UploadFile:
        body = (
            f"--{_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"x" * nbytes + f"\r\n--{_BOUNDARY}--\r\n".encode()

        async def stream():
            for i in range(0, len(body), 4096):
                yield body[i:i + 4096]

        headers = Headers({"content-type": f"multipart/form-data; boundary={_BOUNDARY}"})
        form = await MultiPartParser(headers, stream()).parse()
        return form["file"]

    return asyncio.run(_build())


def _user(max_size: int = 1000, plan: str = "free") -> dict:
    return {
        "id": "proj_test",
        "key_type": "api",
        "subscription": {"max_file_size": max_size, "plan_slug": plan},
    }


@pytest.fixture(autouse=True)
def _no_posthog(monkeypatch):
    """Capture gate events instead of emitting them; keeps tests hermetic."""
    events = []
    monkeypatch.setattr(
        "api.deps._gate_capture",
        lambda user, event, properties: events.append((event, properties)),
    )
    return events


def test_upload_size_is_populated_without_content_length():
    # The premise the fix rests on: Starlette counts the bytes for us.
    assert _upload(5000).size == 5000


def test_oversized_upload_rejected_when_content_length_absent(_no_posthog):
    # THE regression test. Pre-fix this returned None and the file converted.
    with pytest.raises(HTTPException) as excinfo:
        validate_file_size(_request({}), _user(max_size=1000), _upload(5000))
    assert excinfo.value.status_code == 413
    # Pins the analytics contract: the event still fires, with the true size.
    assert _no_posthog[0][0] == "upload_rejected_oversized"
    assert _no_posthog[0][1]["attempted_file_size_bytes"] == 5000


def test_oversized_upload_rejected_when_content_length_lies():
    # file.size must win over a header claiming the body is tiny.
    with pytest.raises(HTTPException) as excinfo:
        validate_file_size(
            _request({"content-length": "10"}), _user(max_size=1000), _upload(5000)
        )
    assert excinfo.value.status_code == 413


def test_under_limit_upload_accepted_when_content_length_absent():
    assert validate_file_size(_request({}), _user(max_size=10_000), _upload(5000)) is None


def test_413_detail_shape_unchanged():
    # 51 routes and the frontend parse this dict; it must not drift.
    with pytest.raises(HTTPException) as excinfo:
        validate_file_size(_request({}), _user(max_size=1000), _upload(5000))
    assert excinfo.value.detail == {
        "error": "File too large",
        "file_size": 5000,
        "max_size": 1000,
        "tier": "free",
        "key_type": "api",
    }


def test_file_at_exact_limit_accepted():
    # Documents the intentional fix of the old envelope-inclusive strictness:
    # Content-Length counted boundaries + form fields, so a file at exactly
    # max_size used to 413.
    assert validate_file_size(_request({}), _user(max_size=5000), _upload(5000)) is None


def test_header_fallback_still_enforced_without_file():
    # Proves the 50 un-migrated call sites keep working unchanged.
    with pytest.raises(HTTPException) as excinfo:
        validate_file_size(_request({"content-length": "5000"}), _user(max_size=1000))
    assert excinfo.value.status_code == 413


def test_missing_header_and_no_file_is_permitted():
    # Pins the documented legacy fallback so the follow-up migration has a
    # baseline: without a file and without a header there is nothing to check.
    assert validate_file_size(_request({}), _user(max_size=1000)) is None


def test_malformed_content_length_does_not_raise_value_error():
    # Would otherwise surface as a 500.
    assert validate_file_size(_request({"content-length": "abc"}), _user(max_size=1000)) is None


def test_admin_unlimited_upload_accepted():
    user = _user(max_size=999999999, plan="admin")
    assert validate_file_size(_request({}), user, _upload(5000)) is None


def test_missing_subscription_falls_back_to_free_default():
    # 5242880 free default (api/deps.py), enforced even with no subscription.
    with pytest.raises(HTTPException) as excinfo:
        validate_file_size(_request({}), {"id": "p"}, _upload(6_000_000))
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail["max_size"] == 5242880
