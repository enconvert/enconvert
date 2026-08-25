import io
import os

from utils.error_capture import describe_image_error
import tempfile
import zipfile


# Render scale: 2.0 == 144 DPI (PDFium's base is 72 DPI), matching the previous
# PyMuPDF ``Matrix(2.0, 2.0)`` output resolution. pypdfium2 (PDFium) is
# BSD-3-Clause / Apache-2.0 licensed — the permissive replacement for PyMuPDF,
# which is AGPL and cannot ship in a hosted service without Artifex licensing.
_RENDER_SCALE = 2.0
_JPEG_QUALITY = 100

# Untrusted-input safety on a ~1GB droplet: a tiny PDF can declare a page
# MediaBox up to the PDF-spec max (14400x14400 pt) which at 2x would allocate a
# multi-GB bitmap, and a small file can declare thousands of pages. Cap both.
_MAX_RENDER_PIXELS = 25_000_000  # 25 MP per page (scale down oversized pages)
_MAX_PAGES = 500


def _render_page_to_jpeg(page: "pdfium.PdfPage") -> bytes:
    """Render one PDF page to JPEG bytes, flattening transparency onto white.

    The render scale is reduced below 2x when a page is large enough that 2x
    would exceed ``_MAX_RENDER_PIXELS``, so an oversized MediaBox cannot OOM the
    worker. Mirrors the prior PyMuPDF output otherwise (quality 100, RGBA over
    white).
    """
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    width_pt, height_pt = page.get_size()
    scale = _RENDER_SCALE
    if width_pt > 0 and height_pt > 0:
        max_scale = (_MAX_RENDER_PIXELS / (width_pt * height_pt)) ** 0.5
        scale = min(_RENDER_SCALE, max_scale)

    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()

    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()


def pdf_to_jpeg(file_bytes: bytes, original_filename: str) -> bytes:
    """Convert a PDF to JPEG(s).

    Single-page PDFs return raw JPEG bytes; multi-page PDFs return a ZIP archive
    of ``page_1.jpeg`` … ``page_N.jpeg`` (the caller detects the ZIP magic and
    switches the output extension to ``.zip``).
    """
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pypdfium2 as pdfium
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".pdf":
        raise ValueError("Expected a PDF file (.pdf)")

    try:
        pdf = pdfium.PdfDocument(file_bytes)
    except Exception as e:  # malformed / encrypted / not a PDF
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path)
        )

    try:
        page_count = len(pdf)
        if page_count == 0:
            raise ValueError("PDF has no pages")
        if page_count > _MAX_PAGES:
            raise ValueError(
                f"PDF has too many pages ({page_count}); the maximum is {_MAX_PAGES}."
            )

        if page_count == 1:
            return _render_page_to_jpeg(pdf[0])

        # Build the archive on disk, not in a BytesIO: the in-memory ZIP held
        # every page's JPEG at once and getvalue() then doubled it. Streaming
        # to a temp file keeps peak memory at one page's working set.
        # ZIP_STORED because JPEG doesn't deflate — it also saves CPU.
        zip_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as zip_file:
                zip_path = zip_file.name  # bound first so finally covers write failures
                with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_STORED) as zf:
                    for index in range(page_count):
                        jpeg_bytes = _render_page_to_jpeg(pdf[index])
                        zf.writestr(f"page_{index + 1}.jpeg", jpeg_bytes)
            with open(zip_path, "rb") as f:
                return f.read()
        finally:
            if zip_path is not None and os.path.exists(zip_path):
                os.unlink(zip_path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path)
        )
    finally:
        pdf.close()
