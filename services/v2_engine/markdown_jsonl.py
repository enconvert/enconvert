"""Chunk -> JSONL serialization for /v2/ingest (Task H.7, plan section 8).

Emits newline-delimited JSON where each line is one chunk:

    {"id": "<page>-0001",
     "content": "<chunk text>",
     "metadata": {"source_url": "...", "title": "...",
                  "headings_path": ["h1", "h2"], "section": "h2",
                  "word_count": 123, "chunk_index": 0}}

The shape is consumer-driven, not gateway-driven: the gateway never reads
JSONL back, but the file must round-trip through the three RAG loaders the
plan targets — LangChain ``JSONLoader`` / LlamaIndex ``SimpleDirectoryReader``
/ vector-DB bulk import. The contract that makes all three work:

* ``content`` is a top-level string -> LangChain
  ``JSONLoader(jq_schema=".", content_key="content", json_lines=True,
  metadata_func=...)`` maps it to ``Document.page_content`` and lifts the
  ``metadata`` object into ``Document.metadata`` (verification b).
* one self-contained JSON object per line -> any line-oriented bulk import.

``ensure_ascii=False`` keeps unicode readable and bytes small; UTF-8 is the
JSONL norm. ``id`` is deterministic per (id_seed, chunk_index) — the seed being
the page's stable identity (its URL, or for an uploaded file its Spaces object
key) — so a re-run / resume produces identical ids. It is NOT seeded from
metadata.source_url, which for a file page is a non-unique filename.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Iterable

from services.v2_engine.chunking.semantic import Chunk

# Top-level key carrying the retrievable text. Exposed so the loader call
# (and its test) reference one source of truth.
CONTENT_KEY = "content"

# Metadata label caps. TITLE_MAX_LENGTH matches ingest_flow._extract_title's
# existing 512 truncate; SOURCE_MAX_LENGTH is generous enough that a real URL is
# never truncated (truncating source_url would destroy RAG provenance).
TITLE_MAX_LENGTH = 512
SOURCE_MAX_LENGTH = 2048

# Bidirectional formatting controls: LRM/RLM, the embed/override set, and the
# isolate set. These let a filename render as something it is not
# ("invoice<U+202E>fdp.exe" displays as "invoice.pdf"). They have no legitimate
# use in a label. NOTE: we deliberately do NOT strip all category Cf — that
# would also remove ZWNJ/ZWJ (U+200C/U+200D), which are REQUIRED in legitimate
# Devanagari/Indic and emoji filenames.
_BIDI_CONTROLS = frozenset(
    "‎‏‪‫‬‭‮⁦⁧⁨⁩"
)
# Zl/Zp. json.dumps(ensure_ascii=False) emits these RAW, and Python's
# str.splitlines() treats them as line breaks — the one way a label can still
# corrupt a line-oriented reader's framing. (C0/C1 and U+0085 are category Cc,
# handled by the Cc test in sanitize_metadata_label.)
_LINE_SEPARATORS = frozenset("  ")


def sanitize_content(text: str) -> str:
    """Neutralize the one thing a chunk body can do to the FILE itself.

    ``json.dumps(ensure_ascii=False)`` does not escape U+2028 / U+2029 —
    they are not JSON control characters — so they are emitted raw inside
    the content string. Python's ``str.splitlines()`` (and every other
    Unicode-aware line splitter, including this module's own
    ``decode_jsonl``) treats them as line breaks, which splits one record
    into two invalid fragments and loses the chunk.

    The label sanitizer has always dropped them; content did not, because
    content used to arrive only from an HTML->Markdown pass that could not
    emit one. A verbatim ``text/plain`` body can. They are line separators,
    so they become a newline rather than being deleted — no character of
    the customer's document is silently dropped.
    """
    if not text:
        return text
    for separator in _LINE_SEPARATORS:
        if separator in text:
            text = text.replace(separator, "\n")
    return text


def sanitize_metadata_label(value: str, *, max_length: int) -> str:
    """Normalize an attacker-influenceable string for JSONL metadata.

    JSON encoding already neutralizes quotes/newlines/C0 inside the file, so this
    is NOT about escaping — it is about what a DOWNSTREAM RAG consumer does with
    the value: line-framing (U+2028/29/85 survive ensure_ascii=False and split
    under str.splitlines()), display spoofing (bidi overrides), and index bloat
    (unbounded length, duplicated into every chunk record).

    Deliberately LESS lossy than utils.storage.sanitize_filename: titles are
    human-readable, so spaces and punctuation are preserved.
    """
    if not value:
        return ""
    # NFC first: composes look-alike sequences into canonical form so the cap and
    # the strips below see one consistent representation.
    text = unicodedata.normalize("NFC", value)
    text = "".join(
        ch
        for ch in text
        if ch not in _BIDI_CONTROLS
        and ch not in _LINE_SEPARATORS
        and (unicodedata.category(ch) != "Cc" or ch == "\t")
    )
    # Collapse whitespace runs (incl. the tab kept above) to single spaces.
    text = " ".join(text.split())
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def safe_source_label(filename: str, *, fallback: str = "upload") -> str:
    """Human-readable, injection-free label for an uploaded file.

    Takes the basename with BOTH separators (os.path.basename does not split
    backslashes on POSIX), which removes traversal prefixes while losing zero
    legitimate information — "../../../../etc/passwd.csv" -> "passwd.csv" — then
    applies the shared label normalization.
    """
    base = filename.replace("\\", "/").split("/")[-1]
    return sanitize_metadata_label(base, max_length=TITLE_MAX_LENGTH) or fallback


def _page_slug(source_url: str) -> str:
    """Stable short id prefix for a page's chunks (md5, first 12 hex)."""
    return hashlib.md5(source_url.encode("utf-8")).hexdigest()[:12]


