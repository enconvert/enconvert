from decimal import Decimal
from typing import Literal, Optional, List
from uuid import uuid4
from pydantic import BaseModel, field_validator
from sqlmodel import SQLModel, Field
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSON, JSONB
from datetime import datetime, timezone


# --- PDF Output Options (non-DB Pydantic models) ---

PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
    "A3": (297, 420), "A4": (210, 297), "A5": (148, 210), "A6": (105, 148),
    "B0": (1000, 1414), "B1": (707, 1000), "B2": (500, 707),
    "B3": (353, 500), "B4": (250, 353), "B5": (176, 250),
    "Letter": (216, 279), "Legal": (216, 356),
    "Tabloid": (279, 432), "Ledger": (432, 279),
}


class PdfMargins(BaseModel):
    top: float = 10
    bottom: float = 10
    left: float = 10
    right: float = 10

    @field_validator("top", "bottom", "left", "right")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Margin values must be non-negative")
        return v


class PdfHeaderFooter(BaseModel):
    content: str = ""
    height: float = 15

    @field_validator("content")
    @classmethod
    def content_max_length(cls, v: str) -> str:
        if len(v) > 2000:
            raise ValueError("Header/footer content must be 2000 characters or fewer")
        return v

    @field_validator("height")
    @classmethod
    def positive_height(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Header/footer height must be positive")
        return v


class PdfOptions(BaseModel):
    page_size: str = "A4"
    page_width: Optional[float] = None
    page_height: Optional[float] = None
    orientation: str = "portrait"
    margins: PdfMargins = PdfMargins()
    scale: float = 1.0
    grayscale: bool = False
    header: Optional[PdfHeaderFooter] = None
    footer: Optional[PdfHeaderFooter] = None

    @field_validator("page_size")
    @classmethod
    def valid_page_size(cls, v: str) -> str:
        normalized = v.strip()
        for key in PAGE_SIZES:
            if key.lower() == normalized.lower():
                return key
        raise ValueError(
            f"Unknown page size '{v}'. "
            f"Supported sizes: {', '.join(sorted(PAGE_SIZES.keys()))}"
        )

    @field_validator("orientation")
    @classmethod
    def valid_orientation(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("portrait", "landscape"):
            raise ValueError("Orientation must be 'portrait' or 'landscape'")
        return v

    @field_validator("scale")
    @classmethod
    def valid_scale(cls, v: float) -> float:
        if not (0.1 <= v <= 2.0):
            raise ValueError("Scale must be between 0.1 and 2.0")
        return v

    @field_validator("page_width", "page_height")
    @classmethod
    def positive_dimension(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("Custom page dimensions must be positive")
        return v

    def get_dimensions_mm(self) -> tuple[float, float]:
        """Return (width, height) in mm, applying orientation."""
        if self.page_width is not None and self.page_height is not None:
            w, h = self.page_width, self.page_height
        else:
            w, h = PAGE_SIZES[self.page_size]

        if self.orientation == "landscape" and w < h:
            w, h = h, w
        elif self.orientation == "portrait" and w > h:
            w, h = h, w

        return w, h


# --- Batch Status Response Models ---

class BatchItemResponse(BaseModel):
    source_url: str
    status: str
    download_url: Optional[str] = None
    output_file_size: Optional[int] = None
    duration: Optional[str] = None


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: Literal["processing", "completed", "partial", "failed"]
    total: int
    completed: int
    failed: int
    in_progress: int
    output_mode: Literal["zip", "individual"]
    zip_download_url: Optional[str] = None
    items: list[BatchItemResponse]


# --- Database Models ---

class User(SQLModel, table=True):
    __tablename__ = "ch_users"
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    full_name: str
    password_hash: str
    is_email_verified: bool = False
    is_admin: bool = False
    active: bool = True
    banned_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    banned_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Optional[datetime] = None


class APIKeys(SQLModel, table=True):
    __tablename__ = "ch_api_keys"
    id: int | None = Field(default=None, primary_key=True)
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    key: str
    key_prefix: str
    key_type: str
    name: str
    project_id: int = Field(index=True)
    allowed_domains: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    allowed_endpoints: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    allowed_ips: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    # Throttle for the "key used from an unauthorized domain" alert (migration
    # 009). SCHEMA TWIN of backend/models.py APIKeys.
    last_unauthorized_alert_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class Project(SQLModel, table=True):
    __tablename__ = "ch_projects"
    id: int | None = Field(default=None, primary_key=True)
    name: str
    is_default: bool = False
    storage_used: int = 0
    # H.8: per-project HMAC-SHA256 key for /v2/ingest (later /v2/watch) webhook
    # signing. NULL until first delivery; lazily generated + persisted by
    # utils/webhook_secret.get_or_create_webhook_secret. Column from migration
    # 015_webhook_secrets.sql.
    webhook_secret: Optional[str] = Field(default=None, max_length=80)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Optional[datetime] = None


class Widget(SQLModel, table=True):
    __tablename__ = "ch_widgets"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    user_id: str
    name: str
    endpoint: str
    api_key_id: int
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class Activity(SQLModel, table=True):
    __tablename__ = "ch_activity"
    id: int | None = Field(default=None, primary_key=True)
    duration: str = ""
    endpoint: str
    url: str = ""
    input_file_size: str
    output_file_size: str = ""
    status: str = "In Progress"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    project_id: str
    batch_id: Optional[str] = Field(default=None, index=True)
    source_url: Optional[str] = None
    # V2 Phase 0 instrumentation (migration 009). Populated only by
    # Playwright-backed converters; remains NULL/0 for everything else.
    content_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    rendered_html_key: Optional[str] = Field(default=None, max_length=512)
    console_error_count: int = 0
    page_load_time_ms: int = 0


class ProjectMember(SQLModel, table=True):
    __tablename__ = "ch_project_members"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    user_id: int = Field(index=True)
    role: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class Alert(SQLModel, table=True):
    __tablename__ = "ch_alerts"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    alert_type: str = Field(index=True)
    severity: str = "info"
    title: str
    message: str = Field(sa_column=Column(Text, default=""))
    alert_metadata: Optional[dict] = Field(default=None, sa_column=Column("metadata", JSON, nullable=True))
    link: Optional[str] = None
    is_visible: bool = Field(default=True, index=True)
    cleared_from_popup: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class Plan(SQLModel, table=True):
    __tablename__ = "ch_plans"
    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=20, unique=True)
    name: str = Field(max_length=50)
    price_monthly: int = 0
    conversion_limit: int = 100
    max_file_size: int = 5242880
    file_retention_hours: int = 1
    batch_limit: int = 0
    has_async_mode: bool = False
    has_webhook: bool = False
    has_zip_output: bool = False
    has_basic_auth: bool = False
    crawl_mode: str = "none"
    server_type: str = "shared"
    widget_branding: bool = True
    storage_management: str = "manual"
    overage_rate_cents: float = 0
    overage_allowed: bool = False
    is_active: bool = True
    # V2 plan gates + quotas (migration 011; per-slug defaults seeded by 012).
    # V3 plan section 7 prose lists 11 columns, but its defaults table and the
    # F.4 verification queries also require perceive_operations_month and
    # distill_operations_month — 13 columns total.
    perceive_enabled: bool = False
    perceive_operations_month: int = 0
    discover_enabled: bool = False
    lookup_enabled: bool = False
    lookup_queries_month: int = 0
    distill_enabled: bool = False
    distill_operations_month: int = 0
    ingest_enabled: bool = False
    ingest_pages_month: int = 0
    watch_enabled: bool = False
    max_watchers: int = 0
    llm_extraction_enabled: bool = False
    agent_model_tier: str = Field(default="none", max_length=20)  # LLM agent tier selector
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class StoragePlan(SQLModel, table=True):
    __tablename__ = "ch_storage_plans"
    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=20, unique=True)
    name: str = Field(max_length=50)
    storage_bytes: int
    price_monthly: int = 0
    is_active: bool = True
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


# SCHEMA TWIN: Subscription/UsagePeriod/Plan (and ~17 more table classes) are
# duplicated in backend/models.py — two services, two Python model trees, ONE
# Postgres schema. Any column/type/default change here MUST be mirrored there
# AND carried by a db/migrations/*.sql migration. Timestamp columns are
# TIMESTAMPTZ in the real DDL (migration 001); sa_type=DateTime(timezone=True)
# makes the ORM declare the same so create_all-bootstrapped dev DBs match prod.
class Subscription(SQLModel, table=True):
    __tablename__ = "ch_subscriptions"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(unique=True, index=True)
    plan_id: int = Field(index=True)
    storage_plan_id: Optional[int] = None
    status: str = "active"
    payment_provider: Optional[str] = None
    payment_subscription_id: Optional[str] = None
    storage_payment_subscription_id: Optional[str] = None
    overage_enabled: bool = False
    has_used_trial: bool = False
    trial_end: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    pending_plan_id: Optional[int] = None
    current_period_start: datetime = Field(sa_type=DateTime(timezone=True))
    current_period_end: datetime = Field(sa_type=DateTime(timezone=True))
    storage_period_start: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    storage_period_end: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    # Enterprise overrides
    override_conversion_limit: Optional[int] = None
    override_max_file_size: Optional[int] = None
    override_file_retention_hours: Optional[int] = None
    override_batch_limit: Optional[int] = None
    # Materialized effective limits
    effective_conversion_limit: int
    effective_max_file_size: int
    effective_file_retention_hours: int
    effective_batch_limit: int
    effective_storage_bytes: int = 0
    # Notification throttles/dedup (migration 009). last_quota_alert_at is set by
    # the gateway on 100% usage; the *_sent_at columns dedup the backend cron
    # reminders. SCHEMA TWIN of backend/models.py Subscription.
    last_quota_alert_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    trial_reminder_sent_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    storage_lapse_warned_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


# SCHEMA TWIN: duplicated in backend/models.py — keep both in sync (see
# Subscription's twin note above).
class UsagePeriod(SQLModel, table=True):
    __tablename__ = "ch_usage_periods"
    # Matches migration 001's inline UNIQUE(project_id, period_start) —
    # declared here too so create_all-bootstrapped dev DBs carry the
    # constraint the ON CONFLICT (project_id, period_start) upserts
    # (billing_rotation, backend _upsert_usage_period) resolve against.
    __table_args__ = (
        UniqueConstraint(
            "project_id", "period_start",
            name="ch_usage_periods_project_id_period_start_key",
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    period_start: datetime = Field(sa_type=DateTime(timezone=True))
    period_end: datetime = Field(sa_type=DateTime(timezone=True))
    plan_id: int = Field(index=True)
    conversions_used: int = 0
    overage_conversions: int = 0
    storage_bytes_peak: int = 0
    # V2 usage counters (migration 011) — independent of V1 conversions_used
    # (coexistence rule 3, V3 plan section 5).
    perceive_operations: int = 0
    ingest_pages: int = 0
    watch_checks: int = 0
    lookup_queries: int = 0
    distill_operations: int = 0
    # NUMERIC(12,4) cents: a single LLM call costs a fraction of a cent,
    # so INTEGER cents would round real costs to zero and break the F.6 gate.
    llm_cost_cents: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(12, 4), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    updated_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


# SCHEMA TWIN: duplicated in backend/models.py (backend writes the
# plan-change compensating rows; the gateway writes everything else).
# Append-only source of truth for the money-critical usage counters
# (migration 016). The UNIQUE index on idempotency_key IS the dedup
# mechanism: INSERT ... ON CONFLICT (idempotency_key) DO NOTHING
# RETURNING id answers "was this exact event already counted?" in one
# round trip. Deltas may be NEGATIVE (llm settle reconciliation,
# plan-change reset compensation) — do not add sign constraints.
class UsageLedger(SQLModel, table=True):
    __tablename__ = "ch_usage_ledger"
    # Mirrors migration 016's CHECKs exactly (names + predicates) so
    # create_all dev DBs enforce what prod enforces. (Divergence note:
    # prod FKs carry ON DELETE CASCADE; the plain foreign_key= here does
    # not — referential integrity matches, cascade behavior is prod-only.)
    __table_args__ = (
        CheckConstraint(
            "counter IN ('conversions_used', 'llm_cost_cents')",
            name="ck_usage_ledger_counter",
        ),
        CheckConstraint(
            "(counter = 'llm_cost_cents'"
            " AND delta_cost_cents IS NOT NULL AND delta_units IS NULL)"
            " OR (counter <> 'llm_cost_cents'"
            " AND delta_units IS NOT NULL AND delta_cost_cents IS NULL)",
            name="ck_usage_ledger_delta_shape",
        ),
    )
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    idempotency_key: str = Field(max_length=200, unique=True)
    project_id: int = Field(index=True, foreign_key="ch_projects.id")
    usage_period_id: int = Field(index=True, foreign_key="ch_usage_periods.id")
    counter: str = Field(max_length=32)  # conversions_used | llm_cost_cents
    # v1_conversion | llm_reserve | llm_settle | plan_change_reset
    event_type: str = Field(max_length=40)
    delta_units: Optional[int] = None
    delta_cost_cents: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(12, 4), nullable=True),
    )
    context: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )


