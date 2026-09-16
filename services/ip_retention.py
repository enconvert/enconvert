"""GDPR storage limitation for the client IPs the backend keeps on ch_users.

backend/utils/client_geo.py records signup_ip (backend migration 013) and
last_login_ip / last_login_ua (backend migration 009). Raw IPs are personal
data, so they are NULLed after IP_RETENTION_DAYS, the period the privacy
policy (section 7) promises. Country codes are kept.

Runs from retention_worker.tick(), i.e. on every enconvert-retention-sweep.timer
fire (or in-process poll). Raw SQL on purpose: the gateway's User twin does not
declare these backend-only columns.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from utils.postgres import get_db

logger = logging.getLogger(__name__)

# 366, not 365: never purge before 12 calendar months, even when the window
# spans Feb 29 (at worst a day late, never early).
IP_RETENTION_DAYS = 366

# ponytail: two unindexed UPDATEs per 15-minute tick, i.e. a seq scan of
# ch_users that matches nothing almost every time. Fine at this table size;
# add partial indexes (... WHERE signup_ip IS NOT NULL) if it ever shows up.
_PURGE_SIGNUP_IPS = text(
    "UPDATE ch_users SET signup_ip = NULL"
    " WHERE signup_ip IS NOT NULL AND created_at < :cutoff"
)
# Every login stamps updated_at and later writes only move it forward, so
# updated_at < cutoff implies the last login is older still (an IP may outlive
# 12 months after the last login, never fall short of it).
_PURGE_LOGIN_IPS = text(
    "UPDATE ch_users SET last_login_ip = NULL, last_login_ua = NULL"
    " WHERE last_login_ip IS NOT NULL AND updated_at < :cutoff"
)


def purge_expired_ips(now: datetime) -> int:
    """NULL raw IPs older than IP_RETENTION_DAYS; returns rows touched.

    Never raises: a failure (e.g. backend migration 013 not applied yet) is
    logged and rolled back, and must not stop the file-retention sweep that
    calls it. The next tick simply retries."""
    cutoff = now - timedelta(days=IP_RETENTION_DAYS)
    db = get_db()
    try:
        touched = sum(
            db.execute(statement, {"cutoff": cutoff}).rowcount
            for statement in (_PURGE_SIGNUP_IPS, _PURGE_LOGIN_IPS)
        )
        db.commit()
        if touched:
            logger.info("ip-retention: cleared %d expired IP row(s)", touched)
        return touched
    except Exception:  # noqa: BLE001 — must never block file retention
        db.rollback()
        logger.exception("ip-retention: purge failed")
        return 0
    finally:
        db.close()
