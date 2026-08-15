from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from typing import Optional
from datetime import datetime, timezone
import logging

from api.deps import get_current_user, check_ops_quota, check_storage_limit, validate_file_size
from monitoring.metrics import log_activity_start, update_activity_status
from utils.error_capture import error_fields
from utils.storage import upload_to_gcs, generate_presigned_url

logger = logging.getLogger("conversion-api-gateway")

router = APIRouter()


@router.post("/capture")
async def upload_capture(
    request: Request,
    capture_type: str = Form(...),
    source_url: Optional[str] = Form(None),
    page_title: Optional[str] = Form(None),
    file_size: Optional[int] = Form(None),
    output_filename: Optional[str] = Form(None),
    store_file: bool = Form(False),
    file: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
):
    """
    Log a browser extension capture and optionally store the file.

    Every call counts as a conversion (billing + activity tracking).
    When store_file=true, the actual file is uploaded to cloud storage.
    """
    if capture_type not in ("screenshot", "pdf"):
        raise HTTPException(status_code=400, detail="capture_type must be 'screenshot' or 'pdf'")

    # Always check the unified ops quota (1 op per capture — billing)
    check_ops_quota(user)

    endpoint = f"extension-{capture_type}"
    input_size = file_size or 0

    activity_id = await log_activity_start(
        project_id=user["id"],
        endpoint=endpoint,
        input_file_size=input_size,
        source_url=source_url,
    )

    start_time = datetime.now(timezone.utc)

    try:
        result = {
            "status": "success",
            "capture_type": capture_type,
            "activity_id": activity_id,
        }

        output_file_size = 0
        object_key = ""

        if store_file:
            if not file:
                raise HTTPException(
                    status_code=400,
                    detail="File is required when store_file=true"
                )

            check_storage_limit(user)
            validate_file_size(request, user, file)

            content = await file.read()
            output_file_size = len(content)

            ext = ".png" if capture_type == "screenshot" else ".pdf"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]

            if output_filename:
                filename = f"{output_filename}_{timestamp}{ext}"
            elif page_title:
                safe_title = "".join(
                    c for c in page_title if c.isalnum() or c in (" ", "-", "_")
                ).strip()[:50]
                filename = f"{safe_title}_{timestamp}{ext}"
            else:
                filename = f"capture_{timestamp}{ext}"

            upload_result = upload_to_gcs(
                file_bytes=content,
                user_id=user["id"],
                endpoint=endpoint,
                original_filename=filename,
            )

            presigned_url = generate_presigned_url(
                upload_result["object_key"], user["id"]
            )

            object_key = upload_result["object_key"]
            output_file_size = upload_result["file_size"]

            result.update({
                "object_key": object_key,
                "filename": upload_result["filename"],
                "file_size": output_file_size,
                "presigned_url": presigned_url,
            })

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        await update_activity_status(
            activity_id,
            "Success",
            output_file_size=output_file_size,
            object_key=object_key,
            duration=duration,
        )

        return JSONResponse(status_code=200, content=result)

    except HTTPException as e:
        # A rejected capture (missing file, storage limit, oversized upload)
        # is still a failed capture: without this the row sat 'In Progress'
        # forever with no recorded cause. Guarded so bookkeeping can never
        # replace the client's real 4xx with a 500.
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            await update_activity_status(
                activity_id, "Failed", duration=duration, **error_fields(e),
            )
        except Exception:
            pass
        raise
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        await update_activity_status(
            activity_id, "Failed", duration=duration, **error_fields(e),
        )
        logger.error(f"Extension capture failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Capture logging failed: {str(e)}")
