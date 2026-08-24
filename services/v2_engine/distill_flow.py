"""/v2/distill orchestration (Task H.5, plan sections 4 + 8).

``/v2/distill`` (Firecrawl ``/extract`` equivalent) is schema-driven
structured extraction with a TWO-PASS engine, tuned so the common case
costs nothing:

  Pass 1 — CSS (free). When the caller supplies a ``css_schema``, run
  Crawl4AI's ``JsonCssExtractionStrategy`` over the rendered HTML
  (``extractors.json_css``). Selector-addressable fields are answered at
  zero LLM cost.

  Pass 2 — LLM (capped, only-when-needed). For the fields the CSS pass
  left missing/empty, escalate to the F.6 Tier-3 extractor
  (``extractors.schema_llm``) with a REDUCED schema containing only those
  fields. Every per-call and per-period budget cap is INHERITED from F.6
  verbatim — schema_llm owns the spend gates; distill only owns the
  trigger (plus a request-level backstop, ``_REQUEST_LLM_BUDGET_CENTS``,
  so a single multi-URL call cannot quietly run up the period budget).

Output shape is GUARANTEED: the merged result is normalized to exactly
the caller's schema keys (missing -> null / empty array), which is the
product differentiator over a raw scraper.

Cost & quota model: distill bills the unified ops counter, one op per
URL completed (with ``distill_operations`` kept as its telemetry
breakdown), and writes one ``ch_distill_operations`` row per URL. It
NEVER routes through a full ``/v2/perceive`` operation (which would
double-bill the op and litter that audit table); it uses
``perceive_flow.render_html``, the persistence-free render entry point.
Rendering is sequential through the shared Chromium singleton (plan A5).

Two URL sources (exactly one per request, enforced in the schema):
``urls[]`` (explicit) or ``discover_from`` (crawl/sitemap the site first
via ``discover_flow``, then distill each discovered URL).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import HTTPException

from api.deps import check_ops_quota
from api.v2.schemas.discover import DiscoverRequest
from api.v2.schemas.distill import (
    RESERVED_SCHEMA_KEYS,
    DistillItemResult,
    DistillRequest,
    DistillResponse,
    DistillTokens,
)
from models import DistillOperation
from services.v2_engine import discover_flow, perceive_flow, usage
from services.v2_engine.extractors import json_css, schema_llm
from services.v2_engine.quality import QUALITY_FLOOR
from utils.postgres import get_db

logger = logging.getLogger(__name__)

# Request-level LLM backstop. The authoritative caps are F.6's (per-call
# $0.05 + the period's AI-credit allowance, ai_credits_granted_cents,
# enforced inside reserve_llm_budget); this is a defence-in-depth ceiling
# so one multi-URL distill cannot consume the whole period budget in a
# single call. URLs past the ceiling return
# CSS-only with a warning. Generous vs the verification target (3-URL
# e-commerce distill, mostly CSS, stays well under $0.10).
_REQUEST_LLM_BUDGET_CENTS = Decimal("50")  # $0.50 per /v2/distill request

# One LLM call per URL at most (all missing fields batched into a single
# escalation), so escalations are naturally bounded by the URL count; this
# is a hard backstop against any future per-field looping.
_MAX_LLM_ESCALATIONS = 50

# Wall-clock ceiling for the synchronous CSS pass (BeautifulSoup parse +
# any user-supplied regex fields). Bounds a ReDoS / pathological-selector
# attempt so a request cannot hang on the CSS pass; on timeout the URL
# falls through to the LLM pass (defence-in-depth with the schema-layer
# nested-quantifier reject).
_CSS_PASS_TIMEOUT_SECONDS = 10.0

# Values that count as "field absent" for both CSS-coverage and the
# missing-field trigger. Empty containers count as missing: a declared
# field with [] / {} was not actually found on the page. NOTE: only None,
# "", [], {} count — a real extracted False or 0 is a present value.
_EMPTY = (None, "", [], {})

_TierName = Literal["css", "llm", "mixed", "none"]


@dataclass
class _LlmBudget:
    """Per-request LLM accumulator (sequential; not concurrency-safe)."""

    spent: Decimal = field(default_factory=lambda: Decimal("0"))
    calls: int = 0


# ── Schema helpers (pure) ────────────────────────────────────────────────


def schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize the caller's output schema to a ``{name: prop_def}`` map.

    Accepts a JSON-Schema object (uses its ``properties``) or a flat
    ``{field: description}`` map (synthesizes nullable-string props, the
    same forgiving shape schema_llm.build_tool_schema produces). The edge
    validator already guaranteed one of these forms.
    """
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        # A bare string under 'properties' is the flat form's description
        # leaking into the JSON-Schema form; normalize it here so the rest
        # of the pipeline only ever sees property objects.
        return {
            str(name): schema_llm.coerce_property(prop)
            for name, prop in properties.items()
        }
    return {
        str(name): {"type": ["string", "null"], "description": str(desc)}
        for name, desc in schema.items()
        if name not in RESERVED_SCHEMA_KEYS
    }


