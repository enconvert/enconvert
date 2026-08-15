"""Subscription lookup and feature gating helpers."""
import logging
from datetime import datetime, timezone
from sqlalchemy import update
from sqlmodel import select
from models import Subscription, Plan, StoragePlan, UsagePeriod, Project, ProjectMember, User
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# The synthetic plan an admin's default project gets on the request path
# (api.deps._attach_subscription). Kept HERE, next to get_subscription, so
# background workers can resolve the exact same plan shape via
# get_effective_subscription — a worker that read the raw Subscription row
# instead would deny V2 features the submit handler already accepted.
ADMIN_SUBSCRIPTION: dict = {
    "plan_slug": "admin",
    # Unified ops (migration 029): 0 = unlimited (check_ops_quota also
    # bypasses outright on plan_slug == "admin").
    "ops_month": 0,
    "effective_ops_month": 0,
    "ai_credits_cents_month": 0,
    "override_ai_credits_cents_month": None,
    "max_file_size": 999999999,
    "file_retention_hours": 99999,
    "batch_limit": 99999,
    "storage_bytes": 999999999999,
    "has_async_mode": True,
    "has_webhook": True,
    "has_zip_output": True,
    "has_basic_auth": True,
    "crawl_mode": "full",
    "widget_branding": False,
    "overage_enabled": False,
    "overage_allowed": False,
    # V2 endpoint flags: admin gets everything (max_watchers 0 = unlimited
    # when the flag is TRUE, migration 011 convention).
    "perceive_enabled": True,
    "discover_enabled": True,
    "lookup_enabled": True,
    "distill_enabled": True,
    "ingest_enabled": True,
    "watch_enabled": True,
    "max_watchers": 0,
    "llm_extraction_enabled": True,
    "agent_model_tier": "both",
}


def get_subscription(project_id: int) -> dict | None:
    """Fetch active subscription with plan details for a project. Returns None if not found."""
    db = get_db()
    try:
        sub = db.exec(
            select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.status.in_(["active", "past_due"]),
            )
        ).first()
        if not sub:
            return None

        plan = db.exec(select(Plan).where(Plan.id == sub.plan_id)).first()
        if not plan:
            return None

        storage_plan = None
        if sub.storage_plan_id:
            storage_plan = db.exec(
                select(StoragePlan).where(StoragePlan.id == sub.storage_plan_id)
            ).first()

        # Effective unified ops limit (migration 029): the override wins,
        # then the materialized effective column, then the plan cap. The
        # NULLIF-style middle step matters: effective_ops_month is 0 on
        # subscription rows created between the 029 apply and this build's
        # deploy, and falling through to plan.ops_month self-heals those
        # without a backfill. A RESOLVED 0 means unlimited (enterprise
        # convention).
        if sub.override_ops_month is not None:
            effective_ops_month = sub.override_ops_month
        else:
            effective_ops_month = sub.effective_ops_month or plan.ops_month

        return {
            "subscription_id": sub.id,
            "project_id": sub.project_id,
            "plan_slug": plan.slug,
            "plan_name": plan.name,
            "status": sub.status,
            "overage_enabled": sub.overage_enabled,
            # Effective limits (use these for gating)
            "effective_ops_month": effective_ops_month,
            "ops_month": plan.ops_month,
            "max_file_size": sub.effective_max_file_size,
            "file_retention_hours": sub.effective_file_retention_hours,
            "batch_limit": sub.effective_batch_limit,
            "storage_bytes": sub.effective_storage_bytes,
            # AI credits (migration 029): these two feed the period's
            # ai_credits_granted_cents at creation time; the LLM budget
            # gate reads the materialized period column, not this dict.
            "ai_credits_cents_month": plan.ai_credits_cents_month,
            "override_ai_credits_cents_month": sub.override_ai_credits_cents_month,
            # Plan features (boolean flags)
            "has_async_mode": plan.has_async_mode,
            "has_webhook": plan.has_webhook,
            "has_zip_output": plan.has_zip_output,
            "has_basic_auth": plan.has_basic_auth,
            "crawl_mode": plan.crawl_mode,
            "server_type": plan.server_type,
            "widget_branding": plan.widget_branding,
            "storage_management": plan.storage_management,
            "overage_rate_cents": plan.overage_rate_cents,
            "overage_allowed": plan.overage_allowed,
            # V2 endpoint flags: pure kill-switches since migration 029
            # (the per-endpoint monthly caps were replaced by the unified
            # ops cap and dropped by migration 030). max_watchers stays a
            # real cap — persistent resource, not consumption.
            "perceive_enabled": plan.perceive_enabled,
            "discover_enabled": plan.discover_enabled,
            "lookup_enabled": plan.lookup_enabled,
            "distill_enabled": plan.distill_enabled,
            "ingest_enabled": plan.ingest_enabled,
            "watch_enabled": plan.watch_enabled,
            "max_watchers": plan.max_watchers,
            "llm_extraction_enabled": plan.llm_extraction_enabled,
            "agent_model_tier": plan.agent_model_tier,
            # Billing period
            "current_period_start": sub.current_period_start,
            "current_period_end": sub.current_period_end,
            # Storage plan info
            "storage_plan_slug": storage_plan.slug if storage_plan else None,
            "storage_plan_name": storage_plan.name if storage_plan else None,
        }
    finally:
        db.close()


