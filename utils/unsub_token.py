"""Stateless unsubscribe tokens for lifecycle email links (migration 031).

There is deliberately NO unsubscribe-token column on ch_users: the token is
an HMAC-SHA256 over the user id, keyed by LIFECYCLE_UNSUB_SECRET, so any
gateway process can mint or verify one without a DB read and old links keep
working after data migrations.

Fail-closed contract:
    * make_token raises RuntimeError when the secret is unset — a lifecycle
      email without a working unsubscribe link must never be built.
    * parse_token returns None on ANY problem (unset secret, malformed
      encoding, wrong signature) — the unsubscribe endpoint then answers with
      a neutral 200 and changes nothing.

Token shape: urlsafe-base64("{user_id}.{hmac_hex}") without padding. The
signature is compared with hmac.compare_digest.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Optional

import config


def _secret() -> Optional[str]:
    """Read the secret at call time so tests (and env reloads) can swap it."""
    secret = getattr(config, "LIFECYCLE_UNSUB_SECRET", None)
    if not secret or not str(secret).strip():
        return None
    return str(secret)


def _sign(user_id: int, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), str(user_id).encode("ascii"), hashlib.sha256
    ).hexdigest()


def make_token(user_id: int) -> str:
    """Mint the unsubscribe token for a user. Raises RuntimeError when the
    HMAC secret is unset — callers must not send mail with a dead link."""
    secret = _secret()
    if secret is None:
        raise RuntimeError(
            "LIFECYCLE_UNSUB_SECRET is not set; refusing to mint an "
            "unsubscribe token (lifecycle mail requires a working link)"
        )
    payload = f"{int(user_id)}.{_sign(int(user_id), secret)}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def parse_token(token: str) -> Optional[int]:
    """Return the user_id a valid token names, else None. Never raises."""
    secret = _secret()
    if secret is None or not token or not isinstance(token, str):
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeError, ValueError):
        return None
    user_part, sep, sig_part = payload.partition(".")
    if not sep or not user_part.isdigit():
        return None
    user_id = int(user_part)
    expected = _sign(user_id, secret)
    if not hmac.compare_digest(expected, sig_part):
        return None
    return user_id
