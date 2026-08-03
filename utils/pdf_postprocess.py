"""
PDF post-processing utilities (grayscale conversion via Ghostscript).
"""
import asyncio
import tempfile
from pathlib import Path

import logging

logger = logging.getLogger(__name__)

# Hard cap on a single Ghostscript run: generous for legitimate PDFs, but
# without it a pathological input pins CPU/RAM on the 1GB droplet indefinitely.
_GS_TIMEOUT_SECONDS = 120


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
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            # Kill + reap: an orphaned gs would otherwise keep running in the
            # worker's cgroup long after the request is gone.
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                "Ghostscript grayscale conversion timed out "
                f"after {_GS_TIMEOUT_SECONDS}s"
            )
        except asyncio.CancelledError:
            # Task cancellation (client disconnect / shutdown) does not kill
            # the child on its own — reap it before propagating.
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Ghostscript grayscale conversion failed: {error_msg}")

        return out_path.read_bytes()
    finally:
        inp_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
