"""PDF -> Markdown via pdfplumber (permissive: MIT + Pillow>=9.1).

PDFs carry no explicit structure, so headings are inferred from font size and
tables from pdfplumber's geometric table finder:

* A document-wide pass buckets every word's rounded font size; the most common
  size is the body text. Sizes larger than the body become heading levels,
  ranked largest -> ``#``, next -> ``##``, capped at ``###``.
* ``page.find_tables()`` regions are extracted as GFM pipe tables; the words
  inside those regions are removed from the prose stream so table content is not
  duplicated as run-on text.
* Running headers/footers that repeat across most pages are detected and dropped
  (they otherwise duplicate N times in retrieval and splice into paragraphs at
  page boundaries).
* Remaining words are grouped into lines and emitted in top-to-bottom order,
  interleaved with tables by vertical position. (Multi-column PDFs read
  column-interleaved — a documented v1 limitation.)

Untrusted-input safety on a ~1GB droplet: page count, per-page word count, and
per-page vector-edge count (the O(n^2) ``find_tables`` cliff) are all bounded, so
a small crafted PDF cannot pin a worker or OOM. Scanned / image-only PDFs yield
no extractable text; that raises a clear error rather than an empty file.
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from typing import Any

import pdfplumber

from .common import join_blocks, rows_to_markdown_table

logger = logging.getLogger(__name__)

# A line is only treated as a heading when it is short and contains letters —
# guards against a large-font page number or figure label becoming a spurious "#".
_MAX_HEADING_WORDS = 14
_MIN_HEADING_CHARS = 3
_MAX_HEADING_LEVEL = 3
# Words whose vertical centre is within this many points of each other belong to
# the same visual line.
_LINE_TOP_TOLERANCE = 3.0

# Resource bounds (untrusted input).
_MAX_PAGES = 2000
_MAX_WORDS_PER_PAGE = 20_000
# Skip find_tables on a page with more vector edges than this — its cost grows
# ~quadratically in edge count (pdfplumber's well-known performance cliff).
_MAX_EDGES_FOR_TABLES = 4_000
# Running-header/footer detection: a line's exact text seen on at least this
# fraction of pages (in a doc of >= _MIN_PAGES_FOR_BOILERPLATE pages) is boilerplate.
_BOILERPLATE_PAGE_FRACTION = 0.6
_MIN_PAGES_FOR_BOILERPLATE = 3


def _word_in_any_bbox(word: dict, bboxes: list[tuple]) -> bool:
    """True when the word's centre falls inside any (x0, top, x1, bottom) bbox."""
    cx = (word["x0"] + word["x1"]) / 2.0
    cy = (word["top"] + word["bottom"]) / 2.0
    for x0, top, x1, bottom in bboxes:
        if x0 <= cx <= x1 and top <= cy <= bottom:
            return True
    return False


def _group_words_into_lines(words: list[dict]) -> list[dict]:
    """Group words into visual lines: ``{"text", "size", "top"}`` each."""
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: list[dict] = []
    buffer: list[dict] = []
    prev_top: float | None = None

    for word in ordered:
        if prev_top is not None and abs(word["top"] - prev_top) > _LINE_TOP_TOLERANCE:
            lines.append(_finish_line(buffer))
            buffer = []
        buffer.append(word)
        prev_top = word["top"]

    if buffer:
        lines.append(_finish_line(buffer))
    return [ln for ln in lines if ln["text"]]


def _finish_line(words: list[dict]) -> dict:
    ordered = sorted(words, key=lambda w: w["x0"])
    text = " ".join(w["text"] for w in ordered).strip()
    size = max((w.get("size", 0.0) for w in ordered), default=0.0)
    top = min((w["top"] for w in ordered), default=0.0)
    return {"text": text, "size": size, "top": top}


def _classify_line(line: dict, heading_ranks: dict[int, int]) -> tuple[int, str]:
    """Return ``(heading_level, text)`` — level 0 means prose.

    Heading-ness is returned as an explicit level, never encoded into the string,
    so a prose line that merely starts with ``#`` is not mistaken for a heading.
    """
    text = line["text"].strip()
    if not text:
        return (0, "")
    size = round(line["size"])
    if (
        size in heading_ranks
        and len(text.split()) <= _MAX_HEADING_WORDS
        and len(text) >= _MIN_HEADING_CHARS
        and any(ch.isalpha() for ch in text)
    ):
        return (min(heading_ranks[size], _MAX_HEADING_LEVEL), text)
    return (0, text)


def _escape_prose_line(text: str) -> str:
    """Escape a leading ``#`` so a prose line is not parsed as an ATX heading
    by the downstream chunker/renderer (e.g. "# of items: 50")."""
    stripped = text.lstrip()
    if stripped.startswith("#"):
        indent = text[: len(text) - len(stripped)]
        return f"{indent}\\{stripped}"
    return text


