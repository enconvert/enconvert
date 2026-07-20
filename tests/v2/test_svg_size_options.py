"""Unit tests for the svg-to-png/jpeg/webp width/height output-size options.

Covers the converter layer (cairosvg pass-through: one dimension scales
proportionally, both set the exact canvas) and the route-level validation gate
(utils.validators.validate_svg_dimensions / svg_intrinsic_size). Pure pytest,
real CairoSVG + Pillow from the gateway venv, per repo convention.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_svg_size_options.py -q
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from PIL import Image

from services.image.converters import svg_to_jpeg, svg_to_png, svg_to_webp
from utils.validators import (
    MAX_IMAGE_DIMENSION,
    MAX_OUTPUT_PIXELS,
    svg_intrinsic_size,
    validate_svg_dimensions,
)

SVG_100x50 = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" '
    b'viewBox="0 0 100 50"><rect width="100" height="50" fill="red"/></svg>'
)
SVG_VIEWBOX_ONLY = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100">'
    b'<rect width="400" height="100" fill="blue"/></svg>'
)
SVG_MM_UNITS = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm">'
    b'<rect width="100" height="50" fill="green"/></svg>'
)
SVG_EXTREME_RATIO = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 10000"></svg>'
SVG_NO_SIZE = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'

CONVERTERS = [
    (svg_to_png, "PNG"),
    (svg_to_jpeg, "JPEG"),
    (svg_to_webp, "WEBP"),
]


def _dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _fmt(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        return image.format


# ---- Converter layer ------------------------------------------------------

@pytest.mark.parametrize("converter,fmt", CONVERTERS)
def test_default_keeps_intrinsic_size(converter, fmt):
    out = converter(SVG_100x50, "a.svg")
    assert _dims(out) == (100, 50)
    assert _fmt(out) == fmt


@pytest.mark.parametrize("converter,fmt", CONVERTERS)
def test_width_only_scales_proportionally(converter, fmt):
    out = converter(SVG_100x50, "a.svg", width=200)
    assert _dims(out) == (200, 100)
    assert _fmt(out) == fmt


@pytest.mark.parametrize("converter,fmt", CONVERTERS)
def test_height_only_scales_proportionally(converter, fmt):
    out = converter(SVG_100x50, "a.svg", height=200)
    assert _dims(out) == (400, 200)


@pytest.mark.parametrize("converter,fmt", CONVERTERS)
def test_both_dimensions_set_exact_canvas(converter, fmt):
    # Both given -> exact size, even when it distorts the aspect ratio.
    out = converter(SVG_100x50, "a.svg", width=300, height=60)
    assert _dims(out) == (300, 60)


def test_viewbox_only_svg_scales_from_viewbox():
    out = svg_to_png(SVG_VIEWBOX_ONLY, "a.svg", width=200)
    assert _dims(out) == (200, 50)


@pytest.mark.parametrize("converter,_", CONVERTERS)
def test_non_svg_extension_rejected(converter, _):
    with pytest.raises(ValueError, match="Expected an SVG"):
        converter(SVG_100x50, "a.png", width=100)


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_dimensions_rejected(bad):
    with pytest.raises(ValueError, match="positive"):
        svg_to_png(SVG_100x50, "a.svg", width=bad)
    with pytest.raises(ValueError, match="positive"):
        svg_to_png(SVG_100x50, "a.svg", height=bad)


# ---- svg_intrinsic_size ---------------------------------------------------

def test_intrinsic_size_from_attributes():
    assert svg_intrinsic_size(SVG_100x50) == (100.0, 50.0)


def test_intrinsic_size_from_viewbox_fallback():
    assert svg_intrinsic_size(SVG_VIEWBOX_ONLY) == (400.0, 100.0)


def test_intrinsic_size_converts_mm_units():
    width, height = svg_intrinsic_size(SVG_MM_UNITS)
    assert round(width / height, 3) == 2.0
    assert round(width, 1) == round(100 * 96 / 25.4, 1)


def test_intrinsic_size_unknown_returns_none():
    assert svg_intrinsic_size(SVG_NO_SIZE) is None
    assert svg_intrinsic_size(b"not xml at all") is None
    assert svg_intrinsic_size(b"") is None


# ---- validate_svg_dimensions (route gate) ---------------------------------

def test_validate_accepts_reasonable_dimensions():
    validate_svg_dimensions(None, None, SVG_100x50)
    validate_svg_dimensions(800, None, SVG_100x50)
    validate_svg_dimensions(None, 800, SVG_100x50)
    validate_svg_dimensions(4000, 4000, SVG_100x50)


@pytest.mark.parametrize("width,height", [(0, None), (MAX_IMAGE_DIMENSION + 1, None),
                                          (None, 0), (None, MAX_IMAGE_DIMENSION + 1)])
def test_validate_rejects_out_of_range(width, height):
    with pytest.raises(HTTPException) as excinfo:
        validate_svg_dimensions(width, height, SVG_100x50)
    assert excinfo.value.status_code == 400


def test_validate_rejects_explicit_pixel_bomb():
    with pytest.raises(HTTPException) as excinfo:
        validate_svg_dimensions(6000, 6000, SVG_100x50)  # 36 MP > 25 MP cap
    assert excinfo.value.status_code == 400
    assert str(MAX_OUTPUT_PIXELS) in excinfo.value.detail


def test_validate_rejects_derived_pixel_bomb():
    # 1:10000 ratio SVG at width=10000 would derive a ~100M-pixel-tall render.
    with pytest.raises(HTTPException) as excinfo:
        validate_svg_dimensions(MAX_IMAGE_DIMENSION, None, SVG_EXTREME_RATIO)
    assert excinfo.value.status_code == 400
    assert "aspect ratio" in excinfo.value.detail


SVG_EM_UNITS = b'<svg xmlns="http://www.w3.org/2000/svg" width="2em" height="12em"></svg>'


def test_validate_fails_closed_when_ratio_unknown_single_dimension():
    # No determinable ratio + single dimension -> reject (cairosvg could still
    # resolve em/ex/% into an unbounded canvas, so we cannot fail open).
    for bytes_ in (SVG_NO_SIZE, SVG_EM_UNITS):
        with pytest.raises(HTTPException) as excinfo:
            validate_svg_dimensions(MAX_IMAGE_DIMENSION, None, bytes_)
        assert excinfo.value.status_code == 400
        assert "both width and height" in excinfo.value.detail


def test_validate_allows_both_dimensions_even_when_ratio_unknown():
    # Explicit width AND height are self-bounding — no ratio needed.
    validate_svg_dimensions(800, 600, SVG_EM_UNITS)


def test_validate_noop_when_no_dimensions_requested():
    # The default path (no width/height) is never affected by ratio unknowns.
    validate_svg_dimensions(None, None, SVG_EM_UNITS)
    validate_svg_dimensions(None, None, SVG_NO_SIZE)
