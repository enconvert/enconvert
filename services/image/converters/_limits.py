"""Shared memory guardrails for the image converters.

Runs on a ~1GB droplet: a kilobyte upload can legally declare a canvas that
decodes (or rasterizes) to multiple GB. Everything here bounds that BEFORE any
pixel allocation, so legitimate conversions pay zero extra cost. ``ValueError``
is raised for rejections because the routes map it to HTTP 400.
"""
from __future__ import annotations

import os
import tempfile

# Hard ceiling on decoded pixels, mirroring compress_image's rationale: a
# 40 MP RGBA canvas is ~160 MB — comfortably under the droplet while still
# admitting essentially every real photo. Pillow's own DecompressionBombError
# only fires at ~358 MP (~1.4 GB decoded), far past the droplet. Read once at
# import so per-conversion cost is a single integer compare.
MAX_PIXELS: int = int(os.environ.get("IMAGE_MAX_PIXELS", "40000000"))

# Render width used when an SVG's intrinsic size cannot be sniffed: render at
# a safe default instead of failing (cairosvg keeps the aspect ratio when only
# one dimension is given).
_SVG_FALLBACK_WIDTH = 2048


def ensure_pixel_limit(image: "Image.Image") -> None:
    """Reject a decompression bomb before any pixel data is decoded.

    Call immediately after ``Image.open()``: at that point only the header has
    been parsed, so width/height are known while the canvas is still
    unallocated — the check is free for legitimate images.
    """
    pixels = image.width * image.height
    if pixels > MAX_PIXELS:
        raise ValueError(
            f"Image is too large to process: {image.width}x{image.height} "
            f"({pixels} pixels) exceeds the {MAX_PIXELS} pixel limit"
        )


def svg_render_cap_kwargs(svg_bytes: bytes) -> dict[str, int]:
    """cairosvg output-size kwargs bounding an intrinsic-size render.

    For callers that pass no explicit width/height: a tiny SVG declaring e.g.
    30000x30000 would otherwise make cairo allocate a multi-GB RGBA surface.
    Returns {} when the declared size is already under MAX_PIXELS, scaled-down
    exact dimensions (aspect preserved) when it is not, and a safe default
    width when the intrinsic size cannot be determined. Rendering fewer pixels
    is strictly faster, never slower.
    """
    # Lazy import: utils.validators pulls in fastapi and the dispatch tables,
    # which the converter modules must not load at import time.
    from utils.validators import svg_intrinsic_size

    intrinsic = svg_intrinsic_size(svg_bytes)
    if intrinsic is None:
        return {"output_width": _SVG_FALLBACK_WIDTH}
    width, height = intrinsic
    if width * height <= MAX_PIXELS:
        return {}
    scale = (MAX_PIXELS / (width * height)) ** 0.5
    return {
        "output_width": max(1, int(width * scale)),
        "output_height": max(1, int(height * scale)),
    }


def write_temp_file(file_bytes: bytes, suffix: str) -> str:
    """Write bytes to a ``delete=False`` temp file and return its path.

    The bare ``with NamedTemporaryFile(...) as tf: tf.write(...)`` pattern the
    converters used leaks the file when write() raises (e.g. ENOSPC), because
    the failure propagates before the converter's own try/finally is entered.
    Here the path is known up front and the file is unlinked on ANY write
    failure, so the caller only ever sees a fully-written file or no file.
    """
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with temp_file:
            temp_file.write(file_bytes)
    except BaseException:
        os.unlink(temp_file.name)
        raise
    return temp_file.name


def describe_image_error(exc: BaseException, *paths: str) -> str:
    """A caller-safe description of a failed image conversion.

    Two production problems, one helper:

    * PIL and cairosvg quote the scratch file they were handed, and the
      routes surface ``ValueError`` as the 400 body — so an internal
      filesystem path (``/tmp/tmp89wyefon.png``) was being returned to
      API callers and stored in the admin error table.
    * ``cannot identify image file`` tells the caller nothing they can
      act on. The actionable version names the real cause: the bytes are
      not the format the extension claims.
    """
    name = type(exc).__name__
    if name == "UnidentifiedImageError":
        return (
            "the file could not be decoded as an image (its contents do not "
            "match its extension, or it is corrupt or truncated)"
        )
    message = str(exc)
    for path in paths:
        if not path:
            continue
        message = message.replace(path, "the uploaded file")
        base = os.path.basename(path)
        if base:
            message = message.replace(base, "the uploaded file")
    return message
