"""Legacy binary + ODF office -> Markdown via unoserver (LibreOffice headless).

.doc/.ppt/.xls (legacy OLE2) and .odt/.ods/.odp (OpenDocument) have no lightweight
pure-Python reader worth maintaining, so they reuse the SAME unoserver the
existing ``*-to-pdf`` document endpoints already rely on — converting to HTML
(instead of PDF) and then through the shared faithful HTML->Markdown pipeline.

This mirrors ``services/documents/converters/convert_to_pdf.py`` exactly (temp
file in, ``unoconvert`` subprocess, temp file out), so it inherits the same
operational assumptions: a running unoserver on the droplet.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from services.conversion_errors import ConversionTimeoutError

from .common import decode_text_bytes
from .html_md import html_to_markdown

logger = logging.getLogger(__name__)

_UNOCONVERT_TIMEOUT_S = 120


def legacy_office_to_markdown(file_bytes: bytes, original_filename: str) -> str:
    """Convert a legacy/ODF office file to Markdown via unoserver -> HTML."""
    ext = os.path.splitext(original_filename or "")[1].lower()
    if not ext:
        raise ValueError("Cannot determine file type: missing file extension.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(file_bytes)
        input_path = temp_file.name
    output_path = os.path.splitext(input_path)[0] + ".html"

    try:
        result = subprocess.run(
            ["unoconvert", "--convert-to", "html", input_path, output_path],
            capture_output=True,
            text=True,
            timeout=_UNOCONVERT_TIMEOUT_S,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "unoserver conversion failed"
            raise ValueError(f"Document conversion failed: {detail}")

        with open(output_path, "rb") as handle:
            html = decode_text_bytes(handle.read())

        markdown = html_to_markdown(html, extract_article=False)
        if not markdown.strip():
            raise ValueError("The document contains no extractable text.")
        return markdown
    except subprocess.TimeoutExpired as exc:
        # Was laundered into ValueError -> HTTP 400, telling the caller their
        # perfectly valid document was malformed and stranding a request that a
        # retry could satisfy. It is an upstream timeout -> 504.
        raise ConversionTimeoutError(
            f"Document conversion timed out after {_UNOCONVERT_TIMEOUT_S} seconds."
        ) from exc
    finally:
        for path in (input_path, output_path):
            if os.path.exists(path):
                os.remove(path)
