"""Pydantic models for the /v2/watch lifecycle (Tasks I.1/I.2).

``/v2/watch`` turns any URL into a change monitor: a droplet-local scheduler
re-renders the page on a fixed cadence, diffs it against the previous capture
and notifies the owner when it changes. There is NO Google Cloud Tasks in this
design (owner decision 2026-06-07: no Google services) — the schedule lives in
``ch_watchers.next_check_at`` and the in-process ``watch_worker`` polls it,
self-rescheduling after every check. See ``services/v2_engine/watch_worker.py``.

Request surface (plan section 4 / section 8 Tasks I.1/I.2):

* ``url`` — the page to monitor (http/https only; SSRF-screened at create and
  again before every render).
* ``frequency_minutes`` — how often to re-check. The HOURLY FLOOR is hard:
  values below 60 are rejected (plan section 4 / Task I.1 step 3). The
  scheduler floors a second time so a stale row can never out-pace it.
* ``diff_mode`` — which diff strategy the I.3 engine applies (``auto`` lets it
  pick by content type). Stored at create; consumed by Task I.3.
* ``track_fields`` — optional field/selector subset to watch (Task I.3).
* ``webhook_url`` — optional change-notification target. Stored only; HMAC
  delivery lands in Task I.4. SSRF-screened at delivery time, not here.
* ``notify_email`` — email the project owner on a change (and on auto-pause).

Watchers are credential-free by design (no auth/cookies/headers): nothing
secret is persisted for the recurring render (security: no secrets at rest),
mirroring /v2/ingest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

DiffMode = Literal["auto", "text", "structured", "tables", "metadata"]
WatcherStatus = Literal["active", "paused", "deleted"]

# Hourly floor (plan section 4 / Task I.1 step 3) and a sane upper bound so a
# single watcher cannot be parked effectively forever (30 days).
MIN_FREQUENCY_MINUTES = 60
MAX_FREQUENCY_MINUTES = 43_200


def _require_http_url(value: str) -> str:
    """Scheme-only guard shared by url + webhook_url validators."""
    value = value.strip()
    lowered = value.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("must start with http:// or https://")
    return value


class WatchCreateRequest(BaseModel):
    """Create one watcher (POST /v2/watch, Task I.2)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048, description="The page to monitor.")
    frequency_minutes: int = Field(
        default=MIN_FREQUENCY_MINUTES,
        ge=MIN_FREQUENCY_MINUTES,
        le=MAX_FREQUENCY_MINUTES,
        description="Minutes between checks. Hourly floor: minimum 60.",
    )
    diff_mode: DiffMode = "auto"
    track_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional field/selector subset to track (Task I.3).",
    )
    webhook_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Optional change-notification webhook (delivery in I.4).",
    )
    notify_email: bool = True

    @field_validator("url")
    @classmethod
    def _url_http_only(cls, value: str) -> str:
        try:
            return _require_http_url(value)
        except ValueError as exc:
            raise ValueError(f"url {exc}") from exc

    @field_validator("webhook_url")
    @classmethod
    def _webhook_http_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            return _require_http_url(value)
        except ValueError as exc:
            raise ValueError(f"webhook_url {exc}") from exc


class WatchUpdateRequest(BaseModel):
    """Patch a watcher (PATCH /v2/watch/{watcher_id}, Task I.2).

    Every field is optional — only the keys present in the body are applied.
    ``status`` toggles active/paused (resume re-arms the schedule, pause stops
    it); the terminal ``deleted`` state is reached through DELETE, not here.
    """

    model_config = ConfigDict(extra="forbid")

    frequency_minutes: Optional[int] = Field(
        default=None,
        ge=MIN_FREQUENCY_MINUTES,
        le=MAX_FREQUENCY_MINUTES,
    )
    diff_mode: Optional[DiffMode] = None
    track_fields: Optional[dict[str, Any]] = None
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    notify_email: Optional[bool] = None
    status: Optional[Literal["active", "paused"]] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "WatchUpdateRequest":
        """Reject an empty PATCH (``{}``) with 422 rather than silently no-op."""
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("provide at least one field to update")
        return self

    @field_validator("webhook_url")
    @classmethod
    def _webhook_http_only(cls, value: Optional[str]) -> Optional[str]:
        # An empty string is the explicit "clear the webhook" signal; the flow
        # translates it to NULL. A non-empty value must be http(s).
        if value is None:
            return None
        value = value.strip()
        if not value:
            return ""
        try:
            return _require_http_url(value)
        except ValueError as exc:
            raise ValueError(f"webhook_url {exc}") from exc


class WatcherResponse(BaseModel):
    """Full view of one watcher (POST/GET/PATCH/DELETE share it)."""

    watcher_id: str
    url: str
    status: WatcherStatus
    frequency_minutes: int
    diff_mode: DiffMode
    track_fields: Optional[dict[str, Any]] = None
    webhook_url: Optional[str] = None
    notify_email: bool = True
    consecutive_errors: int = 0
    checks_count: int = 0
    last_check_at: Optional[datetime] = None
    next_check_at: Optional[datetime] = None
    last_change_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WatcherSummary(BaseModel):
    """Compact watcher row for the dashboard list (Task I.4)."""

    watcher_id: str
    url: str
    status: WatcherStatus
    frequency_minutes: int
    checks_count: int = 0
    consecutive_errors: int = 0
    last_check_at: Optional[datetime] = None
    next_check_at: Optional[datetime] = None
    last_change_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class WatcherListResponse(BaseModel):
    """One newest-first page of a project's watchers (Task I.2/I.4)."""

    watchers: list[WatcherSummary] = Field(default_factory=list)
    skip: int = 0
    limit: int = 20
    has_more: bool = Field(
        default=False,
        description="True when more watchers exist beyond this page.",
    )


class WatcherSnapshotResponse(BaseModel):
    """One check's result for the dashboard timeline (Task I.4).

    ``changes`` is the I.3 diff verdict (already bounded + value-capped by the
    diff engine); the values are untrusted page content, so any HTML consumer
    must escape them.
    """

    checked_at: datetime
    has_changes: bool = False
    similarity: Optional[float] = None
    render_quality: Optional[float] = None
    change_count: int = 0
    changes: list[dict[str, Any]] = Field(default_factory=list)


class WatcherSnapshotListResponse(BaseModel):
    """Newest-first page of a watcher's snapshots (Task I.4)."""

    watcher_id: str
    snapshots: list[WatcherSnapshotResponse] = Field(default_factory=list)
    limit: int = 20