# Gateway-only (written by scripts/ops/reconcile_usage_ledger.py; not
# duplicated in backend). One row per reconciliation run; rows_flagged
# trending at zero night after night is the drift-free confidence signal.
class ReconciliationRun(SQLModel, table=True):
    __tablename__ = "ch_reconciliation_runs"
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    started_at: datetime = Field(sa_type=DateTime(timezone=True))
    finished_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    periods_checked: int = 0
    rows_flagged: int = 0
    status: str = Field(default="ok", max_length=20)  # ok | drift | error
    error_message: Optional[str] = None
    discrepancies: Optional[list] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )


class PaymentHistory(SQLModel, table=True):
    __tablename__ = "ch_payment_history"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    paypal_transaction_id: Optional[str] = None
    paypal_subscription_id: Optional[str] = None
    subscription_type: str = "plan"
    amount_value: str = "0.00"
    amount_currency: str = "USD"
    status: str = "COMPLETED"
    payment_time: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )


# Gateway-only (written by services/subscription_emails.py, the email pass run
# by the enconvert-billing-rotation systemd timer; not duplicated in backend).
# Dedup/audit log for subscription lifecycle emails (migration 019). The UNIQUE
# index on email_key IS the dedup mechanism, mirroring ch_usage_ledger's
# idempotency_key: INSERT ... ON CONFLICT (email_key) DO NOTHING RETURNING id
# is the claim. sent_ok=False with attempts >= 1 means claimed-but-send-failed
# (retryable, capped); attempts=0 rows older than a grace period mean the
# claimant crashed between claim commit and send.
class EmailLog(SQLModel, table=True):
    __tablename__ = "ch_email_log"
    # Mirrors migration 019's indexes exactly (names + predicates) so
    # create_all scratch DBs match prod (same pattern as ScheduledDeletion).
    __table_args__ = (
        Index(
            "idx_email_log_unsent",
            "created_at",
            postgresql_where=text("sent_ok = FALSE"),
        ),
        Index(
            "idx_email_log_project",
            "project_id",
            text("created_at DESC"),
        ),
    )
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    # Plain integer, deliberately NO foreign key: an audit log must survive
    # project deletion (same reasoning as ch_scheduled_deletions).
    project_id: int
    # trial_reminder | storage_lapse | overage_receipt | renewal | upcoming_charge
    email_type: str = Field(max_length=40)
    email_key: str = Field(max_length=200, unique=True)
    recipient: str = Field(max_length=255)
    sent_ok: bool = False
    attempts: int = 0
    last_error: Optional[str] = None
    # Everything the template needs, so the retry sub-pass can rebuild the
    # email without re-querying business tables whose state may have moved.
    context: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    # Confirmed-delivery time; NULL until sent_ok.
    sent_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class ConversionJob(SQLModel, table=True):
    __tablename__ = "ch_conversion_jobs"
    id: str = Field(primary_key=True)
    project_id: str = Field(index=True)
    status: str = ""
    presigned_url: str = ""
    object_key: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class ScheduledDeletion(SQLModel, table=True):
    """Durable file-retention schedule (migration 017), swept by the
    droplet-local retention worker — the no-GCP replacement for the Cloud
    Tasks file-cleanup trigger. One PENDING row per object_key; the partial
    UNIQUE index (object_key WHERE deleted_at IS NULL) makes scheduling
    idempotent, and the partial due index (delete_at WHERE deleted_at IS NULL)
    is the poller's hot scan."""
    __tablename__ = "ch_scheduled_deletions"
    __table_args__ = (
        Index(
            "uq_scheduled_deletions_pending_key",
            "object_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_scheduled_deletions_due",
            "delete_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    object_key: str = Field(sa_column=Column(Text, nullable=False))
    project_id: Optional[int] = None
    delete_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_type=DateTime(timezone=True),
    )
    deleted_at: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    attempts: int = 0
    last_error: Optional[str] = None


# ── V2 Endpoint Tables (migration 011, Sprint F.4) ────────────────
# Written by the /v2/* handlers (Groups F/H/I/J). Mirrors
# backend/models.py. Schema source of truth is
# db/migrations/011_v2_endpoints.sql.


class PerceiveOperation(SQLModel, table=True):
    """One row per /v2/perceive operation (Tasks F.5/F.6/F.8)."""
    __tablename__ = "ch_perceive_operations"
    id: int | None = Field(default=None, primary_key=True)
    operation_id: str = Field(unique=True, index=True, max_length=64)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    url: str
    url_final: Optional[str] = None
    status: str = Field(default="processing", max_length=20)  # processing | completed | failed
    content_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    render_quality_score: Optional[float] = None  # 0.0-1.0 (F.7 scorer)
    is_blocked: bool = False
    is_login_wall: bool = False
    outputs_requested: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    output_keys: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    structured_data: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    extraction_tier: Optional[str] = Field(default=None, max_length=20)  # heuristic | css | llm
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_cents: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(12, 4), nullable=False),
    )
    cache_hit: bool = False
    batch_id: Optional[str] = Field(default=None, max_length=64, index=True)
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Optional[datetime] = None


