import os

from ._limits import describe_image_error, ensure_pixel_limit, write_temp_file


def convert_image(file_bytes: bytes, ext: str, output_format: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    from PIL import Image
    output_ext = f".{output_format}"

    temp_file_path = write_temp_file(file_bytes, ext)

    base_path = os.path.splitext(temp_file_path)[0]
    output_file_path = base_path + output_ext
    try:
        image = Image.open(temp_file_path)
        # Header-only decompression-bomb gate: reject before any decode.
        ensure_pixel_limit(image)

        # JPEG doesn't support transparency — flatten alpha onto white background
        if output_format == "jpeg" and image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[3])
            image = background
        elif output_format == "jpeg" and image.mode != "RGB":
            image = image.convert("RGB")
        elif output_format == "png" and image.mode not in ("RGBA", "RGB"):
            image = image.convert("RGBA")

        save_kwargs = {}
        if output_format == "jpeg":
            save_kwargs["quality"] = 100

        image.save(output_file_path, format=output_format.upper(), **save_kwargs)

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


def jpeg_to_png(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise ValueError("Expected a JPEG file (.jpg or .jpeg)")
    return convert_image(file_bytes, ext, "png")


def png_to_jpeg(file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".png":
        raise ValueError("Expected a PNG file (.png)")
    return convert_image(file_bytes, ext, "jpeg")
