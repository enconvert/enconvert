import markdown
from weasyprint import HTML
from models import PdfOptions
from utils.pdf_helpers import build_weasyprint_page_css, weasyprint_zoom


def markdown_to_pdf(markdown_bytes: bytes, pdf_options: PdfOptions = None) -> bytes:
    """
    Convert Markdown to PDF.

    Args:
        markdown_bytes: Markdown content as bytes

    Returns:
        PDF content as bytes

    Raises:
        ValueError: If Markdown is invalid or conversion fails
    """
    try:
        markdown_str = markdown_bytes.decode('utf-8')

        html_str = markdown.markdown(
            markdown_str,
            extensions=['tables', 'fenced_code', 'codehilite', 'toc', 'attr_list']
        )

        page_css = build_weasyprint_page_css(pdf_options) if pdf_options else ""

        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    {page_css}
    <style>
        body {{
            max-width: 800px;
            margin: 40px auto;
            padding: 0 20px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            line-height: 1.6;
            color: #333333;
        }}

        code {{
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}

        pre {{
            background: #f4f4f4;
            padding: 12px;
            border-radius: 5px;
            overflow-x: auto;
        }}

        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
        }}

        th, td {{
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
        }}

        th {{
            background-color: #f4f4f4;
        }}

        blockquote {{
            border-left: 4px solid #ddd;
            margin: 16px 0;
            padding: 0 16px;
            color: #555;
        }}

        img {{
            max-width: 100%;
        }}
    </style>
</head>
<body>
{html_str}
</body>
</html>"""

        pdf_bytes = HTML(string=full_html).write_pdf(zoom=weasyprint_zoom(pdf_options))
        return pdf_bytes
    except UnicodeDecodeError:
        raise ValueError("Invalid Markdown encoding (expected UTF-8)")
    except Exception as e:
        raise ValueError(f"Markdown to PDF conversion failed: {str(e)}")
