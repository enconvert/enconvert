"""Heading-aware semantic Markdown chunker (Task H.7, plan sections 4 + 8).

Splits a Markdown document into retrieval-sized chunks while preserving the
structures that lose meaning when broken:

* Boundaries are the Markdown headings ``# h1`` / ``## h2`` / ``### h3``.
  Each chunk belongs to exactly one heading section and carries the full
  ``headings_path`` (h1 -> h2 -> h3) in its metadata; the heading text
  itself lives in the metadata, not the chunk body. ``####``-``######`` are
  NOT boundaries (the body of a section can have arbitrarily deep
  sub-headings) and are kept inline as content.
* Fenced code blocks (``` ``` ``` / ``~~~``) and Markdown pipe tables are
  ATOMIC: never split, even when a single block exceeds ``max_words``.
* List items are kept whole — a long list splits BETWEEN items, never
  mid-item.
* Prose paragraphs are packed greedily up to ``max_words``; when a
  paragraph must be split, the break falls on a sentence boundary and the
  trailing ``sentence_overlap`` sentences are repeated at the head of the
  next chunk (configurable; 0 disables overlap). Overlap never crosses a
  heading boundary.

The output is a flat ``list[Chunk]`` in document order. This is the
"semantic_v1" chunker; the plan reserves ``semantic_v2.py``
(sentence-transformer embeddings) for Phase 6 — this module stays pure,
dependency-free (stdlib + ``re`` only) and fully deterministic so it is
trivially unit-testable and safe to run inside the request path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

# Plan H.7 defaults: 512 words per chunk, one sentence of overlap.
DEFAULT_MAX_WORDS = 512
DEFAULT_SENTENCE_OVERLAP = 1

# Sane request bounds, enforced by the schema layer (ChunkOptions). They
# live here so the schema and the chunker share one source of truth; the
# pure chunker itself honours whatever max_words it is handed (>= 1) so it
# stays trivially unit-testable at small sizes.
MIN_MAX_WORDS = 32
MAX_MAX_WORDS = 4000
MAX_SENTENCE_OVERLAP = 10

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+\S")
# A table separator row: every pipe-delimited cell is dashes with optional
# leading/trailing colon, e.g. ``| :--- | ---: |``.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
# Sentence break: terminal punctuation followed by whitespace. Kept simple
# and deterministic (no abbreviation model) on purpose.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ── Public value type ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit produced by :func:`chunk_markdown`."""

    text: str
    headings_path: Tuple[str, ...]
    section: str
    word_count: int


# ── Pure text helpers ────────────────────────────────────────────────────────


def count_words(text: str) -> int:
    """Whitespace-delimited token count (the chunk-size unit)."""
    return len(text.split())


def split_sentences(text: str) -> List[str]:
    """Split prose into sentences on terminal punctuation + whitespace.

    Deterministic and abbreviation-agnostic: ``"Dr. Smith"`` is two
    "sentences", which is acceptable for chunk sizing and overlap. Returns
    ``[]`` for blank input; a run with no terminal punctuation is returned
    as a single sentence.
    """
    stripped = text.strip()
    if not stripped:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip()]


# ── Block model ──────────────────────────────────────────────────────────────


@dataclass
class _Block:
    """A parsed Markdown block. ``level`` is the heading level (1-6) for a
    heading block, 0 otherwise."""

    kind: str  # "heading" | "code" | "table" | "list" | "paragraph"
    text: str
    level: int = 0


@dataclass
class _Atom:
    """The smallest placeable text unit when packing a section.

    ``kind`` drives both splitting and joining:
    * ``"sentence"`` — overlap-eligible prose; same-block sentences join
      with a single space.
    * ``"atomic"`` — a code block / table (never split, never overlapped).
    * ``"list_item"`` — one whole list item (never split, never overlapped).
    * ``"line"`` — a deep (h4-h6) heading line kept inline (never split).
    ``block_index`` groups atoms that came from the same source block so the
    joiner can restore single-space vs blank-line separation.
    """

    kind: str
    text: str
    block_index: int


