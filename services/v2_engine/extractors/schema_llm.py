"""Tier-3 LLM extraction — open-source fallback (cloud-only capability).

The real module runs capped Anthropic Haiku extraction with a layered
budget ledger; that engine ships only in the Enconvert cloud build. This
fallback keeps the PUBLIC surface open callers and tests rely on — the
result dataclasses (same fields), the skip-reason constants and the
``period_cap_cents`` billing helper — while the three async entry points
(``extract``, ``synthesize_schema``, ``answer_from_sources``) raise
:class:`CloudEngineRequired` (HTTP 501).

Self-hosted deployments never reach these entry points in normal
operation: the plan gates (``llm_extraction_enabled`` /
``agent_model_tier``) are off by default, so callers take their
heuristic/CSS paths and only an explicit LLM request surfaces the 501.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from services._engine_fallback import CloudEngineRequired

# Model + cap constants: public information (published Anthropic model
# slug and Enconvert's documented budget caps), kept so imports and
# verification harnesses resolve against the same names.
MODEL_SLUG = "claude-haiku-4-5"

PER_REQUEST_CAP_CENTS = Decimal("5")             # $0.05 per call
DEFAULT_PERIOD_CAP_CENTS = Decimal("500")        # $5  free/starter/unknown
ELEVATED_PERIOD_CAP_CENTS = Decimal("2000")      # $20 pro and above
_ELEVATED_CAP_SLUGS = frozenset({"pro", "business", "enterprise", "admin"})

# Skip reasons (ExtractionResult.skipped_reason values in the cloud build).
SKIP_NOT_CONFIGURED = "anthropic_api_key_not_configured"
SKIP_EMPTY_HTML = "empty_html"
SKIP_NO_USAGE_PERIOD = "no_active_usage_period"
SKIP_PERIOD_CAP = "llm_budget_cap_reached"
SKIP_REQUEST_CAP = "request_cost_estimate_over_cap"
SKIP_API_ERROR = "llm_call_failed"
SKIP_BAD_OUTPUT = "llm_returned_no_usable_extraction"


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one Tier-3 attempt (same fields as the cloud build).

    ``tier`` is "llm" only when the model returned a usable extraction;
    every skip/failure path reports "heuristic" so the caller's existing
    result stands. Cost is Decimal cents.
    """

    data: Optional[dict[str, Any]]
    tier: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: Decimal = Decimal("0")
    skipped_reason: Optional[str] = None


@dataclass(frozen=True)
class SchemaSynthesisResult:
    """Outcome of a prompt -> extraction-schema synthesis call.

    ``schema`` is a flat ``{field: description}`` map or ``None`` on any
    skip/failure. Cost is Decimal cents.
    """

    schema: Optional[dict[str, str]]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: Decimal = Decimal("0")
    skipped_reason: Optional[str] = None


@dataclass(frozen=True)
class AnswerResult:
    """Outcome of an answer-synthesis call over search-result sources."""

    answer: Optional[str]
    sources_used: list[str]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: Decimal = Decimal("0")
    skipped_reason: Optional[str] = None


def period_cap_cents(plan_slug: str) -> Decimal:
    """Budget cap for a plan slug; unknown slugs get the TIGHT cap."""
    return (
        ELEVATED_PERIOD_CAP_CENTS
        if plan_slug in _ELEVATED_CAP_SLUGS
        else DEFAULT_PERIOD_CAP_CENTS
    )


async def extract(
    html: str,
    schema: Optional[dict[str, Any]],
    url: str,
    project_id: str,
    *,
    plan_slug: str = "",
    usage_key: Optional[str] = None,
    feature: str = "schema_extract",
) -> ExtractionResult:
    """Cloud-only: LLM schema extraction over rendered HTML."""
    raise CloudEngineRequired("LLM schema extraction")


async def synthesize_schema(
    prompt: str,
    project_id: str,
    *,
    plan_slug: str = "",
    usage_key: Optional[str] = None,
    feature: str = "distill_synthesize",
) -> SchemaSynthesisResult:
    """Cloud-only: synthesize an extraction schema from a prompt."""
    raise CloudEngineRequired("LLM schema synthesis")


async def answer_from_sources(
    question: str,
    sources: list[tuple[str, str]],
    project_id: str,
    *,
    plan_slug: str = "",
    usage_key: Optional[str] = None,
    feature: str = "lookup_answer",
) -> AnswerResult:
    """Cloud-only: synthesize a cited answer grounded in sources."""
    raise CloudEngineRequired("LLM answer synthesis")