def _is_array_prop(prop: Any) -> bool:
    if not isinstance(prop, dict):
        return False
    declared = prop.get("type")
    if isinstance(declared, list):
        return "array" in declared
    return declared == "array"


def _is_object_prop(prop: Any) -> bool:
    if not isinstance(prop, dict):
        return False
    declared = prop.get("type")
    if isinstance(declared, list):
        return "object" in declared
    return declared == "object" or "properties" in prop


def apply_css_records(
    props: dict[str, Any],
    records: list[dict[str, Any]],
    target_field: Optional[str],
) -> tuple[dict[str, Any], int, list[str]]:
    """Map CSS records onto the schema; return (data, fields_filled, warnings).

    * ``target_field`` set, array property -> the full record list.
    * ``target_field`` set, object property -> the first record whole.
    * ``target_field`` set, scalar property -> the VALUE inside the first
      record, not the record itself.
    * ``target_field`` omitted, schema has exactly one array property ->
      that property receives the list (the e-commerce ``{products: [...]}``
      case).
    * otherwise -> the first record is treated as a single flat object and
      its declared keys are lifted to the top level.

    Every path that cannot place the records says so in ``warnings``.
    Discarding them silently made the free pass look like it found nothing
    and pushed the same fields into the paid LLM pass, which is the one
    failure mode a caller has no way to detect from the response.
    """
    data: dict[str, Any] = {}
    warnings: list[str] = []
    if not records:
        return data, 0, warnings

    field_name = target_field
    if field_name is None:
        array_props = [name for name, prop in props.items() if _is_array_prop(prop)]
        if len(array_props) == 1:
            field_name = array_props[0]

    if field_name is not None and field_name in props:
        prop = props[field_name]
        if _is_array_prop(prop):
            data[field_name] = records
            return data, 1, warnings
        value, unwrap_warning = _scalar_from_record(
            prop, field_name, records[0]
        )
        data[field_name] = value
        if unwrap_warning:
            warnings.append(unwrap_warning)
        return data, 1, warnings

    first = records[0]
    filled = 0
    for name in props:
        if name in first and first[name] not in _EMPTY:
            value = first[name]
            # Never lift a scalar into an array-typed property: a CSS field
            # named after an array property (with a `default`, say) put an
            # int where a list belongs and 500'd the whole batch downstream.
            if _is_array_prop(props[name]) and not isinstance(value, list):
                value = [value]
            data[name] = value
            filled += 1
    if not filled:
        warnings.append(
            f"the CSS pass matched {len(records)} record(s), but none of "
            f"their keys ({', '.join(sorted(str(k) for k in first))}) name a "
            f"schema property ({', '.join(sorted(props))}); the records were "
            "discarded. Set 'css_schema.target_field' to the property they "
            "should fill — otherwise these fields fall through to the paid "
            "LLM pass."
        )
    return data, filled, warnings