def _is_table_separator(line: str) -> bool:
    # Must contain a pipe: a bare ``---`` is a horizontal rule / setext
    # underline, never a one-cell table separator, so prose with an inline
    # ``|`` followed by ``---`` is not misread as a table.
    return "|" in line and bool(line.strip()) and bool(_TABLE_SEP_RE.match(line))


def parse_blocks(markdown: str) -> List[_Block]:
    """Parse Markdown into ordered blocks, preserving atomic structures.

    Single forward pass over the lines; multi-line constructs (fenced code,
    pipe tables, lists) consume their own line ranges so they survive intact
    into a single block.
    """
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: List[_Block] = []
    paragraph: List[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = "\n".join(paragraph).strip()
            if text:
                blocks.append(_Block("paragraph", text))
            paragraph.clear()

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(2)[0]  # ` or ~
            open_len = len(fence.group(2))  # CommonMark: closing fence >= this
            fence_start = index
            buffer = [line]
            index += 1
            closed = False
            while index < total:
                cur = lines[index]
                closing = _FENCE_RE.match(cur)
                buffer.append(cur)
                index += 1
                if (
                    closing
                    and closing.group(2)[0] == marker
                    and len(closing.group(2)) >= open_len
                ):
                    closed = True
                    break
            if closed:
                blocks.append(_Block("code", "\n".join(buffer).strip()))
            else:
                # Unclosed fence: do NOT swallow the rest of the document
                # (headings, etc.). Treat the opening fence line as ordinary
                # text and re-parse from the next line.
                index = fence_start + 1
                paragraph.append(line)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            blocks.append(_Block("heading", heading.group(2).strip(), level))
            index += 1
            continue

        # Table: a pipe row immediately followed by a separator row.
        if "|" in line and index + 1 < total and _is_table_separator(lines[index + 1]):
            flush_paragraph()
            buffer = [line, lines[index + 1]]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                buffer.append(lines[index])
                index += 1
            blocks.append(_Block("table", "\n".join(buffer).strip()))
            continue

        if _LIST_ITEM_RE.match(line):
            flush_paragraph()
            buffer = [line]
            index += 1
            # Consume continuation/nested lines until a blank line or a
            # non-list, non-indented line.
            while index < total:
                nxt = lines[index]
                if not nxt.strip():
                    break
                if _LIST_ITEM_RE.match(nxt) or nxt[:1] in (" ", "\t"):
                    buffer.append(nxt)
                    index += 1
                    continue
                break
            blocks.append(_Block("list", "\n".join(buffer).strip()))
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return blocks


def _split_list_items(block_text: str) -> List[str]:
    """Split a list block into whole top-level items (nested lines stay)."""
    lines = block_text.split("\n")
    items: List[str] = []
    current: List[str] = []
    base_indent: int | None = None
    for line in lines:
        match = _LIST_ITEM_RE.match(line)
        indent = len(line) - len(line.lstrip())
        is_new_item = match is not None and (
            base_indent is None or indent <= base_indent
        )
        if is_new_item:
            if base_indent is None:
                base_indent = indent
            if current:
                items.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        items.append("\n".join(current))
    return [item for item in items if item.strip()]


def _atoms_for_block(block: _Block, block_index: int) -> List[_Atom]:
    """Decompose a body block into placeable atoms."""
    if block.kind in ("code", "table"):
        return [_Atom("atomic", block.text, block_index)]
    if block.kind == "list":
        return [
            _Atom("list_item", item, block_index)
            for item in _split_list_items(block.text)
        ]
    if block.kind == "heading":  # h4-h6 only reach here (boundaries removed)
        return [_Atom("line", block.text, block_index)]
    return [
        _Atom("sentence", sentence, block_index)
        for sentence in split_sentences(block.text)
    ]


def _join_atoms(atoms: List[_Atom]) -> str:
    """Reassemble atoms into chunk text, restoring block separation.

    Two consecutive prose sentences from the same source block rejoin with a
    single space; everything else is separated by a blank line so code,
    tables and list items keep their own paragraph.
    """
    parts: List[str] = []
    previous: _Atom | None = None
    for atom in atoms:
        if previous is None:
            separator = ""
        elif (
            atom.kind == "sentence"
            and previous.kind == "sentence"
            and atom.block_index == previous.block_index
        ):
            separator = " "
        else:
            separator = "\n\n"
        parts.append(separator)
        parts.append(atom.text)
        previous = atom
    return "".join(parts).strip()


def _pack_atoms(
    atoms: List[_Atom], max_words: int, sentence_overlap: int
) -> List[str]:
    """Greedily pack atoms into <= max_words chunk texts (within one section).

    An atom larger than ``max_words`` (oversize code/table/item or a single
    very long sentence) becomes its own chunk — integrity beats the size
    cap. When a prose split happens, the trailing ``sentence_overlap``
    sentences are carried into the next chunk.
    """
    chunks: List[str] = []
    current: List[_Atom] = []
    current_words = 0

    def overlap_tail() -> List[_Atom]:
        if sentence_overlap <= 0:
            return []
        tail: List[_Atom] = []
        for atom in reversed(current):
            if atom.kind != "sentence":
                break
            tail.append(atom)
            if len(tail) >= sentence_overlap:
                break
        return list(reversed(tail))

    for atom in atoms:
        words = count_words(atom.text)

        if words > max_words:
            # Oversize indivisible atom: flush, then emit it alone.
            if current:
                chunks.append(_join_atoms(current))
            current = []
            current_words = 0
            chunks.append(atom.text.strip())
            continue

        if current and current_words + words > max_words:
            chunks.append(_join_atoms(current))
            current = overlap_tail()
            current_words = sum(count_words(a.text) for a in current)

        current.append(atom)
        current_words += words

    if current:
        chunks.append(_join_atoms(current))
    return [chunk for chunk in chunks if chunk.strip()]


def chunk_markdown(
    markdown: str,
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    sentence_overlap: int = DEFAULT_SENTENCE_OVERLAP,
) -> List[Chunk]:
    """Split ``markdown`` into heading-aware, size-bounded chunks.

    Args:
        markdown: The source Markdown (e.g. perceive fit-markdown).
        max_words: Soft cap on words per chunk; atomic blocks may exceed it.
        sentence_overlap: Sentences repeated between consecutive prose chunks
            of the same section (0 disables overlap).

    Returns:
        Chunks in document order. Empty / heading-only input yields ``[]``.
    """
    # The pure chunker honours the caller's size (>= 1 word); API-facing
    # bounds (MIN/MAX_MAX_WORDS) are enforced upstream in ChunkOptions.
    max_words = max(1, int(max_words))
    sentence_overlap = max(0, int(sentence_overlap))

    blocks = parse_blocks(markdown or "")

    # Group body blocks under their (h1, h2, h3) heading path.
    sections: List[Tuple[Tuple[str, ...], List[_Block]]] = []
    path_stack: List[Tuple[int, str]] = []
    body: List[_Block] = []

    def flush_section() -> None:
        if body:
            sections.append((tuple(text for _, text in path_stack), list(body)))
            body.clear()

    for block in blocks:
        if block.kind == "heading" and 1 <= block.level <= 3:
            flush_section()
            while path_stack and path_stack[-1][0] >= block.level:
                path_stack.pop()
            path_stack.append((block.level, block.text))
        else:
            body.append(block)
    flush_section()

    chunks: List[Chunk] = []
    for headings_path, section_blocks in sections:
        atoms: List[_Atom] = []
        for block_index, block in enumerate(section_blocks):
            atoms.extend(_atoms_for_block(block, block_index))
        section = headings_path[-1] if headings_path else ""
        for text in _pack_atoms(atoms, max_words, sentence_overlap):
            chunks.append(
                Chunk(
                    text=text,
                    headings_path=headings_path,
                    section=section,
                    word_count=count_words(text),
                )
            )
    return chunks
