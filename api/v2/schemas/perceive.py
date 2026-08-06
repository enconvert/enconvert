"""Pydantic models for POST /v2/perceive and /v2/perceive/batch.

Request fields mirror the section-4 catalog. Three of them
(``proxy_url``, ``geolocation``, ``action_chain``) are accepted by the
schema but rejected with 422 by the handler until their sprints land —
explicit beats silently ignoring a paid-for knob.

``PdfOptions`` is V1's model, reused verbatim (plan F.5 How step 2) so
``outputs=["pdf"]`` accepts exactly the V1 url-to-pdf option surface.

F.8 layering: ``PerceiveOptionsBase`` carries every per-render option
EXCEPT the URL; ``PerceiveRequest`` adds the URL on top. The batch
endpoint shares one ``options`` block across N URLs and materializes a
full ``PerceiveRequest`` per URL (re-validated), so the single and
batch paths can never drift apart.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import PdfOptions

OutputName = Literal[
    "markdown",
    "html_cleaned",
    "html_raw",
    "screenshot",
    "screenshot_full_page",
    "pdf",
    "links",
    "images",
    "structured",
]

ExtractName = Literal[
    "tables",
    "prices",
    "contacts",
    "metadata",
    "main_content",
    "headings",
    "structured_data",
    "technologies",
    "all",
]

ResourceType = Literal[
    "image",
    "media",
    "font",
    "stylesheet",
    "script",
    "xhr",
    "fetch",
    "websocket",
    "manifest",
    "other",
]

CacheMode = Literal["enabled", "bypass", "refresh"]

# Outputs that become Spaces artifacts (everything except "structured",
# which is inline in the response and persisted as JSONB).
ARTIFACT_OUTPUTS: tuple[str, ...] = (
    "markdown",
    "html_cleaned",
    "html_raw",
    "screenshot",
    "screenshot_full_page",
    "pdf",
    "links",
    "images",
)

# extract[] members served by the heuristic tier at F.5. The remaining
# members (prices, contacts, technologies) land with the F.6 extractor
# pack; requesting them today yields a warning, not an error.
SUPPORTED_EXTRACTS: tuple[str, ...] = (
    "tables",
    "metadata",
    "main_content",
    "headings",
    "structured_data",
)


class PerceiveViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)


class PerceiveAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)


class PerceiveOptionsBase(BaseModel):
    """Per-render options shared by /v2/perceive and the batch endpoint.

    ``extra="forbid"``: an unknown key is a 422 naming the field, never a
    silent no-op. The 2026-08-06 QA cycle filed 22 findings against a
    ``content_only`` parameter this API never had, because the schema
    silently swallowed it — an unknown key MUST fail loudly.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    outputs: list[OutputName] = Field(
        default_factory=lambda: ["markdown", "structured"]
    )
    only_main_content: bool = Field(
        default=True,
        description="When true (default), the markdown output and the "
        "main_content extract strip site chrome (navigation, header, "
        "footer, sidebars, cookie banners, hidden nodes) behind a "
        "fidelity guard that falls back to the full page when stripping "
        "would remove too much real content. When false, the full page "
        "content is kept with no stripping.",
    )
    direct_download: bool = Field(
        default=False,
        description="When true, the response body IS the artifact bytes "
        "(no second fetch to a signed URL). Requires exactly one "
        "artifact-producing output. Not available on the batch endpoint "
        "(use output_mode='zip' there).",
    )
    extract: list[ExtractName] = Field(default_factory=list)
    extraction_schema: Optional[dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="JSON schema for structured extraction (Tier-3 LLM "
        "path lands in Sprint F.6; accepted and stored today).",
    )
    wait_for: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="CSS selector (optionally 'css:...') or 'js:<expr>' "
        "to await after navigation.",
    )
    wait_timeout_ms: int = Field(default=30000, ge=0, le=60000)
    js_code: Optional[str] = Field(default=None, max_length=20000)
    viewport: Optional[PerceiveViewport] = None
    headers: Optional[dict[str, str]] = None
    cookies: Optional[list[dict[str, Any]]] = None
    auth: Optional[PerceiveAuth] = None
    proxy_url: Optional[str] = Field(
        default=None, description="Business+; not yet available (422)."
    )
    geolocation: Optional[dict[str, Any]] = Field(
        default=None, description="Not yet available (422)."
    )
    action_chain: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Not yet available (422)."
    )
    cache_mode: CacheMode = "enabled"
    pdf_options: Optional[PdfOptions] = None
    block_resources: list[ResourceType] = Field(default_factory=list)
    respect_robots: bool = False
    mobile: bool = False


