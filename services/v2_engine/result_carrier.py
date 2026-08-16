"""Per-request render-artifact carrier for hook-based converters.

Crawl4AI hook callbacks cannot return artifacts to the calling handler
directly: ``arun()`` only returns the ``CrawlResult``. Converters that
render inside hooks (Sprint F.1: ``url_pdf``; F.2 adds screenshots and
markdown) stash their bytes here from inside their hooks and pop them
after ``arun()`` returns.

Converters stash from the tail of their ``after_goto`` hook (render
parity: crawl4ai mutates the DOM after that point — see url_pdf.py).
Keys are ``(url, request_id)`` where ``request_id`` is a per-request
uuid generated at converter entry, so concurrent requests for the same
URL can never collide (plan section A3/A6). The converter is responsible
for draining its key on every exit path — ``pop_result()`` removes the
entry, and converters call it in a ``finally`` so a failed render cannot
leak entries.

The dict is intentionally a plain module-level singleton and needs no
locking regardless of the BrowserManager semaphore: keys are unique per
request (uuid), so concurrent tasks can never collide on a key, and each
stash/pop is a single dict operation with no await between read and
write — atomic on a single-threaded asyncio loop. The only real
constraint is single-loop/single-process: if multi-loop or multi-process
workers ever share this module, revisit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

CarrierKey = Tuple[str, str]


@dataclass
class RenderResult:
    """Artifacts produced inside Crawl4AI hooks for a single request.

    F.1 populates ``pdf_bytes``; F.2 adds ``screenshot_bytes`` plus the
    markdown flow fields. The markdown converter stashes the rendered
    ``html`` + ``final_url`` from its hook and computes
    ``markdown_bytes`` / ``fit_markdown_bytes`` AFTER ``arun()`` returns,
    outside the browser slot — V1 also converted outside the browser
    context, and the conversion is CPU-bound. ``fit_markdown_bytes`` is
    the Crawl4AI Fit Markdown for V2 endpoints; V1 ignores it.
    """

    pdf_bytes: Optional[bytes] = None
    screenshot_bytes: Optional[bytes] = None
    html: Optional[str] = None
    final_url: Optional[str] = None
    markdown_bytes: Optional[bytes] = None
    fit_markdown_bytes: Optional[bytes] = None
    # F.5: /v2/perceive can request the viewport screenshot alongside
    # the full-page one; V1 converters never set this.
    screenshot_viewport_bytes: Optional[bytes] = None
    # Non-HTML navigation results (``text/plain``, ``application/json``).
    # ``html`` then holds the DECODED BODY VERBATIM, not markup: a
    # downstream HTML->Markdown pass would collapse its newlines and
    # destroy the document (a 99 KB llms.txt became one 99 KB line, which
    # then chunked to nothing). Consumers must check this before treating
    # ``html`` as HTML. None = an ordinary HTML page.
    content_category: Optional[str] = None
    raw_content_type: Optional[str] = None


_results: Dict[CarrierKey, RenderResult] = {}


def stash_pdf(url: str, request_id: str, pdf_bytes: bytes) -> None:
    """Record PDF bytes for ``(url, request_id)`` from inside a hook."""
    entry = _results.setdefault((url, request_id), RenderResult())
    entry.pdf_bytes = pdf_bytes


def stash_screenshot(url: str, request_id: str, screenshot_bytes: bytes) -> None:
    """Record full-page PNG bytes for ``(url, request_id)`` from a hook."""
    entry = _results.setdefault((url, request_id), RenderResult())
    entry.screenshot_bytes = screenshot_bytes


def stash_screenshot_viewport(
    url: str, request_id: str, screenshot_bytes: bytes
) -> None:
    """Record viewport (non-full-page) PNG bytes from a hook (F.5).

    /v2/perceive can request both the full-page and the viewport
    screenshot from one render; this keeps the carrier the single
    transport for every hook-produced artifact.
    """
    entry = _results.setdefault((url, request_id), RenderResult())
    entry.screenshot_viewport_bytes = screenshot_bytes


def stash_page_html(url: str, request_id: str, html: str, final_url: str) -> None:
    """Record the rendered HTML + final URL for post-arun conversion."""
    entry = _results.setdefault((url, request_id), RenderResult())
    entry.html = html
    entry.final_url = final_url


def stash_content_category(
    url: str,
    request_id: str,
    category: Optional[str],
    raw_content_type: Optional[str],
) -> None:
    """Record that the navigation returned a non-HTML body (from a hook).

    Paired with ``stash_page_html``, whose ``html`` argument then carries
    the decoded body verbatim rather than markup.
    """
    entry = _results.setdefault((url, request_id), RenderResult())
    entry.content_category = category
    entry.raw_content_type = raw_content_type


def pop_result(url: str, request_id: str) -> Optional[RenderResult]:
    """Remove and return the entry for ``(url, request_id)``.

    Returns None when the hooks never stashed anything (navigation or
    hook failure). Always call this on every converter exit path so the
    carrier cannot accumulate entries.
    """
    return _results.pop((url, request_id), None)


def pending_count() -> int:
    """Number of undelivered entries. Diagnostic / test helper only."""
    return len(_results)
