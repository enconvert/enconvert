"""Anything-to-Markdown: convert an uploaded file (PDF, DOCX, PPTX, XLSX, CSV,
HTML, EPUB, plain text, and legacy/ODF office via unoserver) into clean Markdown.

The public entry point is ``convert_to_markdown(file_bytes, filename)`` in
``dispatch`` — a thin async facade reused by the ``/v1/convert/anything-to-markdown``
endpoint and (later) the ``/v2/ingest`` file pipeline. The shared HTML->Markdown
core lives in ``html_md`` and is also used by the web ``url_markdown`` path.
"""

from .dispatch import (
    SUPPORTED_EXTENSIONS,
    UnsupportedFormatError,
    convert_to_markdown,
)

__all__ = [
    "convert_to_markdown",
    "SUPPORTED_EXTENSIONS",
    "UnsupportedFormatError",
]
