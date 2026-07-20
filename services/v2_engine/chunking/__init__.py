"""Semantic chunking for /v2/ingest (Task H.7).

``semantic.py`` is the dependency-free heading-aware chunker shipped in
Sprint H.7. ``semantic_v2.py`` (sentence-transformer embeddings) is reserved
for Phase 6 per the plan's module layout.
"""

from services.v2_engine.chunking.semantic import (
    DEFAULT_MAX_WORDS,
    DEFAULT_SENTENCE_OVERLAP,
    Chunk,
    chunk_markdown,
    count_words,
    split_sentences,
)

__all__ = [
    "Chunk",
    "chunk_markdown",
    "count_words",
    "split_sentences",
    "DEFAULT_MAX_WORDS",
    "DEFAULT_SENTENCE_OVERLAP",
]
