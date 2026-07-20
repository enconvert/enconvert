"""Typed conversion errors and a soft-vs-hard failure taxonomy.

Independently authored for the V3 browser-hardening work. Before this
module every URL-conversion failure — a target that timed out, a target
that was unreachable, a page that rendered nothing, or a genuine bug in
our render code — collapsed into a bare ``RuntimeError`` that surfaced as
an opaque HTTP 500. Callers (and on-call) could not tell "the target site
misbehaved / the input is unsupported" (their problem, a 4xx/502/504)
apart from "our engine faulted" (our problem, a 500).

``ConversionError`` carries an HTTP status, a stable machine ``code``, and
a human ``detail``. ``main.py`` renders it into a structured JSON envelope.
The status -> message table and every message string here are our own; no
third-party code is referenced.
"""

from __future__ import annotations

from typing import Any, Optional

# Our own HTTP-status -> short human label table (authored from scratch).
STATUS_MESSAGES: dict[int, str] = {
    400: "Bad Request",
    413: "Payload Too Large",
    415: "Unsupported Content Type",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class ConversionError(Exception):
    """Base class for URL-conversion failures with an HTTP mapping.

    ``status_code`` distinguishes soft failures (the target/input is at
    fault: 4xx/502/504) from hard failures (our engine: 500). ``code`` is
    a stable, machine-readable slug clients can branch on without parsing
    ``detail``.
    """

    status_code: int = 500
    code: str = "conversion_error"

    def __init__(
        self,
        detail: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        upstream_status: Optional[int] = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.upstream_status = upstream_status

    def to_envelope(self) -> dict[str, Any]:
        """Structured body for the HTTP response."""
        body: dict[str, Any] = {
            "error": STATUS_MESSAGES.get(self.status_code, "Error"),
            "code": self.code,
            "detail": self.detail,
        }
        if self.upstream_status is not None:
            body["upstream_status"] = self.upstream_status
        return body


class UpstreamTimeoutError(ConversionError):
    """The target site took too long to respond or finish loading (504)."""

    status_code = 504
    code = "upstream_timeout"


class UpstreamUnreachableError(ConversionError):
    """The target site could not be reached (DNS/connection failure) (502)."""

    status_code = 502
    code = "upstream_unreachable"


class EmptyRenderError(ConversionError):
    """Navigation finished but produced no capturable content (502)."""

    status_code = 502
    code = "empty_render"


class UnsupportedContentError(ConversionError):
    """The URL returned non-HTML content this converter cannot render (415)."""

    status_code = 415
    code = "unsupported_content_type"


class SelectorNotFoundError(ConversionError):
    """A caller-supplied ``wait_for_selector`` never appeared (422)."""

    status_code = 422
    code = "selector_not_found"


# Substrings that identify the failure class inside crawl4ai/Playwright
# error messages. Ordered most-specific first. These are our own heuristics
# over the underlying engine's free-text messages.
_TIMEOUT_MARKERS = ("timeout", "timed out", "exceeded")
_UNREACHABLE_MARKERS = (
    "net::err_name_not_resolved",
    "err_name_not_resolved",
    "net::err_connection",
    "err_connection",
    "err_address_unreachable",
    "err_internet_disconnected",
    "getaddrinfo",
    "name or service not known",
    "connection refused",
    "econnrefused",
    "dns",
)


def classify_render_failure(
    result: Any, url: str, *, artifact: str = "content"
) -> ConversionError:
    """Map a crawl4ai result with no captured artifact to a typed error.

    ``artifact`` names what we expected ("PDF", "screenshot", "HTML") for a
    clear message. Inspects ``result.error_message`` to decide whether the
    target timed out (504), was unreachable (502), or the render simply came
    back empty (502). Never raises; always returns a ``ConversionError``.
    """
    error_message = ""
    if result is not None:
        error_message = str(getattr(result, "error_message", "") or "")
    lowered = error_message.lower()

    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return UpstreamTimeoutError(
            f"The target site took too long to respond while rendering "
            f"{artifact} for {url}.",
        )
    if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return UpstreamUnreachableError(
            f"The target site could not be reached while rendering "
            f"{artifact} for {url}.",
        )
    reason = error_message or "navigation or hook failure"
    return EmptyRenderError(
        f"Rendering {artifact} for {url} produced no output ({reason}).",
    )
