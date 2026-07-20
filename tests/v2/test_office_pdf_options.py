"""Tests for pdf_options on the nine LibreOffice-backed document endpoints.

doc/excel/ppt/odt/ods/odp/ots/pages/numbers -to-pdf used to accept and validate
``pdf_options``, then silently discard everything except ``grayscale``: their
CONVERTER_MAP entries never opted into ``accepts_pdf_options``, so
forward_to_backend never passed the parsed options to the converter. These
endpoints route to ``unoconvert``, which exposes only PDF export-filter options
(--output-filter / --filter-option); page size, orientation and margins are
properties of the SOURCE document's page style, applied at layout time before
export, so no filter option can control them. Geometry is therefore rejected
with a 400 rather than accepted and dropped.

The route handlers are plain async functions, so they are driven directly with
``forward_to_backend`` monkeypatched — that exercises the real gate and the real
per-endpoint wiring without a DB, storage or auth.

Pure pytest (no pytest-asyncio, per the repo convention) — async handlers are
driven with ``asyncio.run``.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_office_pdf_options.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil

# api.v1.convert transitively imports utils.postgres, which builds the engine at
# import time from DATABASE_URL. These tests never touch the DB, so a dummy URL
# keeps the import working (create_engine is lazy and never connects). Same
# pattern as tests/v2/test_endpoint_allowlist.py.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_dummy")

import pytest
from fastapi import HTTPException, UploadFile

import api.v1.convert as convert
from models import PdfOptions
from services.conversion_errors import UnsupportedOptionError

# endpoint -> (handler, a filename its allowlist accepts)
OFFICE_ENDPOINTS: dict[str, tuple[str, str]] = {
    "doc-to-pdf": ("doc_to_pdf", "report.docx"),
    "excel-to-pdf": ("excel_to_pdf", "book.xlsx"),
    "ppt-to-pdf": ("ppt_to_pdf", "deck.pptx"),
    "odt-to-pdf": ("odt_to_pdf", "text.odt"),
    "ods-to-pdf": ("ods_to_pdf", "sheet.ods"),
    "odp-to-pdf": ("odp_to_pdf", "slides.odp"),
    "ots-to-pdf": ("ots_to_pdf", "template.ots"),
    "pages-to-pdf": ("pages_to_pdf", "letter.pages"),
    "numbers-to-pdf": ("numbers_to_pdf", "budget.numbers"),
}

# Every field PdfOptions exposes that describes page geometry.
GEOMETRY_CASES: list[tuple[str, object]] = [
    ("page_size", "A4"),
    ("page_width", 100),
    ("page_height", 200),
    ("orientation", "landscape"),
    ("margins", {"top": 5}),
    ("scale", 1.5),
    ("header", {"content": "hi"}),
    ("footer", {"content": "bye"}),
]


def _upload(filename: str, data: bytes = b"fake office bytes") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


def _call(
    monkeypatch, endpoint: str, filename: str, pdf_options: str | None
) -> dict:
    """Drive a route handler, recording the forward_to_backend call it makes."""
    handler_name, _ = OFFICE_ENDPOINTS[endpoint]
    handler = getattr(convert, handler_name)
    seen: dict = {}

    async def fake_forward(request, ep, user, content, original_filename,
                           output_filename, direct_download, job_id,
                           pdf_options=None):
        seen["endpoint"] = ep
        seen["filename"] = original_filename
        seen["pdf_options"] = pdf_options
        return {"ok": True}

    # The ceiling check needs a real Request/plan; irrelevant to this gate.
    monkeypatch.setattr(convert, "validate_file_size", lambda *a, **k: None)
    monkeypatch.setattr(convert, "forward_to_backend", fake_forward)

    result = asyncio.run(handler(
        request=None,
        file=_upload(filename),
        output_filename=None,
        direct_download=True,
        job_id=None,
        pdf_options=pdf_options,
        user={"id": "prj_test", "tier": "free"},
    ))
    seen["result"] = result
    return seen


# ---- The defect: explicit geometry must 400, on every one of the nine -------

@pytest.mark.parametrize("endpoint", sorted(OFFICE_ENDPOINTS))
@pytest.mark.parametrize("field,value", GEOMETRY_CASES, ids=[f for f, _ in GEOMETRY_CASES])
def test_explicit_geometry_is_rejected_with_400(monkeypatch, endpoint, field, value):
    _, filename = OFFICE_ENDPOINTS[endpoint]
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, endpoint, filename, json.dumps({field: value}))
    assert excinfo.value.status_code == 400
    assert field in excinfo.value.detail


@pytest.mark.parametrize("endpoint", sorted(OFFICE_ENDPOINTS))
def test_rejection_happens_before_any_conversion(monkeypatch, endpoint):
    """The 400 must fire in the route: forward_to_backend burns quota and logs a
    Failed activity row, neither of which a pure client error should cause."""
    _, filename = OFFICE_ENDPOINTS[endpoint]
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("forward_to_backend must not run for a rejected option")

    monkeypatch.setattr(convert, "validate_file_size", lambda *a, **k: None)
    monkeypatch.setattr(convert, "forward_to_backend", boom)

    handler = getattr(convert, OFFICE_ENDPOINTS[endpoint][0])
    with pytest.raises(HTTPException):
        asyncio.run(handler(
            request=None, file=_upload(filename), output_filename=None,
            direct_download=True, job_id=None,
            pdf_options=json.dumps({"page_size": "Letter"}),
            user={"id": "prj_test", "tier": "free"},
        ))
    assert called == []


def test_multiple_geometry_fields_are_all_named_in_the_error(monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, "doc-to-pdf", "report.docx",
              json.dumps({"page_size": "A3", "orientation": "landscape"}))
    detail = excinfo.value.detail
    assert "orientation" in detail and "page_size" in detail


# ---- THE mandatory gate test: defaults must NOT trigger rejection ----------

@pytest.mark.parametrize("endpoint", sorted(OFFICE_ENDPOINTS))
def test_grayscale_only_is_accepted_and_forwarded(monkeypatch, endpoint):
    """PdfOptions defaults page_size="A4" and scale=1.0, so a VALUE-based check
    would 400 a plain {"grayscale": true}. Detection MUST key off
    model_fields_set. Do not delete: guards the whole design."""
    _, filename = OFFICE_ENDPOINTS[endpoint]
    seen = _call(monkeypatch, endpoint, filename, json.dumps({"grayscale": True}))

    assert seen["endpoint"] == endpoint  # no 400: conversion really proceeded
    assert seen["pdf_options"].grayscale is True
    # The defaults that must not be mistaken for caller intent.
    assert seen["pdf_options"].page_size == "A4"
    assert seen["pdf_options"].scale == 1.0
    assert "page_size" not in seen["pdf_options"].model_fields_set


@pytest.mark.parametrize("endpoint", sorted(OFFICE_ENDPOINTS))
def test_no_pdf_options_still_converts(monkeypatch, endpoint):
    _, filename = OFFICE_ENDPOINTS[endpoint]
    seen = _call(monkeypatch, endpoint, filename, None)
    assert seen["endpoint"] == endpoint
    assert seen["pdf_options"] is None


def test_grayscale_false_is_not_geometry_and_is_accepted(monkeypatch):
    seen = _call(monkeypatch, "doc-to-pdf", "report.docx",
                 json.dumps({"grayscale": False}))
    assert seen["pdf_options"].grayscale is False


def test_malformed_pdf_options_json_still_400s(monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, "doc-to-pdf", "report.docx", "{not json")
    assert excinfo.value.status_code == 400
    assert "Invalid pdf_options" in excinfo.value.detail


def test_invalid_page_size_value_still_400s_via_pydantic(monkeypatch):
    # Pydantic validation must keep running ahead of the geometry gate.
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, "doc-to-pdf", "report.docx",
              json.dumps({"page_size": "A99"}))
    assert excinfo.value.status_code == 400
    assert "Invalid pdf_options" in excinfo.value.detail


# ---- grayscale still applies (the post-process was never broken) ----------

def test_office_endpoints_are_pdf_endpoints_so_grayscale_postprocess_fires():
    """forward_to_backend applies grayscale under `is_pdf_endpoint and
    pdf_options and pdf_options.grayscale`, where is_pdf_endpoint is
    output_ext == ".pdf". Pin the two conditions the nine rely on."""
    for endpoint in OFFICE_ENDPOINTS:
        info = convert.CONVERTER_MAP[endpoint]
        assert info["output_ext"] == ".pdf"
        # They must NOT opt in: unoconvert takes no pdf_options, and the gate
        # has already rejected anything it could not honor.
        assert "accepts_pdf_options" not in info


@pytest.mark.skipif(shutil.which("gs") is None, reason="Ghostscript not installed")
def test_grayscale_postprocess_on_office_output_produces_valid_pdf(monkeypatch):
    """End-to-end on the leg that matters: unoconvert output -> convert_to_grayscale."""
    import services.documents.converters.convert_to_pdf as office
    from PIL import Image

    from utils.pdf_postprocess import convert_to_grayscale

    buf = io.BytesIO()
    Image.new("RGB", (60, 60), (200, 40, 40)).save(buf, format="PDF")
    colour_pdf = buf.getvalue()

    def fake_run(cmd, *args, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(colour_pdf)

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(office.subprocess, "run", fake_run)
    converted = office.doc_to_pdf(b"fake docx", "report.docx")
    gray = asyncio.run(convert_to_grayscale(converted))
    assert gray.startswith(b"%PDF-") and len(gray) > 400


# ---- Error-message coherence with anything-to-pdf --------------------------

def test_message_is_identical_to_anything_to_pdf_for_the_same_input(monkeypatch):
    """The gate is shared, so the 400 body must not diverge between
    /anything-to-pdf and /doc-to-pdf for the same .docx + option."""
    from services.pdf import assert_options_supported

    opts = PdfOptions(**{"page_size": "A4"})
    with pytest.raises(UnsupportedOptionError) as dispatch_err:
        assert_options_supported("report.docx", opts)

    with pytest.raises(HTTPException) as route_err:
        _call(monkeypatch, "doc-to-pdf", "report.docx",
              json.dumps({"page_size": "A4"}))

    assert route_err.value.detail == str(dispatch_err.value)


def test_message_names_libreoffice_and_grayscale(monkeypatch):
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, "excel-to-pdf", "book.xlsx",
              json.dumps({"margins": {"top": 5}}))
    detail = excinfo.value.detail
    assert "LibreOffice" in detail
    assert "grayscale" in detail
    assert "source document" in detail


# ---- Format errors keep precedence over option errors ---------------------

def test_extension_the_endpoint_rejects_defers_to_validate_file_format(monkeypatch):
    """A .html upload to doc-to-pdf is validate_file_format's 400 to raise (it
    runs inside forward_to_backend), not the option gate's — same precedence as
    services/pdf's gate, which stays silent on extensions it does not own."""
    seen = _call(monkeypatch, "doc-to-pdf", "page.html",
                 json.dumps({"page_size": "A4"}))
    # No option 400: the request reached forward_to_backend, which owns the
    # format rejection.
    assert seen["endpoint"] == "doc-to-pdf"


