"""Best-effort API-key usage stamping (migration 031 activation signal).

Writes ``ch_api_keys.first_used_at`` / ``first_used_surface`` (once, ever —
COALESCE / CASE guarded in SQL, so a replay can never overwrite the first
use) and ``last_used_at`` (throttled in-process to at most one write per
``API_KEY_LAST_USED_THROTTLE_SECONDS`` per key, because stamping every
request would turn the hot auth path into a write-per-request).

Contract: ``stamp_api_key_usage`` NEVER raises — auth must keep working even
if the stamp columns are missing or the write fails. Failures log a warning,
roll the session back, and return False.

The throttle is a plain module-level dict keyed by key id. It is consulted
ONLY to skip; an entry is recorded ONLY after a successful commit (a failed
stamp must be retried on the next request, not silenced for an hour). Size is
bounded (oldest-stamped entries dropped beyond ``_THROTTLE_MAX_ENTRIES``) so
a large key population cannot grow it without limit. monotonic() is used so
wall-clock jumps cannot wedge the throttle.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import text

import config

logger = logging.getLogger("conversion-api-gateway")

# One statement: first_used_at set only if NULL, first_used_surface follows
# first_used_at atomically (CASE reads the pre-UPDATE value, same row scan),
# last_used_at always refreshed.
_STAMP_SQL = text(
    """
    UPDATE ch_api_keys
       SET first_used_at = COALESCE(first_used_at, :now),
           first_used_surface = CASE WHEN first_used_at IS NULL
                                     THEN :surface
                                     ELSE first_used_surface END,
           last_used_at = :now
     WHERE id = :key_id
    """
)

_THROTTLE_MAX_ENTRIES = 10_000

# key_id -> time.monotonic() of the last SUCCESSFUL stamp. OrderedDict so the
# oldest stamp is always at the front for bounded eviction.
_last_stamped: OrderedDict[int, float] = OrderedDict()


def stamp_api_key_usage(db, key_id: int, surface: str) -> bool:
    """Stamp usage columns for ``key_id``; True only if a row was committed.

    Best-effort: returns False (never raises) when disabled, throttled, or on
    any error. ``db`` is the caller's open session; on failure it is rolled
    back so the caller can keep using it safely.
    """
    try:
        if not config.API_KEY_USAGE_STAMP_ENABLED:
            return False

        now_mono = time.monotonic()
        last = _last_stamped.get(key_id)
        if last is not None and (now_mono - last) < config.API_KEY_LAST_USED_THROTTLE_SECONDS:
            return False

        db.execute(
            _STAMP_SQL,
            {
                "now": datetime.now(timezone.utc),
                "surface": surface,
                "key_id": key_id,
            },
        )
        db.commit()

        # Record only after the commit so a transient failure is retried on
        # the very next request instead of being suppressed for an hour.
        _last_stamped[key_id] = now_mono
        _last_stamped.move_to_end(key_id)
        while len(_last_stamped) > _THROTTLE_MAX_ENTRIES:
            _last_stamped.popitem(last=False)
        return True
    except Exception:  # noqa: BLE001 — the stamp must never break auth
        logger.warning("API key usage stamp failed (key_id=%s)", key_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 — session may already be unusable
            logger.warning("API key usage stamp rollback failed (key_id=%s)", key_id)
        return False
