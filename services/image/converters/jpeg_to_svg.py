import tempfile
import os
import base64
from PIL import Image
import cairosvg


def jpeg_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise ValueError("Expected a JPEG file (.jpg or .jpeg)")

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
            f'  <image href="data:image/jpeg;base64,{img_base64}" '
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


def svg_to_jpeg(
    file_bytes: bytes,
    original_filename: str,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """Render an SVG to JPEG. Optional width/height set the output size in px:
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
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(png_file_path):
            os.remove(png_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
