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
# A detected "table" whose region is less upright than this is a FIGURE whose
# line art the table finder read as a grid. Attention-visualisation plots,
# flow charts and org diagrams all draw rules and label them with rotated
# text; extracting them yields cells of one or two reversed characters
# ("| ehT | waL | lliw |" for "The Law will"), which is worse than no table.
# A genuine data table may carry rotated COLUMN HEADERS, but its body is
# upright, so its region stays far above this bar — measured across an
# academic-paper / standards / financial-report corpus, every real table
# scored 1.00 upright and every figure scored 0.68 or below.
_MIN_TABLE_UPRIGHT_RATIO = 0.80
# How much larger than body text a size must be to mean "heading". Font sizes
# are rounded to integers, so one body font routinely lands in two adjacent
# buckets (Berkshire's 2023 report: 1,714 words at 12pt and 410 at 13pt, all
# the same face) — and "strictly larger than body" then promoted 410 words of
# ordinary prose to headings, one sentence at a time. A real heading is a
# visible step up, not a rounding artifact; 12% clears every rounding pair
# (10/11, 12/13, 16/17) while keeping every genuine step (10->12, 12->14,
# 16->18, 18->21) in the test corpus.
_MIN_HEADING_SIZE_RATIO = 1.12
# Words a page needs before its own dominant size is trusted as that page's
# body size. Below it the page is a cover, a divider or a plate, and its
# "body" would be the title itself.
_MIN_WORDS_FOR_PAGE_BODY = 30
# Absolute companion to the ratio. A ratio alone cannot separate a rounding
# pair from a real step at small sizes: 8pt fine print rounds into 8 and 9,
# and 9/8 = 1.125 clears a 1.12 ratio, so footnote continuations in a
# financial filing became headings. Nobody sets a heading one point larger
# than its body, so a heading must ALSO be at least this many points up.
_MIN_HEADING_SIZE_POINTS = 2
# Upright words a page needs before the rotated ones are treated as noise. A
# landscape appendix or a sideways-printed table is rotated in its ENTIRETY,
# so dropping non-upright text would silently empty the page; a figure sits
# on a page that still has real upright prose around it. Well clear of both
# observed cases (a rotated page has 0-2 upright words, an
# attention-visualisation page had 24).
_MIN_UPRIGHT_WORDS_PER_PAGE = 5


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


# Fraction of a fragment's own extent that a gap must exceed to be a word
# space. Rotated text comes back one GLYPH at a time, so the space has to be
# rebuilt from geometry: measured on rotated pages, intra-word gaps are ~0pt
# and word gaps ~2.8pt against 3-7pt glyphs, so anything in between works.
_ROTATED_SPACE_GAP_RATIO = 0.25


def _group_rotated_words_into_lines(
    words: list[dict], rotation: int = 90
) -> list[dict]:
    """Line-group text on a page that is rotated as a whole.

    A landscape appendix or a sideways-printed wide table runs down the page
    in unrotated coordinates, so the horizontal grouper produced one line per
    GLYPH — a column of single characters. Here the line axis is x and the
    reading axis is y, and word spaces are rebuilt from the gaps between
    fragments (the extractor splits rotated runs per glyph, so joining them
    naively gives "AppendixTableA-1").
    """
    # /Rotate 90 is displayed clockwise: reading runs down the unrotated page
    # and successive lines advance towards smaller x. /Rotate 270 mirrors
    # both axes — read the other way and the text comes back reversed
    # character by character ("noigeR yB euneveR").
    downwards = rotation != 270
    lines: list[dict] = []
    buckets: dict[int, list[dict]] = {}
    for word in words:
        key = int(word["x0"] // _LINE_TOP_TOLERANCE)
        buckets.setdefault(key, []).append(word)

    for key in sorted(buckets):
        run = sorted(buckets[key], key=lambda w: w["top"], reverse=not downwards)
        parts: list[str] = []
        previous: dict | None = None
        for word in run:
            if previous is not None:
                extent = max(word["bottom"] - word["top"], 1.0)
                gap = (
                    word["top"] - previous["bottom"]
                    if downwards
                    else previous["top"] - word["bottom"]
                )
                if gap > extent * _ROTATED_SPACE_GAP_RATIO:
                    parts.append(" ")
            parts.append(word["text"])
            previous = word
        text = "".join(parts).strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "size": _dominant_size(run),
                # ``top`` is what the emitter sorts on. Successive lines of a
                # page rotated clockwise (/Rotate 90) advance towards SMALLER
                # x in unrotated coordinates, so the sign follows the
                # rotation or the page comes out last-line-first.
                "top": float(-key if rotation == 90 else key)
                * _LINE_TOP_TOLERANCE,
            }
        )
    return lines


