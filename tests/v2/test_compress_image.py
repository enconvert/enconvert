"""Unit tests for the compress-image converter (same-format PNG/JPEG/WebP).

Stage 1 (lossless: strip metadata, strongest lossless re-encode, palette
candidate only when provably pixel-identical, never larger than the input) and
stage 2 (target_size_kb: aspect-locked LANCZOS downscale, best effort). Pure
pytest against the real Pillow in the gateway venv, per repo convention.

Run: cd api/gateway && ./.venv/bin/python -m pytest tests/v2/test_compress_image.py -q
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from services.image.converters import compress_image
from services.image.converters.compress_image import _webp_is_lossless


# ---- fixture builders -----------------------------------------------------

def _gradient_rgb(size=(400, 300)) -> Image.Image:
    image = Image.new("RGB", size)
    image.putdata([
        (x % 16 * 15, y % 16 * 15, 128)
        for y in range(size[1]) for x in range(size[0])
    ])
    return image


def _noise_rgb(size=(200, 150), seed: int = 7) -> Image.Image:
    # Deterministic high-entropy image (poorly compressible, >256 colors).
    values = []
    state = seed
    for _ in range(size[0] * size[1]):
        state = (state * 1103515245 + 12345) % (2 ** 31)
        values.append(((state >> 16) & 255, (state >> 8) & 255, state & 255))
    image = Image.new("RGB", size)
    image.putdata(values)
    return image


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ---- Stage 1: lossless ----------------------------------------------------

def test_png_lossless_smaller_and_pixel_identical():
    original = _encode(_gradient_rgb(), "PNG")
    out = compress_image(original, "img.png")
    assert len(out) <= len(original)
    result = _open(out)
    assert result.format == "PNG"
    assert result.convert("RGB").tobytes() == _gradient_rgb().tobytes()


def test_png_palette_candidate_used_when_provably_lossless():
    # Few flat colors in a noisy arrangement: 1-byte palette indices beat RGB
    # scanline filtering, so the (provably lossless) palette candidate wins.
    # (For smooth gradients the RGB candidate wins instead — min() decides.)
    palette_colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    values, state = [], 7
    for _ in range(300 * 200):
        state = (state * 1103515245 + 12345) % (2 ** 31)
        values.append(palette_colors[state % 4])
    source = Image.new("RGB", (300, 200))
    source.putdata(values)
    original = _encode(source, "PNG")
    out = compress_image(original, "img.png")
    result = _open(out)
    assert result.mode == "P"
    assert result.convert("RGB").tobytes() == source.tobytes()


def test_png_noise_never_larger_than_input():
    original = _encode(_noise_rgb(), "PNG", optimize=True, compress_level=9)
    out = compress_image(original, "img.png")
    assert len(out) <= len(original)


def test_png_text_metadata_stripped():
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Comment", "secret authoring notes " * 50)
    original = _encode(_gradient_rgb(), "PNG", pnginfo=info)
    out = compress_image(original, "img.png")
    assert b"secret authoring notes" not in out


def test_jpeg_lossless_reencode_smaller_same_dimensions():
    original = _encode(_gradient_rgb((800, 600)), "JPEG", quality=95)
    out = compress_image(original, "img.jpg")
    result = _open(out)
    assert result.format == "JPEG"
    assert result.size == (800, 600)
    assert len(out) < len(original)


def test_jpeg_exif_orientation_preserved_other_exif_stripped():
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation
    exif[0x010E] = "a private description that must be stripped"
    original = _encode(_gradient_rgb(), "JPEG", quality=90, exif=exif.tobytes())
    out = compress_image(original, "img.jpg")
    out_exif = _open(out).getexif()
    assert out_exif.get(0x0112) == 6
    assert out_exif.get(0x010E) is None


def test_icc_profile_preserved():
    profile = b"\x00" * 128 + b"fake-icc-profile-payload"
    original = _encode(_gradient_rgb(), "JPEG", quality=90, icc_profile=profile)
    out = compress_image(original, "img.jpg")
    assert _open(out).info.get("icc_profile") == profile


def test_webp_lossless_source_stays_pixel_identical():
    source = _gradient_rgb()
    original = _encode(source, "WEBP", lossless=True, quality=50)
    out = compress_image(original, "img.webp")
    assert len(out) <= len(original)
    assert _open(out).convert("RGB").tobytes() == source.tobytes()


def test_webp_lossy_source_returns_original_when_nothing_lossless_to_gain():
    original = _encode(_noise_rgb(), "WEBP", quality=60)
    out = compress_image(original, "img.webp")
    # Lossless re-encode of decoded lossy pixels is bigger -> original wins.
    assert out == original


# ---- Stage 2: target size, aspect ratio locked ----------------------------

def test_target_reached_by_downscaling_with_aspect_ratio_locked():
    original = _encode(_noise_rgb((800, 600)), "JPEG", quality=95)
    assert len(original) > 100 * 1024
    out = compress_image(original, "photo.jpg", target_size_kb=100)
    assert len(out) <= 100 * 1024
    result = _open(out)
    assert result.size[0] < 800 and result.size[1] < 600
    assert abs(result.size[0] / result.size[1] - 800 / 600) < 0.02


def test_target_already_met_returns_lossless_result():
    original = _encode(_gradient_rgb(), "PNG")
    unconstrained = compress_image(original, "img.png")
    with_target = compress_image(original, "img.png", target_size_kb=10_000)
    assert with_target == unconstrained


def test_unreachable_target_returns_best_effort_not_error():
    original = _encode(_noise_rgb((800, 600)), "JPEG", quality=95)
    out = compress_image(original, "photo.jpg", target_size_kb=1)
    assert len(out) < len(original)  # smallest achieved, no exception
    assert _open(out).format == "JPEG"


def test_png_target_stays_png():
    original = _encode(_noise_rgb((600, 400)), "PNG")
    out = compress_image(original, "img.png", target_size_kb=50)
    assert _open(out).format == "PNG"


# ---- Rejections -----------------------------------------------------------

def test_unsupported_extension_rejected():
    original = _encode(_gradient_rgb(), "PNG")
    with pytest.raises(ValueError, match="Expected a PNG, JPEG or WebP"):
        compress_image(original, "img.gif")


def test_content_extension_mismatch_rejected():
    jpeg = _encode(_gradient_rgb(), "JPEG", quality=90)
    with pytest.raises(ValueError, match="never changes the format"):
        compress_image(jpeg, "img.png")


def test_invalid_target_rejected():
    original = _encode(_gradient_rgb(), "PNG")
    with pytest.raises(ValueError, match="positive integer"):
        compress_image(original, "img.png", target_size_kb=0)


def test_animated_webp_rejected():
    frames = [Image.new("RGB", (60, 60), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=100)
    with pytest.raises(ValueError, match="Animated"):
        compress_image(buffer.getvalue(), "anim.webp")


def test_garbage_bytes_rejected():
    with pytest.raises(ValueError, match="Could not read image"):
        compress_image(b"definitely not an image", "img.png")


def test_oversized_canvas_rejected_before_decode():
    # Decompression bomb: small bytes, huge declared canvas. Must be rejected
    # via the header check before .load() allocates the surface.
    from services.image.converters.compress_image import MAX_COMPRESS_PIXELS
    side = int(MAX_COMPRESS_PIXELS ** 0.5) + 2000  # comfortably over the cap
    bomb = Image.new("RGB", (side, side), (255, 255, 255))
    buffer = io.BytesIO()
    bomb.save(buffer, "PNG", compress_level=9)  # solid color -> tiny bytes
    with pytest.raises(ValueError, match="too large to compress"):
        compress_image(buffer.getvalue(), "bomb.png")


def test_orientation_at_cap_boundary_allowed():
    # A canvas exactly at the cap must still pass (boundary is inclusive).
    from services.image.converters.compress_image import MAX_COMPRESS_PIXELS
    # 8000x5000 = 40 MP == cap; keep it a solid color so bytes stay small.
    assert 8000 * 5000 == MAX_COMPRESS_PIXELS
    img = Image.new("RGB", (8000, 5000), (10, 20, 30))
    buffer = io.BytesIO()
    img.save(buffer, "PNG", compress_level=9)
    out = compress_image(buffer.getvalue(), "big.png")
    assert Image.open(io.BytesIO(out)).format == "PNG"


def test_malformed_ascii_orientation_does_not_crash():
    # A non-int (ASCII) orientation tag must not raise struct.error -> 500;
    # metadata is best-effort, so it is simply dropped.
    from PIL import Image as PILImage
    exif = PILImage.Exif()
    exif[0x0112] = "6"  # ASCII where a SHORT is expected (buggy writer)
    img = _gradient_rgb()
    buffer = io.BytesIO()
    try:
        img.save(buffer, "JPEG", quality=90, exif=exif.tobytes())
    except Exception:
        pytest.skip("Pillow refused to write the malformed EXIF; sniff N/A")
    out = compress_image(buffer.getvalue(), "photo.jpg")  # must not raise
    assert Image.open(io.BytesIO(out)).format == "JPEG"


# ---- _webp_is_lossless ----------------------------------------------------

def test_webp_lossless_detection():
    lossless = _encode(_gradient_rgb(), "WEBP", lossless=True)
    lossy = _encode(_gradient_rgb(), "WEBP", quality=80)
    assert _webp_is_lossless(lossless) is True
    assert _webp_is_lossless(lossy) is False
    assert _webp_is_lossless(b"") is False
    assert _webp_is_lossless(_encode(_gradient_rgb(), "PNG")) is False