def build_record(
    chunk: Chunk,
    *,
    source_url: str,
    title: str,
    index: int,
    id_seed: str | None = None,
) -> dict[str, Any]:
    """Build one JSONL record dict from a chunk (plan section 8 step 4).

    ``id_seed`` is the value the id slug is hashed from; it defaults to
    ``source_url``. They diverge for FILE pages, whose ``source_url`` is the
    user's original filename (a human label, NOT a unique identity — two uploads
    in one job can share it). Callers pass the page's Spaces object key as the
    seed there, so ids stay unique per file while the metadata keeps showing the
    filename.

    ``source_url``/``title`` are sanitized HERE, the single choke point every
    record passes through, so the file path (original upload name) and the URL
    path (<title> lifted from attacker-controlled HTML by
    ``ingest_flow._extract_title``) are both covered.
    """
    safe_source = sanitize_metadata_label(source_url, max_length=SOURCE_MAX_LENGTH)
    safe_title = sanitize_metadata_label(title, max_length=TITLE_MAX_LENGTH)
    # The seed is the caller-supplied canonical page identity (page.url) and is
    # deliberately NOT sanitized: it never reaches the deliverable, and hashing
    # it raw keeps every emitted id byte-identical to what it was before the
    # label sanitizer existed. Only the no-seed fallback hashes the sanitized
    # label, so a direct caller's id and metadata.source_url stay consistent.
    seed = id_seed if id_seed is not None else safe_source
    return {
        "id": f"{_page_slug(seed)}-{index:04d}",
        CONTENT_KEY: sanitize_content(chunk.text),
        "metadata": {
            "source_url": safe_source,
            "title": safe_title,
            "headings_path": list(chunk.headings_path),
            "section": chunk.section,
            "word_count": chunk.word_count,
            "chunk_index": index,
        },
    }


def page_records(
    chunks: Iterable[Chunk],
    *,
    source_url: str,
    title: str,
    start_index: int = 0,
    id_seed: str | None = None,
) -> list[dict[str, Any]]:
    """All JSONL records for one page's chunks, in order."""
    return [
        build_record(
            chunk,
            source_url=source_url,
            title=title,
            index=start_index + i,
            id_seed=id_seed,
        )
        for i, chunk in enumerate(chunks)
    ]


def encode_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    """Serialize records to UTF-8 JSONL bytes (one compact object per line)."""
    lines = [
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    ]
    if not lines:
        return b""
    # errors="replace": real-world HTML mis-decoded upstream can leave lone
    # surrogate code points in the text, which would make a plain UTF-8 encode
    # raise. Replacing them with U+FFFD keeps the JSONL valid and loadable.
    return ("\n".join(lines) + "\n").encode("utf-8", errors="replace")


def decode_jsonl(blob: bytes) -> list[dict[str, Any]]:
    """Parse JSONL bytes back to records (resume reassembly + tests)."""
    records: list[dict[str, Any]] = []
    for line in blob.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records
