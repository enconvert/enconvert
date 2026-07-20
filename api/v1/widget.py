from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Optional
from sqlmodel import Session, select
import logging

from models import Widget, APIKeys, Subscription, Plan
from rate_limiting.limiter import enforce_ip
from utils.postgres import get_session
from utils.turnstile import verify_turnstile
from utils.validators import is_domain_allowed
from utils.subscription import get_subscription, is_project_owner_active
from auth.jwt_handler import generate_jwt_token, generate_refresh_token, validate_refresh_token
from config import TURNSTILE_SITE_KEY, WIDGET_ORIGIN

logger = logging.getLogger("conversion-api-gateway")

router = APIRouter()

URL_BASED_ENDPOINTS = {"url-to-pdf", "url-to-screenshot"}


def _derive_input_type(endpoint: str) -> str:
    """Return 'url' for URL-based endpoints, 'file' otherwise."""
    slug = endpoint.rsplit("/", 1)[-1]
    return "url" if slug in URL_BASED_ENDPOINTS else "file"


@router.get("/{widget_id}/config")
def get_widget_config(widget_id: str, request: Request, db: Session = Depends(get_session)):
    widget = db.exec(
        select(Widget, APIKeys.allowed_domains)
        .join(APIKeys, Widget.api_key_id == APIKeys.id)
        .where(Widget.id == widget_id, Widget.active == True)
    ).first()

    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    w, allowed_domains = widget

    request.state.frame_ancestors = allowed_domains or []

    # Get widget branding flag from subscription
    api_key = db.exec(select(APIKeys).where(APIKeys.id == w.api_key_id)).first()
    sub = get_subscription(api_key.project_id) if api_key else None
    widget_branding = sub["widget_branding"] if sub else True

    return {
        "endpoint": w.endpoint,
        "input_type": _derive_input_type(w.endpoint),
        "allowed_domains": allowed_domains or [],
        "turnstile_site_key": TURNSTILE_SITE_KEY,
        "widget_branding": widget_branding,
    }


class WidgetTokenRequest(BaseModel):
    turnstile_token: Optional[str] = None