# ---- The geometry-capable endpoints must NOT have gained a gate ------------

@pytest.mark.parametrize(
    "handler_name,endpoint,filename",
    [
        ("html_to_pdf", "html-to-pdf", "page.html"),
        ("markdown_to_pdf", "markdown-to-pdf", "readme.md"),
    ],
)
def test_weasyprint_endpoints_still_honor_geometry(
    monkeypatch, handler_name, endpoint, filename
):
    """html-to-pdf / markdown-to-pdf render through WeasyPrint, which CAN honor
    page geometry. They must keep accepting it — no 400, options forwarded."""
    handler = getattr(convert, handler_name)
    seen: dict = {}

    async def fake_forward(request, ep, user, content, original_filename,
                           output_filename, direct_download, job_id,
                           pdf_options=None):
        seen["endpoint"] = ep
        seen["pdf_options"] = pdf_options
        return {"ok": True}

    monkeypatch.setattr(convert, "validate_file_size", lambda *a, **k: None)
    monkeypatch.setattr(convert, "forward_to_backend", fake_forward)

    asyncio.run(handler(
        request=None, file=_upload(filename, b"<h1>hi</h1>"),
        output_filename=None, direct_download=True, job_id=None,
        pdf_options=json.dumps({"page_size": "A5", "orientation": "landscape"}),
        user={"id": "prj_test", "tier": "free"},
    ))

    assert seen["endpoint"] == endpoint
    assert seen["pdf_options"].page_size == "A5"
    assert seen["pdf_options"].orientation == "landscape"
    # And they still opt in, so the converter actually receives them.
    assert convert.CONVERTER_MAP[endpoint]["accepts_pdf_options"] is True


# ---- The shared helper itself ---------------------------------------------

def test_assert_geometry_supported_is_silent_without_explicit_geometry():
    from utils.pdf_helpers import assert_geometry_supported

    assert_geometry_supported(None, fmt=".docx", engine="LibreOffice")
    assert_geometry_supported(
        PdfOptions(**{"grayscale": True}), fmt=".docx", engine="LibreOffice"
    )


def test_geometry_fields_covers_every_non_grayscale_pdf_option():
    """Guards against a field being added to PdfOptions later and silently
    escaping the gate (i.e. being validated then discarded again)."""
    from utils.pdf_helpers import GEOMETRY_FIELDS

    assert set(PdfOptions.model_fields) - GEOMETRY_FIELDS == {"grayscale"}


def test_unsupported_option_error_is_a_value_error():
    # Pins the 400 mapping in forward_to_backend's except-ladder.
    assert issubclass(UnsupportedOptionError, ValueError)


def test_unsupported_option_error_is_the_same_class_everywhere():
    """It moved to services/conversion_errors; the old import paths must keep
    resolving to the SAME class or the except-ladder silently stops catching."""
    from services.pdf import UnsupportedOptionError as from_pkg
    from services.pdf.dispatch import UnsupportedOptionError as from_dispatch

    assert from_pkg is UnsupportedOptionError
    assert from_dispatch is UnsupportedOptionError
