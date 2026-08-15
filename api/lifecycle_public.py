"""Public, unauthenticated lifecycle-email endpoints (Phase 6).

    GET/POST /email/unsubscribe?t=...   stateless HMAC one-click unsubscribe
    POST     /email/webhooks/brevo      Brevo bounce/spam suppression webhook

Unsubscribe is RFC 8058 one-click capable: mail clients POST to the URL from
the List-Unsubscribe header with no body semantics we depend on — the token
travels in the ``t`` query parameter for both verbs. The response is ALWAYS
the same neutral 200 page, valid token or not (idempotent, and an invalid
token must not be distinguishable from a valid one).

The Brevo webhook always answers 200 (Brevo retries non-2xx, and a retry
storm helps nobody) and processes nothing unless the X-Brevo-Auth header
matches BREVO_WEBHOOK_SECRET via hmac.compare_digest — unset secret means
fail closed: acknowledge, act on nothing.

Suppression/opt-out state lives on ch_users (migration 031) and is read by
the lifecycle candidate layer only — transactional mail is never gated here.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

import config
from utils import unsub_token
from utils.postgres import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lifecycle-public"])

_UNSUB_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Unsubscribed</title></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:560px;
             margin:60px auto;padding:0 20px;">
  <h2 style="font-weight:normal;">You are unsubscribed from EnConvert onboarding emails.</h2>
  <p style="color:#666;">You will still receive account emails like password
  resets and billing receipts.</p>
</body></html>"""

_OPT_OUT = text("""
    UPDATE ch_users
       SET lifecycle_opt_out_at = COALESCE(lifecycle_opt_out_at, :now)
     WHERE id = :user_id
""")

_SUPPRESS_BOUNCE = text("""
    UPDATE ch_users
       SET email_suppressed_at = COALESCE(email_suppressed_at, :now),
           email_suppression_reason = COALESCE(email_suppression_reason,
                                               'hard_bounce')
     WHERE LOWER(email) = LOWER(:email)
""")

_SUPPRESS_SPAM = text("""
    UPDATE ch_users
       SET email_suppressed_at = COALESCE(email_suppressed_at, :now),
           email_suppression_reason = COALESCE(email_suppression_reason, 'spam'),
           lifecycle_opt_out_at = COALESCE(lifecycle_opt_out_at, :now)
     WHERE LOWER(email) = LOWER(:email)
""")


def _apply_unsubscribe(token: Optional[str]) -> None:
    """Set lifecycle_opt_out_at for a valid token; silently do nothing for an
    invalid one. Idempotent (COALESCE keeps the first opt-out instant)."""
    user_id = unsub_token.parse_token(token or "")
    if user_id is None:
        return  # neutral: bad HMAC / unset secret changes nothing
    db = get_db()
    try:
        db.execute(
            _OPT_OUT, {"user_id": user_id, "now": datetime.now(timezone.utc)}
        )
        db.commit()
        logger.info("[lifecycle] unsubscribe recorded for user %s", user_id)
    finally:
        db.close()


@router.get("/email/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(t: Optional[str] = None) -> HTMLResponse:
    """Human click-through from the visible footer link."""
    _apply_unsubscribe(t)
    return HTMLResponse(content=_UNSUB_PAGE, status_code=200)


@router.post("/email/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_post(t: Optional[str] = None) -> HTMLResponse:
    """RFC 8058 one-click POST from the mail client (List-Unsubscribe-Post)."""
    _apply_unsubscribe(t)
    return HTMLResponse(content=_UNSUB_PAGE, status_code=200)


def _normalize_event(event: str) -> Optional[str]:
    """Map Brevo's event spellings onto the two we act on. Brevo has used both
    snake_case and camelCase across webhook versions; anything else is
    deliberately ignored (soft bounces, opens, deliveries...)."""
    slug = event.strip().lower().replace("_", "")
    if slug == "hardbounce":
        return "hard_bounce"
    if slug in ("spam", "complaint"):
        return "spam"
    return None


@router.post("/email/webhooks/brevo")
async def brevo_webhook(request: Request) -> JSONResponse:
    """Suppression webhook: hard_bounce marks the address undeliverable; spam
    additionally opts the user out of lifecycle mail. Always 200."""
    secret = getattr(config, "BREVO_WEBHOOK_SECRET", None)
    provided = request.headers.get("x-brevo-auth", "")
    if not secret or not hmac.compare_digest(str(secret), provided):
        # Fail closed: acknowledge (Brevo retries non-2xx) but act on nothing.
        return JSONResponse({"ok": True}, status_code=200)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is still a 200
        return JSONResponse({"ok": True}, status_code=200)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True}, status_code=200)

    event = _normalize_event(str(payload.get("event") or ""))
    email = str(payload.get("email") or "").strip()
    if event is None or not email:
        return JSONResponse({"ok": True}, status_code=200)

    db = get_db()
    try:
        statement = _SUPPRESS_BOUNCE if event == "hard_bounce" else _SUPPRESS_SPAM
        db.execute(statement, {"email": email, "now": datetime.now(timezone.utc)})
        db.commit()
        # Log the event, never the address (email is PII).
        logger.info("[lifecycle] brevo webhook processed event=%s", event)
    except Exception:  # noqa: BLE001 — a DB hiccup must not trigger Brevo retries
        db.rollback()
        logger.exception("[lifecycle] brevo webhook write failed")
    finally:
        db.close()
    return JSONResponse({"ok": True}, status_code=200)
