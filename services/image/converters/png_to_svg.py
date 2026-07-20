import tempfile
import os 
import base64
from PIL import Image
import cairosvg

def png_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".png":
        raise ValueError("Expected a PNG file (.png)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".svg"
    try:
        with Image.open(temp_file_path) as image:
            width, height = image.size

        with open(temp_file_path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode("utf-8")

        svg_content = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n'
            f'  <image href="data:image/png;base64,{img_base64}" '
            f'width="{width}" height="{height}"/>\n'
            f'</svg>'
        )

        with open(output_file_path, "w", encoding="utf-8") as f:
            f.write(svg_content)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)


def svg_to_png(
    file_bytes: bytes,
    original_filename: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Render an SVG to PNG. Optional width/height set the output size in px:
    one dimension alone scales proportionally (cairosvg derives the other from
    the SVG's aspect ratio); both together set the exact canvas size."""
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

    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    try:
        cairosvg.svg2png(url=temp_file_path, write_to=output_file_path, **size_kwargs)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)