class PerceiveRequest(PerceiveOptionsBase):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def http_scheme_only(cls, v: str) -> str:
        v = v.strip()
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class OutputArtifact(BaseModel):
    """A rendered output stored in Spaces, addressed by signed URL only."""

    url: Optional[str] = Field(
        default=None, description="Pre-signed download URL (15 min)."
    )
    object_key: str
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    expires_in: int = 900


class PerceiveTokens(BaseModel):
    input: int = 0
    output: int = 0


class PerceiveResponse(BaseModel):
    operation_id: str
    # "queued" is the F.8 pre-created batch row state: the URL is
    # accepted and waiting for the droplet-local worker.
    status: Literal["queued", "processing", "completed", "failed"]
    url: str
    url_final: Optional[str] = None
    content_hash: Optional[str] = None
    status_code: Optional[int] = Field(
        default=None,
        description="HTTP status of the final main-document response "
        "(e.g. 200, 404). None when the engine could not observe it.",
    )
    render_quality: Optional[float] = Field(
        default=None, description="0.0-1.0; populated by the F.7 scorer."
    )
    deductions: dict[str, float] = Field(
        default_factory=dict,
        description="Named render-quality deductions that fired for this "
        "render (e.g. {'http_error': 0.7}). Empty on a clean render.",
    )
    options_echo: Optional[dict[str, Any]] = Field(
        default=None,
        description="Echo of the request options the server honoured "
        "(secrets redacted to booleans), so a caller can verify what "
        "was actually applied.",
    )
    cache_hit: bool = False
    outputs: dict[str, OutputArtifact] = Field(default_factory=dict)
    structured: Optional[dict[str, Any]] = None
    extraction_tier: Optional[Literal["heuristic", "css", "llm"]] = None
    tokens: PerceiveTokens = Field(default_factory=PerceiveTokens)
    cost_cents: float = 0.0
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


BatchOutputMode = Literal["manifest", "zip"]

BatchStatus = Literal[
    "queued", "processing", "completed", "failed", "partial", "canceled"
]


class PerceiveBatchRequest(BaseModel):
    """POST /v2/perceive/batch (Task F.8, no-GCP revision).

    ``urls`` share one ``options`` block; each URL becomes a full
    PerceiveRequest. The plan's batch_limit gates the count; 1000 is a
    schema-level backstop above every plan limit.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    urls: list[str] = Field(min_length=1, max_length=1000)
    options: PerceiveOptionsBase = Field(default_factory=PerceiveOptionsBase)
    output_mode: BatchOutputMode = "manifest"


class PerceiveBatchResponse(BaseModel):
    """Shared by POST (inline 200 / queued 202) and the status GET.

    ``items`` carry one PerceiveResponse per URL: fully populated on the
    inline path and on status polls; empty on the initial 202 (poll the
    status endpoint with ``job_id``). ``zip`` appears once a
    ``output_mode="zip"`` batch has bundled its artifacts.
    """

    job_id: str
    status: BatchStatus
    output_mode: BatchOutputMode = "manifest"
    total: int
    completed: int = 0
    failed: int = 0
    pending: int = 0
    zip: Optional[OutputArtifact] = None
    items: list[PerceiveResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
