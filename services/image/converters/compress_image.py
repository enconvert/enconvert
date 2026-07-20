"""compress-image: same-format image compression for PNG, JPEG and WebP.

Two stages, matching the product spec ("lossless as far as possible, then
reduce pixel size keeping the aspect ratio constant"):

1. Lossless-first. Metadata is stripped (EXIF, XMP, PNG text chunks), except
   the ICC color profile and the EXIF orientation flag, which are preserved so
   colors and rotation do not change. The image is then re-encoded with the
   strongest lossless settings Pillow offers for its format:
     - PNG:  zlib level 9 + optimize, plus a palette (P-mode) candidate when
             the image has <= 256 unique colors AND the roundtrip is proven
             pixel-identical.
     - JPEG: re-encode reusing the ORIGINAL quantization tables
             (quality='keep') with optimized progressive Huffman coding — no
             additional quantization loss is introduced.
     - WebP: true lossless VP8L re-encode (method=6).
   The smallest of {original bytes, candidates} wins, so this stage can never
   make the file bigger.

2. Dimension reduction — only when target_size_kb is provided and stage 1 is
   still above it. The image is downscaled with the aspect ratio locked
   (LANCZOS) and re-encoded, binary-searching the scale factor for the largest
   dimensions that fit the target. If the target is unreachable even at the
   minimum scale, the smallest encoding achieved is returned (best effort —
   the response's file_size tells the caller what was actually reached).

Pillow-only (HPND/MIT-CMU, permissive). Deliberately avoids GPL-licensed
tools such as pngquant, jpegoptim and gifsicle, per licensing policy.
"""
import io
import math
import os

from PIL import Image

_SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_EXT_TO_FORMAT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
_PALETTE_SOURCE_MODES = ("RGB", "RGBA", "L", "LA")
# Stage 2 re-encode quality for lossy formats. Stage 2 exists to shrink
# dimensions, not to crush quality, so this stays in the visually-safe band.
_STAGE2_JPEG_QUALITY = 85
_STAGE2_WEBP_QUALITY = 85
_MAX_SEARCH_ITERATIONS = 8
_MIN_SCALE = 0.01
_ORIENTATION_TAG = 0x0112
# Hard input ceiling on decoded pixels. Byte size is bounded upstream by the
# plan's max_file_size, but a small file can decode to a huge canvas
# (decompression bomb) — a 40 MP RGBA image is ~160 MB, comfortably under the
# ~1 GB droplet, while still admitting essentially every real photo (a 100 MP
# medium-format frame is the rare exception). Enforced before any encode.
MAX_COMPRESS_PIXELS = 40_000_000
# WebP lossless method=6 is the slowest, highest-ratio effort; on a large
# canvas it can burn a minute-plus of CPU while holding the single conversion
# slot. Drop to the still-good method=4 above this many pixels to bound it.
_WEBP_FAST_METHOD_PIXELS = 4_000_000


def _webp_method(pixels: int) -> int:
    return 6 if pixels <= _WEBP_FAST_METHOD_PIXELS else 4


def _preserved_metadata(image: Image.Image) -> dict:
    """Save-kwargs that keep visual fidelity while stripping private metadata.

    Everything (EXIF, XMP, text chunks) is dropped EXCEPT the ICC color
    profile and the EXIF orientation flag: dropping the profile shifts colors
    in color-managed viewers, and dropping the orientation flag renders
    portrait photos sideways. The orientation is re-emitted alone in a minimal
    EXIF block instead of transposing pixels, which would break the JPEG
    quality='keep' lossless path.
    """
    kwargs: dict = {}
    icc_profile = image.info.get("icc_profile")
    if icc_profile:
        kwargs["icc_profile"] = icc_profile
    try:
        orientation = image.getexif().get(_ORIENTATION_TAG)
    except Exception:
        orientation = None
    # Only re-emit a well-formed orientation (SHORT, values 2-8). Buggy writers
    # store it as ASCII or an out-of-range int; Image.Exif().tobytes() would
    # then raise struct.error — which is not a ValueError, so it would surface
    # as a 500 for an otherwise-valid image. Metadata is best-effort: on any
    # trouble, drop it rather than fail the conversion.
    if isinstance(orientation, int) and 2 <= orientation <= 8:
        try:
            exif = Image.Exif()
            exif[_ORIENTATION_TAG] = orientation
            kwargs["exif"] = exif.tobytes()
        except Exception:
            pass
    return kwargs


def _webp_is_lossless(file_bytes: bytes) -> bool:
    """Walk the RIFF chunk list and report whether the image data is VP8L."""
    if len(file_bytes) < 16 or file_bytes[:4] != b"RIFF" or file_bytes[8:12] != b"WEBP":
        return False
    position = 12
    while position + 8 <= len(file_bytes):
        fourcc = file_bytes[position:position + 4]
        if fourcc == b"VP8L":
            return True
        if fourcc == b"VP8 ":
            return False
        size = int.from_bytes(file_bytes[position + 4:position + 8], "little")
        position += 8 + size + (size & 1)
    return False


def _png_candidates(image: Image.Image, meta: dict) -> list[bytes]:
    candidates = []
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True, compress_level=9, **meta)
    candidates.append(buffer.getvalue())
    # Palette candidate: only when provably lossless (exact roundtrip).
    if image.mode in _PALETTE_SOURCE_MODES:
        try:
            colors = image.getcolors(256)
            if colors:
                palette_image = image.convert(
                    "P", palette=Image.ADAPTIVE, colors=max(2, min(256, len(colors)))
                )
                if palette_image.convert(image.mode).tobytes() == image.tobytes():
                    palette_buffer = io.BytesIO()
                    palette_image.save(palette_buffer, format="PNG", optimize=True, **meta)
                    candidates.append(palette_buffer.getvalue())
        except Exception:
            pass  # palette path is opportunistic; the plain re-encode stands
    return candidates


