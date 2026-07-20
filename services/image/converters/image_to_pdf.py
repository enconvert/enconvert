"""Raster image / SVG -> single-page PDF.

Both converters return PDF bytes and are used only by the ``anything-to-pdf``
dispatcher (``services/pdf/dispatch.py``); they are not wired to standalone
endpoints. No new dependency: Pillow (with pillow-heif for HEIC) and CairoSVG
are already installed for the existing image-conversion endpoints.

Errors are raised as ``ValueError`` so the gateway maps them to an HTTP 400
(bad input) rather than a 500 — matching the convention in the sibling
``convert_image`` module.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING, Optional

import pillow_heif
from PIL import Image

# Cheap import: utils.pdf_helpers defers WeasyPrint to inside render_media_pdf.
from utils.pdf_helpers import explicit_geometry_fields

if TYPE_CHECKING:
    from models import PdfOptions

# Register the HEIF/HEIC opener so ``Image.open`` accepts .heic/.heif input,
# exactly as the existing *_to_heic converters do.
pillow_heif.register_heif_opener()


def _wants_geometry(pdf_options: "Optional[PdfOptions]") -> bool:
    """True when the caller EXPLICITLY set a page-geometry option.

    Only geometry justifies paying for a full WeasyPrint layout pass;
    ``grayscale`` is a post-process and never lands here.

    Shares ``explicit_geometry_fields`` (and so the one GEOMETRY_FIELDS list)
    with the rejection gate: a local copy would go stale the moment a geometry
    field is added to PdfOptions, silently sending it down the fast path — i.e.
    validated then discarded, the very defect this family keeps hitting.
    """
    return bool(explicit_geometry_fields(pdf_options))


def image_to_pdf(
    file_bytes: bytes,
    original_filename: str,
    pdf_options: "Optional[PdfOptions]" = None,
) -> bytes:
    """Convert a raster image to a single-page PDF.

    Transparency is flattened onto a white background (PDF has no alpha), and
    every other mode is normalised to RGB so the PDF encoder always succeeds.
    Only the first frame of a multi-frame image (animated GIF, multi-page TIFF)
    is used.

    Without explicit page geometry the image is written straight out by Pillow
    (page = image_px/100*72pt). With geometry, it is laid onto a
    pdf_options-controlled page via WeasyPrint instead.
    """
    try:
        image = Image.open(BytesIO(file_bytes))
        image.load()
    except Image.DecompressionBombError:
        raise ValueError("Image is too large to process safely.")
    except Exception as exc:  # Pillow raises assorted types for bad input.
        raise ValueError(f"Could not read the image: {exc}")

    try:
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        if not _wants_geometry(pdf_options):
            buffer = BytesIO()
            image.save(buffer, format="PDF", resolution=100.0)
            return buffer.getvalue()

        from utils.pdf_helpers import render_media_pdf

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        data_uri = "data:image/png;base64," + base64.b64encode(
            buffer.getvalue()
        ).decode()
        return render_media_pdf(data_uri, pdf_options)
    except Exception as exc:
        raise ValueError(f"Image to PDF conversion failed: {exc}")


def svg_to_pdf(
    file_bytes: bytes,
    original_filename: str,
    pdf_options: "Optional[PdfOptions]" = None,
) -> bytes:
    """Convert an SVG document to PDF (vector, no rasterisation).

    CairoSVG handles the default path. With explicit page geometry the SVG is
    embedded as a data URI and laid out by WeasyPrint, which honours margins /
    headers / footers that CairoSVG cannot express — and stays vector.
    """
    try:
        if not _wants_geometry(pdf_options):
            import cairosvg

            return cairosvg.svg2pdf(bytestring=file_bytes)

        from utils.pdf_helpers import render_media_pdf

        data_uri = "data:image/svg+xml;base64," + base64.b64encode(
            file_bytes
        ).decode()
        return render_media_pdf(data_uri, pdf_options)
    except Exception as exc:
        raise ValueError(f"SVG to PDF conversion failed: {exc}")
