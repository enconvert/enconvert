"""Anything-to-PDF dispatcher.

``convert_to_pdf(file_bytes, filename)`` picks a converter by file extension and
returns PDF bytes. It is ASYNC and offloads every CPU-bound (or subprocess)
converter to a worker thread (``asyncio.to_thread``) so it never blocks the
event loop — the same shape as ``services/markdown/dispatch.py``.

Every underlying engine already ships with the gateway; this endpoint adds no
new dependency:

  * Office / legacy / CSV / RTF -> LibreOffice headless (``unoconvert``), the
    exact path the existing ``doc-to-pdf`` family uses.
  * HTML / Markdown / plain text -> WeasyPrint.
  * EPUB -> reuse the EPUB->Markdown reader, then WeasyPrint.
  * Raster images -> Pillow ``Image.save(..., "PDF")``.
  * SVG -> CairoSVG (vector).
  * PDF input -> validated passthrough (lets callers normalise / grayscale an
    existing PDF through the same endpoint).

Per-format libraries are imported lazily inside each branch so importing this
module (and having ``utils/validators`` read ``SUPPORTED_EXTENSIONS``) stays
cheap — the heavy libs load only when a matching file is converted.

PDF_OPTIONS CONTRACT (per engine — the engines genuinely differ):

  * WeasyPrint (html/markdown/text/epub) and the image/SVG paths honour the
    full geometry set: page_size, page_width/height, orientation, margins,
    scale, header, footer.
  * LibreOffice (office/odf/iwork/rtf/csv) CANNOT: page geometry comes from the
    source document's own page style, applied at layout time before PDF export,
    and ``unoconvert`` exposes only export-filter options (no layout control).
  * PDF passthrough would need re-imposition of an existing document.

For the latter two, an EXPLICITLY-SET geometry option raises
``UnsupportedOptionError`` (-> HTTP 400) rather than being silently discarded.
``grayscale`` always works everywhere: it is a post-process applied by the
caller, not by this dispatcher.

The rejection rule and its message are NOT implemented here: they live in
``utils/pdf_helpers.assert_geometry_supported``, shared with the nine
LibreOffice-backed document endpoints (doc-to-pdf, excel-to-pdf, ...), which
have the same constraint with a fixed engine. This module owns only the
extension -> engine resolution, which is its actual job.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import os
from typing import TYPE_CHECKING, Optional

from services.conversion_errors import UnsupportedOptionError
from utils.pdf_helpers import assert_geometry_supported, explicit_geometry_fields

if TYPE_CHECKING:
    from models import PdfOptions

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "UnsupportedFormatError",
    "UnsupportedOptionError",
    "assert_options_supported",
    "convert_to_pdf",
]


class UnsupportedFormatError(ValueError):
    """Raised for a file extension this endpoint does not convert.

    Subclasses ``ValueError`` so the gateway's converter error handling maps it
    to an HTTP 400 with the message text.
    """


# Extension groups. Office/legacy/tabular go through LibreOffice (unoconvert);
# everything else uses a pure-Python / already-installed engine.
_OFFICE_EXTS = frozenset(
    {
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odp", ".ots", ".pages", ".numbers",
        ".rtf", ".csv",
    }
)
_HTML_EXTS = frozenset({".html", ".htm", ".xhtml"})
_MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd"})
_TEXT_EXTS = frozenset({".txt", ".text"})
_IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
)
_SVG_EXTS = frozenset({".svg"})
_EPUB_EXTS = frozenset({".epub"})
_PDF_EXTS = frozenset({".pdf"})

# Public allowlist (mirrored into utils/validators.ALLOWED_EXTENSIONS for the
# endpoint's extension gate — imported there so this stays the single source of
# truth).
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(
        _OFFICE_EXTS
        | _HTML_EXTS
        | _MARKDOWN_EXTS
        | _TEXT_EXTS
        | _IMAGE_EXTS
        | _SVG_EXTS
        | _EPUB_EXTS
        | _PDF_EXTS
    )
)


# Engines that can honor page geometry. See the module docstring for why the
# office family and PDF passthrough cannot.
_GEOMETRY_CAPABLE_EXTS = (
    _HTML_EXTS | _MARKDOWN_EXTS | _TEXT_EXTS | _EPUB_EXTS | _IMAGE_EXTS | _SVG_EXTS
)


def assert_options_supported(
    filename: str, pdf_options: "Optional[PdfOptions]" = None
) -> None:
    """Reject explicitly-set options the engine for ``filename`` cannot honor.

    Resolves extension -> engine, then defers the rule and the message to the
    shared ``assert_geometry_supported``. Raises ``UnsupportedOptionError``
    (-> HTTP 400). Silent on defaults and on unknown extensions (there the
    normal UnsupportedFormatError should win).
    """
    if not explicit_geometry_fields(pdf_options):
        return

    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _GEOMETRY_CAPABLE_EXTS or ext not in SUPPORTED_EXTENSIONS:
        return

    engine = "LibreOffice" if ext in _OFFICE_EXTS else "PDF passthrough"
    assert_geometry_supported(pdf_options, fmt=ext, engine=engine)


async def convert_to_pdf(
    file_bytes: bytes,
    filename: str,
    pdf_options: "Optional[PdfOptions]" = None,
) -> bytes:
    """Convert an uploaded file's bytes to PDF bytes.

    Raises ``UnsupportedFormatError`` (400) for an unknown extension,
    ``UnsupportedOptionError`` (400) for a geometry option this input's engine
    cannot honor, and ``ValueError`` (400) for a malformed/empty document;
    unexpected converter failures surface as a generic 500 via the caller.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext:
        raise UnsupportedFormatError(
            "Cannot determine the file type: the filename has no extension."
        )
    # Defense in depth: the route gates this too, so quota isn't burned on a
    # pure client error. Cheap, and covers any other caller of the dispatcher.
    assert_options_supported(filename, pdf_options)

    # Normalise away options that carry no geometry (e.g. grayscale-only) so
    # every converter below takes its original, byte-identical no-options path.
    if not explicit_geometry_fields(pdf_options):
        pdf_options = None
    return await _dispatch(ext, file_bytes, filename, pdf_options)


