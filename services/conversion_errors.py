"""Shared converter exception types.

Lives at the ``services`` package root — not inside ``documents`` or
``markdown`` — because the unoserver/LibreOffice timeout is raised from BOTH
``services/documents/converters/convert_to_pdf.py`` and
``services/markdown/legacy_md.py``, and is caught in ``api/v1/convert.py``.

``UnsupportedOptionError`` is here for the same reason: it is raised from the
anything-to-pdf dispatcher AND from the LibreOffice-backed document endpoints
(via ``utils/pdf_helpers.assert_geometry_supported``), and caught in
``api/v1/convert.py``. Keeping it out of ``services/pdf`` stops the document
family from having to import the anything-to-pdf package to name its own error.
"""

from __future__ import annotations


class UnsupportedOptionError(ValueError):
    """Raised for a pdf_option the engine for this input cannot honor.

    Subclasses ``ValueError`` so the gateway's converter error handling maps it
    to an HTTP 400 — the caller asked for something this format cannot express,
    which is a client error, not a server fault.
    """


class ConversionTimeoutError(Exception):
    """A conversion subprocess (unoserver/LibreOffice) exceeded its time budget.

    Deliberately NOT a ``ValueError``: ``forward_to_backend`` maps ``ValueError``
    to HTTP 400 (bad input), and a timeout is not the caller's fault. It maps to
    HTTP 504 instead — ``unoconvert`` is a thin client driving an upstream
    unoserver daemon, so an unanswered conversion is an upstream timeout
    (RFC 9110 §15.6.5), not a gateway bug.
    """
