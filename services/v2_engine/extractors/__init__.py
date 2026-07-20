"""V2-engine extractors — open-source fallback package.

json_css   — real CSS-selector extraction (no LLM, works self-hosted).
schema_llm — cloud-only surface: dataclasses and billing helpers are kept
             so open callers import cleanly; the extraction entry points
             raise CloudEngineRequired.
prompts    — empty stubs (the prompt IP ships only in the cloud build).
"""
