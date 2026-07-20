import tempfile 
import os 
from PIL import Image 

def jpeg_to_webp (file_bytes: bytes, original_filename: str) -> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg"):
        raise ValueError("Expected a JPEG file (.jpg or .jpeg)")
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
         temp_file.write(file_bytes)
         temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".webp"
    try:
        image = Image.open(temp_file_path)

        if image.mode != "RGB":
            image = image.convert("RGB")

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


def webp_to_jpeg(file_bytes: bytes, original_filename:str) ->bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in (".webp"):
        raise ValueError("Expected a WEBP file (.webp)")
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
         temp_file.write(file_bytes)
         temp_file_path = temp_file.name

    output_file_path = os.path.splitext(temp_file_path)[0] + ".jpeg"
    try:
        image = Image.open(temp_file_path)

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