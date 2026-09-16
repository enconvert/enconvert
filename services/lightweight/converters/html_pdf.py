from models import PdfOptions
from utils.pdf_helpers import build_weasyprint_page_css, render_html_pdf
from services.markdown.common import decode_text_bytes


def html_to_pdf(html_bytes: bytes, pdf_options: PdfOptions = None) -> bytes:
    """
    Convert HTML to PDF.

    Args:
        html_bytes: HTML content as bytes
        pdf_options: Optional PDF output configuration

    Returns:
        PDF content as bytes

    Raises:
        ValueError: If HTML is invalid or conversion fails
    """
    try:
        html_str = decode_text_bytes(html_bytes)

        if pdf_options:
            page_css = build_weasyprint_page_css(pdf_options)
            if "</head>" in html_str:
                html_str = html_str.replace("</head>", f"{page_css}</head>")
            elif "<html" in html_str.lower():
                html_str = page_css + html_str
            else:
                html_str = f"<html><head>{page_css}</head><body>{html_str}</body></html>"

        pdf_bytes = render_html_pdf(html_str, pdf_options)
        return pdf_bytes
    except Exception as e:
        raise ValueError(f"HTML to PDF conversion failed: {str(e)}")
