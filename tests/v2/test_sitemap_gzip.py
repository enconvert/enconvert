"""Sitemap fetch: gzip (.xml.gz) decoding + WAF-hardened headers.

Hermetic pytest — httpx.AsyncClient is faked, no network.

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_sitemap_gzip.py -v
"""

import asyncio
import gzip
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

import httpx

from utils import sitemap


def _run(coro):
    return asyncio.run(coro)


_URLSET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://ex.test/a</loc></url>"
    "<url><loc>https://ex.test/b</loc></url>"
    "</urlset>"
)


class _FakeResponse:
    def __init__(self, *, content: bytes, headers: dict, status_code: int = 200):
        self.content = content
        self.headers = headers
        self.status_code = status_code
        self.encoding = None  # force the utf-8 fallback path

    @property
    def text(self):  # pragma: no cover - guards against the old .text path
        raise AssertionError("_fetch_xml must decode from .content, never .text")


class _FakeClient:
    """Records the headers the client was constructed with; serves one response."""

    captured_headers: dict = {}

    def __init__(self, *args, **kwargs):
        _FakeClient.captured_headers = dict(kwargs.get("headers") or {})
        self._response = _FakeClient.next_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return self._response


def _install(monkeypatch, response):
    _FakeClient.next_response = response
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def test_gzip_body_by_magic_bytes(monkeypatch):
    """A body served as application/gzip (magic bytes) is gunzipped, not mojibake'd."""
    gz = gzip.compress(_URLSET.encode("utf-8"))
    _install(monkeypatch, _FakeResponse(content=gz, headers={"content-type": "application/gzip"}))
    urls = _run(sitemap.parse_sitemap("https://ex.test/sitemap.xml.gz"))
    assert urls == ["https://ex.test/a", "https://ex.test/b"]


def test_gzip_body_by_gz_suffix(monkeypatch):
    """Suffix .gz + gzip magic decompresses even with a generic content-type."""
    gz = gzip.compress(_URLSET.encode("utf-8"))
    _install(monkeypatch, _FakeResponse(content=gz, headers={"content-type": "application/octet-stream"}))
    urls = _run(sitemap.parse_sitemap("https://ex.test/sitemap.xml.gz"))
    assert urls == ["https://ex.test/a", "https://ex.test/b"]


def test_plain_xml_unaffected(monkeypatch):
    """A normal, uncompressed XML sitemap still parses (no false gunzip)."""
    _install(monkeypatch, _FakeResponse(content=_URLSET.encode("utf-8"), headers={"content-type": "text/xml"}))
    urls = _run(sitemap.parse_sitemap("https://ex.test/sitemap.xml"))
    assert urls == ["https://ex.test/a", "https://ex.test/b"]


def test_waf_hardened_headers_sent(monkeypatch):
    """The sitemap client sends a browser-like User-Agent (not bare python-httpx)."""
    _install(monkeypatch, _FakeResponse(content=_URLSET.encode("utf-8"), headers={"content-type": "text/xml"}))
    _run(sitemap.parse_sitemap("https://ex.test/sitemap.xml"))
    ua = _FakeClient.captured_headers.get("User-Agent", "")
    assert "Mozilla/5.0" in ua and "Chrome/" in ua
    assert _FakeClient.captured_headers.get("Accept-Language")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
