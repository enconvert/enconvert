import logging
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select
from models import ConversionJob
from utils.postgres import get_db
from utils.storage import generate_presigned_url

logger = logging.getLogger(__name__)


class JobIdConflict(Exception):
    """The requested job_id is already owned by a different project."""


def create_conversion_job(job_id: str, project_id: str) -> bool:
    """Idempotently claim a job row for this project.

    ``job_id`` is supplied by the CLIENT as a poll-on-timeout idempotency key,
    so the same id legitimately arrives more than once (the playground retries
    once on a fast non-2xx). A bare INSERT turned that expected retry into a
    UniqueViolation logged as an error — the 2026-07-29 "duplicate key value
    violates unique constraint" noise, which was a symptom of a failing render,
    not a database fault.

    Re-claiming your OWN id resets the row to the in-flight state and returns
    True. An id owned by ANOTHER project raises JobIdConflict and leaves that
    row untouched, so a guessed id can never clobber a foreign job.
    """
    db = get_db()
    tbl = ConversionJob.__table__
    try:
        stmt = (
            pg_insert(tbl)
            .values(id=job_id, project_id=project_id)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "status": "",
                    "presigned_url": "",
                    "object_key": "",
                    "error_message": "",
                },
                # Only the owning project may re-claim; a foreign id updates
                # zero rows and returns no id.
                where=(tbl.c.project_id == project_id),
            )
            .returning(tbl.c.id)
        )
        claimed = db.exec(stmt).first()
        db.commit()
        if claimed is None:
            raise JobIdConflict(job_id)
        return True
    except JobIdConflict:
        db.rollback()
        logger.warning(
            "Rejected conversion job %s: id belongs to another project", job_id
        )
        raise
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
        job = db.exec(
            select(ConversionJob).where(
                ConversionJob.id == job_id,
                ConversionJob.project_id == project_id,
            )
        ).first()
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


def update_conversion_job_failure(job_id: str, project_id: str, error_message: str):
    """Update job row on failed conversion (scoped to the owning project)."""
    db = get_db()
    try:
        job = db.exec(
            select(ConversionJob).where(
                ConversionJob.id == job_id,
                ConversionJob.project_id == project_id,
            )
        ).first()
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