async def _dispatch(
    ext: str,
    file_bytes: bytes,
    filename: str,
    pdf_options: "Optional[PdfOptions]" = None,
) -> bytes:
    if ext in _OFFICE_EXTS:
        # LibreOffice headless. Sync + subprocess -> offload to a thread. The
        # real filename is required so the temp file keeps its extension and
        # LibreOffice selects the correct import filter.
        # Geometry was already rejected by assert_options_supported, so there is
        # nothing to thread through here.
        from services.documents.converters.convert_to_pdf import convert_to_pdf

        return await asyncio.to_thread(convert_to_pdf, file_bytes, filename)

    if ext in _HTML_EXTS:
        from services.lightweight.converters import html_to_pdf

        return await asyncio.to_thread(html_to_pdf, file_bytes, pdf_options)

    if ext in _MARKDOWN_EXTS:
        from services.lightweight.converters import markdown_to_pdf

        return await asyncio.to_thread(markdown_to_pdf, file_bytes, pdf_options)

    if ext in _TEXT_EXTS:
        return await asyncio.to_thread(_text_to_pdf, file_bytes, pdf_options)

    if ext in _EPUB_EXTS:
        return await asyncio.to_thread(_epub_to_pdf, file_bytes, pdf_options)

    if ext in _IMAGE_EXTS:
        from services.image.converters import image_to_pdf

        return await asyncio.to_thread(image_to_pdf, file_bytes, filename, pdf_options)

    if ext in _SVG_EXTS:
        from services.image.converters import svg_to_pdf

        return await asyncio.to_thread(svg_to_pdf, file_bytes, filename, pdf_options)

    if ext in _PDF_EXTS:
        return _pdf_passthrough(file_bytes)

    raise UnsupportedFormatError(f"Unsupported file type '{ext}' for anything-to-pdf.")


def _text_to_pdf(
    file_bytes: bytes, pdf_options: "Optional[PdfOptions]" = None
) -> bytes:
    """Render plain text to PDF, preserving whitespace via a monospace ``<pre>``."""
    from services.lightweight.converters import html_to_pdf
    from services.markdown.common import decode_text_bytes

    escaped = html_lib.escape(decode_text_bytes(file_bytes))
    # The built-in 2cm margin is this path's default, but it must not fight
    # caller-supplied margins: html_to_pdf injects the @page rule from
    # pdf_options after this <style>, so drop the default rather than rely on
    # the cascade to resolve two competing @page blocks.
    default_page_css = "@page{margin:2cm}" if pdf_options is None else ""
    document = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<style>{default_page_css}"
        "pre{white-space:pre-wrap;word-wrap:break-word;"
        "font-family:'Courier New',monospace;font-size:11pt;line-height:1.4;"
        "color:#222}</style></head>"
        f"<body><pre>{escaped}</pre></body></html>"
    )
    return html_to_pdf(document.encode("utf-8"), pdf_options)


def _epub_to_pdf(
    file_bytes: bytes, pdf_options: "Optional[PdfOptions]" = None
) -> bytes:
    """Convert EPUB -> Markdown (existing reader) -> PDF (WeasyPrint)."""
    from services.lightweight.converters import markdown_to_pdf
    from services.markdown.epub_md import epub_to_markdown

    markdown_text = epub_to_markdown(file_bytes)
    return markdown_to_pdf(markdown_text.encode("utf-8"), pdf_options)


def _pdf_passthrough(file_bytes: bytes) -> bytes:
    """Accept an already-PDF upload (idempotent normalise/grayscale path)."""
    if not file_bytes.startswith(b"%PDF-"):
        raise ValueError("The file has a .pdf extension but is not a valid PDF.")
    return file_bytes
