import logging
from sqlmodel import select
from models import Activity, BatchItemResponse, BatchStatusResponse
from utils.postgres import get_db
from utils.storage import generate_presigned_url

logger = logging.getLogger(__name__)


def get_batch_status(batch_id: str, project_id: str) -> BatchStatusResponse | None:
    """Query Activity rows by batch_id, compute aggregate status, and build response."""
    db = get_db()
    try:
        activities = db.exec(
            select(Activity)
            .where(Activity.batch_id == batch_id)
            .where(Activity.project_id == project_id)
            .order_by(Activity.id)
        ).all()

        if not activities:
            return None

        items: list[BatchItemResponse] = []
        completed_count = 0
        failed_count = 0
        in_progress_count = 0
        # Track object_keys from successful rows for ZIP mode detection
        success_object_keys: list[str] = []

        for activity in activities:
            # Parse output_file_size from string to int
            file_size: int | None = None
            if activity.output_file_size:
                try:
                    file_size = int(activity.output_file_size)
                except (ValueError, TypeError):
                    file_size = None

            duration = activity.duration if activity.duration else None
            download_url: str | None = None

            if activity.status == "Success":
                completed_count += 1
                object_key = activity.url  # activity.url stores the object_key
                if object_key:
                    success_object_keys.append(object_key)
                    try:
                        download_url = generate_presigned_url(object_key, project_id)
                    except Exception:
                        logger.warning(
                            f"Failed to generate presigned URL for batch {batch_id}, "
                            f"object_key={object_key}"
                        )
            elif activity.status == "Failed":
                failed_count += 1
            else:
                in_progress_count += 1

            items.append(BatchItemResponse(
                source_url=activity.source_url or "",
                status=activity.status,
                download_url=download_url,
                output_file_size=file_size,
                duration=duration,
            ))

        # Detect output mode
        # ZIP mode: all successful rows share the same object_key ending in .zip
        unique_keys = set(success_object_keys)
        is_zip = (
            len(unique_keys) == 1
            and next(iter(unique_keys)).endswith(".zip")
        ) if unique_keys else False

        output_mode = "zip" if is_zip else "individual"
        zip_download_url: str | None = None
        if is_zip:
            zip_key = next(iter(unique_keys))
            try:
                zip_download_url = generate_presigned_url(zip_key, project_id)
            except Exception:
                logger.warning(
                    f"Failed to generate presigned URL for batch ZIP {batch_id}, "
                    f"object_key={zip_key}"
                )

        # Derive overall status
        total = len(activities)
        if in_progress_count > 0:
            status = "processing"
        elif completed_count == total:
            status = "completed"
        elif failed_count == total:
            status = "failed"
        else:
            status = "partial"

        return BatchStatusResponse(
            batch_id=batch_id,
            status=status,
            total=total,
            completed=completed_count,
            failed=failed_count,
            in_progress=in_progress_count,
            output_mode=output_mode,
            zip_download_url=zip_download_url,
            items=items,
        )
    finally:
        db.close()
