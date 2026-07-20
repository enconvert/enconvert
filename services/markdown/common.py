"""Small shared helpers for the anything-to-markdown extractors.

Pure and dependency-light (only ``tabulate`` + stdlib); no import of the heavy
per-format libraries, so it is safe to import from every extractor without cycles.

Also home to the untrusted-upload safety guards shared across the zip-based and
table-emitting extractors (a public endpoint on a ~1GB droplet):

* ``guard_zip_bomb`` rejects decompression bombs (EPUB/DOCX/XLSX/PPTX are ZIPs).
* ``rows_to_markdown_table`` escapes pipes and hard-caps cell width + row count so
  a wide-cell/many-row grid cannot amplify (tabulate pads every cell to the
  column's widest cell — a known TB-scale amplification vector).
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable, Sequence

from tabulate import tabulate

# Encodings tried, in order, when decoding text-ish uploads (CSV/HTML/TXT and the
# HTML produced by LibreOffice/EPUB chapters). utf-8-sig strips a BOM.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_BLANKS_RE = re.compile(r"\n{3,}")

# ── Untrusted-upload safety limits ───────────────────────────────────────────
# ZIP decompression-bomb guard: bound the declared uncompressed total and the
# entry count. The upload gate only bounds COMPRESSED bytes; DEFLATE reaches
# ~1032:1, so a few-MB archive can claim gigabytes.
MAX_ZIP_UNCOMPRESSED_BYTES = 400 * 1024 * 1024  # 400 MB
MAX_ZIP_ENTRIES = 10_000

# Table render caps: tabulate space-pads every cell to its column's widest cell,
# so one wide cell over many rows amplifies output to OOM. Truncate cells and cap
# rows; both degrade gracefully (with a note) rather than erroring on big data.
MAX_TABLE_CELL_CHARS = 500
MAX_TABLE_ROWS = 5_000


def decode_text_bytes(data: bytes) -> str:
    """Best-effort decode of text bytes to str (never raises)."""
    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def guard_zip_bomb(
    file_bytes: bytes,
    *,
    max_uncompressed: int = MAX_ZIP_UNCOMPRESSED_BYTES,
    max_entries: int = MAX_ZIP_ENTRIES,
) -> None:
    """Reject a decompression bomb before an extractor reads the archive.

    Checks the ZIP central directory's declared sizes (cheap — no decompression).
    A non-ZIP input is left alone so the extractor raises its own clear error.
    Raises ``ValueError`` (-> HTTP 400) when the archive is abusive.

    NOTE: declared ``file_size`` can be understated by a crafted archive; this is
    defense-in-depth atop the compressed-upload size cap, not a complete defense
    against every nested/overlapping bomb.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise ValueError("Archive rejected: too many entries.")
            total = sum(info.file_size for info in infos)
            if total > max_uncompressed:
                raise ValueError(
                    "Archive rejected: decompressed content is too large."
                )
    except zipfile.BadZipFile:
        return  # not a ZIP — let the extractor produce its own error


def rows_to_markdown_table(rows: Iterable[Sequence[object]]) -> str:
    """Render a 2D grid (first row = header) as a safe GFM pipe table.

    Cells are coerced to single-line strings, pipe-escaped, and truncated to
    ``MAX_TABLE_CELL_CHARS``; fully empty rows are dropped and ragged rows are
    right-padded. Body rows are capped at ``MAX_TABLE_ROWS`` (with a note).
    ``disable_numparse`` keeps numeric-looking cells verbatim. Returns "" for an
    empty grid.
    """
    normalized: list[list[str]] = []
    for row in rows:
        cells = [_clean_cell(cell) for cell in row]
        normalized.append(cells)

    normalized = [row for row in normalized if any(cell for cell in row)]
    if not normalized:
        return ""

    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]

    header, body = normalized[0], normalized[1:]
    truncated = len(body) > MAX_TABLE_ROWS
    if truncated:
        body = body[:MAX_TABLE_ROWS]

    table = tabulate(body, headers=header, tablefmt="github", disable_numparse=True)
    if truncated:
        table += f"\n\n_(table truncated to {MAX_TABLE_ROWS} rows)_"
    return table


def _clean_cell(cell: object) -> str:
    """One-line, pipe-escaped, length-capped cell text."""
    text = ("" if cell is None else str(cell)).replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|").strip()
    if len(text) > MAX_TABLE_CELL_CHARS:
        text = text[:MAX_TABLE_CELL_CHARS] + "…"
    return text


def normalize_markdown(text: str) -> str:
    """Normalize line endings and collapse 3+ blank lines to one; trailing \\n."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip() + "\n"


def join_blocks(blocks: Iterable[str]) -> str:
    """Join non-empty markdown blocks with a blank line between them."""
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())
