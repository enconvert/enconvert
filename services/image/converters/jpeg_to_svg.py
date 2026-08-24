import os
import base64

from ._limits import describe_image_error, svg_render_cap_kwargs, write_temp_file


def jpeg_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise ValueError("Expected a JPEG file (.jpg or .jpeg)")

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
                f'  <image href="data:image/jpeg;base64,'
            ).encode("ascii"),
            img_b64,
            (
                f'" width="{width}" height="{height}"/>\n'
                f'</svg>'
            ).encode("ascii"),
        ))
    except Exception as e:
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path, output_file_path)
        )
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def svg_to_jpeg(
    file_bytes: bytes,
    original_filename: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Render an SVG to JPEG. Optional width/height set the output size in px:
    one dimension alone scales proportionally (cairosvg derives the other from
    the SVG's aspect ratio); both together set the exact canvas size."""
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    import cairosvg
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".svg":
        raise ValueError("Expected an SVG file (.svg)")
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

    png_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    output_file_path = os.path.splitext(temp_file_path)[0] + ".jpeg"
    try:
        # Convert SVG to PNG first using cairosvg
        cairosvg.svg2png(url=temp_file_path, write_to=png_file_path, **size_kwargs)

        # Open the PNG and convert to JPEG
        with Image.open(png_file_path) as image:
            # JPEG doesn't support transparency — flatten alpha onto white background
            if image.mode == "RGBA":
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[3])
                background.save(output_file_path, format="JPEG", quality=100)
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output_file_path, format="JPEG", quality=100)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path, output_file_path)
        )
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(png_file_path):
            os.remove(png_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