def _scalar_from_record(
    prop: Any, field_name: str, record: Any
) -> tuple[Any, Optional[str]]:
    """The value a non-array property should take from one CSS record.

    A scalar target used to receive the whole record dict, so a schema
    saying ``{"price": {"type": "number"}}`` came back as
    ``{"price": {"price": "51.77"}}`` — an object sitting in a number
    field, with no warning, against a documented shape guarantee.
    """
    if not isinstance(record, dict) or _is_object_prop(prop):
        return record, None
    if field_name in record:
        return record[field_name], None
    if len(record) == 1:
        return next(iter(record.values())), None
    return record, (
        f"'{field_name}' is a scalar property but the CSS record carries "
        f"{len(record)} fields ({', '.join(sorted(str(k) for k in record))}); "
        f"name one of them '{field_name}' or declare the property as an "
        "object to control what it receives."
    )


def _array_items_incomplete(prop: dict[str, Any], value: Any) -> bool:
    """True when a non-empty array has items missing declared sub-fields.

    Lets a ``{products: [{name, price, sku}]}`` schema escalate when CSS
    found the products but missed a per-item field (e.g. ``sku`` absent on
    some pages) — the two-pass differentiator. An array whose item schema
    declares no sub-properties is "complete" as soon as it is non-empty.
    """
    items_schema = prop.get("items")
    sub_props = (
        items_schema.get("properties")
        if isinstance(items_schema, dict)
        else None
    )
    if not isinstance(sub_props, dict) or not sub_props:
        return False
    if not isinstance(value, (list, tuple)):
        # PRODUCTION 500: a scalar reaching an array-typed property (a CSS
        # field named after an array property with a numeric `default`)
        # was iterated here and raised TypeError: 'int' object is not
        # iterable. missing_fields() runs OUTSIDE the per-URL guard, so
        # that sank the WHOLE batch — including URLs already rendered,
        # billed and persisted. A non-list here is simply "not filled".
        return True
    for item in value:
        if not isinstance(item, dict):
            return True
        if any(item.get(sub) in _EMPTY for sub in sub_props):
            return True
    return False


def missing_fields(props: dict[str, Any], data: dict[str, Any]) -> list[str]:
    """Schema fields the CSS pass left absent (the LLM-escalation set).

    A scalar/object field is missing when empty; an array field is missing
    when empty OR when its items lack declared sub-fields
    (``_array_items_incomplete``)."""
    missing: list[str] = []
    for name, prop in props.items():
        value = data.get(name)
        if _is_array_prop(prop):
            if not value or _array_items_incomplete(prop, value):
                missing.append(name)
        elif value in _EMPTY:
            missing.append(name)
    return missing


def reduced_schema(
    props: dict[str, Any], fields: list[str]
) -> dict[str, Any]:
    """A JSON-Schema object carrying only the named (missing) fields.

    The LLM pass extracts ONLY what CSS missed (plan H.5 How step 2:
    "call schema_llm.extract with just those fields"), keeping the prompt
    and the worst-case cost minimal.
    """
    return {
        "type": "object",
        "properties": {name: props[name] for name in fields if name in props},
    }


# JSON/JS keywords a model sometimes emits as TEXT for an absent value.
# Only literals that are never a natural English answer are coerced —
# "none" and "n/a" stay untouched because they are real page content.
_NULL_LITERALS = frozenset({"null", "undefined"})


