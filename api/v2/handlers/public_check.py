"""GET /v2/public/check/{operation_id} — unauthenticated render-quality permalink.

The playground lets a visitor share the verdict of a read ("this page
scored 0.30, blocked by anti_bot_challenge") without an account. Only
rows the playground itself rendered are visible: ``perceive_flow`` stamps
``output_keys["_public"]`` (``operations.PUBLIC_KEY``) when the caller is
the anonymous playground JWT. The founder's own sk_ reads live on the
same admin project and never carry the marker, so every other id, foreign
or unknown, is a 404 and existence is never leaked. The payload is the
verdict only — no artifact URLs, no structured data, nothing signed.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.v2_engine import operations

router = APIRouter()


class PublicCheck(BaseModel):
    operation_id: str
    url: str
    render_quality: float | None
    is_blocked: bool
    deductions: dict[str, float]
    status_code: int | None
    created_at: datetime


@router.get("/public/check/{operation_id}", response_model=PublicCheck)
async def public_check(operation_id: str) -> PublicCheck:
    """Public verdict of one playground read; 404 for anything else."""
    op = operations.get_operation(operation_id)
    keys = (op.output_keys or {}) if op is not None else {}
    if op is None or op.status != "completed" or keys.get(operations.PUBLIC_KEY) is not True:
        raise HTTPException(status_code=404, detail="Check not found")
    return PublicCheck(
        operation_id=op.operation_id,
        url=op.url,
        render_quality=op.render_quality_score,
        is_blocked=bool(op.is_blocked),
        deductions=dict(keys.get(operations.DEDUCTIONS_KEY) or {}),
        status_code=keys.get(operations.HTTP_STATUS_KEY),
        created_at=op.created_at,
    )
