"""Anything-to-Markdown dispatcher.

``convert_to_markdown(file_bytes, filename)`` picks an extractor by file
extension and returns UTF-8 Markdown bytes. It is ASYNC and offloads every
CPU-bound extractor to a worker thread (``asyncio.to_thread``) so it never
blocks the event loop, and it is reused verbatim by both the
``/v1/convert/anything-to-markdown`` endpoint and (later) the ``/v2/ingest``
file pipeline.

Per-format libraries are imported lazily inside each branch so importing this
module (and the package) stays cheap; the heavy libs (pdfplumber, mammoth,
python-pptx, pandas/openpyxl) load only when a matching file is converted.

Image OCR is intentionally NOT handled here: it would require an LLM vision call
governed by the same non-negotiable budget/rate gate as the schema extractor
(``services/v2_engine/extractors/schema_llm``). It returns a clear
``UnsupportedFormatError`` (-> HTTP 400) until that gate is wired in.
"""

from __future__ import annotations

import asyncio
import os

from .common import decode_text_bytes, normalize_markdown
from .html_md import html_to_markdown


class UnsupportedFormatError(ValueError):
    """Raised for a file extension this endpoint does not (yet) convert.

    Subclasses ``ValueError`` so the gateway's converter error handling maps it
    to an HTTP 400 with the message text.
    """


# Extension groups. Native = a dedicated pure-/light-Python extractor; legacy =
# routed through unoserver (LibreOffice) like the existing *-to-pdf endpoints.
_TEXT_EXTS = frozenset({".txt", ".text", ".md", ".markdown", ".mdown", ".mkd"})
_HTML_EXTS = frozenset({".html", ".htm", ".xhtml"})
_LEGACY_EXTS = frozenset({".doc", ".ppt", ".xls", ".odt", ".ods", ".odp", ".rtf"})
_NATIVE_EXTS = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".epub"})

# Recognised but deferred (needs OCR / an LLM budget gate).
_IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
)

# Public allowlist (also mirrored in utils/validators.ALLOWED_EXTENSIONS for the
# endpoint's extension gate — keep the two in sync).
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(_TEXT_EXTS | _HTML_EXTS | _LEGACY_EXTS | _NATIVE_EXTS)
)


async def convert_to_markdown(file_bytes: bytes, filename: str) -> bytes:
    """Convert an uploaded file's bytes to UTF-8 Markdown bytes.

    Raises ``UnsupportedFormatError`` (400) for an unknown/deferred extension and
    ``ValueError`` (400) for a malformed/empty document; the extractor libraries'
    own exceptions surface as a generic 500 via the caller.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext:
        raise UnsupportedFormatError(
            "Cannot determine the file type: the filename has no extension."
        )
    markdown = await _dispatch(ext, file_bytes, filename)
    return normalize_markdown(markdown).encode("utf-8", errors="replace")


def _uploaded_html_to_markdown(html: str) -> str:
    """An uploaded web page gets the SAME treatment as a crawled one.

    A saved page carries the site's navigation, cookie banner and footer, and
    converting it faithfully put all of that in the chunks — while crawling
    the identical URL produced clean article markdown. Same input, two
    different answers, depending only on how it arrived. This routes an
    uploaded page through the shared page-markdown ensemble, which has a
    fidelity guard: if stripping would take too much, it returns the whole
    page rather than a stub.

    Imported lazily — the ensemble reaches back into this package for the
    shared HTML core, so a module-level import would be circular. It also
    keeps the browser/V2 dependency off the import path of every other
    format.
    """
    from services.v2_engine.page_markdown import main_content_markdown

    warnings: list[str] = []
    # images_to_alt=False: /v2/perceive can drop an image URL because it
    # offers the full image list as a separate output. A file upload has no
    # second channel, so the reference has to survive in the markdown.
    curated = main_content_markdown(
        html, "", warnings, images_to_alt=False
    ).decode("utf-8", errors="replace")
    if curated.strip():
        return curated
    return html_to_markdown(html, "", extract_article=False)


async def _dispatch(ext: str, file_bytes: bytes, filename: str) -> str:
    if ext in _TEXT_EXTS:
        # Plain text / Markdown: decode off the event loop (a large upload with a
        # late invalid byte forces multiple full-buffer decode attempts).
        return await asyncio.to_thread(decode_text_bytes, file_bytes)

    if ext in _HTML_EXTS:
        html = decode_text_bytes(file_bytes)
        return await asyncio.to_thread(_uploaded_html_to_markdown, html)

    if ext == ".pdf":
        from .pdf_md import pdf_to_markdown

        return await asyncio.to_thread(pdf_to_markdown, file_bytes)

    if ext == ".docx":
        from .office_md import docx_to_markdown

        return await asyncio.to_thread(docx_to_markdown, file_bytes)

    if ext == ".pptx":
        from .office_md import pptx_to_markdown

        return await asyncio.to_thread(pptx_to_markdown, file_bytes)

    if ext == ".xlsx":
        from .office_md import xlsx_to_markdown

        return await asyncio.to_thread(xlsx_to_markdown, file_bytes)

    if ext == ".csv":
        from .office_md import csv_to_markdown

        return await asyncio.to_thread(csv_to_markdown, file_bytes)

    if ext == ".epub":
        from .epub_md import epub_to_markdown

        return await asyncio.to_thread(epub_to_markdown, file_bytes)

    if ext in _LEGACY_EXTS:
        from .legacy_md import legacy_office_to_markdown

        return await asyncio.to_thread(legacy_office_to_markdown, file_bytes, filename)

    if ext in _IMAGE_EXTS:
        raise UnsupportedFormatError(
            f"Image OCR ('{ext}') is not yet supported on this endpoint. Upload a "
            "text-based document (PDF, DOCX, PPTX, XLSX, CSV, HTML, EPUB, TXT, MD)."
        )

    raise UnsupportedFormatError(
        f"Unsupported file type '{ext}' for anything-to-markdown."
    )