def _finish_line(words: list[dict]) -> dict:
    ordered = sorted(words, key=lambda w: w["x0"])
    text = " ".join(w["text"] for w in ordered).strip()
    top = min((w["top"] for w in ordered), default=0.0)
    return {"text": text, "size": _dominant_size(ordered), "top": top}


def _dominant_size(words: list[dict]) -> float:
    """The size MOST of a line is set in, not the largest size on it.

    Taking the maximum let a single oversized glyph reclassify a whole line
    of body prose as a heading: an arXiv sidebar stamp shares its vertical
    position with the abstract, so ``plewis@fb.com`` came out as an h1 and
    every following chunk inherited it as a headings_path ancestor. A
    trailing footnote marker, a drop cap or an inline logo does the same.

    Votes are weighted by CHARACTER count, not word count. A drop cap is one
    word of one letter, so by word count it ties with the single short word
    beside it on the opening line — and a tie broken toward the larger size
    reproduces exactly the bug this replaced. By characters it loses, which
    is also the right answer for the honest case: a line is set in whatever
    size most of its text is set in. Ties still go to the larger size, so a
    genuine short heading is unaffected.
    """
    if not words:
        return 0.0
    counts: Counter[int] = Counter()
    for word in words:
        weight = len(word.get("text") or "") or 1
        counts[round(word.get("size", 0.0))] += weight
    top = max(counts.items(), key=lambda item: (item[1], item[0]))
    return float(top[0])


def _classify_line(
    line: dict, heading_ranks: dict[int, int], heading_floor: float = 0.0
) -> tuple[int, str]:
    """Return ``(heading_level, text)`` — level 0 means prose.

    Heading-ness is returned as an explicit level, never encoded into the string,
    so a prose line that merely starts with ``#`` is not mistaken for a heading.

    ``heading_floor`` is the size THIS PAGE must exceed to be a heading (see
    ``_page_heading_floor``); the LEVEL still comes from the document-wide
    ranking, so h1/h2/h3 mean the same thing on every page.
    """
    text = line["text"].strip()
    if not text:
        return (0, "")
    size = round(line["size"])
    if (
        size >= heading_floor
        and size in heading_ranks
        and len(text.split()) <= _MAX_HEADING_WORDS
        and len(text) >= _MIN_HEADING_CHARS
        and any(ch.isalpha() for ch in text)
        and _has_a_real_word(text)
    ):
        return (min(heading_ranks[size], _MAX_HEADING_LEVEL), text)
    return (0, text)


def _has_a_real_word(text: str) -> bool:
    """False when a line is nothing but isolated letterforms.

    Letterhead styling sets each word's initial capital much larger than the
    rest, and the enlarged caps sit on their own baseline — far enough apart
    that they group into a line of their own. "BERKSHIRE HATHAWAY" split into
    a line ``B H`` (promoted to a heading, being oversized) and a paragraph
    ``ERKSHIRE ATHAWAY``, so the company name lost its first letters. A
    heading always contains at least one whole word.
    """
    return any(len(token) > 1 for token in text.split())


