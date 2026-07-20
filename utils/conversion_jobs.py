import logging
from sqlmodel import select
from models import ConversionJob
from utils.postgres import get_db
from utils.storage import generate_presigned_url

logger = logging.getLogger(__name__)


def create_conversion_job(job_id: str, project_id: str) -> bool:
    """Create a new ConversionJob row. Returns True on success, False on duplicate."""
    db = get_db()
    try:
        job = ConversionJob(id=job_id, project_id=project_id)
        db.add(job)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to create conversion job {job_id}: {e}")
        return False
    finally:
        db.close()


def update_conversion_job_success(
    job_id: str, project_id: str, object_key: str,
):
    """Update job row on successful conversion with presigned URL."""
    db = get_db()
    try:
        job = db.exec(select(ConversionJob).where(ConversionJob.id == job_id)).first()
        if not job:
            return
        job.status = "success"
        job.object_key = object_key
        job.presigned_url = generate_presigned_url(object_key, project_id)
        db.add(job)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update conversion job {job_id}: {e}")
    finally:
        db.close()


def update_conversion_job_failure(job_id: str, error_message: str):
    """Update job row on failed conversion."""
    db = get_db()
    try:
        job = db.exec(select(ConversionJob).where(ConversionJob.id == job_id)).first()
        if not job:
            return
        job.status = "failed"
        job.error_message = error_message
        db.add(job)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update conversion job {job_id} as failed: {e}")
    finally:
        db.close()


def get_conversion_job(job_id: str) -> ConversionJob | None:
    """Fetch a conversion job by ID."""
    db = get_db()
    try:
        return db.exec(select(ConversionJob).where(ConversionJob.id == job_id)).first()
    finally:
        db.close()