def _safe_find_tables(page: Any) -> list[Any]:
    """find_tables, skipped when a page has too many vector edges (cost cliff)."""
    try:
        if len(page.edges) > _MAX_EDGES_FOR_TABLES:
            logger.debug("pdf: skipping find_tables on high-edge-count page")
            return []
        return page.find_tables() or []
    except Exception:  # noqa: BLE001 — table detection is best-effort
        logger.debug("pdf table detection failed on a page", exc_info=True)
        return []


def _detect_boilerplate(page_text_sets: list[set[str]], num_pages: int) -> set[str]:
    """Exact line texts that recur on most pages (running headers/footers)."""
    if num_pages < _MIN_PAGES_FOR_BOILERPLATE:
        return set()
    counts: Counter[str] = Counter()
    for texts in page_text_sets:
        for text in texts:
            counts[text] += 1
    threshold = max(2, int(_BOILERPLATE_PAGE_FRACTION * num_pages))
    return {text for text, count in counts.items() if text and count >= threshold}


def pdf_to_markdown(file_bytes: bytes) -> str:
    """Convert a (text-based) PDF to Markdown.

    Raises ``ValueError`` when the PDF has too many pages or no extractable text.
    """
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        num_pages = len(pdf.pages)
        if num_pages > _MAX_PAGES:
            raise ValueError(
                f"PDF has too many pages ({num_pages}); the maximum is {_MAX_PAGES}."
            )

        # ── Pass 1: collect font sizes + per-page (lines, tables) ────────────
        size_counts: Counter[int] = Counter()
        pages_content: list[tuple[list[dict], list[Any]]] = []
        page_text_sets: list[set[str]] = []

        for page in pdf.pages:
            tables = _safe_find_tables(page)
            table_bboxes = [t.bbox for t in tables]
            words = page.extract_words(extra_attrs=["size"], use_text_flow=True)
            if len(words) > _MAX_WORDS_PER_PAGE:
                words = words[:_MAX_WORDS_PER_PAGE]
            prose_words = [w for w in words if not _word_in_any_bbox(w, table_bboxes)]
            for word in prose_words:
                size_counts[round(word.get("size", 0.0))] += 1
            lines = _group_words_into_lines(prose_words)
            pages_content.append((lines, tables))
            page_text_sets.append({ln["text"] for ln in lines})

        if not size_counts and not any(tables for _, tables in pages_content):
            raise ValueError(
                "No extractable text found in the PDF. Scanned or image-only PDFs "
                "require OCR, which this endpoint does not perform."
            )

        body_size = size_counts.most_common(1)[0][0] if size_counts else 0
        heading_sizes = sorted((s for s in size_counts if s > body_size), reverse=True)
        heading_ranks = {size: rank + 1 for rank, size in enumerate(heading_sizes)}
        boilerplate = _detect_boilerplate(page_text_sets, num_pages)

        # ── Pass 2: emit headings, paragraphs and tables in vertical order ───
        # Consecutive prose lines merge into one paragraph (joined with newlines)
        # so a sentence wrapped across visual lines stays one unit for the
        # sentence-aware chunker; a paragraph breaks on a heading, a table, or a
        # vertical gap wider than a normal line advance.
        blocks: list[str] = []
        para: list[str] = []

        def _flush_para() -> None:
            if para:
                blocks.append("\n".join(para))
                para.clear()

        for lines, tables in pages_content:
            elements: list[tuple[float, str, Any]] = []
            for line in lines:
                elements.append((line["top"], "line", line))
            for table in tables:
                elements.append((table.bbox[1], "table", table))
            elements.sort(key=lambda e: e[0])

            prev_top: float | None = None  # reset per page (top is page-relative)
            prev_size = 0.0

            for top, kind, obj in elements:
                if kind == "table":
                    _flush_para()
                    prev_top = None
                    try:
                        md = rows_to_markdown_table(obj.extract())
                    except Exception:  # noqa: BLE001 — skip an unreadable table
                        logger.debug("pdf table extraction failed", exc_info=True)
                        md = ""
                    if md:
                        blocks.append(md)
                    continue

                # Drop running headers/footers so they neither duplicate across
                # pages nor splice into a paragraph at the page boundary.
                if obj["text"] in boilerplate:
                    continue

                level, text = _classify_line(obj, heading_ranks)
                if level > 0:
                    _flush_para()
                    blocks.append(f"{'#' * level} {text}")
                    prev_top = None
                    continue

                if (
                    para
                    and prev_top is not None
                    and (top - prev_top) > 1.7 * max(prev_size, 1.0)
                ):
                    _flush_para()
                para.append(_escape_prose_line(text))
                prev_top = top
                prev_size = obj["size"]

        _flush_para()

    markdown = join_blocks(blocks)
    if not markdown.strip():
        raise ValueError(
            "No extractable text found in the PDF. Scanned or image-only PDFs "
            "require OCR, which this endpoint does not perform."
        )
    return markdown
