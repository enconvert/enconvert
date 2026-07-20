from fastapi import APIRouter, Depends, HTTPException, Request, Response, Cookie
from pydantic import BaseModel
from typing import Optional
import logging

from api.deps import get_current_user
from auth.jwt_handler import generate_jwt_token, generate_refresh_token, validate_refresh_token
from config import WIDGET_ORIGIN
from rate_limiting.limiter import enforce_ip
from utils.subscription import is_project_owner_active
from utils.turnstile import verify_turnstile
from utils.validators import is_domain_allowed

logger = logging.getLogger("conversion-api-gateway")

router = APIRouter()

class TokenRequest(BaseModel):
    turnstile_token: Optional[str] = None

class TokenResponse(BaseModel):
    token: str
    token_type: str = "Bearer"
    expires_in: int

class BrandingResponse(BaseModel):
    required: bool

@router.post("/token", response_model=TokenResponse)
async def exchange_token(
    body: TokenRequest,
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user)
):
    """
    Exchange public API key for short-lived JWT.
    Turnstile verification required only for widget-based requests.
    Also sets a refresh token in HTTP-only cookie for seamless token renewal.
    """

    logger.info(f"Token exchange: user={user['id']}, key_type={user['key_type']}")

    if user["key_type"] != "public":
        logger.warning(f"Token exchange rejected: non-public key type ({user['key_type']})")
        raise HTTPException(
            status_code=400,
            detail="Only public API keys can exchange for tokens. Private keys should be used directly."
        )

    # Get origin headers
    origin = request.headers.get("Origin")
    parent_origin = request.headers.get("X-Parent-Origin")
    allowed_domains = user.get("allowed_domains", [])

    logger.info(f"Token exchange: origin={origin}, parent_origin={parent_origin}, allowed_domains={allowed_domains}")

    # Check if this is a playground request (has turnstile token but no parent origin)
    is_playground_request = body.turnstile_token and not parent_origin

    # Verify Turnstile for widget-based requests and playground requests
    turnstile_verified = False
    if origin == WIDGET_ORIGIN:
        if not body.turnstile_token:
            logger.warning(f"Token exchange rejected: widget missing turnstile token")
            raise HTTPException(
                status_code=400,
                detail="Turnstile token required for widget-based requests"
            )
        await verify_turnstile(body.turnstile_token, request.client.host if request.client else None)
        logger.info("Token exchange: turnstile verification passed")
        turnstile_verified = True
    else:
        logger.info("Token exchange: non-widget request, skipping turnstile verification")

    # Widget requests (origin == WIDGET_ORIGIN) must validate parent_origin
    # But playground requests (with turnstile but no parent_origin) are allowed
    if origin == WIDGET_ORIGIN and not is_playground_request:
        if not parent_origin:
            logger.warning(f"Token exchange rejected: widget missing X-Parent-Origin header")
            raise HTTPException(
                status_code=403,
                detail="Missing parent origin header. Widget must be embedded on an authorized domain."
            )
        if not is_domain_allowed(parent_origin, allowed_domains):
            logger.warning(f"Token exchange rejected: parent origin not authorized (parent_origin={parent_origin}, allowed={allowed_domains})")
            raise HTTPException(
                status_code=403,
                detail=f"Domain {parent_origin} is not authorized to use this widget"
            )
        logger.info(f"Token exchange: widget parent origin check passed")
    elif is_playground_request:
        logger.info("Token exchange: playground request, skipping parent origin check")
    # Direct requests: origin already validated in api_key.py, no additional check needed

    # Generate JWT token — store widget origin and validated parent origin
    token = generate_jwt_token(user, origin, parent_origin=parent_origin, turnstile_verified=turnstile_verified)

    # Generate refresh token and store in HTTP-only cookie
    refresh_token = generate_refresh_token(user["id"])
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 60 * 60  # 7 days
    )

    request.state.allowed_domains = user.get("allowed_domains", [])

    logger.info(f"Token exchange successful: origin={origin}, parent_origin={parent_origin}, turnstile_verified={turnstile_verified}")

    return TokenResponse(
        token=token,
        expires_in=3600,
    )

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    response: Response,
    refresh_token: Optional[str] = Cookie(None)
):
    """
    Refresh access token using refresh token from HTTP-only cookie.
    Returns a new access token and refresh token.
    """
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token not found")

    # Per-IP throttle: this handler bypasses get_current_user (cookie-only),
    # so without this it would be an unlimited JWT mill (3 DB queries/call).
    enforce_ip(request, "auth_refresh")

    # Validate refresh token and get user ID
    user_id = await validate_refresh_token(refresh_token)
    logger.info(f"Token refresh: user={user_id}")

    # Fetch user from database
    from utils.postgres import get_db
    from models import APIKeys, Subscription, Plan
    from sqlmodel import select

    db = get_db()
    try:
        # Get the API key associated with this user (project)
        api_key = db.exec(
            select(APIKeys).where(
                APIKeys.project_id == int(user_id),
                APIKeys.active == True,
                APIKeys.key_type == "public"
            )
        ).first()

        if not api_key:
            raise HTTPException(status_code=401, detail="User not found or invalid")

        if not is_project_owner_active(db, api_key.project_id):
            raise HTTPException(status_code=403, detail="Account suspended")

        # Get tier from subscription/plan
        sub = db.exec(select(Subscription).where(
            Subscription.project_id == api_key.project_id,
            Subscription.status == "active",
        )).first()
        plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first() if sub else None
        plan_slug = plan.slug if plan else "free"

        # Build user dict
        user = {
            "id": str(api_key.project_id),
            "tier": plan_slug,
            "plan_slug": plan_slug,
            "key_type": api_key.key_type,
            "allowed_domains": api_key.allowed_domains or [],
            "allowed_endpoints": api_key.allowed_endpoints or [],
        }

        # Get origin headers for token generation
        origin = request.headers.get("Origin")
        parent_origin = request.headers.get("X-Parent-Origin")

        # Generate new access token
        token = generate_jwt_token(user, origin, parent_origin=parent_origin, turnstile_verified=True)

        # Generate new refresh token
        new_refresh_token = generate_refresh_token(user["id"])
        response.set_cookie(
            key="refresh_token",
            value=new_refresh_token,
            httponly=True,
            secure=True,
            samesite="none",
            max_age=7 * 24 * 60 * 60
        )

        logger.info(f"Token refresh successful: user={user_id}")

        return TokenResponse(
            token=token,
            expires_in=3600,
        )
    finally:
        db.close()

@router.get("/branding", response_model=BrandingResponse)
async def get_branding(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Return whether widget embeds must show the 'Powered by EnConvert' branding.

    True for free plans (branding cannot be hidden), False for paid plans
    (the embedder may choose to hide it via plugin settings).

    Public API keys (pk_*) are permitted to call this endpoint directly so
    plugins can decide rendering before exchanging for a JWT.
    """
    request.state.allowed_domains = user.get("allowed_domains", [])
    sub = user.get("subscription", {}) or {}
    return BrandingResponse(required=bool(sub.get("widget_branding", True)))


@router.get("/verify")
async def verify_auth(
    request: Request,
    user: dict = Depends(get_current_user)
):
    """
    Verify authentication is working

    Useful for:
    - Testing API keys
    - Checking domain restrictions
    - Debugging auth issues
    """

    # Store allowed domains for CORS middleware
    request.state.allowed_domains = user.get("allowed_domains", [])

    return {
        "authenticated": True,
        "project_id": user["id"],
        "tier": user["tier"],
        "key_type": user["key_type"],
        "allowed_domains": user.get("allowed_domains", []) if user["key_type"] == "public" else None,
        "allowed_endpoints": user.get("allowed_endpoints", []) if user["key_type"] == "public" else None,
    }