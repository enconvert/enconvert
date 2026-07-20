"""User-Agent selection — open-source fallback.

The cloud build rotates a maintained ``fake_useragent`` pool; this open
fallback rotates a small curated list of complete, plausible desktop
Chrome/Edge strings using only the stdlib, so the self-hosted build has
no extra dependency. Same public names: ``DEFAULT_USER_AGENT`` and
``pick_user_agent()``.
"""

from __future__ import annotations

import random

# Complete, plausible desktop Chrome UA — the deterministic default.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Curated pool of complete desktop UAs (Chrome/Edge on Windows and macOS).
_FALLBACK_POOL: tuple[str, ...] = (
    DEFAULT_USER_AGENT,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
)


def pick_user_agent() -> str:
    """Return a complete, realistic desktop UA, rotated per call.

    Never raises; the open build always draws from the curated pool.
    """
    return random.choice(_FALLBACK_POOL)
