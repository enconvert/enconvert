"""Open-source fallback for ``services.page_quality.instrumentation``.

The cloud build instruments every Playwright render with console-error and
failed-subresource listeners feeding the proprietary quality scorer. This
fallback keeps the public surface — :func:`header_opts_out` (real logic) and
:class:`PageInstrumentation` (a lightweight shim) — so the open build boots
and the V1/V2 call sites work unchanged.

The shim does only the cheap, honest part: it snapshots the rendered HTML,
its SHA-256 (used downstream as a dedup / cache key), the final URL, and the
navigation-to-capture wall time. Console-error and resource-failure counting
stay at their zero defaults.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Opt-out request header consumers can send to skip rendered-HTML capture.
OPT_OUT_HEADER = "x-enconvert-no-capture"


def header_opts_out(headers: Optional[dict]) -> bool:
    """Return True iff the caller asked us to skip rendered-HTML capture.

    Accepts either a Starlette/FastAPI Headers object or a plain dict.
    Header names are matched case-insensitively. Any non-true value is
    treated as opt-in (the default).
    """
    if not headers:
        return False
    raw = headers.get(OPT_OUT_HEADER) if hasattr(headers, "get") else None
    if raw is None and isinstance(headers, dict):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == OPT_OUT_HEADER:
                raw = value
                break
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PageInstrumentation:
    """One-shot recorder for a single Playwright page render (open shim).

    Field names and the ``attach()`` / ``capture()`` protocol match the cloud
    build so ``utils.processor`` and the fallback converters work unchanged.
    Error/failure counters remain 0 in the open build.
    """

    skip: bool = False
    requested_url: Optional[str] = None

    nav_start: Optional[float] = None
    console_error_count: int = 0
    resource_failure_count: int = 0

    rendered_html: Optional[str] = None
    content_hash: Optional[str] = None
    page_load_time_ms: int = 0
    final_url: Optional[str] = None

    @classmethod
    def from_headers(cls, headers: Optional[dict]) -> "PageInstrumentation":
        """Build an instance respecting the opt-out header."""
        return cls(skip=header_opts_out(headers))

    def attach(self, page: Any) -> None:
        """Start the navigation clock. Call before ``page.goto()``.

        The cloud build registers console / network listeners here; the open
        shim only records the start time. No-op when ``skip`` is True.
        """
        del page  # No listeners in the open build.
        if self.skip:
            return
        self.nav_start = time.monotonic()

    async def capture(self, page: Any) -> None:
        """Snapshot HTML, its SHA-256, the final URL, and elapsed time.

        Best-effort: instrumentation is observability, never load-bearing, so
        failures are logged and swallowed. No-op when ``skip`` is True.
        """
        if self.skip:
            return
        try:
            self.final_url = page.url
            html = await page.content()
            self.rendered_html = html
            self.content_hash = hashlib.sha256(
                html.encode("utf-8")
            ).hexdigest()
            if self.nav_start is not None:
                self.page_load_time_ms = int(
                    (time.monotonic() - self.nav_start) * 1000
                )
        except Exception as exc:  # noqa: BLE001 — never break the conversion
            logger.warning("PageInstrumentation.capture failed: %s", exc)
