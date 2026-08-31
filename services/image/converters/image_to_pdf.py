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
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Optional


# Cheap import: utils.pdf_helpers defers WeasyPrint to inside render_media_pdf.
from utils.pdf_helpers import explicit_geometry_fields
from ._limits import ensure_svg_depth

from ._limits import ensure_pixel_limit

if TYPE_CHECKING:
    from models import PdfOptions

# Register the HEIF/HEIC opener so ``Image.open`` accepts .heic/.heif input,
# exactly as the existing *_to_heic converters do.


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
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    try:
        image = Image.open(BytesIO(file_bytes))
        # Header-only gate BEFORE load(): Pillow's own DecompressionBombError
        # fires only at ~358 MP (~1.4 GB decoded) — far past the 1 GB droplet.
        ensure_pixel_limit(image)
        image.load()
    except Image.DecompressionBombError:
        raise ValueError("Image is too large to process safely.")
    except ValueError:
        raise  # the pixel-limit gate already carries a clear message
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

        # Hand WeasyPrint a file:// URL instead of a base64 data URI: the URI
        # path materialized ~5 full-image copies (PNG buffer, +33% base64 str,
        # HTML string, WeasyPrint's own decode). The fetcher allow-lists
        # exactly this one path, keeping the no-external-fetches posture.
        png_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as png_file:
                png_path = png_file.name  # bound first so finally covers write failures
                image.save(png_file, format="PNG")
            return render_media_pdf(
                Path(png_path).as_uri(), pdf_options, allow_file_path=png_path
            )
        finally:
            if png_path is not None and os.path.exists(png_path):
                os.unlink(png_path)
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
    # Both branches parse the SVG recursively. Outside the try so the depth
    # rejection reaches the caller as itself, not wrapped in a second message.
    ensure_svg_depth(file_bytes)
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
