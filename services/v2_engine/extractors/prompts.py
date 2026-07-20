"""Prompt templates — open-source fallback (empty stubs).

The real prompt engineering ships only in the Enconvert cloud build. The
open fallback's ``schema_llm`` raises ``CloudEngineRequired`` before any
prompt is needed, so these names exist purely so imports resolve.
"""

from __future__ import annotations

from typing import Any, Optional

EXTRACTION_TOOL_NAME = "report_extraction"
SYSTEM_PROMPT = ""

SCHEMA_SYNTHESIS_TOOL_NAME = "define_schema"
SCHEMA_SYNTHESIS_SYSTEM_PROMPT = ""
SCHEMA_SYNTHESIS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

ANSWER_SYSTEM_PROMPT = ""


def build_user_prompt(
    url: str, html: str, schema: Optional[dict[str, Any]]
) -> str:
    """Stub — the cloud build assembles the real extraction prompt."""
    return ""


def build_schema_synthesis_prompt(goal: str) -> str:
    """Stub — the cloud build assembles the real synthesis prompt."""
    return ""


def build_answer_prompt(question: str, sources: list[tuple[str, str]]) -> str:
    """Stub — the cloud build assembles the real answer prompt."""
    return ""