class PerceiveBatch(SQLModel, table=True):
    """One row per /v2/perceive/batch job — the durable job envelope.

    The per-URL work already lives in ch_perceive_operations (grouped by
    batch_id); this table persists the batch envelope that used to live
    only in memory (the shared render ``options`` and ``output_mode``), so
    an interrupted batch RESUMES after a restart instead of being failed.
    Status flow: queued -> processing -> completed | partial | failed |
    canceled.
    """
    __tablename__ = "ch_perceive_batches"
    id: int | None = Field(default=None, primary_key=True)
    batch_id: str = Field(unique=True, index=True, max_length=64)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    status: str = Field(default="queued", max_length=20)
    output_mode: str = Field(default="manifest", max_length=20)  # manifest | zip
    # The single shared PerceiveOptions block for every URL (JSON-mode dump);
    # each URL's row in ch_perceive_operations carries only url + operation_id,
    # so the worker rebuilds each PerceiveRequest from these options on resume.
    options: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    total: int = 0
    completed: int = 0
    failed: int = 0
    zip_object_key: Optional[str] = Field(default=None, max_length=512)
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class IngestJob(SQLModel, table=True):
    """One row per /v2/ingest job; Cloud Tasks-driven lifecycle (Task H.7).

    Status flow: queued -> discovering -> processing -> completed | failed
    | canceled.
    """
    __tablename__ = "ch_ingest_jobs"
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(unique=True, index=True, max_length=64)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    source_url: Optional[str] = None                       # single URL / sitemap / crawl seed
    source_urls: Optional[List[str]] = Field(              # explicit URL-list mode
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    mode: str = Field(default="urls", max_length=20)       # sitemap | crawl | urls
    chunk_options: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    status: str = Field(default="queued", max_length=20)
    pages_discovered: int = 0
    pages_processed: int = 0
    pages_failed: int = 0
    total_chunks: int = 0
    output_key: Optional[str] = Field(default=None, max_length=512)  # final JSONL key in Spaces
    webhook_url: Optional[str] = None
    webhook_delivered: bool = False                        # H.8: set True on 2xx
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class IngestPage(SQLModel, table=True):
    """Per-URL row within an ingest job — resume/partial recovery (Task H.7).

    Uniqueness on (job_id, md5(url)) is an expression index owned by
    db/migrations/011_v2_endpoints.sql; it cannot be declared here.
    """
    __tablename__ = "ch_ingest_pages"
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="ch_ingest_jobs.job_id", index=True, max_length=64)
    url: str                                               # URL, or (file mode) the uploaded object key
    source_type: str = Field(default="url", max_length=10)  # url | file (migration 020)
    filename: Optional[str] = None                         # original upload name (file pages only)
    status: str = Field(default="pending", max_length=20)  # pending | processing | completed | failed | skipped
    chunk_count: int = 0
    word_count: int = 0
    content_hash: Optional[str] = Field(default=None, max_length=64)
    error_message: Optional[str] = None
    processed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class Watcher(SQLModel, table=True):
    """One row per /v2/watch monitor (Tasks I.1/I.2)."""
    __tablename__ = "ch_watchers"
    id: int | None = Field(default=None, primary_key=True)
    watcher_id: str = Field(unique=True, index=True, max_length=64)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    url: str
    status: str = Field(default="active", max_length=20)   # active | paused | deleted
    frequency_minutes: int = 60                            # hourly floor enforced in app (I.1)
    diff_mode: str = Field(default="auto", max_length=20)  # auto | text | structured | tables | metadata
    track_fields: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    webhook_url: Optional[str] = None
    notify_email: bool = True
    consecutive_errors: int = 0                            # >= 3 pauses the watcher (I.1)
    checks_count: int = 0
    last_check_at: Optional[datetime] = None
    next_check_at: Optional[datetime] = Field(default=None, index=True)
    last_change_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Optional[datetime] = None


