"""Pydantic models for POST/GET/DELETE /v2/ingest (Task H.7).

``/v2/ingest`` is Firecrawl ``/crawl`` + chunking for EnConvert: it turns a
site (or an explicit URL list) into RAG-ready chunks and emits a single
JSONL file (LangChain ``JSONLoader`` / LlamaIndex ``SimpleDirectoryReader``
/ vector-DB bulk-import compatible). It is ALWAYS asynchronous — per-page
browser renders run 10-30 s each (F.1 perf data), far over the 300 s request
window for any non-trivial job — so POST answers 202 with a ``job_id`` and
the droplet-local ingest worker drains it; GET reports progress; DELETE
cancels (the worker sees the canceled status and stops between pages).

Request surface (plan section 4 / section 8 Task H.7):

* ``mode`` — ``urls`` (explicit list), ``sitemap`` or ``crawl`` (discover a
  site first via ``discover_flow``, then ingest each URL).
* ``url`` XOR ``urls`` — ``sitemap``/``crawl`` require a seed ``url``;
  ``urls`` mode requires a non-empty ``urls`` list. Exactly one source.
* discovery knobs (``max_pages``, ``max_depth``, ``same_domain_only``,
  ``include_patterns`` / ``exclude_patterns`` regex, ``respect_robots``) —
  passed through to ``discover_flow`` for sitemap/crawl mode.
* render knobs (``wait_for``, ``wait_timeout_ms``, ``respect_robots``) — the
  per-page render is intentionally credential-free: ingest does NOT accept
  auth/cookies/custom headers, so nothing secret is persisted for the
  durable resume (security: no secrets at rest).
* ``chunk`` — heading-aware chunker parameters (``max_words``,
  ``sentence_overlap``).
* ``webhook_url`` — stored for completion delivery (HMAC signing lands in
  Task H.8).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.v2_engine.chunking.semantic import (
    DEFAULT_MAX_WORDS,
    DEFAULT_SENTENCE_OVERLAP,
    MAX_MAX_WORDS,
    MAX_SENTENCE_OVERLAP,
    MIN_MAX_WORDS,
)
from utils.callback_notifier import (
    DEFAULT_REPLAY_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    SIGNATURE_SCHEME,
    TIMESTAMP_HEADER,
)

IngestMode = Literal["urls", "sitemap", "crawl", "files"]
IngestStatus = Literal[
    "queued", "discovering", "processing", "completed", "failed", "canceled"
]

# Per-request page ceiling. The monthly ``ingest_pages`` quota is the real
# spend limiter (enforced per page in ingest_flow); this just bounds a single
# job so one request cannot enqueue an unbounded crawl.
MAX_PAGES_PER_JOB = 1000


class ChunkOptions(BaseModel):
    """Heading-aware chunker parameters (bounds mirror chunking.semantic)."""

    model_config = ConfigDict(extra="forbid")

    max_words: int = Field(
        default=DEFAULT_MAX_WORDS,
        ge=MIN_MAX_WORDS,
        le=MAX_MAX_WORDS,
        description="Soft cap on words per chunk. Code blocks and tables stay "
        "atomic and may exceed this.",
    )
    sentence_overlap: int = Field(
        default=DEFAULT_SENTENCE_OVERLAP,
        ge=0,
        le=MAX_SENTENCE_OVERLAP,
        description="Sentences repeated between consecutive prose chunks of "
        "the same section (0 disables overlap).",
    )


class IngestRequest(BaseModel):
    """One /v2/ingest job request (plan section 4 / section 8 Task H.7)."""

    model_config = ConfigDict(extra="forbid")

    mode: IngestMode = "urls"
    url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Seed URL for sitemap/crawl mode (the site to ingest).",
    )
    urls: Optional[list[str]] = Field(
        default=None,
        max_length=MAX_PAGES_PER_JOB,
        description="Explicit URLs to ingest (urls mode). Mutually exclusive "
        "with url.",
    )

    # Discovery knobs (sitemap / crawl), forwarded to discover_flow.
    max_pages: int = Field(
        default=50,
        ge=1,
        le=MAX_PAGES_PER_JOB,
        description="Cap on URLs discovered AND ingested in sitemap/crawl mode.",
    )
    max_depth: int = Field(default=2, ge=1, le=5)
    same_domain_only: bool = True
    include_patterns: list[str] = Field(default_factory=list, max_length=50)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=50)
    respect_robots: bool = False

    # Render knobs (credential-free subset of /v2/perceive).
    wait_for: Optional[str] = Field(default=None, max_length=1024)
    wait_timeout_ms: int = Field(default=30000, ge=0, le=60000)

    # Markdown knobs — same names, semantics and defaults as /v2/perceive,
    # because both endpoints now produce the SAME markdown for a page.
    only_main_content: bool = Field(
        default=True,
        description="When true (default), each page's markdown strips site "
        "chrome (navigation, header, footer, sidebars, cookie banners, "
        "hidden nodes) behind a fidelity guard that falls back to the full "
        "page when stripping would remove too much real content. When "
        "false, the full page is chunked as-is.",
    )
    truncate_data_arrays: Optional[bool] = Field(
        default=None,
        description="Collapse long runs of numeric literals (raw embedding "
        "vectors, tensor dumps in notebook output cells) to a leading "
        "sample plus a count. Unset (the default) follows "
        "only_main_content.",
    )

    chunk: ChunkOptions = Field(default_factory=ChunkOptions)

    webhook_url: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("url")
    @classmethod
    def _url_http_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        lowered = value.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @field_validator("urls")
    @classmethod
    def _urls_http_only(cls, urls: Optional[list[str]]) -> Optional[list[str]]:
        if urls is None:
            return None
        cleaned: list[str] = []
        for raw in urls:
            value = (raw or "").strip()
            lowered = value.lower()
            if not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError("every url must start with http:// or https://")
            if len(value) > 2048:
                raise ValueError("url exceeds 2048 characters")
            cleaned.append(value)
        return cleaned

    @field_validator("webhook_url")
    @classmethod
    def _webhook_http_only(cls, value: Optional[str]) -> Optional[str]:
        # Scheme-only check here. Delivery + HMAC signing land in Task H.8; at
        # submit time the value is only stored. The DELIVERY path
        # (ingest_flow.deliver_ingest_webhook) SSRF-screens this URL with
        # assert_public_http_url right before POSTing — storing an
        # internal/metadata URL is inert until something sends to it.
        if value is None:
            return None
        value = value.strip()
        lowered = value.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("webhook_url must start with http:// or https://")
        return value

    @field_validator("include_patterns", "exclude_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}")
        return patterns

    @model_validator(mode="after")
    def _source_matches_mode(self) -> "IngestRequest":
        """``urls`` mode needs ``urls``; ``sitemap``/``crawl`` need ``url``."""
        if self.mode == "urls":
            if not self.urls:
                raise ValueError("mode 'urls' requires a non-empty 'urls' list")
            if self.url is not None:
                raise ValueError("mode 'urls' does not accept 'url'; use 'urls'")
        else:  # sitemap | crawl
            if not self.url:
                raise ValueError(f"mode '{self.mode}' requires a seed 'url'")
            if self.urls is not None:
                raise ValueError(
                    f"mode '{self.mode}' does not accept 'urls'; use 'url'"
                )
        return self


class IngestJobResponse(BaseModel):
    """Lifecycle view of one ingest job (POST 202, GET, DELETE all share it)."""

    job_id: str
    status: IngestStatus
    mode: IngestMode
    pages_discovered: int = 0
    pages_found: Optional[int] = Field(
        default=None,
        description=(
            "Unique eligible URLs discovery yielded BEFORE the max_pages "
            "cap (sitemap: true unique count; crawl: bounded by the crawl "
            "budget, so a lower bound). Absent for explicit-urls jobs and "
            "jobs created before migration 026."
        ),
    )
    discovery_truncated: bool = Field(
        default=False,
        description=(
            "True when discovery found more unique URLs than max_pages "
            "allowed the job to enqueue."
        ),
    )
    pages_processed: int = 0
    pages_failed: int = 0
    total_chunks: int = 0
    output_url: Optional[str] = Field(
        default=None,
        description="Signed URL to the final JSONL; present once completed.",
    )
    error_message: Optional[str] = None
    webhook_url: Optional[str] = Field(
        default=None,
        description="Completion-webhook target registered for this job, if any.",
    )
    webhook_delivered: bool = Field(
        default=False,
        description="True once the signed completion webhook got a 2xx (H.8).",
    )
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    warnings: list[str] = Field(default_factory=list)


class IngestJobSummary(BaseModel):
    """Compact job row for the dashboard list (GET /v2/ingest, Task H.8)."""

    job_id: str
    status: IngestStatus
    mode: IngestMode
    pages_discovered: int = 0
    pages_found: Optional[int] = None
    discovery_truncated: bool = False
    pages_processed: int = 0
    pages_failed: int = 0
    total_chunks: int = 0
    output_url: Optional[str] = Field(
        default=None,
        description="Signed URL to the final JSONL; present once completed.",
    )
    error_message: Optional[str] = None
    webhook_configured: bool = Field(
        default=False,
        description="True when a completion-webhook URL is registered.",
    )
    webhook_delivered: bool = False
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class IngestJobListResponse(BaseModel):
    """One newest-first page of a project's ingest jobs (Task H.8)."""

    jobs: list[IngestJobSummary] = Field(default_factory=list)
    skip: int = 0
    limit: int = 20
    has_more: bool = Field(
        default=False,
        description="True when more jobs exist beyond this page.",
    )


class WebhookRetryResponse(BaseModel):
    """Result of a manual completion-webhook redelivery (Task H.8)."""

    job_id: str
    delivered: bool
    attempts: int = Field(description="Number of POST attempts made.")
    status_code: Optional[int] = Field(
        default=None,
        description="HTTP status of the last attempt (null on network error).",
    )
    detail: str = Field(description="Human-readable delivery outcome.")


class WebhookSecretResponse(BaseModel):
    """A project's webhook signing secret plus the headers a consumer verifies.

    SENSITIVE: returned only over the authenticated dashboard channel so a
    customer can configure signature verification on their endpoint. ``rotated``
    is True when this response just replaced the previous secret (signatures
    computed with the old secret stop verifying immediately).
    """

    secret: str
    signature_header: str = SIGNATURE_HEADER
    timestamp_header: str = TIMESTAMP_HEADER
    signature_scheme: str = SIGNATURE_SCHEME
    replay_tolerance_seconds: int = DEFAULT_REPLAY_TOLERANCE_SECONDS
    rotated: bool = False
