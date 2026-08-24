"""Pydantic models for POST /v2/distill (Task H.5).

``/v2/distill`` is Firecrawl ``/extract`` for EnConvert: schema-driven
structured extraction with a two-pass engine. The first pass is a fast,
free CSS extraction (Crawl4AI ``JsonCssExtractionStrategy``) driven by
caller-supplied selectors; any field the CSS pass leaves missing or empty
escalates to the Tier-3 LLM extractor (F.6 ``schema_llm``) under that
module's hard per-call + per-period budget caps.

Request surface (plan section 4 / section 8 Task H.5):

* ``schema`` (required) — the OUTPUT shape. Either a JSON-Schema object
  (``{"type": "object", "properties": {...}}``) or a forgiving flat
  ``{field: description}`` map. This is what the response ``data`` is
  guaranteed to match (shape-normalized) and what the LLM tool schema is
  built from. A structurally invalid schema is a 422 at the edge
  (verification d).
* ``css_schema`` (optional) — the Crawl4AI CSS extraction schema
  (``baseSelector`` + ``fields``). When present, the CSS pass runs first
  and most fields are answered for free; when absent, every field falls
  straight to the LLM pass. ``target_field`` names which top-level output
  property the extracted records fill.
* ``urls`` XOR ``discover_from`` — distill an explicit list, or discover
  a site's URLs first (``discover_flow``) then distill each. Exactly one
  must be supplied.

Strict validation everywhere (constraint): regexes compile at the edge,
CSS field types are enumerated, ``attribute``/``pattern``/``fields`` are
required for the field types that need them, and the output schema must
be a non-empty object — so a malformed request is a 422, never a 500
inside the flow.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from api.v2.schemas.discover import DiscoverMode

# Caller-supplied CSS field types accepted by Crawl4AI's
# JsonElementExtractionStrategy (extraction_strategy.py). "computed" is
# deliberately excluded: its expression/function forms are either disabled
# (eval on untrusted input) or require a Python callable that cannot cross
# the JSON request boundary.
CssFieldType = Literal[
    "text",
    "attribute",
    "html",
    "regex",
    "nested",
    "list",
    "nested_list",
]

# Hard ceiling on URLs distilled in one synchronous request. Each URL is a
# full browser render through the shared singleton; this keeps p95 latency
# under the 300 s TimeoutMiddleware and bounds the worst-case LLM spend of
# a single call (see distill_flow._MAX_LLM_ESCALATIONS).
MAX_DISTILL_URLS = 50

# Bounds that keep a hostile request cheap to parse and cheap to run.
MAX_CSS_FIELD_DEPTH = 5      # nested/list field recursion (DOM is shallow)
MAX_SCHEMA_PROPERTIES = 200  # output-schema top-level field count

_TRANSFORMS = frozenset({"lowercase", "uppercase", "strip"})

# Conservative catastrophic-backtracking detector: a group whose body
# already contains a quantifier, immediately re-quantified — the classic
# ReDoS shapes ``(a+)+`` / ``(\d*)*`` / ``(.{2,})+``. The CSS regex runs
# against page text in a worker thread, so a malicious pattern must never
# reach it (defence-in-depth alongside the extraction timeout in
# distill_flow). Narrow on purpose: it only flags a re-quantified group
# that itself contains '*'/'+'/'{', so ordinary patterns like ``(abc)+``
# or ``\d+`` pass.
_REDOS_RE = re.compile(r"\([^()]*[*+{][^()]*\)\s*[*+{]")

# Second ReDoS family the nested-quantifier rule above misses entirely:
# a re-quantified ALTERNATION whose branches can match the same single
# character — ``(a|a)+``, ``(\w|\w)+``, ``([a-z]|[a-z])*``, ``(?:a|a)+``.
# These double their work per input character exactly like ``(a+)+``.
# Multi-character branches (``(cat|dog)+``) do not overlap that way and
# stay allowed. Both rules are heuristics: they bound the shapes that are
# known to explode, not every regex that can.
_ALT_GROUP_RE = re.compile(r"\((?:\?:)?([^()]*\|[^()]*)\)\s*[*+{]")
_SINGLE_TOKEN_RE = re.compile(r"^(?:\\.|\[[^\]]*\]|[^\\])$")

# Keys that describe the schema itself rather than naming an output
# field. The edge validator and distill_flow.schema_properties MUST agree
# on this set: when they disagreed, a flat-map field named 'required'
# passed validation and then vanished from the output.
RESERVED_SCHEMA_KEYS = frozenset({"type", "properties", "required"})


def _alternation_is_explosive(pattern: str) -> bool:
    """True when a quantified group alternates between single tokens."""
    for branches in _ALT_GROUP_RE.findall(pattern):
        parts = re.split(r"(?<!\\)\|", branches)
        singles = [p for p in parts if _SINGLE_TOKEN_RE.match(p)]
        if len(parts) >= 2 and len(singles) >= 2:
            return True
    return False


def schema_property_names(schema: dict[str, Any]) -> list[str]:
    """The output field names a caller schema declares, in either form."""
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        return [str(name) for name in properties]
    return [str(name) for name in schema if name not in RESERVED_SCHEMA_KEYS]


def _css_schema_depth(fields: object, depth: int) -> int:
    """Max nesting depth of a raw ``fields`` list, computed ITERATIVELY.

    Runs in a ``mode="before"`` validator so depth is bounded BEFORE
    Pydantic recurses into the model tree — a deep-but-narrow payload
    cannot trigger a RecursionError (-> 500) on the parser; it is a clean
    422 instead."""
    max_seen = depth - 1
    stack: list[tuple[object, int]] = [(fields, depth)]
    while stack:
        current, level = stack.pop()
        if not isinstance(current, list):
            continue
        max_seen = max(max_seen, level)
        if level > MAX_CSS_FIELD_DEPTH:
            return level
        for field in current:
            if isinstance(field, dict) and isinstance(field.get("fields"), list):
                stack.append((field["fields"], level + 1))
    return max_seen


class CssField(BaseModel):
    """One field in a Crawl4AI CSS extraction schema.

    Mirrors the library's field dict (``name``, ``selector``, ``type``,
    ``attribute``, ``pattern``, ``default``, ``fields``) so it serializes
    straight through ``to_crawl4ai`` with no re-mapping. ``fields`` makes
    the model recursive for ``nested`` / ``list`` / ``nested_list``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    type: CssFieldType
    selector: Optional[str] = Field(default=None, max_length=1024)
    attribute: Optional[str] = Field(default=None, max_length=128)
    pattern: Optional[str] = Field(default=None, max_length=1024)
    default: Optional[Any] = None
    transform: Optional[str] = None
    fields: Optional[list["CssField"]] = Field(default=None, max_length=64)

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                # Do not echo the attacker-supplied pattern back in the
                # 422 body; re.error is descriptive enough.
                raise ValueError(f"invalid regex pattern: {exc}")
            if _REDOS_RE.search(value):
                raise ValueError(
                    "regex pattern rejected: nested quantifiers risk "
                    "catastrophic backtracking (ReDoS)"
                )
            if _alternation_is_explosive(value):
                raise ValueError(
                    "regex pattern rejected: a re-quantified alternation of "
                    "single-character branches risks catastrophic "
                    "backtracking (ReDoS)"
                )
        return value

    @field_validator("transform")
    @classmethod
    def _transform_known(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in _TRANSFORMS:
            raise ValueError(
                f"unknown transform {value!r}; allowed: "
                f"{', '.join(sorted(_TRANSFORMS))}"
            )
        return value

    @model_validator(mode="after")
    def _type_requirements(self) -> "CssField":
        if self.type == "attribute" and not self.attribute:
            raise ValueError("field type 'attribute' requires 'attribute'")
        if self.type == "regex":
            if not self.pattern:
                raise ValueError("field type 'regex' requires 'pattern'")
            # Crawl4AI reads match.group(1), so a groupless pattern matches
            # and yields nothing on every record. That looked identical to
            # "the selector found no data" and pushed the field into the
            # paid LLM pass, with no way for the caller to see why.
            if re.compile(self.pattern).groups < 1:
                raise ValueError(
                    "field type 'regex' requires a capture group: the "
                    "extracted value is group 1 of the match, so a pattern "
                    "without '(...)' can never return anything"
                )
        if self.type in ("nested", "list", "nested_list"):
            if not self.fields:
                raise ValueError(
                    f"field type '{self.type}' requires a non-empty 'fields'"
                )
            if not self.selector:
                raise ValueError(
                    f"field type '{self.type}' requires 'selector'"
                )
        elif self.type in ("text", "attribute", "html", "regex"):
            # A leaf field reads either a sub-selector or the base element
            # itself; both are valid, so 'selector' stays optional here.
            pass
        return self

    def to_crawl4ai(self) -> dict[str, Any]:
        """Serialize to the dict shape JsonCssExtractionStrategy expects."""
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.selector is not None:
            out["selector"] = self.selector
        if self.attribute is not None:
            out["attribute"] = self.attribute
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.default is not None:
            out["default"] = self.default
        if self.transform is not None:
            out["transform"] = self.transform
        if self.fields:
            out["fields"] = [child.to_crawl4ai() for child in self.fields]
        return out


CssField.model_rebuild()


class CssSchema(BaseModel):
    """A Crawl4AI CSS extraction schema (no-LLM, free first pass).

    ``base_selector`` matches the repeating container (one extracted
    record per match); ``fields`` are read out of each. ``target_field``
    names the top-level output-schema property the records fill — an
    array property receives the full list, a scalar/object property the
    first record. When omitted, the flow infers the single array property
    if there is exactly one.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _bound_nesting_depth(cls, data: Any) -> Any:
        """Reject over-deep field nesting BEFORE Pydantic recurses (422)."""
        if isinstance(data, dict):
            fields = data.get("fields")
            if _css_schema_depth(fields, 1) > MAX_CSS_FIELD_DEPTH:
                raise ValueError(
                    f"css_schema field nesting exceeds max depth "
                    f"{MAX_CSS_FIELD_DEPTH}"
                )
        return data

    base_selector: str = Field(
        alias="baseSelector", min_length=1, max_length=1024
    )
    fields: list[CssField] = Field(min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, max_length=128)
    target_field: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Top-level output-schema property the CSS records fill. "
        "Array property -> full list; scalar/object property -> first "
        "record. Inferred when omitted and the schema has one array "
        "property.",
    )

    def to_crawl4ai(self) -> dict[str, Any]:
        """Serialize to the dict JsonCssExtractionStrategy expects."""
        out: dict[str, Any] = {
            "baseSelector": self.base_selector,
            "fields": [field.to_crawl4ai() for field in self.fields],
        }
        out["name"] = self.name or "distill"
        return out


class DiscoverFrom(BaseModel):
    """Crawl-then-distill source: discover a site's URLs, then distill each."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048)
    mode: DiscoverMode = "hybrid"
    max_pages: int = Field(
        default=10,
        ge=1,
        le=MAX_DISTILL_URLS,
        description="Cap on URLs discovered AND distilled. Each is a full "
        "render, so this is bounded by MAX_DISTILL_URLS.",
    )

    @field_validator("url")
    @classmethod
    def _http_scheme_only(cls, value: str) -> str:
        value = value.strip()
        lowered = value.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class DistillRequest(BaseModel):
    """One /v2/distill request (plan section 4 / section 8 Task H.5)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    urls: Optional[list[str]] = Field(
        default=None,
        max_length=MAX_DISTILL_URLS,
        description="Explicit URLs to distill. Mutually exclusive with "
        "discover_from; exactly one is required.",
    )
    discover_from: Optional[DiscoverFrom] = None
    extraction_schema: Optional[dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="Output shape: a JSON-Schema object or a flat "
        "{field: description} map. The response data is guaranteed to "
        "match this shape. Optional when 'prompt' is supplied — the schema "
        "is then synthesized from the prompt.",
    )
    prompt: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Natural-language description of what to extract. When "
        "given without a 'schema', the extraction schema is synthesized from "
        "this prompt (single-model LLM). Ignored when 'schema' is present.",
    )
    css_schema: Optional[CssSchema] = None
    # Render knobs (subset of /v2/perceive); distill needs only the DOM.
    wait_for: Optional[str] = Field(default=None, max_length=1024)
    wait_timeout_ms: int = Field(default=30000, ge=0, le=60000)
    headers: Optional[dict[str, str]] = None
    cookies: Optional[list[dict[str, Any]]] = None
    respect_robots: bool = False

    @field_validator("urls")
    @classmethod
    def _urls_http_scheme(
        cls, urls: Optional[list[str]]
    ) -> Optional[list[str]]:
        if urls is None:
            return None
        cleaned: list[str] = []
        for raw in urls:
            value = (raw or "").strip()
            lowered = value.lower()
            if not (
                lowered.startswith("http://") or lowered.startswith("https://")
            ):
                raise ValueError(
                    "every url must start with http:// or https://"
                )
            if len(value) > 2048:
                raise ValueError("url exceeds 2048 characters")
            cleaned.append(value)
        return cleaned

    @field_validator("prompt")
    @classmethod
    def _prompt_non_empty(cls, prompt: Optional[str]) -> Optional[str]:
        if prompt is None:
            return None
        cleaned = prompt.strip()
        if not cleaned:
            raise ValueError("prompt must not be blank")
        return cleaned

    @field_validator("extraction_schema")
    @classmethod
    def _schema_is_object(
        cls, schema: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Reject a structurally invalid output schema at the edge (422).

        Accepts ``None`` (prompt-only mode synthesizes the schema later), a
        JSON-Schema object (type==object and/or a non-empty ``properties``
        map), or a forgiving flat ``{field: description}`` map. Everything
        else is a malformed schema -> 422.
        """
        if schema is None:
            return None
        if not isinstance(schema, dict) or not schema:
            raise ValueError("schema must be a non-empty object")
        declared_type = schema.get("type")
        if declared_type is not None and declared_type != "object":
            raise ValueError("schema 'type' must be 'object'")
        if "properties" in schema:
            properties = schema["properties"]
            if not isinstance(properties, dict) or not properties:
                raise ValueError(
                    "schema 'properties' must be a non-empty object"
                )
            if len(properties) > MAX_SCHEMA_PROPERTIES:
                raise ValueError(
                    f"schema declares too many fields "
                    f"(max {MAX_SCHEMA_PROPERTIES})"
                )
            return schema
        # Flat {field: description} map form — every key names a field,
        # except the schema-level keys the pipeline strips. Those are the
        # ONLY reserved names: a business field called 'items' or '$ref'
        # used to be a hard 422 even though nothing downstream minded it.
        field_names = [
            name for name in schema if name not in RESERVED_SCHEMA_KEYS
        ]
        if not field_names:
            # e.g. {"required": ["a", "b"]}: structurally valid JSON, no
            # extractable field in it. This used to render every URL,
            # consume an op each and return an empty data object.
            raise ValueError(
                "schema declares no fields; use a flat {field: description} "
                "map or a JSON-Schema object with a non-empty 'properties'"
            )
        if len(field_names) > MAX_SCHEMA_PROPERTIES:
            raise ValueError(
                f"schema declares too many fields (max {MAX_SCHEMA_PROPERTIES})"
            )
        return schema

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "DistillRequest":
        has_urls = bool(self.urls)
        has_discover = self.discover_from is not None
        if has_urls == has_discover:
            raise ValueError(
                "provide exactly one of 'urls' or 'discover_from'"
            )
        return self

    @model_validator(mode="after")
    def _target_field_is_a_schema_property(self) -> "DistillRequest":
        """A target_field that names nothing is a 422, not a silent bill.

        A typo'd target_field ('prodcts' for 'products') matched every CSS
        record, discarded all of them, reported ``fields_from_css: 0`` and
        re-extracted the identical data through the paid LLM pass with an
        empty warnings list. Nothing in the response revealed it.
        """
        if self.extraction_schema is None or self.css_schema is None:
            return self
        target = self.css_schema.target_field
        if target is None:
            return self
        names = schema_property_names(self.extraction_schema)
        if target not in names:
            raise ValueError(
                f"css_schema.target_field '{target}' is not a property of "
                f"'schema' (declared properties: {', '.join(sorted(names))})"
            )
        return self

    @model_validator(mode="after")
    def _schema_or_prompt(self) -> "DistillRequest":
        """Require at least one of 'schema' or 'prompt'. Schema wins if both."""
        if self.extraction_schema is None and self.prompt is None:
            raise ValueError("provide either 'schema' or 'prompt'")
        return self


class DistillTokens(BaseModel):
    input: int = 0
    output: int = 0


class DistillItemResult(BaseModel):
    """The distilled output for one URL."""

    url: str
    url_final: Optional[str] = None
    status: Literal["completed", "failed"] = "completed"
    data: Optional[dict[str, Any]] = None
    extraction_tier: Literal["css", "llm", "mixed", "none"] = "none"
    fields_from_css: int = 0
    fields_from_llm: int = 0
    render_quality: Optional[float] = None
    tokens: DistillTokens = Field(default_factory=DistillTokens)
    cost_cents: float = 0.0
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _always_emit_data(self, handler: Any) -> dict[str, Any]:
        """Keep ``data`` in the payload even when it is null.

        The route serializes with ``exclude_none``, which stripped the
        key entirely from a failed row. The documented contract is
        ``data: null`` on failure, and a typed client doing
        ``result.data.heading`` crashed on the missing key instead of
        reading a null. ``url_final`` and ``error`` stay conditional —
        those the docs do describe as omitted.
        """
        dumped = handler(self)
        if "data" not in dumped:
            dumped["data"] = None
        return dumped


class DistillResponse(BaseModel):
    """Schema-driven extraction results for one /v2/distill call."""

    operation_id: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    results: list[DistillItemResult] = Field(default_factory=list)
    total_cost_cents: float = 0.0
    synthesized_schema: Optional[dict[str, Any]] = Field(
        default=None,
        description="When 'prompt' was used without a 'schema', the schema "
        "that was synthesized from the prompt and used for extraction.",
    )
    warnings: list[str] = Field(default_factory=list)
