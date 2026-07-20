"""Storage management: auto-delete oldest files when storage is full."""
from sqlmodel import select
from models import Activity, Project, Subscription
from utils.storage import delete_from_storage
from utils.postgres import get_db


def auto_delete_oldest_if_needed(project_id: int, new_file_size: int):
    """If project uses auto_delete_oldest and adding new_file_size
    would exceed storage, delete oldest files until there's room."""
    db = get_db()
    try:
        sub = db.exec(
            select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.status == "active",
            )
        ).first()
        if not sub or sub.effective_storage_bytes == 0:
            return  # No storage plan, nothing to manage

        project = db.exec(select(Project).where(Project.id == project_id)).first()
        if not project:
            return

        available = sub.effective_storage_bytes - (project.storage_used or 0)
        if new_file_size <= available:
            return  # Enough space

        # Need to free: new_file_size - available
        to_free = new_file_size - available

        # Get oldest files for this project
        activities = db.exec(
            select(Activity)
            .where(Activity.project_id == str(project_id))
            .where(Activity.status == "Success")
            .where(Activity.url != "")
            .order_by(Activity.timestamp.asc())
        ).all()

        freed = 0
        for act in activities:
            if freed >= to_free:
                break
            try:
                file_size = int(act.output_file_size) if act.output_file_size else 0
            except ValueError:
                file_size = 0
            if file_size > 0 and act.url:
                delete_from_storage(act.url)
                freed += file_size
                act.url = ""  # Mark as deleted
                db.add(act)

        if freed > 0:
            project.storage_used = max(0, (project.storage_used or 0) - freed)
            db.add(project)
            db.commit()
    finally:
        db.close()
