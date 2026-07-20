"""Unit tests for utils/pdf_helpers.py (the WeasyPrint PdfOptions translator).

Pure pytest, no engine calls — these pin the CSS the helper emits, which is the
contract the html-to-pdf / markdown-to-pdf / anything-to-pdf paths depend on.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_pdf_helpers.py -q
"""

from __future__ import annotations

from models import PdfHeaderFooter, PdfOptions
from utils.pdf_helpers import (
    _css_string_escape,
    build_weasyprint_page_css,
    weasyprint_zoom,
)


def test_build_weasyprint_page_css_divides_geometry_by_scale():
    # A4 (210x297) at scale 2 must emit half-size geometry; the companion
    # write_pdf(zoom=2) multiplies it back so the page stays A4.
    css = build_weasyprint_page_css(PdfOptions(page_size="A4", scale=2.0))
    assert "size: 105.0mm 148.5mm" in css


def test_build_weasyprint_page_css_scale_one_is_a_no_op():
    css = build_weasyprint_page_css(PdfOptions(page_size="A4"))
    assert "size: 210.0mm 297.0mm" in css


def test_build_weasyprint_page_css_divides_margins_by_scale():
    css = build_weasyprint_page_css(PdfOptions(page_size="A4", scale=2.0))
    # Default margins are 10mm each.
    assert "margin: 5.0mm 5.0mm 5.0mm 5.0mm" in css


def test_weasyprint_zoom_defaults_to_one():
    assert weasyprint_zoom(None) == 1.0
    assert weasyprint_zoom(PdfOptions()) == 1.0


def test_weasyprint_zoom_returns_scale():
    assert weasyprint_zoom(PdfOptions(scale=1.5)) == 1.5


def test_css_string_escape_neutralizes_quotes_and_backslashes():
    assert _css_string_escape('a"b') == 'a\\"b'
    assert _css_string_escape("a\\b") == "a\\\\b"
    # Backslash is escaped first, so an escaped quote does not get double-processed.
    assert _css_string_escape('a\\"b') == 'a\\\\\\"b'


def test_header_injection_is_escaped_in_the_emitted_css():
    evil = 'He said "hi"; } @page { size: 9mm 9mm } /*'
    css = build_weasyprint_page_css(
        PdfOptions(page_size="A4", header=PdfHeaderFooter(content=evil))
    )
    # Every quote from the caller's text is escaped, so the whole payload —
    # including the @page it contains — stays inert inside the content string
    # instead of closing it and becoming live CSS.
    assert 'content: "He said \\"hi\\"; } @page { size: 9mm 9mm } /*"' in css
    # No unescaped quote from the caller survives.
    assert '"hi"' not in css


def test_footer_injection_is_escaped_in_the_emitted_css():
    css = build_weasyprint_page_css(
        PdfOptions(page_size="A4", footer=PdfHeaderFooter(content='x" } @page { size: 9mm'))
    )
    assert 'content: "x\\" } @page { size: 9mm"' in css
    assert 'content: "x" ' not in css


def test_page_counter_placeholders_still_break_out_of_the_string():
    # Escaping must NOT neutralise the deliberate counter() breakout, or
    # {{page}} would render as literal text.
    css = build_weasyprint_page_css(
        PdfOptions(page_size="A4", header=PdfHeaderFooter(content="Page {{page}} of {{total_pages}}"))
    )
    assert 'counter(page)' in css
    assert 'counter(pages)' in css
