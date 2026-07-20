"""
Helpers for translating PdfOptions into engine-specific formats.

The Playwright translator lives in services/page_quality/pdf_translate.py
(Sprint F0.3); this module keeps the WeasyPrint side for the document
converters.

``build_weasyprint_page_css`` and ``weasyprint_zoom`` MUST be used together.
WeasyPrint's ``write_pdf(zoom=)`` scales the page box as well as the content, so
zoom alone silently turns an A4 request at scale=2 into A2. The pair works by
pre-dividing the @page geometry by ``scale`` and letting zoom multiply it back:
the page keeps its requested size while only the content scales, which is the
semantics Playwright's ``scale`` already has on the url-to-pdf path.

``assert_geometry_supported`` is the counterpart for engines that CANNOT honor
geometry at all (LibreOffice, PDF passthrough). It lives here — beside the
translator it mirrors — so the one rejection rule and the one error message are
shared by every caller: the anything-to-pdf dispatcher (which resolves an engine
per file extension) and the LibreOffice-backed document endpoints (whose engine
is fixed by the route). Two copies would drift into two different 400 bodies.
"""
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from services.conversion_errors import UnsupportedOptionError

if TYPE_CHECKING:
    from models import PdfOptions


# --- Geometry capability gate ---

# Fields describing page geometry, as opposed to `grayscale` — a post-process
# applied to the finished PDF by the caller, so every engine supports it.
GEOMETRY_FIELDS = frozenset(
    {"page_size", "page_width", "page_height", "orientation", "margins",
     "scale", "header", "footer"}
)


def explicit_geometry_fields(pdf_options: "Optional[PdfOptions]") -> list[str]:
    """Geometry options the caller EXPLICITLY set, sorted.

    Keys off ``model_fields_set``, never values: PdfOptions defaults page_size
    to "A4" and scale to 1.0, so a value-based check would reject a plain
    ``{"grayscale": true}`` request.
    """
    if pdf_options is None:
        return []
    return sorted(GEOMETRY_FIELDS & pdf_options.model_fields_set)


def assert_geometry_supported(
    pdf_options: "Optional[PdfOptions]", *, fmt: str, engine: str
) -> None:
    """Reject explicitly-set geometry for an ``engine`` that cannot honor it.

    ``fmt`` is the input extension (e.g. ".docx") and ``engine`` the renderer
    naming it in the message. Raises ``UnsupportedOptionError`` (-> HTTP 400);
    silent when the caller set no geometry, so defaults never trigger it.
    """
    explicit = explicit_geometry_fields(pdf_options)
    if not explicit:
        return
    raise UnsupportedOptionError(
        f"pdf_options {explicit} cannot be applied to '{fmt}' input: page "
        f"geometry for this format is determined by the source document "
        f"({engine}), not by the converter. Remove these options (only "
        f"'grayscale' is supported for '{fmt}'), or set the page layout in "
        f"the source file before uploading."
    )


# --- Template variable translation ---

# WeasyPrint uses CSS counters (only page/total_pages; others are literal)
_WEASYPRINT_COUNTER_VARS = {
    "{{page}}": "\" counter(page) \"",
    "{{total_pages}}": "\" counter(pages) \"",
}


def translate_template_weasyprint(template: str, title: str = "", url: str = "") -> str:
    """
    Replace {{var}} placeholders for WeasyPrint CSS content strings.

    For {{page}} and {{total_pages}}, inserts CSS counter() functions.
    For {{date}}, {{title}}, {{url}}, inserts literal strings.
    """
    now = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # First replace literal vars (these become part of the CSS string)
    result = template.replace("{{date}}", now)
    result = result.replace("{{title}}", title)
    result = result.replace("{{url}}", url)

    # For CSS counters we need special handling — they break out of the string
    for placeholder, counter_expr in _WEASYPRINT_COUNTER_VARS.items():
        result = result.replace(placeholder, counter_expr)

    return result


