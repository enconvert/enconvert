from models import PdfOptions
from utils.pdf_helpers import build_weasyprint_page_css, weasyprint_zoom


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
    # WeasyPrint imported lazily (kept off idle RAM until a PDF is rendered).
    from weasyprint import HTML
    try:
        html_str = html_bytes.decode('utf-8')

        if pdf_options:
            page_css = build_weasyprint_page_css(pdf_options)
            if "</head>" in html_str:
                html_str = html_str.replace("</head>", f"{page_css}</head>")
            elif "<html" in html_str.lower():
                html_str = page_css + html_str
            else:
                html_str = f"<html><head>{page_css}</head><body>{html_str}</body></html>"

        pdf_bytes = HTML(string=html_str).write_pdf(zoom=weasyprint_zoom(pdf_options))
        return pdf_bytes
    except UnicodeDecodeError:
        raise ValueError("Invalid HTML encoding (expected UTF-8)")
    except Exception as e:
        raise ValueError(f"HTML to PDF conversion failed: {str(e)}")
