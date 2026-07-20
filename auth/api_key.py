from fastapi import HTTPException, Request
from sqlmodel import select
import hashlib
import logging
import threading
from datetime import datetime, timezone, timedelta

from models import APIKeys, Subscription, Plan
from config import WIDGET_ORIGIN
from utils.postgres import get_db
from utils.validators import is_domain_allowed
from utils.subscription import get_project_owner_email
from utils.email_notifier import send_api_key_unauthorized_domain_email

logger = logging.getLogger("conversion-api-gateway")


def _maybe_alert_unauthorized_domain(db, key_data, origin: str) -> None:
    """Best-effort, throttled (once/24h per key) owner alert when a public key is
    presented from an origin not on its allowed-domains list. Runs only on the
    reject path, so it never touches a successful request. Uses the caller's open
    ``db`` session to persist the throttle before the request unwinds; the email
    itself (owner lookup + Brevo HTTP) is dispatched to a daemon thread so it
    can never block the event loop — validate_api_key runs synchronously inside
    async request handling on a single-worker server."""
    try:
        now = datetime.now(timezone.utc)
        last = key_data.last_unauthorized_alert_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last) < timedelta(hours=24):
                return
        # Capture plain values before the commit expires the instance.
        project_id = key_data.project_id
        key_name = key_data.name
        key_prefix = key_data.key_prefix
        key_data.last_unauthorized_alert_at = now
        db.add(key_data)
        db.commit()
    except Exception:
        logger.exception("Failed to persist unauthorized-domain alert throttle")
        return

    def _send_alert():
        try:
            email = get_project_owner_email(project_id)
            if email:
                send_api_key_unauthorized_domain_email(
                    email, key_name, key_prefix, origin or "unknown"
                )
        except Exception:
            logger.exception("Failed to send unauthorized-domain alert")

    try:
        threading.Thread(target=_send_alert, daemon=True).start()
    except Exception:
        # Thread creation can fail under resource exhaustion; the alert is
        # best-effort and must never turn the 403 into a 500.
        logger.exception("Failed to start unauthorized-domain alert thread")


def validate_api_key(api_key: str, request: Request) -> dict:
    """
    Validate API key and return user info.

    Returns:
        dict: {id, tier, key_type, allowed_domains, allowed_endpoints}
    """
    if not api_key or len(api_key) < 45:
        logger.warning(f"API key validation failed: invalid format (length={len(api_key) if api_key else 0})")
        raise HTTPException(status_code=401, detail="Invalid API Key format")

    if api_key.startswith("sk_"):
        key_type = "private"
    elif api_key.startswith("pk_"):
        key_type = "public"
    else:
        logger.warning(f"API key validation failed: invalid prefix")
        raise HTTPException(status_code=401, detail="Invalid API Key Format")

    origin = request.headers.get("Origin")
    parent_origin = request.headers.get("X-Parent-Origin")
    is_browser_request = origin is not None
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    logger.info(f"API key auth: type={key_type}, origin={origin}, parent_origin={parent_origin}, browser={is_browser_request}")

    db = get_db()

    # Find API key in database
    key_data = db.exec(select(APIKeys).where(APIKeys.key == key_hash)).first()

    if not key_data:
        db.close()
        logger.warning(f"API key validation failed: key not found in database")
        raise HTTPException(status_code=401, detail="Invalid API Key")

    if not key_data.active:
        db.close()
        logger.warning(f"API key validation failed: key is revoked (prefix={key_data.key_prefix})")
        raise HTTPException(status_code=401, detail="API Key revoked")

    if key_type == "private" and is_browser_request:
        db.close()
        logger.warning(f"API key validation failed: private key used from browser (origin={origin})")
        raise HTTPException(status_code=403, detail="Private API keys cannot be used from browsers")

    if key_type == "public" and is_browser_request:
        allowed_domains = key_data.allowed_domains or []
        # Browser extensions have chrome-extension:// origins — treat as trusted clients
        if origin and origin.startswith("chrome-extension://"):
            logger.info(f"API key auth: browser extension origin ({origin}), skipping domain check")
        # When the request comes from the widget iframe, the Origin header
        # is the widget app's origin, not the parent page's origin.
        # Skip domain check here — parent origin validation happens in
        # the /v1/auth/token endpoint via the X-Parent-Origin header.
        elif origin == WIDGET_ORIGIN:
            logger.info(f"API key auth: origin is widget app ({WIDGET_ORIGIN}), skipping domain check (parent origin validated later)")
        elif not is_domain_allowed(origin, allowed_domains):
            _maybe_alert_unauthorized_domain(db, key_data, origin)
            db.close()
            logger.warning(f"API key validation failed: domain not authorized (origin={origin}, allowed={allowed_domains})")
            raise HTTPException(status_code=403, detail=f"Domain {origin} not authorized")
        else:
            logger.info(f"API key auth: domain check passed (origin={origin}, allowed={allowed_domains})")

    # Get plan slug from subscription
    sub = db.exec(select(Subscription).where(
        Subscription.project_id == key_data.project_id,
        Subscription.status == "active",
    )).first()
    plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first() if sub else None
    plan_slug = plan.slug if plan else "free"
    db.close()

    logger.info(f"API key auth successful: project={key_data.project_id}, plan={plan_slug}, key_type={key_type}")

    return {
        "id": str(key_data.project_id),
        "tier": plan_slug,  # backward compat
        "plan_slug": plan_slug,
        "key_type": key_type,
        "allowed_domains": key_data.allowed_domains or [],
        "allowed_endpoints": key_data.allowed_endpoints or [],
    }
