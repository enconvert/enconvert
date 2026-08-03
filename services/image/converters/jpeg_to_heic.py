import os

from ._limits import ensure_pixel_limit, write_temp_file


def jpeg_to_heic(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise ValueError("Expected a JPEG file (.jpg or .jpeg)")

    temp_file_path = write_temp_file(file_bytes, ext)

    output_file_path = os.path.splitext(temp_file_path)[0] + ".heic"
    try:
        image = Image.open(temp_file_path)
        # Header-only decompression-bomb gate: reject before any decode.
        ensure_pixel_limit(image)

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
        if os.path.exists(output_file_path):
            os.remove(output_file_path)


def heic_to_jpeg(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".heic", ".heif"):
        raise ValueError("Expected a HEIC file (.heic or .heif)")

    temp_file_path = write_temp_file(file_bytes, ext)

    output_file_path = os.path.splitext(temp_file_path)[0] + ".jpeg"
    try:
        image = Image.open(temp_file_path)
        # Header-only decompression-bomb gate: reject before any decode.
        ensure_pixel_limit(image)

        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.save(output_file_path, format="JPEG", quality=100)

        with open(output_file_path, "rb") as f:
            return f.read()
    except Exception as e:
        raise ValueError(f"Image conversion failed: {str(e)}")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)