class WatcherSnapshot(SQLModel, table=True):
    """One row per watcher check; capture body lives in Spaces (Task I.3)."""
    __tablename__ = "ch_watcher_snapshots"
    id: int | None = Field(default=None, primary_key=True)
    watcher_id: str = Field(foreign_key="ch_watchers.watcher_id", index=True, max_length=64)
    content_hash: Optional[str] = Field(default=None, max_length=64)
    snapshot_key: Optional[str] = Field(default=None, max_length=512)  # Spaces object key
    structured_data: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    render_quality_score: Optional[float] = None
    has_changes: bool = False
    similarity: Optional[float] = None                     # DiffResult.similarity
    changes: Optional[list] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class LookupQuery(SQLModel, table=True):
    """One row per /v2/lookup Serper call (Task H.3)."""
    __tablename__ = "ch_lookup_queries"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    query: str
    category: str = Field(default="web", max_length=20)    # web | news | images | scholar | patents | maps
    country: Optional[str] = Field(default=None, max_length=8)
    locale: Optional[str] = Field(default=None, max_length=16)
    time_filter: Optional[str] = Field(default=None, max_length=20)
    results_count: int = 0
    perceive_top: int = 0
    perceive_operation_ids: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(ARRAY(String), nullable=True)
    )
    serper_cost_cents: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(12, 4), nullable=False),
    )
    status: str = Field(default="completed", max_length=20)  # completed | failed
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)


class DistillOperation(SQLModel, table=True):
    """One row per URL per /v2/distill request (Task H.5).

    operation_id groups the rows of one request — indexed, NOT unique.
    """
    __tablename__ = "ch_distill_operations"
    id: int | None = Field(default=None, primary_key=True)
    operation_id: str = Field(index=True, max_length=64)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    url: str
    extraction_schema: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    result_data: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    extraction_tier: Optional[str] = Field(default=None, max_length=20)  # css | llm | mixed
    fields_from_css: int = 0
    fields_from_llm: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_cents: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(12, 4), nullable=False),
    )
    status: str = Field(default="processing", max_length=20)  # processing | completed | failed
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Optional[datetime] = None


class ExtractionFeedback(SQLModel, table=True):
    """/v2/feedback corrections (Task J.4) — Phase 4 ML training labels.

    operation_id may reference a perceive OR a distill operation
    (polymorphic), so it carries no foreign key by design.
    """
    __tablename__ = "ch_extraction_feedback"
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="ch_projects.id", index=True)
    operation_id: str = Field(index=True, max_length=64)
    field_name: str
    original_value: Optional[str] = None
    corrected_value: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), nullable=False)
