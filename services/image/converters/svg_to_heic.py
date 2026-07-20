import tempfile 
import os 
from PIL import Image 
import cairosvg 
import pillow_heif

pillow_heif.register_heif_opener()

def svg_to_heic(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".svg":
        raise ValueError("Expected an SVG file (.svg)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    png_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    output_file_path = os.path.splitext(temp_file_path)[0] + ".heic"
    try:
        # Step 1: SVG → PNG using cairosvg
        cairosvg.svg2png(url=temp_file_path, write_to=png_file_path)

        # Step 2: PNG → HEIC using PIL + pillow_heif
        with Image.open(png_file_path) as image:
            if image.mode == "RGBA":
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[3])
                background.save(output_file_path, format="HEIF", quality=100)
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(output_file_path, format="HEIF", quality=100)

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


def heic_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".heic", ".heif"):
        raise ValueError("Expected a HEIC file (.heic or .heif)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".svg"
    try:
        with Image.open(temp_file_path) as image:
            width, height = image.size

            # Save as PNG bytes for base64 embedding
            import io
            png_buffer = io.BytesIO()
            if image.mode not in ("RGBA", "RGB"):
                image = image.convert("RGB")
            image.save(png_buffer, format="PNG")
            png_bytes = png_buffer.getvalue()

        import base64
        img_base64 = base64.b64encode(png_bytes).decode("utf-8")

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