def _escape_prose_line(text: str) -> str:
    """Escape a leading ``#`` so a prose line is not parsed as an ATX heading
    by the downstream chunker/renderer (e.g. "# of items: 50")."""
    stripped = text.lstrip()
    if stripped.startswith("#"):
        indent = text[: len(text) - len(stripped)]
        return f"{indent}\\{stripped}"
    return text


def _page_heading_floor(
    page_counts: "Counter[int]", document_body: int
) -> float:
    """Smallest size that counts as a heading ON THIS PAGE.

    Body size is a PAGE property, not a document one. A 10-K bundles a
    shareholder letter set at 12pt with financial statements set at 10pt;
    the document-wide mode is therefore 10, and every line of the letter —
    ordinary prose — cleared "larger than body" and became a heading, 82 of
    them in one filing. Each page is typographically coherent, so the page's
    own dominant size is the right reference. A page too sparse to model one
    (a cover, a divider, a page of figures) falls back to the document's.
    """
    total = sum(page_counts.values())
    body = (
        page_counts.most_common(1)[0][0]
        if total >= _MIN_WORDS_FOR_PAGE_BODY
        else document_body
    )
    return max(
        body * _MIN_HEADING_SIZE_RATIO, body + _MIN_HEADING_SIZE_POINTS
    )


