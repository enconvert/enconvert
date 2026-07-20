"""Anything-to-PDF service package.

Public entry point is the async ``convert_to_pdf(file_bytes, filename)``
dispatcher, wired to the ``/v1/convert/anything-to-pdf`` endpoint. Mirrors the
sibling ``services/markdown`` package (anything-to-markdown).
"""

from .dispatch import (
    SUPPORTED_EXTENSIONS,
    UnsupportedFormatError,
    UnsupportedOptionError,
    assert_options_supported,
    convert_to_pdf,
)

__all__ = [
    "convert_to_pdf",
    "SUPPORTED_EXTENSIONS",
    "UnsupportedFormatError",
    "UnsupportedOptionError",
    "assert_options_supported",
]