def _css_string_escape(text: str) -> str:
    """Escape a string for use inside a CSS ``content: "..."`` literal.

    Without this, a header/footer containing a double quote closes the string
    early and everything after it is parsed as CSS — letting caller-supplied
    text inject arbitrary rules (e.g. its own @page block) into the document.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_weasyprint_page_css(pdf_options, title: str = "", url: str = "") -> str:
    """
    Build a <style> block with @page rules from PdfOptions for WeasyPrint.

    Returns a complete <style>...</style> string to prepend/inject into HTML.

    Geometry is divided by ``pdf_options.scale`` and must be paired with
    ``write_pdf(zoom=weasyprint_zoom(pdf_options))`` — see the module docstring.
    ``scale`` defaults to 1.0, so this is a no-op unless a caller sets it.
    """
    w, h = pdf_options.get_dimensions_mm()
    m = pdf_options.margins
    scale = pdf_options.scale or 1.0

    # Pre-divide so the companion write_pdf(zoom=scale) multiplies back to the
    # requested page size, leaving only the content scaled.
    parts = [
        f"@page {{",
        f"    size: {w / scale}mm {h / scale}mm;",
        f"    margin: {m.top / scale}mm {m.right / scale}mm "
        f"{m.bottom / scale}mm {m.left / scale}mm;",
    ]

    if pdf_options.header and pdf_options.header.content:
        # Escape BEFORE translating: the counter placeholders deliberately emit
        # quotes to break out of the CSS string, so escaping afterwards would
        # neutralise {{page}}/{{total_pages}} along with the injection.
        header_css = translate_template_weasyprint(
            _css_string_escape(pdf_options.header.content),
            title=_css_string_escape(title),
            url=_css_string_escape(url),
        )
        # Strip any HTML tags for CSS content property (WeasyPrint @page margin
        # boxes only support plain text + counters, not HTML)
        header_text = re.sub(r"<[^>]+>", "", header_css).strip()
        parts.append(f'    @top-center {{ content: "{header_text}"; font-size: 10px; }}')

    if pdf_options.footer and pdf_options.footer.content:
        footer_css = translate_template_weasyprint(
            _css_string_escape(pdf_options.footer.content),
            title=_css_string_escape(title),
            url=_css_string_escape(url),
        )
        footer_text = re.sub(r"<[^>]+>", "", footer_css).strip()
        parts.append(f'    @bottom-center {{ content: "{footer_text}"; font-size: 10px; }}')

    parts.append("}")

    return f"<style>{chr(10).join(parts)}</style>"


def weasyprint_zoom(pdf_options) -> float:
    """Zoom factor for WeasyPrint's ``write_pdf()``.

    Pairs with the 1/scale geometry in ``build_weasyprint_page_css`` so the page
    size stays fixed while the content scales.
    """
    return pdf_options.scale if pdf_options else 1.0


def render_media_pdf(data_uri: str, pdf_options) -> bytes:
    """Lay a single image/SVG data URI onto a pdf_options-controlled page."""
    from weasyprint import HTML

    def _data_only_fetcher(url, timeout=10, ssl_context=None):
        # CairoSVG renders with unsafe=False (no external fetches); keep that
        # posture now that the geometry path routes SVG through WeasyPrint,
        # whose default fetcher would happily resolve http(s) references.
        raise ValueError(f"External resources are not fetched: {url}")

    page_css = build_weasyprint_page_css(pdf_options)
    document = (
        "<html><head><meta charset='utf-8'>" + page_css +
        "<style>html,body{margin:0;padding:0}"
        "img{max-width:100%;max-height:100%;display:block;margin:auto}</style>"
        f'</head><body><img src="{data_uri}"></body></html>'
    )
    return HTML(string=document, url_fetcher=_data_only_fetcher).write_pdf(
        zoom=weasyprint_zoom(pdf_options)
    )
