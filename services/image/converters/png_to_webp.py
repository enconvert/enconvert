import os

from ._limits import describe_image_error, ensure_pixel_limit, write_temp_file


def png_to_webp(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".png":
        raise ValueError("Expected a PNG file (.png)")

    temp_file_path = write_temp_file(file_bytes, ext)

    output_file_path = os.path.splitext(temp_file_path)[0] + ".webp"
    try:
        image = Image.open(temp_file_path)
        # Header-only decompression-bomb gate: reject before any decode.
        ensure_pixel_limit(image)

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        image.save(output_file_path, format="WEBP", quality=100)

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
        if os.path.exists(output_file_path):
            os.remove(output_file_path)

def webp_to_png(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".webp":
        raise ValueError("Expected a WebP file (.webp)")

    temp_file_path = write_temp_file(file_bytes, ext)

    output_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    try:
        image = Image.open(temp_file_path)
        # Header-only decompression-bomb gate: reject before any decode.
        ensure_pixel_limit(image)

        if image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        image.save(output_file_path, format="PNG")

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
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