def get_effective_subscription(project_id: int) -> dict | None:
    """Subscription as the request path sees it (admin bypass included).

    Background workers (e.g. ingest) MUST use this instead of
    get_subscription when re-deriving quota context off-request: the admin
    default project is granted ADMIN_SUBSCRIPTION at request time, so its
    real Subscription row (if any) does not reflect what the submit handler
    allowed — the raw lookup would silently deny the queued work.
    """
    db = get_db()
    try:
        if is_admin_default_project(db, project_id):
            return dict(ADMIN_SUBSCRIPTION)
    finally:
        db.close()
    return get_subscription(project_id)


def get_current_usage_period(project_id: int) -> UsagePeriod | None:
    """Get the current billing period's usage record."""
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        return db.exec(
            select(UsagePeriod).where(
                UsagePeriod.project_id == project_id,
                UsagePeriod.period_start <= now,
                UsagePeriod.period_end > now,
            )
        ).first()
    finally:
        db.close()


def update_storage_peak(project_id: int, current_storage_used: int):
    """Raise the storage high-water mark for the current usage period.

    Single conditional UPDATE: a MAX is idempotent by construction
    (applying it twice, out of order, or redundantly is always safe),
    so this needs neither a ledger row nor an idempotency key — the
    ``storage_bytes_peak < :new`` predicate under the row lock replaces
    the old read-modify-write, which could regress the peak when two
    writers raced.
    """
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        db.execute(
            update(UsagePeriod)
            .where(
                UsagePeriod.project_id == project_id,
                UsagePeriod.period_start <= now,
                UsagePeriod.period_end > now,
                UsagePeriod.storage_bytes_peak < current_storage_used,
            )
            .values(storage_bytes_peak=current_storage_used, updated_at=now)
        )
        db.commit()
    finally:
        db.close()


def is_admin_default_project(db, project_id: int) -> bool:
    """Return True if the project is the default project of an admin user."""
    project = db.exec(select(Project).where(Project.id == project_id)).first()
    if not project or not project.is_default:
        return False
    owner = db.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "owner",
        )
    ).first()
    if not owner:
        return False
    user = db.exec(select(User).where(User.id == owner.user_id)).first()
    return bool(user and user.is_admin)


def recompute_effective_limits(plan_id: int):
    """Recompute effective limits for all subscriptions on a given plan.
    Called when an admin updates a plan's limits."""
    db = get_db()
    try:
        plan = db.exec(select(Plan).where(Plan.id == plan_id)).first()
        if not plan:
            return
        subs = db.exec(
            select(Subscription).where(Subscription.plan_id == plan_id)
        ).all()
        now = datetime.now(timezone.utc)
        for sub in subs:
            # Materialize the unified ops cap (migration 029) — the read
            # path still falls back to plan.ops_month when this is 0, so a
            # stale 0 here degrades gracefully rather than blocking anyone.
            sub.effective_ops_month = sub.override_ops_month if sub.override_ops_month is not None else plan.ops_month
            sub.effective_max_file_size = sub.override_max_file_size if sub.override_max_file_size is not None else plan.max_file_size
            sub.effective_file_retention_hours = sub.override_file_retention_hours if sub.override_file_retention_hours is not None else plan.file_retention_hours
            sub.effective_batch_limit = sub.override_batch_limit if sub.override_batch_limit is not None else plan.batch_limit
            sub.updated_at = now
            db.add(sub)
        db.commit()
    finally:
        db.close()


def get_project_owner_email(project_id: int) -> str | None:
    """Get the email address of the project owner."""
    db = get_db()
    try:
        owner = db.exec(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == "owner",
            )
        ).first()
        if not owner:
            return None
        user = db.exec(select(User).where(User.id == owner.user_id)).first()
        return user.email if user else None
    finally:
        db.close()


def is_project_owner_active(db, project_id: int) -> bool:
    """Return False when the project owner's account is suspended.

    Defaults to True when no owner/user row exists so orphaned projects are
    never locked out by this check. Single JOIN query — this runs on every
    authenticated request via _attach_subscription."""
    row = db.exec(
        select(User.active)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "owner",
        )
    ).first()
    if row is None:
        return True
    return bool(row)