def _is_upright_region(words: list[dict], bbox: tuple) -> bool:
    """True when a detected table's region is really tabular, not a figure.

    See ``_MIN_TABLE_UPRIGHT_RATIO``. An empty region is accepted: a table
    of pure rules with no text cannot produce garbage anyway, and the row
    renderer drops it.
    """
    inside = [w for w in words if _word_in_any_bbox(w, [bbox])]
    if not inside:
        return True
    upright = sum(1 for w in inside if w.get("upright", True))
    return upright / len(inside) >= _MIN_TABLE_UPRIGHT_RATIO


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
        pages_content: list[tuple[list[dict], list[tuple[Any, Any]]]] = []
        page_text_sets: list[set[str]] = []
        page_size_counts: list[Counter[int]] = []

        for page in pdf.pages:
            all_words = page.extract_words(
                extra_attrs=["size", "upright"], use_text_flow=True
            )
            if len(all_words) > _MAX_WORDS_PER_PAGE:
                all_words = all_words[:_MAX_WORDS_PER_PAGE]
            # Rotated text is normally not body prose. It is a margin stamp, a
            # watermark, or a figure's axis labels — and because it shares a
            # vertical position with real lines, letting it through both
            # corrupted the line's text ("C Largepre-trained...") and, via the
            # oversized stamp glyph, its heading classification.
            #
            # UNLESS the page is rotated as a whole (a landscape appendix, a
            # sideways-printed wide table), in which case the rotated text IS
            # the page and dropping it would lose every word on it without a
            # trace. Told apart by what is left standing: a figure sits on a
            # page that still has upright prose around it.
            words = [w for w in all_words if w.get("upright", True)]
            page_is_rotated = (
                len(words) < _MIN_UPRIGHT_WORDS_PER_PAGE
                and len(all_words) > len(words)
            )
            if page_is_rotated:
                words = all_words

            tables = [
                t
                for t in _safe_find_tables(page)
                if _is_upright_region(all_words, t.bbox)
            ]
            table_bboxes = [t.bbox for t in tables]
            # Materialize (bbox, rows) NOW instead of keeping Table objects:
            # a live Table pins its Page (and the page's char/word caches) for
            # the whole document — multi-GB on large PDFs. extract() was paid
            # in pass 2 anyway, so this is zero net CPU. rows=None marks a
            # failed extract so pass 2 still breaks the paragraph there.
            table_data: list[tuple[Any, Any]] = []
            for t in tables:
                try:
                    table_data.append((t.bbox, t.extract()))
                except Exception:  # noqa: BLE001 — skip an unreadable table
                    logger.debug("pdf table extraction failed", exc_info=True)
                    table_data.append((t.bbox, None))
            prose_words = [w for w in words if not _word_in_any_bbox(w, table_bboxes)]
            # Body size is measured over EVERY word, table cells included.
            # Measuring prose only left the counter empty on a page whose
            # layout border pdfplumber reads as a table (it swallows the
            # whole body), and an empty counter made body_size 0 — which
            # ranks every size as a heading and turns each remaining prose
            # line into an h1/h2/h3. Table text is body text; it belongs in
            # the estimate.
            # Body size is measured over PROSE, because that is what it is
            # compared against: a page whose data table is set smaller than
            # its prose would otherwise take the table's size as "body" and
            # promote every real sentence to a heading. Table text is the
            # fallback (not nothing) for a page whose layout border the table
            # finder swallowed whole — an empty counter used to make
            # body_size 0, which made every size a heading.
            prose_counts: Counter[int] = Counter()
            for word in prose_words:
                prose_counts[round(word.get("size", 0.0))] += 1
            this_page = prose_counts
            if sum(prose_counts.values()) < _MIN_WORDS_FOR_PAGE_BODY:
                this_page = Counter(prose_counts)
                for word in words:
                    this_page[round(word.get("size", 0.0))] += 1
            size_counts.update(this_page)
            page_size_counts.append(this_page)
            lines = (
                _group_rotated_words_into_lines(
                    prose_words, getattr(page, "rotation", 0) or 90
                )
                if page_is_rotated
                else _group_words_into_lines(prose_words)
            )
            pages_content.append((lines, table_data))
            page_text_sets.append({ln["text"] for ln in lines})
            # Everything pass 2 needs is now in plain lists — drop this page's
            # cached chars/words/edges so peak memory stays ~one page.
            page.flush_cache()

        if not size_counts and not any(tables for _, tables in pages_content):
            raise ValueError(
                "No extractable text found in the PDF. Scanned or image-only PDFs "
                "require OCR, which this endpoint does not perform."
            )

        # No size model at all -> no heading model. Falling back to
        # ``body_size = 0`` would make EVERY size "larger than the body" and
        # promote every short line to a heading, leaving a document with no
        # body text at all.
        page_floors: list[float] = []
        if size_counts:
            body_size = size_counts.most_common(1)[0][0]
            page_floors = [
                _page_heading_floor(counts, body_size)
                for counts in page_size_counts
            ]
            # A size is a heading size if it clears the floor on the page it
            # appears on; the RANK is document-wide so h1/h2/h3 stay
            # consistent across a document with mixed body sizes.
            heading_sizes_set: set[int] = set()
            for counts, floor in zip(page_size_counts, page_floors):
                heading_sizes_set.update(s for s in counts if s >= floor)
            heading_sizes = sorted(heading_sizes_set, reverse=True)
            heading_ranks = {
                size: rank + 1 for rank, size in enumerate(heading_sizes)
            }
        else:
            heading_ranks = {}
            page_floors = [0.0] * len(pages_content)
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

        for page_index, (lines, tables) in enumerate(pages_content):
            heading_floor = (
                page_floors[page_index] if page_index < len(page_floors) else 0.0
            )
            elements: list[tuple[float, str, Any]] = []
            for line in lines:
                elements.append((line["top"], "line", line))
            for bbox, rows in tables:
                elements.append((bbox[1], "table", rows))
            elements.sort(key=lambda e: e[0])

            prev_top: float | None = None  # reset per page (top is page-relative)
            prev_size = 0.0

            for top, kind, obj in elements:
                if kind == "table":
                    _flush_para()
                    prev_top = None
                    try:
                        # Rows were materialized in pass 1; None = failed extract.
                        md = rows_to_markdown_table(obj) if obj is not None else ""
                    except Exception:  # noqa: BLE001 — skip an unreadable table
                        logger.debug("pdf table rendering failed", exc_info=True)
                        md = ""
                    if md:
                        blocks.append(md)
                    continue

                # Drop running headers/footers so they neither duplicate across
                # pages nor splice into a paragraph at the page boundary.
                if obj["text"] in boilerplate:
                    continue

                level, text = _classify_line(obj, heading_ranks, heading_floor)
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