@router.post("/{widget_id}/token")
async def get_widget_token(
    widget_id: str,
    body: WidgetTokenRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Issue a short-lived JWT for a widget without exposing the API key.
    The widget is identified by its UUID; the caller's domain is validated
    against the widget's allowed_domains list.
    """

    # Per-IP throttle: widget mint bypasses get_current_user (UUID+Turnstile
    # auth), so the main limiter never sees it. Placed before any DB work.
    enforce_ip(request, "widget_mint")

    # 1. Look up the widget and its associated API key
    row = db.exec(
        select(Widget, APIKeys)
        .join(APIKeys, Widget.api_key_id == APIKeys.id)
        .where(Widget.id == widget_id, Widget.active == True)
    ).first()

    if not row:
        raise HTTPException(status_code=404, detail="Widget not found")

    widget, api_key = row

    if not api_key.active:
        raise HTTPException(status_code=403, detail="Widget API key has been revoked")

    if not is_project_owner_active(db, api_key.project_id):
        raise HTTPException(status_code=403, detail="Account suspended")

    allowed_domains = api_key.allowed_domains or []

    # 2. Validate the parent domain
    origin = request.headers.get("Origin")
    parent_origin = request.headers.get("X-Parent-Origin")

    logger.info(
        f"Widget token request: widget={widget_id}, origin={origin}, "
        f"parent_origin={parent_origin}, allowed={allowed_domains}"
    )

    # For widget iframe requests, origin will be the widget app itself.
    # The real embedding domain comes via X-Parent-Origin.
    effective_origin = parent_origin or origin

    if effective_origin and effective_origin != WIDGET_ORIGIN:
        if not is_domain_allowed(effective_origin, allowed_domains):
            logger.warning(
                f"Widget token rejected: domain not authorized "
                f"(origin={effective_origin}, allowed={allowed_domains})"
            )
            raise HTTPException(
                status_code=403,
                detail=f"Domain {effective_origin} is not authorized for this widget",
            )

    # 3. Verify Turnstile (bot protection)
    if not body.turnstile_token:
        raise HTTPException(status_code=400, detail="Turnstile token required")

    await verify_turnstile(body.turnstile_token, request.client.host if request.client else None)

    # 4. Look up plan slug from subscription
    sub = db.exec(select(Subscription).where(
        Subscription.project_id == api_key.project_id,
        Subscription.status == "active",
    )).first()
    plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first() if sub else None
    plan_slug = plan.slug if plan else "free"

    # 5. Build the user dict that generate_jwt_token expects
    user = {
        "id": str(api_key.project_id),
        "tier": plan_slug,  # backward compat
        "plan_slug": plan_slug,
        "key_type": api_key.key_type,
        "allowed_domains": allowed_domains,
        "allowed_endpoints": api_key.allowed_endpoints or [],
    }

    token = generate_jwt_token(
        user, origin,
        parent_origin=parent_origin,
        turnstile_verified=True,
    )

    # Set refresh token cookie
    refresh_token = generate_refresh_token(user["id"])
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 60 * 60,
    )

    # Set allowed_domains for CORS middleware
    request.state.allowed_domains = allowed_domains
    request.state.frame_ancestors = allowed_domains

    logger.info(f"Widget token issued: widget={widget_id}, project={api_key.project_id}")

    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@router.post("/{widget_id}/refresh")
async def refresh_widget_token(
    widget_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Refresh an expired widget JWT using the httpOnly refresh_token cookie.
    No Turnstile challenge required — the refresh token proves the user
    previously passed bot verification.
    """

    # Per-IP throttle: refresh bypasses get_current_user AND Turnstile, so
    # without this a captured cookie is an unlimited JWT mill.
    enforce_ip(request, "widget_refresh")

    # 1. Read and validate the refresh token cookie
    refresh_cookie = request.cookies.get("refresh_token")
    if not refresh_cookie:
        raise HTTPException(status_code=401, detail="No refresh token")

    project_id = await validate_refresh_token(refresh_cookie)

    # 2. Look up the widget and its API key
    row = db.exec(
        select(Widget, APIKeys)
        .join(APIKeys, Widget.api_key_id == APIKeys.id)
        .where(Widget.id == widget_id, Widget.active == True)
    ).first()

    if not row:
        raise HTTPException(status_code=404, detail="Widget not found")

    widget, api_key = row

    if not api_key.active:
        raise HTTPException(status_code=403, detail="Widget API key has been revoked")

    if not is_project_owner_active(db, api_key.project_id):
        raise HTTPException(status_code=403, detail="Account suspended")

    # 3. Verify the refresh token belongs to the same project
    if str(api_key.project_id) != project_id:
        raise HTTPException(status_code=403, detail="Refresh token does not match widget")

    # 4. Validate parent origin
    origin = request.headers.get("Origin")
    parent_origin = request.headers.get("X-Parent-Origin")
    effective_origin = parent_origin or origin
    allowed_domains = api_key.allowed_domains or []

    if effective_origin and effective_origin != WIDGET_ORIGIN:
        if not is_domain_allowed(effective_origin, allowed_domains):
            raise HTTPException(
                status_code=403,
                detail=f"Domain {effective_origin} is not authorized for this widget",
            )

    # 5. Look up plan
    sub = db.exec(select(Subscription).where(
        Subscription.project_id == api_key.project_id,
        Subscription.status == "active",
    )).first()
    plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first() if sub else None
    plan_slug = plan.slug if plan else "free"

    # 6. Issue new JWT
    user = {
        "id": str(api_key.project_id),
        "tier": plan_slug,
        "plan_slug": plan_slug,
        "key_type": api_key.key_type,
        "allowed_domains": allowed_domains,
        "allowed_endpoints": api_key.allowed_endpoints or [],
    }

    token = generate_jwt_token(
        user, origin,
        parent_origin=parent_origin,
        turnstile_verified=True,
    )

    # 7. Rotate the refresh token
    new_refresh = generate_refresh_token(user["id"])
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 60 * 60,
    )

    request.state.allowed_domains = allowed_domains
    request.state.frame_ancestors = allowed_domains

    logger.info(f"Widget token refreshed: widget={widget_id}, project={api_key.project_id}")

    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }
