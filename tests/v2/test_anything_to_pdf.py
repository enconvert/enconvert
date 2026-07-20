"""Unit tests for the anything-to-pdf dispatcher (services/pdf/dispatch.py).

Pure pytest (no pytest-asyncio, per the repo convention) — async dispatch is
driven with ``asyncio.run``. The non-office paths (image, svg, txt, markdown,
html, epub, pdf passthrough) run for real against Pillow / CairoSVG / WeasyPrint,
which are installed in the gateway venv. The office path (unoconvert) needs a
running unoserver on the droplet, so it is exercised with the subprocess mocked.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_anything_to_pdf.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import zipfile

import pytest

from models import PdfHeaderFooter, PdfMargins, PdfOptions
from services.pdf.dispatch import (
    SUPPORTED_EXTENSIONS,
    UnsupportedFormatError,
    UnsupportedOptionError,
    convert_to_pdf,
)


def _convert(data: bytes, name: str, opts: PdfOptions | None = None) -> bytes:
    return asyncio.run(convert_to_pdf(data, name, opts))


def _page_mm(pdf_bytes: bytes, index: int = 0) -> tuple[float, float]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return tuple(round(v / 72 * 25.4, 1) for v in doc[index].get_size())
    finally:
        doc.close()


def _first_text_height_pt(pdf_bytes: bytes) -> float:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        textpage = doc[0].get_textpage()
        textpage.count_rects()  # mandatory before get_rect(), else PdfiumError
        rect = textpage.get_rect(0)
        return round(rect[3] - rect[1], 2)
    finally:
        doc.close()


def _png_bytes(mode: str = "RGB", size=(80, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    fill = (10, 120, 200) if mode == "RGB" else (0, 0, 0, 0)
    Image.new(mode, size, fill).save(buf, format="PNG")
    return buf.getvalue()


def _is_pdf(data: bytes) -> bool:
    return isinstance(data, bytes) and data.startswith(b"%PDF-") and len(data) > 400


# ---- Registry / allowlist -------------------------------------------------

def test_supported_extensions_is_sorted_lowercase_and_covers_families():
    assert list(SUPPORTED_EXTENSIONS) == sorted(SUPPORTED_EXTENSIONS)
    for ext in SUPPORTED_EXTENSIONS:
        assert ext.startswith(".") and ext == ext.lower()
    for ext in (".docx", ".rtf", ".csv", ".html", ".md", ".txt", ".epub",
                ".png", ".jpg", ".heic", ".svg", ".pdf"):
        assert ext in SUPPORTED_EXTENSIONS


# ---- Image path (real Pillow) ---------------------------------------------

def test_png_rgb_to_pdf():
    assert _is_pdf(_convert(_png_bytes("RGB"), "photo.png"))


def test_png_rgba_transparency_flattened_to_pdf():
    # A fully transparent RGBA PNG must not blow up the PDF encoder.
    assert _is_pdf(_convert(_png_bytes("RGBA"), "logo.png"))


def test_jpeg_uppercase_extension_to_pdf():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 60), (200, 50, 50)).save(buf, format="JPEG")
    assert _is_pdf(_convert(buf.getvalue(), "IMG_1234.JPG"))


def test_corrupt_image_raises_value_error():
    with pytest.raises(ValueError):
        _convert(b"not a real png", "broken.png")


def test_heic_to_pdf():
    # HEIC -> PDF is a headline use case (iPhone photos). This also proves the
    # pillow-heif opener is actually registered/available.
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), (30, 60, 90)).save(buf, format="HEIF")
    assert _is_pdf(_convert(buf.getvalue(), "IMG_9001.heic"))


def test_bmp_and_webp_to_pdf():
    from PIL import Image

    for fmt, name in (("BMP", "raster.bmp"), ("WEBP", "raster.webp")):
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (120, 10, 10)).save(buf, format=fmt)
        assert _is_pdf(_convert(buf.getvalue(), name))


def _pdf_page_count(pdf_bytes: bytes) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return len(doc)
    finally:
        doc.close()


def test_animated_gif_uses_first_frame_only():
    # Multi-frame images must collapse to a single-page PDF (documented contract).
    from PIL import Image

    buf = io.BytesIO()
    frame1 = Image.new("P", (24, 24), 0)
    frame2 = Image.new("P", (24, 24), 1)
    frame1.save(buf, format="GIF", save_all=True, append_images=[frame2])
    out = _convert(buf.getvalue(), "anim.gif")
    assert _is_pdf(out)
    assert _pdf_page_count(out) == 1


def test_multipage_tiff_uses_first_frame_only():
    from PIL import Image

    buf = io.BytesIO()
    page1 = Image.new("RGB", (24, 24), (1, 1, 1))
    page2 = Image.new("RGB", (24, 24), (2, 2, 2))
    page1.save(buf, format="TIFF", save_all=True, append_images=[page2])
    out = _convert(buf.getvalue(), "scan.tiff")
    assert _is_pdf(out)
    assert _pdf_page_count(out) == 1


# ---- SVG path (real CairoSVG) ---------------------------------------------

def test_svg_to_pdf():
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        b'<rect width="100" height="50" fill="green"/></svg>'
    )
    assert _is_pdf(_convert(svg, "vector.svg"))


def test_corrupt_svg_raises_value_error():
    with pytest.raises(ValueError):
        _convert(b"<svg><rect width=", "broken.svg")


# ---- Text / Markdown / HTML paths (real WeasyPrint) -----------------------

def test_plain_text_to_pdf():
    assert _is_pdf(_convert(b"Hello world\n\tindented line", "notes.txt"))


def test_text_with_html_metacharacters_is_escaped_not_injected():
    # <script> must be escaped into the text layer, never rendered as markup.
    assert _is_pdf(_convert(b"<script>alert(1)</script>", "xss.txt"))


def test_text_invalid_utf8_does_not_crash():
    assert _is_pdf(_convert(b"\xff\xfe bad bytes here", "weird.txt"))


def test_markdown_to_pdf():
    assert _is_pdf(_convert(b"# Title\n\n- a\n- b\n", "readme.md"))


def test_markdown_invalid_utf8_raises_value_error():
    # Contract divergence pinned intentionally: .txt tolerates bad bytes (see
    # test_text_invalid_utf8_does_not_crash) because it routes through the
    # BOM-aware decoder, but .md/.html go straight to the shared WeasyPrint
    # converters which require UTF-8 and raise ValueError (-> HTTP 400) on
    # malformed input — matching the existing markdown-to-pdf/html-to-pdf
    # endpoints. A clean 400 (not a 500, not silent corruption) is the contract.
    with pytest.raises(ValueError):
        _convert(b"# t\xff\xfe bad", "notes.md")


def test_html_to_pdf():
    assert _is_pdf(_convert(b"<h1>Hi</h1><p>body</p>", "page.html"))


# ---- EPUB path (real stdlib reader -> WeasyPrint) -------------------------

def _minimal_epub() -> bytes:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<manifest><item id="c1" href="c1.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
        "<body><h1>Chapter One</h1><p>Some readable text.</p></body></html>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("content.opf", opf)
        z.writestr("c1.xhtml", chapter)
    return buf.getvalue()


def test_epub_to_pdf():
    assert _is_pdf(_convert(_minimal_epub(), "book.epub"))


def test_malformed_epub_raises_value_error():
    # A valid ZIP with no META-INF/container.xml is a structurally-invalid EPUB.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    with pytest.raises(ValueError):
        _convert(buf.getvalue(), "broken.epub")


# ---- PDF passthrough ------------------------------------------------------

def test_pdf_passthrough_returns_input_unchanged():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (0, 0, 0)).save(buf, format="PDF")
    original = buf.getvalue()
    assert _convert(original, "already.pdf") == original


def test_pdf_passthrough_rejects_non_pdf_bytes():
    with pytest.raises(ValueError):
        _convert(b"PK\x03\x04 this is a zip, not a pdf", "fake.pdf")


# ---- Dispatch guards ------------------------------------------------------

def test_missing_extension_raises_unsupported():
    with pytest.raises(UnsupportedFormatError):
        _convert(b"data", "noextension")


def test_unknown_extension_raises_unsupported():
    with pytest.raises(UnsupportedFormatError):
        _convert(b"data", "archive.zip")


# ---- Office path (unoconvert mocked; no unoserver in test env) ------------

def test_office_path_invokes_unoconvert(monkeypatch):
    import services.documents.converters.convert_to_pdf as office

    calls = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        out_path = cmd[-1]  # unoconvert writes the output file; emulate that.
        with open(out_path, "wb") as f:
            f.write(b"%PDF-1.4\n%mock\n")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    result = _convert(b"fake docx bytes", "report.docx")
    assert result.startswith(b"%PDF-")
    assert calls["cmd"][0] == "unoconvert"
    assert "pdf" in calls["cmd"]


def test_office_path_surfaces_unoconvert_failure(monkeypatch):
    import services.documents.converters.convert_to_pdf as office

    def fake_run(cmd, *args, **kwargs):
        class R:
            returncode = 1
            stderr = "boom"

        return R()

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    with pytest.raises(ValueError):
        _convert(b"fake xlsx", "sheet.xlsx")


# ---- LibreOffice timeout -> 504, not 500 ----------------------------------

def test_office_path_timeout_raises_conversion_timeout_not_value_error(monkeypatch):
    """A 120s unoconvert timeout must map to 504, never 400 (ValueError) or 500."""
    import services.documents.converters.convert_to_pdf as office
    from services.conversion_errors import ConversionTimeoutError

    def fake_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    with pytest.raises(ConversionTimeoutError) as excinfo:
        _convert(b"fake docx bytes", "report.docx")
    # The ladder in api/v1/convert.py maps ValueError -> 400 and bare
    # Exception -> 500. Both are wrong for a timeout; pin the discrimination.
    assert not isinstance(excinfo.value, ValueError)
    assert "timed out" in str(excinfo.value)
    # Regression: the old 500 leaked the argv and temp paths into the response.
    assert "unoconvert" not in str(excinfo.value)
    assert "/tmp" not in str(excinfo.value)


def test_office_path_timeout_still_cleans_up_temp_files(monkeypatch):
    """try/except/finally must not lose the finally-block cleanup."""
    import services.documents.converters.convert_to_pdf as office
    from services.conversion_errors import ConversionTimeoutError

    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen["input"], seen["output"] = cmd[-2], cmd[-1]
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(office.subprocess, "run", fake_run)
    with pytest.raises(ConversionTimeoutError):
        _convert(b"fake docx bytes", "report.docx")

    assert not os.path.exists(seen["input"])
    assert not os.path.exists(seen["output"])


def test_conversion_timeout_error_is_not_caught_by_the_400_or_500_branches():
    """api/v1/convert.py maps ValueError -> 400 and Exception -> 500. The 504
    branch only works while ConversionTimeoutError is neither a ValueError nor
    an HTTPException. Pin it so a reparent can't silently regress the status."""
    from fastapi import HTTPException

    from services.conversion_errors import ConversionTimeoutError

    assert issubclass(ConversionTimeoutError, Exception)
    assert not issubclass(ConversionTimeoutError, ValueError)
    assert not issubclass(ConversionTimeoutError, HTTPException)
    # UnsupportedFormatError must remain a 400.
    assert issubclass(UnsupportedFormatError, ValueError)


# ---- Empty / zero-byte input contract per family --------------------------
# testing.md mandates zero-byte cases. The contract differs by engine and is
# pinned here so a refactor can't silently turn an expected 400 into a 500.

@pytest.mark.parametrize("name", [".png", ".svg", ".epub", ".pdf"], ids=lambda s: s)
def test_empty_binary_input_raises_value_error(name):
    # Image/SVG/EPUB/PDF reject empty input as a 400 (ValueError).
    with pytest.raises(ValueError):
        _convert(b"", f"empty{name}")


@pytest.mark.parametrize("name", ["empty.txt", "empty.md", "empty.html"])
def test_empty_text_input_yields_blank_pdf(name):
    # Text families legitimately emit a (blank) PDF for empty input.
    assert _is_pdf(_convert(b"", name))


def test_empty_office_input_raises_value_error(monkeypatch):
    # An empty office upload -> unoconvert fails -> ValueError (400), never 500.
    import services.documents.converters.convert_to_pdf as office

    def fake_run(cmd, *args, **kwargs):
        class R:
            returncode = 1
            stderr = "Error: source file could not be loaded"

        return R()

    monkeypatch.setattr(office.subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        _convert(b"", "empty.docx")


# ---- PDF passthrough + grayscale (the one genuinely-new behaviour) ---------
# The dispatcher returns a PDF upload unchanged; forward_to_backend then applies
# convert_to_grayscale when pdf_options.grayscale is set. That grayscale leg on
# an arbitrary (non-Pillow) PDF is the new capability this endpoint adds, so it
# is covered here directly (it lives outside the dispatcher, in forward_to_backend).

# ---- pdf_options are honoured, not discarded (the defect) -----------------
# Every test in this section fails against the pre-fix dispatcher, which took
# only (file_bytes, filename) and dropped everything except grayscale.

def test_html_page_size_letter_applied():
    out = _convert(b"<h1>Hi</h1>", "p.html", PdfOptions(page_size="Letter"))
    assert _page_mm(out) == (216.0, 279.0)


def test_markdown_a5_page_applied():
    assert _page_mm(_convert(b"# T", "r.md", PdfOptions(page_size="A5"))) == (148.0, 210.0)


def test_txt_page_size_applied():
    assert _page_mm(_convert(b"hello", "n.txt", PdfOptions(page_size="A5"))) == (148.0, 210.0)


def test_epub_page_size_applied():
    out = _convert(_minimal_epub(), "b.epub", PdfOptions(page_size="A5"))
    assert _page_mm(out) == (148.0, 210.0)


def test_png_page_size_applied():
    out = _convert(_png_bytes("RGB"), "p.png", PdfOptions(page_size="A4"))
    assert _page_mm(out) == (210.0, 297.0)
    assert _pdf_page_count(out) == 1


def test_svg_page_size_applied():
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        b'<rect width="100" height="50" fill="green"/></svg>'
    )
    out = _convert(svg, "v.svg", PdfOptions(page_size="A4"))
    assert _page_mm(out) == (210.0, 297.0)


def test_landscape_orientation_swaps_dimensions():
    out = _convert(b"<h1>Hi</h1>", "p.html",
                   PdfOptions(page_size="A4", orientation="landscape"))
    assert _page_mm(out) == (297.0, 210.0)


def test_custom_page_dimensions_applied():
    out = _convert(b"<h1>Hi</h1>", "p.html", PdfOptions(page_width=100, page_height=200))
    assert _page_mm(out) == (100.0, 200.0)


@pytest.mark.parametrize(
    "name,payload",
    [("p.png", None), ("p.html", b"<h1>Hi</h1>")],
    ids=["image-oversized-aspect", "html"],
)
def test_geometry_path_stays_single_page(name, payload):
    # Percentage max-height in paged media is subtle; pin that an image far
    # wider/taller than the page still collapses to exactly one page.
    data = payload if payload is not None else _png_bytes("RGB", size=(2000, 30))
    out = _convert(data, name, PdfOptions(page_size="A4"))
    assert _pdf_page_count(out) == 1


def test_png_landscape_geometry_single_page():
    out = _convert(_png_bytes("RGB", size=(40, 2000)), "tall.png",
                   PdfOptions(page_size="A4", orientation="landscape"))
    assert _page_mm(out) == (297.0, 210.0)
    assert _pdf_page_count(out) == 1


def test_txt_custom_margins_override_hardcoded_2cm():
    # Pins the interaction with _text_to_pdf's built-in @page{margin:2cm}: an
    # explicit zero margin must win, putting the text measurably higher.
    baseline = _convert(b"hello", "n.txt", PdfOptions(page_size="A4"))
    zeroed = _convert(b"hello", "n.txt",
                      PdfOptions(page_size="A4",
                                 margins=PdfMargins(top=0, bottom=0, left=0, right=0)))

    import pypdfium2 as pdfium

    def _text_top(pdf_bytes: bytes) -> float:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            tp = doc[0].get_textpage()
            tp.count_rects()
            return tp.get_rect(0)[3]  # top edge, PDF origin is bottom-left
        finally:
            doc.close()

    assert _text_top(zeroed) > _text_top(baseline)


# ---- scale: page stays fixed, content scales (Playwright semantics) --------

def test_scale_keeps_page_size_and_scales_content():
    body = b"<p>Hello world scaling test</p>"
    base = _convert(body, "p.html", PdfOptions(page_size="A4"))
    big = _convert(body, "p.html", PdfOptions(page_size="A4", scale=2.0))
    # A naive write_pdf(zoom=2) would make this A2 (420x594) — the guard
    # against that exact wrong implementation.
    assert _page_mm(big) == (210.0, 297.0)
    assert _first_text_height_pt(big) == pytest.approx(
        _first_text_height_pt(base) * 2, rel=0.02
    )


def test_scale_half_shrinks_content():
    body = b"<p>Hello world scaling test</p>"
    base = _convert(body, "p.html", PdfOptions(page_size="A4"))
    small = _convert(body, "p.html", PdfOptions(page_size="A4", scale=0.5))
    assert _page_mm(small) == (210.0, 297.0)
    assert _first_text_height_pt(small) == pytest.approx(
        _first_text_height_pt(base) * 0.5, rel=0.02
    )


# ---- The rejection gate ---------------------------------------------------

def test_defaults_only_pdf_options_do_not_trigger_rejection_on_office(monkeypatch):
    # THE critical gate test. PdfOptions defaults page_size to "A4", so a
    # value-based check would 400 this. Detection MUST key off
    # model_fields_set. Do not delete: guards the whole design.
    import services.documents.converters.convert_to_pdf as office

    calls = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        with open(cmd[-1], "wb") as f:
            f.write(b"%PDF-1.4\n%mock\n")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    # Constructed exactly as the route does it (convert.py: PdfOptions(**json)).
    opts = PdfOptions(**json.loads('{"grayscale": true}'))
    assert opts.page_size == "A4"  # the default that must NOT be treated as set
    out = _convert(b"fake docx", "r.docx", opts)
    assert out.startswith(b"%PDF-")
    assert calls["cmd"][0] == "unoconvert"  # conversion really ran


def test_office_explicit_page_size_raises_unsupported_option(monkeypatch):
    import services.documents.converters.convert_to_pdf as office

    calls = {}

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        raise AssertionError("unoconvert must not be invoked for a rejected option")

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    with pytest.raises(UnsupportedOptionError) as excinfo:
        _convert(b"fake docx", "r.docx", PdfOptions(**{"page_size": "A4"}))
    assert "page_size" in str(excinfo.value)
    assert calls == {}  # rejected before the subprocess


def test_pdf_passthrough_explicit_geometry_raises_unsupported_option():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (0, 0, 0)).save(buf, format="PDF")
    with pytest.raises(UnsupportedOptionError):
        _convert(buf.getvalue(), "a.pdf", PdfOptions(**{"orientation": "landscape"}))


def test_pdf_passthrough_grayscale_only_still_passes_through():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (0, 0, 0)).save(buf, format="PDF")
    original = buf.getvalue()
    assert _convert(original, "a.pdf", PdfOptions(**{"grayscale": True})) == original


def test_unsupported_option_error_is_a_value_error():
    # Pins the 400 mapping in forward_to_backend's except-ladder.
    assert issubclass(UnsupportedOptionError, ValueError)


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_size", "A4"),
        ("page_width", 100),
        ("page_height", 200),
        ("orientation", "landscape"),
        ("margins", {"top": 5}),
        ("scale", 1.5),
        ("header", {"content": "hi"}),
        ("footer", {"content": "bye"}),
    ],
)
@pytest.mark.parametrize("name", ["r.docx", "a.pdf"])
def test_every_geometry_field_is_rejected_for_engines_that_cannot_honor_it(
    field, value, name, monkeypatch
):
    # Guards against a field being added to PdfOptions later and silently
    # escaping _GEOMETRY_FIELDS (i.e. being validated then discarded again).
    import services.documents.converters.convert_to_pdf as office

    def fake_run(cmd, *args, **kwargs):
        raise AssertionError("must not reach unoconvert")

    monkeypatch.setattr(office.subprocess, "run", fake_run)

    with pytest.raises(UnsupportedOptionError):
        _convert(b"%PDF-1.4\nfake", name, PdfOptions(**{field: value}))


# ---- No-regression: no geometry => the original fast path, byte-identical --

def test_image_without_options_is_byte_identical_to_pillow_path():
    from PIL import Image

    png = _png_bytes("RGB")
    assert _convert(png, "p.png") == _convert(png, "p.png", None)
    # Grayscale-only must also keep the untouched Pillow path.
    assert _convert(png, "p.png", PdfOptions(**{"grayscale": True})) == _convert(png, "p.png")

    expected = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(expected, format="PDF", resolution=100.0)
    assert _convert(png, "p.png") == expected.getvalue()


def test_svg_without_options_still_uses_cairosvg():
    import cairosvg

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        b'<rect width="100" height="50" fill="green"/></svg>'
    )
    assert _convert(svg, "v.svg") == cairosvg.svg2pdf(bytestring=svg)


def test_txt_without_geometry_keeps_its_default_2cm_margin():
    # The built-in @page{margin:2cm} is only dropped when the caller supplies
    # geometry; a grayscale-only request must render exactly as before.
    assert _convert(b"hello", "n.txt") == _convert(
        b"hello", "n.txt", PdfOptions(**{"grayscale": True})
    )


# ---- Security / robustness ------------------------------------------------

def test_header_with_double_quote_does_not_break_css():
    # Reproduces the pdf_helpers CSS-injection hole: unescaped header text
    # closed the content string and injected its own @page rule.
    evil = 'He said "hi"; } @page { size: 9mm 9mm } /*'
    out = _convert(
        b"<h1>Hi</h1>", "p.html",
        PdfOptions(page_size="A4", header=PdfHeaderFooter(content=evil)),
    )
    assert _page_mm(out) == (210.0, 297.0)  # the injected 9mm page never applied


def test_svg_with_external_ref_does_not_fetch(monkeypatch):
    # CairoSVG renders with unsafe=False; the WeasyPrint geometry path must keep
    # that posture rather than resolve an attacker-supplied URL (SSRF).
    import utils.pdf_helpers as helpers

    fetched = []
    real_render = helpers.render_media_pdf

    def spy_render(data_uri, pdf_options):
        from weasyprint import HTML

        original = HTML.__init__

        def tracking_init(self, *args, **kwargs):
            fetcher = kwargs.get("url_fetcher")
            if fetcher is not None:
                def wrapped(url, *a, **k):
                    fetched.append(url)
                    return fetcher(url, *a, **k)

                kwargs["url_fetcher"] = wrapped
            return original(self, *args, **kwargs)

        monkeypatch.setattr(HTML, "__init__", tracking_init)
        return real_render(data_uri, pdf_options)

    monkeypatch.setattr(helpers, "render_media_pdf", spy_render)

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="50">'
        b'<image xlink:href="http://169.254.169.254/latest/meta-data/" '
        b'width="100" height="50"/></svg>'
    )
    out = _convert(svg, "v.svg", PdfOptions(page_size="A4"))
    assert _is_pdf(out)  # blocked resource is dropped, render still completes
    assert not [u for u in fetched if u.startswith("http")]


@pytest.mark.skipif(shutil.which("gs") is None, reason="Ghostscript not installed")
def test_pdf_passthrough_then_grayscale_produces_valid_pdf():
    from PIL import Image

    from utils.pdf_postprocess import convert_to_grayscale

    buf = io.BytesIO()
    Image.new("RGB", (60, 60), (200, 40, 40)).save(buf, format="PDF")
    passthrough = _convert(buf.getvalue(), "colour.pdf")  # dispatcher: unchanged
    gray = asyncio.run(convert_to_grayscale(passthrough))  # forward_to_backend leg
    assert _is_pdf(gray)
