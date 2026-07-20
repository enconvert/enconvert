"""Pydantic models for POST /v2/lookup (Task H.3).

``/v2/lookup`` is Firecrawl ``/search`` for EnConvert: it runs a Serper
query in one of six categories and, optionally, auto-perceives the top-N
result URLs through ``/v2/perceive`` so an agent gets both the SERP and
the page content in a single round trip.

The request mirrors the neutral search vocabulary (``category``,
``country``/``locale``, ``time_filter``) plus the auto-perceive knob
(``perceive_top``). The response is a flat, provider-neutral result list;
when ``perceive_top`` is used, each perceived result carries its full
``PerceiveResponse`` inline under ``perceive``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.v2.schemas.perceive import OutputName, PerceiveResponse
from services.v2_search.adapter import SearchCategory, TimeFilter


class LookupEnrich(BaseModel):
    """Optional high-value enrichment for /v2/lookup's top-N results.

    When present (with ``perceive_top > 0``), the top-N result URLs are
    rendered CONCURRENTLY (bounded) in the requested output formats — not just
    markdown, and not one-at-a-time — and can additionally run schema-driven
    structured extraction per result and synthesize one cited answer across
    them. This is what makes lookup beat a plain "search + scrape".
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    outputs: list[OutputName] = Field(
        default_factory=lambda: ["markdown"],
        max_length=8,
        description="Perceive outputs to produce per enriched result "
        "(markdown, html_cleaned, links, screenshot, structured, ...). "
        "Defaults to markdown.",
    )
    concurrency: int = Field(
        default=3,
        ge=1,
        le=5,
        description="How many result URLs to enrich in parallel. Markdown/"
        "HTML renders use the no-browser TLS path and truly parallelize; "
        "screenshot/pdf renders serialize on the shared browser.",
    )
    extraction_schema: Optional[dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="When set, run structured extraction against each enriched "
        "result (JSON-Schema object or flat {field: description} map). The "
        "extracted data appears under each result's perceive.structured.",
    )
    synthesize_answer: bool = Field(
        default=False,
        description="Synthesize one cited, grounded answer to the query across "
        "the enriched results (LLM). Returned on the response as 'answer'.",
    )
    answer_prompt: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional question to answer instead of the raw query "
        "(only used when synthesize_answer is true).",
    )

    @field_validator("outputs")
    @classmethod
    def _outputs_non_empty(cls, outputs: list[str]) -> list[str]:
        if not outputs:
            return ["markdown"]
        # De-duplicate, preserve order.
        return list(dict.fromkeys(outputs))


class LookupRequest(BaseModel):
    """One /v2/lookup request (plan section 4 / section 8 Task H.3)."""

    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(min_length=1, max_length=512)
    category: SearchCategory = "web"
    country: Optional[str] = Field(
        default=None,
        max_length=8,
        description="Google 'gl' country code (e.g. 'us', 'in').",
    )
    locale: Optional[str] = Field(
        default=None,
        max_length=16,
        description="Google 'hl' interface language (e.g. 'en').",
    )
    time_filter: Optional[TimeFilter] = Field(
        default=None,
        description="Restrict to results from the past "
        "hour / day / week / month / year.",
    )
    num_results: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Serper 'num' page size (results per page).",
    )
    page: int = Field(default=1, ge=1, le=10)
    location: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Full-text location string (e.g. 'Austin, Texas').",
    )
    autocorrect: bool = True
    perceive_top: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Auto-perceive the top-N result URLs through "
        "/v2/perceive. Each one is a full browser render that runs "
        "sequentially through the shared singleton and consumes one "
        "perceive operation from your quota, so this is capped low; use "
        "/v2/perceive/batch for larger sets. 0 disables auto-perceive.",
    )
    enrich: Optional[LookupEnrich] = Field(
        default=None,
        description="High-value enrichment tuning for the top-N results "
        "(concurrent multi-format rendering, structured extraction, and a "
        "synthesized cited answer). When omitted, perceive_top keeps its "
        "legacy behavior: markdown-only, one result at a time.",
    )

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be empty")
        return value


class LookupResult(BaseModel):
    """One provider-neutral search hit, optionally perceived."""

    title: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None
    position: Optional[int] = None
    source: Optional[str] = None
    date: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)
    perceive: Optional[PerceiveResponse] = Field(
        default=None,
        description="Present only for the top-N results when perceive_top "
        "> 0 and the perception succeeded.",
    )


class LookupResponse(BaseModel):
    """Search results + provenance for one /v2/lookup call."""

    lookup_id: Optional[int] = Field(
        default=None,
        description="ch_lookup_queries row id for support correlation "
        "(null if the audit write failed; the results are still valid).",
    )
    query: str
    category: SearchCategory
    country: Optional[str] = None
    locale: Optional[str] = None
    time_filter: Optional[TimeFilter] = None
    total: int = 0
    results: list[LookupResult] = Field(default_factory=list)
    perceive_top: int = Field(
        default=0,
        description="How many results were actually perceived (<= the "
        "requested perceive_top; lower if quota ran out or URLs failed).",
    )
    perceive_operation_ids: list[str] = Field(default_factory=list)
    answer_box: Optional[dict[str, Any]] = None
    knowledge_graph: Optional[dict[str, Any]] = None
    answer: Optional[str] = Field(
        default=None,
        description="Synthesized cited answer across the enriched results "
        "(present only when enrich.synthesize_answer is true and it succeeded).",
    )
    answer_sources: list[str] = Field(
        default_factory=list,
        description="URLs used as grounding for 'answer', in citation order.",
    )
    credits: Optional[int] = Field(
        default=None, description="Serper credits consumed by this query."
    )
    cost_cents: float = 0.0
    warnings: list[str] = Field(default_factory=list)
