from .auth import router as auth_router
from .convert import router as convert_router
from .widget import router as widget_router
from .extension import router as extension_router
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.deps import get_current_user

router = APIRouter()


class WhoAmIResponse(BaseModel):
    """Minimal identity pass-through: only what the MCP server needs to attach a
    PostHog project group. Deliberately excludes key_type, domains, and limits."""

    project_id: str
    plan_slug: str

router.include_router(auth_router, prefix="/auth", tags=["v1-auth"])
router.include_router(convert_router, prefix="/convert", tags=["v1-convert"])
router.include_router(widget_router, prefix="/widget", tags=["v1-widget"])
router.include_router(extension_router, prefix="/extension", tags=["v1-extension"])

@router.get("/", tags=["v1-info"])
def v1_info():
    """V1 API Information"""
    return {
        "version": "v1",
        "status": "stable",
        "endpoints": {
            "auth": "/v1/auth/*",
            "convert": "/v1/convert/*"
        }
    }


@router.get("/whoami", response_model=WhoAmIResponse, tags=["v1-info"])
async def whoami(user: dict = Depends(get_current_user)) -> WhoAmIResponse:
    """Resolve an sk_ API key to its project identity so an external client (the
    MCP server) can attach a PostHog project group. Pure pass-through — no DB
    query; get_current_user already resolved everything."""
    # Defense-in-depth: a browser-session JWT resolves as key_type "public" and
    # must not reach this endpoint. MCP authenticates with sk_ (private) keys.
    if user.get("key_type") != "private":
        raise HTTPException(
            status_code=403,
            detail="GET /v1/whoami requires a private API key (sk_...)."
        )
    return WhoAmIResponse(
        project_id=user["id"],
        plan_slug=user.get("plan_slug", "free"),
    )