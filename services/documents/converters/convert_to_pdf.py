import tempfile
import subprocess
import os

from services.conversion_errors import ConversionTimeoutError

_UNOCONVERT_TIMEOUT_S = 120


def convert_to_pdf(
    file_bytes: bytes,
    original_filename: str  
)-> bytes:
    ext = os.path.splitext(original_filename or "")[1].lower()
    if not ext:
        raise ValueError("cannot determine file type:missing file extension")
    # Path bound before the write, and unlinked on a failed write (e.g.
    # ENOSPC): the write happens before the try/finally below is entered, so
    # a failure there would otherwise leak the temp file forever.
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    temp_file_path = temp_file.name
    try:
        with temp_file:
            temp_file.write(file_bytes)
    except BaseException:
        os.unlink(temp_file_path)
        raise

    output_file_path = temp_file_path.replace(ext, ".pdf")
    try:
        result=subprocess.run(["unoconvert","--convert-to", "pdf", temp_file_path, output_file_path], capture_output=True , text=True , timeout=_UNOCONVERT_TIMEOUT_S)
        if result.returncode !=0:
            error_message=result.stderr.strip() or "unoserver conversion failed"
            raise ValueError(f"Document Conversion Failed:{error_message}")
        with open(output_file_path,"rb") as f:
            return f.read()
    except subprocess.TimeoutExpired as exc:
        # Previously propagated uncaught -> HTTP 500 (and leaked the argv and
        # temp paths into the response body). An upstream unoserver that never
        # answered is a 504, not a gateway fault.
        raise ConversionTimeoutError(
            f"Document conversion timed out after {_UNOCONVERT_TIMEOUT_S} seconds."
        ) from exc
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        if os.path.exists(output_file_path):
            os.remove(output_file_path)

def doc_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def excel_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def ppt_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def odt_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def ods_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def odp_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def ots_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def pages_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)

def numbers_to_pdf(file_bytes:bytes, original_filename:str)->bytes:
    return convert_to_pdf(file_bytes,original_filename)
