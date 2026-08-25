import os

from ._limits import ensure_pixel_limit, svg_render_cap_kwargs, write_temp_file
from utils.error_capture import describe_image_error


def svg_to_heic(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    import cairosvg
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext != ".svg":
        raise ValueError("Expected an SVG file (.svg)")

    # This endpoint takes no width/height, so always cap the intrinsic render:
    # a tiny SVG declaring a huge canvas would otherwise make cairo allocate a
    # multi-GB surface.
    size_kwargs = svg_render_cap_kwargs(file_bytes)

    temp_file_path = write_temp_file(file_bytes, ".svg")

    png_file_path = os.path.splitext(temp_file_path)[0] + ".png"
    output_file_path = os.path.splitext(temp_file_path)[0] + ".heic"
    try:
        # Step 1: SVG → PNG using cairosvg
        cairosvg.svg2png(url=temp_file_path, write_to=png_file_path, **size_kwargs)

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


def heic_to_svg(file_bytes: bytes, original_filename: str) -> bytes:
    # lazy import: keep the heavy native lib off idle RAM (B3)
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".heic", ".heif"):
        raise ValueError("Expected a HEIC file (.heic or .heif)")

    temp_file_path = write_temp_file(file_bytes, ext)

    try:
        with Image.open(temp_file_path) as image:
            # Header-only decompression-bomb gate: reject before any decode.
            ensure_pixel_limit(image)
            width, height = image.size

            # Save as PNG bytes for base64 embedding
            import io
            png_buffer = io.BytesIO()
            if image.mode not in ("RGBA", "RGB"):
                image = image.convert("RGB")
            image.save(png_buffer, format="PNG")

        import base64
        # bytes end-to-end: getbuffer() (no getvalue copy) + undecoded base64
        # joined once avoids the multiple full-size transient copies the
        # str + f-string assembly materialized. Output bytes are identical.
        img_b64 = base64.b64encode(png_buffer.getbuffer())

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
    except Exception as e:
        raise ValueError(
            "Image conversion failed: "
            + describe_image_error(e, temp_file_path, output_file_path)
        )
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
