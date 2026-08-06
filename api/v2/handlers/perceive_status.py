"""GET /v2/perceive/{operation_id} (Task F.5).

Returns the same response shape as the POST with freshly signed URLs
rebuilt from the persisted output_keys. A foreign or unknown
operation_id is a 404 in both cases — existence is not leaked across
projects.

``?direct_download=true`` (QA report 2026-08-06, fix E) streams the
artifact bytes straight back instead of a JSON envelope — no second
fetch to a signed URL. ``output=`` names which artifact when the
operation produced more than one.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from api.deps import get_current_user
from api.v2.schemas.perceive import PerceiveResponse, PerceiveTokens
from models import PerceiveOperation
from services.v2_engine import operations
from services.v2_engine.perceive_flow import outputs_from_keys
from utils.storage import download_from_storage

router = APIRouter()


@router.get("/perceive/{operation_id}", response_model=PerceiveResponse)
async def perceive_status(
    operation_id: str,
    direct_download: bool = False,
    output: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Look up one perceive operation with refreshed signed URLs.

    With ``direct_download=true`` the response body IS the artifact.
    """
    op = operations.get_operation(operation_id)
    if op is None or str(op.project_id) != str(user["id"]):
        raise HTTPException(status_code=404, detail="Operation not found")
    response = _response_from_operation(op)
    if not direct_download:
        return response
    return await stream_artifact(response, output)


async def stream_artifact(
    response: PerceiveResponse, output: Optional[str] = None
) -> Response:
    """Stream one artifact's bytes as the HTTP response (fix E).

    ``output`` picks the artifact; when the operation produced exactly
    one, it may be omitted. Errors are 400/404 with a message naming
    the available outputs, so the caller always learns what IS there.
    """
    available = sorted(response.outputs.keys())
    if not available:
        raise HTTPException(
            status_code=404,
            detail="This operation produced no downloadable artifacts "
            "(only inline 'structured' data, or it failed).",
        )
    if output is None:
        if len(available) > 1:
            raise HTTPException(
                status_code=400,
                detail="direct_download needs 'output' to pick one of the "
                f"artifacts this operation produced: {', '.join(available)}.",
            )
        output = available[0]
    artifact = response.outputs.get(output)
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"No '{output}' artifact on this operation. "
            f"Available: {', '.join(available)}.",
        )
    try:
        payload = await asyncio.to_thread(
            download_from_storage, artifact.object_key
        )
    except Exception as exc:  # noqa: BLE001 — expired retention, S3 fault
        raise HTTPException(
            status_code=410,
            detail="The artifact is no longer in storage (it may have "
            "passed your plan's file-retention window). Re-run the "
            "perceive request to regenerate it.",
        ) from exc
    filename = artifact.object_key.rsplit("/", 1)[-1]
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(payload)),
        "Cache-Control": "no-transform",
        "X-Operation-Id": response.operation_id,
        "X-Object-Key": artifact.object_key,
        "X-Cache-Hit": str(response.cache_hit).lower(),
    }
    if response.render_quality is not None:
        headers["X-Render-Quality"] = str(response.render_quality)
    if response.status_code is not None:
        headers["X-Source-Status-Code"] = str(response.status_code)
    if response.content_hash:
        headers["X-Content-Hash"] = response.content_hash
    if response.warnings:
        headers["X-Warnings-Count"] = str(len(response.warnings))
    return Response(
        content=payload,
        media_type=artifact.content_type,
        headers=headers,
    )


def _response_from_operation(op: PerceiveOperation) -> PerceiveResponse:
    keys = op.output_keys or {}
    return PerceiveResponse(
        operation_id=op.operation_id,
        status=op.status,  # type: ignore[arg-type]
        url=op.url,
        url_final=op.url_final,
        content_hash=op.content_hash,
        status_code=keys.get(operations.HTTP_STATUS_KEY),
        render_quality=op.render_quality_score,
        deductions=dict(keys.get(operations.DEDUCTIONS_KEY) or {}),
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
