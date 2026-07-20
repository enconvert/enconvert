
from datetime import datetime, timedelta, timezone
import os
import uuid
from fastapi import HTTPException
import jwt
from utils.validators import is_domain_allowed

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 1
REFRESH_TOKEN_EXPIRATION_DAYS = 7

def generate_jwt_token(user: dict, origin: str = None, parent_origin: str = None, turnstile_verified: bool = False) -> str:
    """
    Generate JWT token for authenticated user
    """

    plan_slug = user.get("plan_slug", user.get("tier", "free"))
    payload = {
        "turnstile_verified": turnstile_verified,
        "sub": user["id"],  # Subject (user ID)
        "tier": plan_slug,  # backward compat
        "plan_slug": plan_slug,
        "key_type": user["key_type"],
        "origin": origin,
        "parent_origin": parent_origin,
        "iat": datetime.now(timezone.utc),  # Issued at
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS),  # Expires
        "jti": str(uuid.uuid4()),  # JWT ID (for revocation)
    }

    # Include allowed_domains and allowed_endpoints for public keys
    if user["key_type"] == "public":
        payload["allowed_domains"] = user.get("allowed_domains", [])
        payload["allowed_endpoints"] = user.get("allowed_endpoints", [])

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token

async def validate_token(token: str, current_origin: str = None, current_parent_origin: str = None) -> dict:
    """
    Validate JWT token

    Returns:
        dict: User info

    Raises:
        HTTPException: If token invalid or expired
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Validate origin for public keys
    if payload.get("key_type") == "public":
        token_origin = payload.get("origin")

        # Verify the widget origin hasn't changed (prevents token theft)
        if current_origin and token_origin and current_origin != token_origin:
            raise HTTPException(
                status_code=403,
                detail="Token issued for different origin"
            )

        # Verify the parent origin matches what was validated at token issuance
        token_parent_origin = payload.get("parent_origin")
        if current_parent_origin and token_parent_origin and current_parent_origin != token_parent_origin:
            raise HTTPException(
                status_code=403,
                detail="Parent origin does not match token"
            )

    plan_slug = payload.get("plan_slug", payload.get("tier", "free"))
    return {
        "id": str(payload["sub"]),
        "tier": plan_slug,  # backward compat
        "plan_slug": plan_slug,
        "key_type": payload["key_type"],
        "allowed_domains": payload.get("allowed_domains", []),
        "allowed_endpoints": payload.get("allowed_endpoints", []),
    }

def generate_refresh_token(user_id: str) -> str:
    """
    Generate a refresh token (long-lived, only stores user ID)
    """
    payload = {
        "sub": user_id,
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRATION_DAYS),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def validate_refresh_token(token: str) -> str:
    """
    Validate refresh token and return user ID

    Returns:
        str: User ID

    Raises:
        HTTPException: If token invalid or expired
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    return str(payload["sub"])