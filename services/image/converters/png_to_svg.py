import os
import base64

from ._limits import ensure_svg_depth, svg_render_cap_kwargs, write_temp_file
from utils.error_capture import describe_image_error

# PNG signature (RFC 2083 §3.1).
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def png_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image, UnidentifiedImageError
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".png":
        raise ValueError("Expected a PNG file (.png)")

    # Magic bytes, before anything touches disk. A file merely NAMED .png
    # used to reach PIL and come back as
    # "cannot identify image file '/tmp/tmpXXXX.png'" — a message that says
    # nothing to the caller and puts a server temp path in the 400 body.
    if not file_bytes.startswith(_PNG_MAGIC):
        raise ValueError(
            "Not a valid PNG file: the contents are not PNG data (the file "
            "may be renamed, corrupt, or truncated)"
        )

    temp_file_path = write_temp_file(file_bytes, ext)

    try:
        with Image.open(temp_file_path) as image:
            width, height = image.size

        # bytes end-to-end: keeping the base64 payload undecoded and joining
        # once avoids the ~4.6x-input transient copies the str + f-string
        # assembly materialized. Output bytes are identical.
        with open(temp_file_path, "rb") as f:
            img_b64 = base64.b64encode(f.read())

        return b"".join((
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}">\n'
                f'  <image href="data:image/png;base64,'
            ).encode("ascii"),
            img_b64,
            (
                f'" width="{width}" height="{height}"/>\n'
                f'</svg>'
            ).encode("ascii"),
        ))
    except UnidentifiedImageError:
        raise ValueError(
            "Not a valid PNG file: the image could not be decoded (the file "
            "may be corrupt or truncated)"
        )
    except Exception as e:
        # str(e) on a PIL/OS error routinely contains the temp path we just
        # wrote; the caller gets the reason, the path stays server-side.
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path)
        )
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def svg_to_png(
    file_bytes: bytes,
    original_filename: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Render an SVG to PNG. Optional width/height set the output size in px:
    one dimension alone scales proportionally (cairosvg derives the other from
    the SVG's aspect ratio); both together set the exact canvas size."""
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import cairosvg
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".svg":
        raise ValueError("Expected an SVG file (.svg)")
    # Bound nesting before CairoSVG's recursive parser sees the bytes.
    ensure_svg_depth(file_bytes)
    if (width is not None and width <= 0) or (height is not None and height <= 0):
        raise ValueError("width and height must be positive integers")

    size_kwargs = {}
    if width is not None:
        size_kwargs["output_width"] = int(width)
    if height is not None:
        size_kwargs["output_height"] = int(height)
    if not size_kwargs:
        # No explicit size: cap the intrinsic render, otherwise a tiny SVG
        # declaring a huge canvas makes cairo allocate a multi-GB surface.
        size_kwargs = svg_render_cap_kwargs(file_bytes)

    temp_file_path = write_temp_file(file_bytes, ".svg")

    output_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    try:
        cairosvg.svg2png(url=temp_file_path, write_to=output_file_path, **size_kwargs)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(
            f"Image conversion failed: "
            f"{describe_image_error(e, temp_file_path, output_file_path)}"
        )
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
