import tempfile 
import os 
from PIL import Image 


def png_to_webp(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".png":
        raise ValueError("Expected a PNG file (.png)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".webp"
    try:
        image = Image.open(temp_file_path)

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        image.save(output_file_path, format="WEBP", quality=100)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)

def webp_to_png(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".webp":
        raise ValueError("Expected a WebP file (.webp)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    try:
        image = Image.open(temp_file_path)

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        image.save(output_file_path, format="PNG")

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