def _jpeg_candidates(image: Image.Image, meta: dict) -> list[bytes]:
    candidates = []
    try:
        buffer = io.BytesIO()
        # quality='keep' reuses the source's quantization tables: entropy
        # coding is re-optimized, no further quantization loss is introduced.
        image.save(buffer, format="JPEG", quality="keep", optimize=True,
                   progressive=True, **meta)
        candidates.append(buffer.getvalue())
    except Exception:
        pass  # 'keep' requires a JPEG source image; fail open to the original
    return candidates


def _webp_candidates(image: Image.Image, meta: dict) -> list[bytes]:
    candidates = []
    try:
        buffer = io.BytesIO()
        method = _webp_method(image.width * image.height)
        image.save(buffer, format="WEBP", lossless=True, quality=100, method=method, **meta)
        candidates.append(buffer.getvalue())
    except Exception:
        pass
    return candidates


_LOSSLESS_ENCODERS = {
    "PNG": _png_candidates,
    "JPEG": _jpeg_candidates,
    "WEBP": _webp_candidates,
}


def _encode_scaled(
    image: Image.Image,
    fmt: str,
    scale: float,
    meta: dict,
    webp_lossless: bool,
) -> bytes:
    """Encode the image at `scale` (aspect ratio locked, LANCZOS)."""
    new_width = max(1, round(image.width * scale))
    new_height = max(1, round(image.height * scale))
    if (new_width, new_height) == image.size:
        resized = image
    else:
        resized = image.resize((new_width, new_height), Image.LANCZOS)

    buffer = io.BytesIO()
    if fmt == "PNG":
        return min(_png_candidates(resized, meta), key=len)
    if fmt == "JPEG":
        if resized.mode not in ("RGB", "L", "CMYK"):
            resized = resized.convert("RGB")
        resized.save(buffer, format="JPEG", quality=_STAGE2_JPEG_QUALITY,
                     optimize=True, progressive=True, **meta)
    else:  # WEBP
        method = _webp_method(resized.width * resized.height)
        if webp_lossless:
            resized.save(buffer, format="WEBP", lossless=True, quality=100,
                         method=method, **meta)
        else:
            resized.save(buffer, format="WEBP", quality=_STAGE2_WEBP_QUALITY,
                         method=method, **meta)
    return buffer.getvalue()


def compress_image(
    file_bytes: bytes,
    original_filename: str,
    target_size_kb: int | None = None,
) -> bytes:
    """Compress a PNG, JPEG or WebP image without changing its format.

    Args:
        file_bytes: The raw image bytes.
        original_filename: Used for the extension gate (.png/.jpg/.jpeg/.webp).
        target_size_kb: Optional size budget. Omitted -> lossless-only result.

    Returns:
        The compressed bytes — never larger than the input when no target is
        set; best-effort smallest when a target is set but unreachable.
    """
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            "Expected a PNG, JPEG or WebP file (.png, .jpg, .jpeg, .webp)"
        )
    if target_size_kb is not None and target_size_kb < 1:
        raise ValueError("target_size_kb must be a positive integer")

    try:
        image = Image.open(io.BytesIO(file_bytes))
        # Header-only at this point: check the declared canvas BEFORE load()
        # decodes pixels, so a decompression bomb is rejected without ever
        # allocating the full surface.
        pixels = image.width * image.height
        if pixels > MAX_COMPRESS_PIXELS:
            raise ValueError(
                f"Image is too large to compress: {image.width}x{image.height} "
                f"({pixels} pixels) exceeds the {MAX_COMPRESS_PIXELS} pixel limit"
            )
        image.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read image: {exc}")

    fmt = _EXT_TO_FORMAT[ext]
    if image.format != fmt:
        raise ValueError(
            f"File content is {image.format or 'unrecognized'} but the "
            f"extension is '{ext}' — compress-image never changes the format"
        )
    if getattr(image, "n_frames", 1) > 1:
        raise ValueError(
            "Animated images (APNG / animated WebP) are not supported by "
            "compress-image"
        )

    meta = _preserved_metadata(image)
    webp_lossless_source = fmt == "WEBP" and _webp_is_lossless(file_bytes)

    # ---- Stage 1: lossless (the original bytes always compete) -------------
    candidates = [file_bytes]
    candidates += _LOSSLESS_ENCODERS[fmt](image, meta)
    best = min(candidates, key=len)

    if target_size_kb is None:
        return best
    target_bytes = target_size_kb * 1024
    if len(best) <= target_bytes:
        return best

    # ---- Stage 2: downscale, aspect ratio locked ---------------------------
    # Bisect the scale factor; bytes-vs-scale is monotone enough that 8
    # iterations land within ~0.5% of the largest scale that fits.
    low, high = 0.0, 1.0
    scale = max(_MIN_SCALE, min(0.95, math.sqrt(target_bytes / len(best))))
    best_fit: bytes | None = None
    smallest = best
    for _ in range(_MAX_SEARCH_ITERATIONS):
        scale = max(_MIN_SCALE, scale)
        encoded = _encode_scaled(image, fmt, scale, meta, webp_lossless_source)
        if len(encoded) < len(smallest):
            smallest = encoded
        if len(encoded) <= target_bytes:
            best_fit = encoded  # scales only grow past a fit -> last fit wins
            low = scale
        else:
            high = scale
        if high <= _MIN_SCALE or high - low <= 0.02:
            break
        scale = (low + high) / 2
    return best_fit if best_fit is not None else smallest
