"""
PDF post-processing utilities (grayscale conversion via Ghostscript).
"""
import asyncio
import tempfile
from pathlib import Path

import logging

logger = logging.getLogger(__name__)


async def convert_to_grayscale(pdf_bytes: bytes) -> bytes:
    """
    Convert a color PDF to grayscale using Ghostscript.

    Args:
        pdf_bytes: Input PDF content as bytes.

    Returns:
        Grayscale PDF content as bytes.

    Raises:
        RuntimeError: If Ghostscript is not installed or conversion fails.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as inp, \
         tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out:
        inp_path = Path(inp.name)
        out_path = Path(out.name)

    try:
        inp_path.write_bytes(pdf_bytes)

        cmd = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-sColorConversionStrategy=Gray",
            "-dProcessColorModel=/DeviceGray",
            "-dCompatibilityLevel=1.4",
            "-dBATCH",
            "-dNOPAUSE",
            "-dQUIET",
            f"-sOutputFile={out_path}",
            str(inp_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Ghostscript grayscale conversion failed: {error_msg}")

        return out_path.read_bytes()
    finally:
        inp_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
