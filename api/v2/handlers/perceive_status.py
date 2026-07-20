"""GET /v2/perceive/{operation_id} (Task F.5).

Returns the same response shape as the POST with freshly signed URLs
rebuilt from the persisted output_keys. A foreign or unknown
operation_id is a 404 in both cases — existence is not leaked across
projects.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_current_user
from api.v2.schemas.perceive import PerceiveResponse, PerceiveTokens
from models import PerceiveOperation
from services.v2_engine import operations
from services.v2_engine.perceive_flow import outputs_from_keys

router = APIRouter()


@router.get("/perceive/{operation_id}", response_model=PerceiveResponse)
async def perceive_status(
    operation_id: str,
    user: dict = Depends(get_current_user),
) -> PerceiveResponse:
    """Look up one perceive operation with refreshed signed URLs."""
    op = operations.get_operation(operation_id)
    if op is None or str(op.project_id) != str(user["id"]):
        raise HTTPException(status_code=404, detail="Operation not found")
    return _response_from_operation(op)


def _response_from_operation(op: PerceiveOperation) -> PerceiveResponse:
    return PerceiveResponse(
        operation_id=op.operation_id,
        status=op.status,  # type: ignore[arg-type]
        url=op.url,
        url_final=op.url_final,
        content_hash=op.content_hash,
        render_quality=op.render_quality_score,
        cache_hit=op.cache_hit,
        outputs=outputs_from_keys(op.output_keys, op.project_id),
        structured=op.structured_data,
        extraction_tier=op.extraction_tier,  # type: ignore[arg-type]
        tokens=PerceiveTokens(
            input=op.llm_input_tokens, output=op.llm_output_tokens
        ),
        cost_cents=float(op.llm_cost_cents or 0),
        duration_ms=op.duration_ms,
        error=op.error_message,
    )
