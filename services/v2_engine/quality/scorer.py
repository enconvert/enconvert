"""Open-source fallback for ``services.v2_engine.quality.scorer``.

The cloud build runs an 8-point deduction scorer (anti-bot / WAF / login-wall
marker corpora, redirect and timing heuristics). This fallback keeps the same
public surface — the :class:`RenderQuality` dataclass and the ``score()``
signature — with a deliberately permissive heuristic: a render with a
non-trivial amount of visible text is acceptable; a near-empty body is the
only thing flagged. It never claims a page is blocked or login-walled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from services.page_quality.instrumentation import PageInstrumentation

# A body under this many visible words is a failed render (empty SPA shell,
# title-only stub), scored well below the 0.40 quality floor used downstream.
_EMPTY_HARD_WORDS = 20
_EMPTY_HARD_DEDUCTION = 0.7

# Blocks whose text content is invisible to a human looking at the render.
_INVISIBLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class RenderQuality:
    """Verdict for one render. Field-compatible with the cloud scorer.

    ``deductions`` maps each fired deduction to the amount it removed (only
    fired deductions appear). ``score`` is 1.0 minus their sum, clamped to
    [0.0, 1.0].
    """

    score: float
    is_blocked: bool
    is_login_wall: bool
    deductions: dict[str, float]


def _visible_word_count(html: str) -> int:
    """Approximate count of words a human would see (stdlib only)."""
    stripped = _INVISIBLE_BLOCK_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", stripped)
    return len(text.split())


def score(
    html: str,
    instrumentation: PageInstrumentation,
    structured: Optional[dict[str, Any]],
) -> RenderQuality:
    """Naive open-build scorer: non-empty visible text => acceptable.

    Args:
        html: The rendered HTML.
        instrumentation: The per-render capture. Accepted for signature
            compatibility; the naive scorer does not read its counters.
        structured: Heuristic structured payload. Accepted but unused,
            matching the cloud signature.

    Returns:
        An immutable RenderQuality verdict. Only the ``empty_body`` deduction
        can fire; ``is_blocked`` and ``is_login_wall`` are always False.
    """
    del instrumentation, structured  # Signature compatibility only.

    deductions: dict[str, float] = {}
    if _visible_word_count(html or "") < _EMPTY_HARD_WORDS:
        deductions["empty_body"] = _EMPTY_HARD_DEDUCTION

    total = round(1.0 - sum(deductions.values()), 4)
    return RenderQuality(
        score=max(0.0, min(1.0, total)),
        is_blocked=False,
        is_login_wall=False,
        deductions=deductions,
    )