def _json_null(value: Any) -> Any:
    """Turn a stringified null back into a real null, recursively.

    The documented guarantee is "missing scalars become null". A model
    answering with the STRING "null" broke it silently: a typed client
    reading ``data["stock"]`` got truthy text where the contract promised
    ``None``.
    """
    if isinstance(value, str):
        return None if value.strip().lower() in _NULL_LITERALS else value
    if isinstance(value, list):
        return [_json_null(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_null(item) for key, item in value.items()}
    return value


def merge_llm_data(
    data: dict[str, Any],
    llm_data: dict[str, Any],
    fields: list[str],
) -> tuple[dict[str, Any], int]:
    """Fill the missing fields from the LLM result; return (data, filled).

    Only the escalated field names are merged (the reduced tool schema
    cannot return others), and only non-empty values count toward
    ``fields_from_llm``."""
    filled = 0
    for name in fields:
        if name in llm_data:
            value = _json_null(llm_data[name])
            data[name] = value
            if value not in _EMPTY:
                filled += 1
    return data, filled


def normalize_to_schema(
    props: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    """Return a dict with EXACTLY the schema's keys (the shape guarantee).

    Missing scalar -> null, missing array -> []. Keys outside the schema
    are dropped, so the response ``data`` always matches the requested
    schema regardless of what CSS/LLM produced."""
    normalized: dict[str, Any] = {}
    for name, prop in props.items():
        if name not in data:
            normalized[name] = [] if _is_array_prop(prop) else None
            continue
        value = data[name]
        # An array property must hold an array whatever the passes produced,
        # or the guarantee is not a guarantee. A stray scalar is wrapped
        # rather than dropped: the caller asked for that value.
        if _is_array_prop(prop) and not isinstance(value, list):
            value = [] if value in _EMPTY else [value]
        normalized[name] = value
    return normalized


def _tier(fields_from_css: int, fields_from_llm: int) -> _TierName:
    if fields_from_css and fields_from_llm:
        return "mixed"
    if fields_from_css:
        return "css"
    if fields_from_llm:
        return "llm"
    return "none"


# ── Per-URL distillation ─────────────────────────────────────────────────


async def _distill_one_url(
    url: str, request: DistillRequest, **kwargs: Any
) -> DistillItemResult:
    """Two-pass distill of one URL; never raises (failures become a row).

    The guarantee is enforced HERE, not merely promised in a docstring.
    Only the render was guarded before, so anything raised by the work
    that follows it (CSS mapping, the missing-field scan, merge,
    normalize) escaped into the handler and 500'd the ENTIRE batch —
    discarding URLs that had already rendered, billed and persisted. One
    bad URL costs one row.
    """
    try:
        return await _distill_one_url_inner(url, request, **kwargs)
    except Exception:  # noqa: BLE001 — one bad URL must not sink the request
        logger.exception("distill failed unexpectedly for %s", _safe(url))
        return DistillItemResult(
            url=url, status="failed", error="distillation failed"
        )


async def _distill_one_url_inner(
    url: str,
    request: DistillRequest,
    *,
    props: dict[str, Any],
    css_schema_dict: Optional[dict[str, Any]],
    target_field: Optional[str],
    llm_enabled: bool,
    plan_slug: str,
    user: dict,
    llm_budget: _LlmBudget,
    usage_key: Optional[str] = None,
) -> DistillItemResult:
    """The two-pass body; wrapped by _distill_one_url, which cannot raise."""
    item_warnings: list[str] = []

    try:
        rendered = await perceive_flow.render_html(
            url,
            respect_robots=request.respect_robots,
            wait_for=request.wait_for,
            wait_timeout_ms=request.wait_timeout_ms,
            headers=request.headers,
            cookies=request.cookies,
        )
    except HTTPException as exc:
        # SSRF / robots / render gate. Keep provider/internal detail
        # server-side only (a caller who influences discover_from could
        # otherwise probe internal hosts via echoed errors).
        logger.warning("distill render rejected for %s: %s", _safe(url), exc.detail)
        return DistillItemResult(
            url=url, status="failed", error="render rejected or unavailable"
        )
    except Exception:  # noqa: BLE001 — one bad URL must not sink the request
        logger.exception("distill render crashed for %s", _safe(url))
        return DistillItemResult(
            url=url, status="failed", error="render failed"
        )

    item_warnings.extend(rendered.warnings)
    html = rendered.html
    final_url = rendered.final_url

    # Pass 1 — CSS (free). Time-bounded: a pathological selector / regex
    # field cannot hang the request; on timeout the URL falls through to
    # the LLM pass with a warning.
    data: dict[str, Any] = {}
    fields_from_css = 0
    if css_schema_dict is not None:
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(
                    json_css.extract_records, html, final_url, css_schema_dict
                ),
                timeout=_CSS_PASS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("distill CSS pass timed out for %s", _safe(url))
            item_warnings.append(
                "CSS extraction timed out; fell back to the LLM pass."
            )
            records = []
        data, fields_from_css, css_warnings = apply_css_records(
            props, records, target_field
        )
        item_warnings.extend(css_warnings)

    # Pass 2 — LLM (only the fields CSS missed, capped).
    missing = missing_fields(props, data)
    fields_from_llm = 0
    cost_cents = Decimal("0")
    tokens = DistillTokens()
    if missing:
        # D6 (QA report 2026-08-06): gate on the full quality floor, not
        # just is_blocked — a 404 / empty shell / unhydrated render has
        # nothing to extract either.
        low_quality = (
            rendered.is_blocked or rendered.render_quality < QUALITY_FLOOR
        )
        skip = _llm_skip_reason(missing, llm_enabled, low_quality, llm_budget)
        if skip is not None:
            item_warnings.append(skip)
        else:
            llm_budget.calls += 1
            result = await schema_llm.extract(
                html,
                reduced_schema(props, missing),
                final_url,
                str(user.get("id", "")),
                plan_slug=plan_slug,
                usage_key=usage_key,
                feature="distill_extract",
            )
            cost_cents = result.cost_cents
            llm_budget.spent += cost_cents
            tokens = DistillTokens(
                input=result.input_tokens, output=result.output_tokens
            )
            if result.data is not None:
                data, fields_from_llm = merge_llm_data(data, result.data, missing)
            item_warnings.extend(
                _llm_result_warnings(result, missing, fields_from_llm)
            )

    data = normalize_to_schema(props, data)
    return DistillItemResult(
        url=url,
        url_final=final_url,
        status="completed",
        data=data,
        extraction_tier=_tier(fields_from_css, fields_from_llm),
        fields_from_css=fields_from_css,
        fields_from_llm=fields_from_llm,
        render_quality=rendered.render_quality,
        tokens=tokens,
        cost_cents=float(cost_cents),
        warnings=item_warnings,
    )


def _llm_result_warnings(
    result: "schema_llm.ExtractionResult",
    missing: list[str],
    fields_from_llm: int,
) -> list[str]:
    """Everything the caller needs to explain a null the LLM pass left.

    Silence here is what made the expensive failures indistinguishable
    from "the data genuinely is not on the page": a truncated completion,
    a truncated page and a model that simply found nothing all produced
    the same empty fields and, in two of the three cases, a real bill.
    """
    warnings: list[str] = []
    if result.output_truncated:
        warnings.append(
            "the model hit its output limit before finishing this answer, "
            "so the extraction was discarded and you were still billed for "
            "the call. Ask for fewer fields, or split a large list across "
            "several requests."
        )
    if result.html_truncated:
        warnings.append(
            "the page was larger than the extraction token budget and was "
            "truncated, so a field reported as null may simply not have "
            "been shown to the model. Narrow the page (a deep-link or a "
            "'css_schema') to cover the whole document."
        )
    if result.skipped_reason is not None:
        # A truncated output is not a skip — the call ran and was billed.
        if result.skipped_reason != schema_llm.SKIP_OUTPUT_TRUNCATED:
            warnings.append(
                f"LLM extraction skipped ({result.skipped_reason}); "
                "missing fields returned as null."
            )
    elif fields_from_llm == 0 and missing:
        warnings.append(
            "the LLM pass ran and was billed but found no value for: "
            f"{', '.join(missing)}."
        )
    return warnings


def _llm_skip_reason(
    missing: list[str],
    llm_enabled: bool,
    is_blocked: bool,
    llm_budget: _LlmBudget,
) -> Optional[str]:
    """Why the LLM pass should NOT fire (None == fire it)."""
    fields = ", ".join(missing)
    if not llm_enabled:
        return (
            f"fields not found by CSS ({fields}) need an LLM-enabled plan; "
            "returned CSS-only."
        )
    if is_blocked:
        return (
            "LLM extraction skipped: render quality flagged the page as "
            "blocked or below the quality floor (anti-bot page, error "
            "page, or failed render); returned CSS-only."
        )
    if (
        llm_budget.spent >= _REQUEST_LLM_BUDGET_CENTS
        or llm_budget.calls >= _MAX_LLM_ESCALATIONS
    ):
        return (
            "distill LLM budget for this request reached; remaining fields "
            "returned CSS-only."
        )
    return None


# ── Orchestration ────────────────────────────────────────────────────────


async def run(request: DistillRequest, operation_id: str, user: dict) -> DistillResponse:
    """Execute one /v2/distill operation end-to-end."""
    project_id = int(user["id"])
    schema = request.extraction_schema

    subscription = user.get("subscription") or {}
    llm_enabled = bool(subscription.get("llm_extraction_enabled")) and (
        subscription.get("agent_model_tier") not in (None, "none", "", 0)
    )
    plan_slug = str(
        subscription.get("plan_slug") or user.get("plan_slug") or ""
    )

    warnings: list[str] = []
    synthesized_schema: Optional[dict] = None
    synth_cost = 0.0
    # Prompt-only mode: synthesize the extraction schema from the goal
    # (single-model LLM, same reserve-then-settle budget ledger as the
    # extraction pass). Falls through to the normal two-pass engine.
    if schema is None:
        if not llm_enabled:
            warnings.append(
                "prompt-only distillation needs LLM extraction, which is not "
                "enabled on your plan; supply an explicit 'schema' or upgrade."
            )
            return DistillResponse(operation_id=operation_id, warnings=warnings)
        synth = await schema_llm.synthesize_schema(
            request.prompt or "",
            str(project_id),
            plan_slug=plan_slug,
            usage_key=f"{operation_id}:synth",
            feature="distill_synthesize",
        )
        synth_cost = round(float(synth.cost_cents), 4)
        if not synth.schema:
            warnings.append(
                "could not synthesize a schema from the prompt "
                f"({synth.skipped_reason or 'no fields returned'})."
            )
            return DistillResponse(
                operation_id=operation_id,
                warnings=warnings,
                total_cost_cents=synth_cost,
            )
        schema = synth.schema
        synthesized_schema = synth.schema

    props = schema_properties(schema)
    css_schema_dict = (
        request.css_schema.to_crawl4ai() if request.css_schema else None
    )
    target_field = request.css_schema.target_field if request.css_schema else None

    urls = await _resolve_urls(request, user, warnings)
    if not urls:
        warnings.append("no URLs to distill.")
        return DistillResponse(
            operation_id=operation_id,
            warnings=warnings,
            total_cost_cents=synth_cost,
            synthesized_schema=synthesized_schema,
        )

    llm_budget = _LlmBudget()
    results: list[DistillItemResult] = []

    for url_index, url in enumerate(urls):
        # Per-URL quota: enforces the unified ops cap for discover_from
        # (count unknown up front) AND stops a urls[] request that crosses
        # the boundary mid-way, instead of 402-ing the whole batch.
        try:
            check_ops_quota(user, units=1)
        except HTTPException:
            warnings.append(
                f"monthly ops quota exhausted at {len(results)}/{len(urls)} "
                "URLs; remaining URLs skipped. Upgrade your plan to continue."
            )
            break

        started = time.monotonic()
        item = await _distill_one_url(
            url,
            request,
            props=props,
            css_schema_dict=css_schema_dict,
            target_field=target_field,
            llm_enabled=llm_enabled,
            plan_slug=plan_slug,
            user=user,
            llm_budget=llm_budget,
            # Ledger key for the LLM pass: operation_id alone would
            # collide across this loop's N URLs (one extract each) and
            # dedup away all but the first URL's spend — the index makes
            # each extract's reserve/settle pair unique (migration 016).
            usage_key=f"{operation_id}:{url_index}",
        )
        results.append(item)
        # Sync SQLModel write moved off the event loop — up to MAX_DISTILL_URLS
        # of these run per request, one per URL.
        await asyncio.to_thread(
            _persist_distill_operation,
            operation_id=operation_id,
            project_id=project_id,
            item=item,
            schema=schema,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if item.status == "completed":
            # operation_id + hashed URL: URLs are de-duplicated per request
            # (_resolve_urls), so the pair is unique per completed URL and a
            # replay of the same operation bills each URL exactly once.
            url_digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
            usage.increment_distill_usage(
                project_id,
                idempotency_key=f"v2:op:distill:{operation_id}:{url_digest}",
            )

    completed = sum(1 for item in results if item.status == "completed")
    failed = sum(1 for item in results if item.status == "failed")
    total_cost = round(sum(item.cost_cents for item in results) + synth_cost, 4)
    return DistillResponse(
        operation_id=operation_id,
        total=len(results),
        completed=completed,
        failed=failed,
        results=results,
        total_cost_cents=total_cost,
        synthesized_schema=synthesized_schema,
        warnings=warnings,
    )


async def _resolve_urls(
    request: DistillRequest, user: dict, warnings: list[str]
) -> list[str]:
    """The ordered, de-duplicated URL list to distill (explicit or discovered)."""
    if request.discover_from is not None:
        discover = request.discover_from
        discover_request = DiscoverRequest(
            url=discover.url,
            mode=discover.mode,
            max_urls=discover.max_pages,
        )
        # SSRF on the seed raises HTTPException(400) here and propagates to
        # the handler as a client error (correct); crawl faults degrade to
        # warnings inside discover_flow.
        discovered = await discover_flow.run(discover_request, user)
        warnings.extend(discovered.warnings)
        return list(dict.fromkeys(discovered.urls))[: discover.max_pages]
    return list(dict.fromkeys(request.urls or []))


def _persist_distill_operation(
    *,
    operation_id: str,
    project_id: int,
    item: DistillItemResult,
    schema: dict[str, Any],
    duration_ms: int,
) -> None:
    """Insert one ch_distill_operations row for this URL.

    Best-effort (mirrors lookup_flow._persist_lookup_query): the
    extraction already happened, so an audit-write failure is logged and
    swallowed rather than 500-ing a good response."""
    db = get_db()
    try:
        now = datetime.now(timezone.utc)
        db.add(
            DistillOperation(
                operation_id=operation_id,
                project_id=project_id,
                url=item.url,
                extraction_schema=schema,
                result_data=item.data,
                extraction_tier=item.extraction_tier,
                fields_from_css=item.fields_from_css,
                fields_from_llm=item.fields_from_llm,
                llm_input_tokens=item.tokens.input,
                llm_output_tokens=item.tokens.output,
                llm_cost_cents=Decimal(str(item.cost_cents)),
                status=item.status,
                error_message=item.error,
                duration_ms=duration_ms,
                created_at=now,
                completed_at=now,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "/v2/distill: failed to persist ch_distill_operations row for %s",
            _safe(item.url),
        )
    finally:
        db.close()


def _safe(url: str) -> str:
    """Truncate an attacker-influenceable URL before logging."""
    return (url or "")[:256]
