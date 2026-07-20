from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from typing import Optional
from fastapi.responses import JSONResponse, Response
from datetime import datetime, timezone
import asyncio
import json as json_lib
import os

from api.deps import check_abuse_patterns, check_rate_limits, check_conversion_limit, check_storage_limit, check_feature_access, get_current_user, validate_file_size, check_crawl_access, check_batch_limit

from monitoring.metrics import log_activity_start, log_batch_activity_start, update_activity_status
from monitoring import posthog_client
from utils.storage import upload_to_gcs
from utils.processor import handle_url_conversion, process_batch_async, validate_auth_cookies_headers, discover_website_urls
from services.v2_engine.url_safety import assert_public_http_url
from services.page_quality.instrumentation import header_opts_out
from utils.conversion_jobs import get_conversion_job
from utils.batch_status import get_batch_status
from utils.storage import generate_presigned_url
from utils.validators import ALLOWED_EXTENSIONS, validate_file_format, validate_file_content, validate_svg_dimensions
from utils.sitemap import fetch_sitemap_urls
from utils.subscription import get_project_owner_email
from utils.email_notifier import send_job_completion_email
import uuid

from services.lightweight import converters as lightweight_converters
from services.documents import converters as document_converters
from models import PdfOptions
from utils.pdf_postprocess import convert_to_grayscale
from utils.pdf_helpers import assert_geometry_supported
from services.image import converters as image_converters
from services.markdown import convert_to_markdown as anything_to_markdown_converter
from services.pdf import (
    assert_options_supported,
    convert_to_pdf as anything_to_pdf_converter,
)
from services.conversion_errors import ConversionTimeoutError, UnsupportedOptionError


import logging

logger = logging.getLogger(__name__)

router = APIRouter()

# Cap concurrent in-gateway CPU conversions (image/document/WeasyPrint) so a
# burst can't exhaust the ~1GB droplet. The browser path has its own
# Semaphore(1) in browser_manager; this bounds the non-browser converters,
# which otherwise run unbounded via asyncio.to_thread. Tune via env.
_MAX_CONCURRENT_CONVERSIONS = int(os.getenv("MAX_CONCURRENT_CONVERSIONS", "1"))
_CONVERSION_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_CONVERSIONS)

# Fail-fast admission gate: requests queued at the semaphore hold their full
# upload bytes in RAM (bodies are read before dispatch), so an unbounded queue
# is an OOM vector — ~10 queued 150MB Business uploads exceed the droplet's
# 1GB. Counts running + waiting conversions; excess gets 503 + Retry-After.
_MAX_PENDING_CONVERSIONS = int(os.getenv("MAX_PENDING_CONVERSIONS", "10"))
_pending_conversions = 0


def _capture_upload_rejected(
    user: dict, request: Request, endpoint: str, provided_ext: str,
    reason: str, detail: str = None,
) -> None:
    """Emit upload_rejected_bad_format (extension or magic-byte mismatch)."""
    posthog_client.capture(
        posthog_client.distinct_id_for_project(user["id"]),
        "upload_rejected_bad_format",
        {
            "endpoint": f"/v1/convert/{endpoint}",
            "provided_extension": provided_ext,
            "reason": reason,
            "detail": detail,
            "plan_tier": user.get("subscription", {}).get("plan_slug", "free"),
            "source": posthog_client.source_from(user, request),
        },
        posthog_client.group_of(user["id"]),
    )


def _capture_batch_requested(
    user: dict, request: Request, endpoint: str, url_count: int,
    total_discovered: int, discovery_method: str,
) -> None:
    """Emit batch_conversion_requested for the website-capture endpoints."""
    posthog_client.capture(
        posthog_client.distinct_id_for_project(user["id"]),
        "batch_conversion_requested",
        {
            "endpoint": f"/v1/convert/{endpoint}",
            "url_count": url_count,
            "total_discovered": total_discovered,
            "discovery_method": discovery_method,
            "plan_tier": user.get("subscription", {}).get("plan_slug", "free"),
            "key_type": user.get("key_type", "unknown"),
            "source": posthog_client.source_from(user, request),
        },
        posthog_client.group_of(user["id"]),
    )


def _capture_conversion_failed(
    distinct_id: str, group: dict, endpoint: str, converter_module: str,
    source_format: str, target_format: str, input_size: int, duration: float,
    plan_tier: str, key_type: str, source: str, *, error_type: str,
    error_code: int,
) -> None:
    """Emit conversion_failed with a consistent property shape."""
    posthog_client.capture(distinct_id, "conversion_failed", {
        "endpoint": endpoint,
        "converter_module": converter_module,
        "source_format": source_format,
        "target_format": target_format,
        "input_file_size_bytes": input_size,
        "duration_ms": int(duration * 1000),
        "is_async": False,
        "is_batch": False,
        "plan_tier": plan_tier,
        "key_type": key_type,
        "source": source,
        "error_type": error_type,
        "error_code": error_code,
    }, group)

# Map endpoint names to their converter function and output extension.
# Converters not yet implemented (documents, media, AI) are omitted —
# forward_to_backend will return 503 for unknown endpoints.
CONVERTER_MAP = {
    "json-to-xml": {"fn": lightweight_converters.json_to_xml, "output_ext": ".xml"},
    "xml-to-json": {"fn": lightweight_converters.xml_to_json, "output_ext": ".json"},
    "json-to-yaml": {"fn": lightweight_converters.json_to_yaml, "output_ext": ".yaml"},
    "yaml-to-json": {"fn": lightweight_converters.yaml_to_json, "output_ext": ".json"},
    "csv-to-json": {"fn": lightweight_converters.csv_to_json, "output_ext": ".json"},
    "json-to-csv": {"fn": lightweight_converters.json_to_csv, "output_ext": ".csv"},
    "markdown-to-html": {"fn": lightweight_converters.markdown_to_html, "output_ext": ".html"},
    "markdown-to-pdf": {"fn": lightweight_converters.markdown_to_pdf, "output_ext": ".pdf", "accepts_pdf_options": True},
    # Anything -> Markdown (RAG ingestion building block): async converter that
    # dispatches by file extension and offloads CPU-bound extraction to threads.
    "anything-to-markdown": {"fn": anything_to_markdown_converter, "output_ext": ".md", "needs_filename": True},
    # Anything -> PDF: dispatches by extension to LibreOffice (office/legacy/csv/
    # rtf), WeasyPrint (html/markdown/txt/epub), Pillow (raster images) or
    # CairoSVG (svg). needs_filename because dispatch and the office import
    # filter both key off the extension.
    "anything-to-pdf": {"fn": anything_to_pdf_converter, "output_ext": ".pdf", "needs_filename": True, "accepts_pdf_options": True},
    "html-to-pdf": {"fn": lightweight_converters.html_to_pdf, "output_ext": ".pdf", "accepts_pdf_options": True},
    "doc-to-pdf": {"fn": document_converters.doc_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "excel-to-pdf": {"fn": document_converters.excel_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "ppt-to-pdf": {"fn": document_converters.ppt_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "odt-to-pdf": {"fn": document_converters.odt_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "ods-to-pdf": {"fn": document_converters.ods_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "odp-to-pdf": {"fn": document_converters.odp_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "ots-to-pdf": {"fn": document_converters.ots_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "pages-to-pdf": {"fn": document_converters.pages_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "numbers-to-pdf": {"fn": document_converters.numbers_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    # "key-to-pdf": {"fn": document_converters.key_to_pdf, "output_ext": ".pdf", "needs_filename": True},
    "json-to-toml": {"fn": lightweight_converters.json_to_toml, "output_ext": ".toml"},
    "toml-to-json": {"fn": lightweight_converters.toml_to_json, "output_ext": ".json"},
    "csv-to-xml": {"fn": lightweight_converters.csv_to_xml, "output_ext": ".xml"},
    "xml-to-csv": {"fn": lightweight_converters.xml_to_csv, "output_ext": ".csv"},
    "jpeg-to-png": {"fn": image_converters.jpeg_to_png, "output_ext": ".png", "needs_filename": True},
    "png-to-jpeg": {"fn": image_converters.png_to_jpeg, "output_ext": ".jpeg", "needs_filename": True},
    "jpeg-to-svg": {"fn": image_converters.jpeg_to_svg, "output_ext": ".svg", "needs_filename": True},
    "svg-to-jpeg": {"fn": image_converters.svg_to_jpeg, "output_ext": ".jpeg", "needs_filename": True},
    "jpeg-to-heic": {"fn": image_converters.jpeg_to_heic, "output_ext": ".heic", "needs_filename": True},
    "heic-to-jpeg": {"fn": image_converters.heic_to_jpeg, "output_ext": ".jpeg", "needs_filename": True},
    "jpeg-to-webp": {"fn": image_converters.jpeg_to_webp, "output_ext": ".webp", "needs_filename": True},
    "webp-to-jpeg": {"fn": image_converters.webp_to_jpeg, "output_ext": ".jpeg", "needs_filename": True},
    "png-to-svg": {"fn": image_converters.png_to_svg, "output_ext": ".svg", "needs_filename": True},
    "svg-to-png": {"fn": image_converters.svg_to_png, "output_ext": ".png", "needs_filename": True},
    "png-to-heic": {"fn": image_converters.png_to_heic, "output_ext": ".heic", "needs_filename": True},
    "heic-to-png": {"fn": image_converters.heic_to_png, "output_ext": ".png", "needs_filename": True},
    "png-to-webp": {"fn": image_converters.png_to_webp, "output_ext": ".webp", "needs_filename": True},
    "webp-to-png": {"fn": image_converters.webp_to_png, "output_ext": ".png", "needs_filename": True},
    "svg-to-heic": {"fn": image_converters.svg_to_heic, "output_ext": ".heic", "needs_filename": True},
    "heic-to-svg": {"fn": image_converters.heic_to_svg, "output_ext": ".svg", "needs_filename": True},
    "svg-to-webp": {"fn": image_converters.svg_to_webp, "output_ext": ".webp", "needs_filename": True},
    "webp-to-svg": {"fn": image_converters.webp_to_svg, "output_ext": ".svg", "needs_filename": True}, 
    "heic-to-webp": {"fn": image_converters.heic_to_webp, "output_ext": ".webp", "needs_filename": True},
    "webp-to-heic": {"fn": image_converters.webp_to_heic, "output_ext": ".heic", "needs_filename": True},
    "pdf-to-jpeg": {"fn": image_converters.pdf_to_jpeg, "output_ext": ".jpeg", "needs_filename": True},
    # Same-format compression: output_ext None means "keep the input's
    # extension" (resolved in forward_to_backend). PNG in -> PNG out, etc.
    "compress-image": {"fn": image_converters.compress_image, "output_ext": None, "needs_filename": True},
}


def _parse_office_pdf_options(
    pdf_options: Optional[str], filename: Optional[str], endpoint: str
) -> Optional[PdfOptions]:
    """Parse pdf_options for a LibreOffice-backed endpoint, rejecting geometry.

    Shared by the nine unoconvert endpoints (doc/excel/ppt/odt/ods/odp/ots/
    pages/numbers -to-pdf). Unlike anything-to-pdf, which resolves an engine per
    extension, these route to LibreOffice for EVERY input they accept, and
    ``unoconvert`` exposes only PDF export-filter options — page size,
    orientation and margins come from the source document's own page style,
    applied at layout time before export. So explicitly-set geometry is
    unsupported here and returns 400 instead of being silently discarded.

    Rejecting in the route (not in the converter) keeps a pure client error from
    burning quota and logging a Failed activity row — the same reason
    anything-to-pdf gates in its handler. ``grayscale`` is untouched: it is a
    post-process applied by forward_to_backend after conversion.
    """
    if not pdf_options:
        return None
    try:
        parsed = PdfOptions(**json_lib.loads(pdf_options))
    except (json_lib.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid pdf_options: {str(e)}")

    ext = os.path.splitext(filename or "")[1].lower()
    # An extension this endpoint does not accept is validate_file_format's 400 to
    # raise, not ours — same precedence as services/pdf's gate, which stays
    # silent on unknown extensions so the format error wins.
    if ext in ALLOWED_EXTENSIONS.get(endpoint, ()):
        try:
            assert_geometry_supported(parsed, fmt=ext, engine="LibreOffice")
        except UnsupportedOptionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return parsed


async def forward_to_backend(
    request: Request,
    endpoint: str,
    user: dict,
    file_content: bytes = None,
    original_filename: str = None,
    output_filename: str = None,
    direct_download: bool = True,
    job_id: str = None,
    pdf_options: PdfOptions = None,
    converter_kwargs: dict = None,
):
    """
    Run the conversion directly in-process and store result in storage.

    Args:
        request: The FastAPI request object
        endpoint: The conversion endpoint (e.g., 'json-to-xml')
        user: User dict with id and tier
        file_content: File bytes for file upload requests
        original_filename: Original filename of the uploaded file (preserves input name)
        output_filename: Custom output filename specified by user (overrides default)
        direct_download: If True (default), return file content directly. If False, return JSON metadata.
        converter_kwargs: Endpoint-specific extra kwargs already validated by
            the calling route (e.g. width/height for svg-to-*, target_size_kb
            for compress-image). Routes and CONVERTER_MAP are edited together,
            so a route only passes kwargs its own converter accepts.
    """

    check_conversion_limit(user)
    check_storage_limit(user)

    # Extension gate (declared type) followed by a conservative content
    # (magic-byte) gate. Both rejections surface as upload_rejected_bad_format
    # with a distinguishing reason.
    provided_ext = os.path.splitext(original_filename or "")[1].lower()
    try:
        validate_file_format(endpoint, original_filename)
    except HTTPException:
        _capture_upload_rejected(user, request, endpoint, provided_ext, "extension_not_allowed")
        raise
    content_mismatch = validate_file_content(endpoint, original_filename, file_content or b"")
    if content_mismatch:
        _capture_upload_rejected(
            user, request, endpoint, provided_ext, "magic_byte_mismatch",
            detail=content_mismatch,
        )
        raise HTTPException(
            status_code=400,
            detail=f"File content does not match the '{endpoint}' input type.",
        )

    converter_info = CONVERTER_MAP.get(endpoint)
    if not converter_info:
        raise HTTPException(status_code=503, detail=f"Converter not available: {endpoint}")

    full_endpoint = f"/v1/convert/{endpoint}"
    converter_module = posthog_client.converter_module_of(converter_info["fn"])
    source_format, target_format = posthog_client.split_endpoint_formats(endpoint)
    source = posthog_client.source_from(user, request)
    distinct_id = posthog_client.distinct_id_for_project(user["id"])
    group = posthog_client.group_of(user["id"])
    plan_tier = user.get("subscription", {}).get("plan_slug", user.get("plan_slug", "free"))
    key_type = user.get("key_type", "unknown")

    # Compute input size upfront
    input_size = len(file_content) if file_content is not None else 0

    # Log activity as In Progress immediately
    activity_id = await log_activity_start(
        project_id=user["id"], endpoint=full_endpoint, input_file_size=input_size
    )
    # BUG FIX A: expose the activity id (and endpoint) to the TimeoutMiddleware
    # through the shared ASGI scope, so a 300s timeout can transition this row
    # to Failed instead of leaving it 'In Progress' forever.
    request.state.activity_id = activity_id
    request.state.endpoint = full_endpoint

    posthog_client.capture(distinct_id, "conversion_requested", {
        "endpoint": full_endpoint,
        "converter_module": converter_module,
        "source_format": source_format,
        "target_format": target_format,
        "input_file_size_bytes": input_size,
        "is_async": False,
        "is_batch": False,
        "plan_tier": plan_tier,
        "key_type": key_type,
        "source": source,
    }, group)

    # Create conversion job row (enables polling on timeout for any key type)
    needs_polling = True
    if needs_polling and job_id:
        from utils.conversion_jobs import create_conversion_job
        create_conversion_job(job_id, str(user["id"]))

    start_time = datetime.now(timezone.utc)

    try:
        # Call converter directly — no HTTP round-trip
        converter_fn = converter_info["fn"]
        output_ext = converter_info["output_ext"]
        if output_ext is None:
            # Same-format endpoints (compress-image): keep the input extension.
            output_ext = os.path.splitext(original_filename or "")[1].lower() or ".bin"

        is_pdf_endpoint = output_ext == ".pdf"

        needs_filename = converter_info.get("needs_filename", False)

        # Fail-fast admission gate + bounded concurrency (see module constants).
        # The pending counter spans the semaphore wait AND the CPU work, capping
        # how many request bodies sit in RAM at once. Race-safe: no await
        # between the check and the increment. The semaphore is held only
        # around the CPU work, released before the S3 upload below.
        global _pending_conversions
        if _pending_conversions >= _MAX_PENDING_CONVERSIONS:
            raise HTTPException(
                status_code=503,
                detail="Server is at capacity. Please retry shortly.",
                headers={"Retry-After": "10"},
            )
        _pending_conversions += 1
        try:
            # Build the call explicitly. The previous if/elif made needs_filename
            # and pdf_options mutually exclusive, so a converter needing both
            # (anything-to-pdf) silently lost its validated options. Only
            # converters opting in via `accepts_pdf_options` receive them; every
            # other endpoint is invoked exactly as before.
            call_args = [file_content]
            if needs_filename:
                call_args.append(original_filename)
            call_kwargs = {}
            if converter_info.get("accepts_pdf_options") and pdf_options:
                call_kwargs["pdf_options"] = pdf_options
            if converter_kwargs:
                call_kwargs.update(converter_kwargs)

            async with _CONVERSION_SEMAPHORE:
                if asyncio.iscoroutinefunction(converter_fn):
                    output_bytes = await converter_fn(*call_args, **call_kwargs)
                else:
                    output_bytes = await asyncio.to_thread(
                        converter_fn, *call_args, **call_kwargs
                    )

                # Post-processing: grayscale (for file-upload PDF endpoints)
                if is_pdf_endpoint and pdf_options and pdf_options.grayscale:
                    output_bytes = await convert_to_grayscale(output_bytes)
        finally:
            _pending_conversions -= 1

        # Ensure output is bytes
        if isinstance(output_bytes, str):
            output_bytes = output_bytes.encode("utf-8")

        # Detect if output is a ZIP archive (overrides output_ext)
        if output_bytes[:4] == b'PK\x03\x04':
            output_ext = ".zip"

        # Generate output filename
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]

        if output_filename:
            base_name = output_filename
            for ext in [output_ext, output_ext.upper()]:
                if base_name.lower().endswith(ext.lower()):
                    base_name = base_name[:-len(ext)]
                    break
            filename = f"{base_name}_{timestamp}{output_ext}"
        elif original_filename:
            base_name, _ = os.path.splitext(original_filename)
            filename = f"{base_name}_{timestamp}{output_ext}"
        else:
            filename = f"output_{timestamp}{output_ext}"

        upload_result = upload_to_gcs(
            file_bytes=output_bytes, user_id=user["id"],
            endpoint=endpoint, original_filename=filename
        )

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        # Mark activity as Success
        try:
            await update_activity_status(
                activity_id, "Success",
                output_file_size=upload_result["file_size"],
                object_key=upload_result["object_key"], duration=duration
            )
        except Exception:
            pass

        posthog_client.capture(distinct_id, "conversion_completed", {
            "endpoint": full_endpoint,
            "converter_module": converter_module,
            "source_format": source_format,
            "target_format": target_format,
            "input_file_size_bytes": input_size,
            "output_file_size_bytes": upload_result["file_size"],
            "duration_ms": int(duration * 1000),
            "is_async": False,
            "is_batch": False,
            "plan_tier": plan_tier,
            "key_type": key_type,
            "source": source,
        }, group)

        # Update conversion job on success
        if needs_polling and job_id:
            from utils.conversion_jobs import update_conversion_job_success
            update_conversion_job_success(job_id, str(user["id"]), upload_result["object_key"])
        if needs_polling:
            # Browser-based requests (widgets/playground/dashboard): return a
            # presigned URL so the client can fetch the file directly from CDN.
            # This avoids streaming large files through the gateway which can
            # exceed reverse-proxy idle timeouts.
            presigned_url = generate_presigned_url(upload_result["object_key"], str(user["id"]))
            return JSONResponse(
                status_code=200,
                content={
                    "presigned_url": presigned_url,
                    "object_key": upload_result["object_key"],
                    "filename": filename,
                    "file_size": upload_result["file_size"],
                    "conversion_time_seconds": duration,
                    "job_id": job_id,
                },
                headers={
                    "X-Object-Key": upload_result["object_key"],
                    "X-File-Size": str(upload_result["file_size"]),
                    "X-Conversion-Time": str(duration),
                    "X-Filename": filename,
                }
            )

        if direct_download:
            # Private keys with explicit direct_download=True: stream raw bytes
            ext_media_types = {
                ".pdf": "application/pdf",
                ".png": "image/png",
                ".jpeg": "image/jpeg",
                ".jpg": "image/jpeg",
                ".svg": "image/svg+xml",
                ".xml": "application/xml",
                ".json": "application/json",
                ".html": "text/html",
                ".md": "text/markdown; charset=utf-8",
                ".yaml": "application/x-yaml",
                ".yml": "application/x-yaml",
                ".csv": "text/csv",
                ".toml": "application/toml",
                ".zip": "application/zip",
                ".heic": "image/heic",
                ".webp": "image/webp",
            }
            response_media_type = ext_media_types.get(output_ext, "application/octet-stream")
            return Response(
                content=output_bytes,
                media_type=response_media_type,
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"',
                    "X-Object-Key": upload_result["object_key"],
                    "X-File-Size": str(upload_result["file_size"]),
                    "X-Conversion-Time": str(duration),
                    "X-Filename": filename,
                }
            )

        # For private keys without direct_download, return JSON metadata
        presigned_url = generate_presigned_url(upload_result["object_key"], str(user["id"]))
        return JSONResponse(
            status_code=200,
            content={
                "presigned_url": presigned_url,
                "object_key": upload_result["object_key"],
                "filename": upload_result["filename"],
                "file_size": upload_result["file_size"],
                "conversion_time_seconds": duration
            }
        )

    except HTTPException as e:
        # Mark activity as Failed, then re-raise
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            await update_activity_status(activity_id, "Failed", duration=duration)
        except Exception:
            pass
        _capture_conversion_failed(
            distinct_id, group, full_endpoint, converter_module, source_format,
            target_format, input_size, duration, plan_tier, key_type, source,
            error_type="http_exception", error_code=e.status_code,
        )
        if needs_polling and job_id:
            from utils.conversion_jobs import update_conversion_job_failure
            update_conversion_job_failure(job_id, str(e.detail))
        raise
    except ConversionTimeoutError as e:
        # The unoserver/LibreOffice subprocess blew its own time budget. That is
        # an upstream timeout, not bad input and not a gateway bug -> 504.
        # MUST stay above `except Exception` (which would 500 it); kept above
        # `except ValueError` so the mapping survives any future reparenting.
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            await update_activity_status(activity_id, "Failed", duration=duration)
        except Exception:
            pass
        _capture_conversion_failed(
            distinct_id, group, full_endpoint, converter_module, source_format,
            target_format, input_size, duration, plan_tier, key_type, source,
            error_type="conversion_timeout", error_code=504,
        )
        if needs_polling and job_id:
            from utils.conversion_jobs import update_conversion_job_failure
            update_conversion_job_failure(job_id, str(e))
        raise HTTPException(status_code=504, detail=str(e))
    except ValueError as e:
        # Converter raised a validation error (bad input)
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            await update_activity_status(activity_id, "Failed", duration=duration)
        except Exception:
            pass
        _capture_conversion_failed(
            distinct_id, group, full_endpoint, converter_module, source_format,
            target_format, input_size, duration, plan_tier, key_type, source,
            error_type="value_error", error_code=400,
        )
        if needs_polling and job_id:
            from utils.conversion_jobs import update_conversion_job_failure
            update_conversion_job_failure(job_id, str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            await update_activity_status(activity_id, "Failed", duration=duration)
        except Exception:
            pass
        _capture_conversion_failed(
            distinct_id, group, full_endpoint, converter_module, source_format,
            target_format, input_size, duration, plan_tier, key_type, source,
            error_type=type(e).__name__, error_code=500,
        )
        if needs_polling and job_id:
            from utils.conversion_jobs import update_conversion_job_failure
            update_conversion_job_failure(job_id, str(e))
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")




# Browser converters (URL-based) - using shared processor
@router.post("/url-to-pdf")
async def url_to_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user)
):
    """Convert URL(s) to PDF. Supports sync/async, direct download, batch ZIP."""
    return await handle_url_conversion(
        request, background_tasks, user,
        endpoint="url-to-pdf", ext=".pdf", media_type="application/pdf"
    )


@router.post("/url-to-screenshot")
async def url_to_screenshot(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user)
):
    """Convert URL(s) to screenshot. Supports sync/async, direct download, batch ZIP."""
    return await handle_url_conversion(
        request, background_tasks, user,
        endpoint="url-to-screenshot", ext=".png", media_type="image/png"
    )


@router.post("/url-to-markdown")
async def url_to_markdown(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user)
):
    """Convert URL(s) to clean Markdown (article content + YAML frontmatter metadata)."""
    return await handle_url_conversion(
        request, background_tasks, user,
        endpoint="url-to-markdown", ext=".md", media_type="text/markdown; charset=utf-8"
    )


@router.post("/website-to-pdf")
async def website_to_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Convert all pages of a website to PDFs. Supports sitemap-only and full crawl modes. Async-only, returns ZIP."""
    data = await request.json()
    base_url = data.get("url", "").strip()
    if not base_url:
        raise HTTPException(400, "'url' must be provided")

    # SSRF: screen the crawl seed before we fetch its sitemap / crawl it.
    # Discovered URLs are re-screened at call_backend before rendering.
    await assert_public_http_url(base_url)

    notification_email = data.get("notification_email")
    if not notification_email:
        try:
            notification_email = get_project_owner_email(int(user["id"]))
        except (TypeError, ValueError):
            pass

    # Validate and gate auth/cookies/headers if provided
    validate_auth_cookies_headers(data)
    if data.get("auth") or data.get("cookies") or data.get("headers"):
        check_feature_access(user, "has_basic_auth")

    # Discover URLs based on user's plan and requested crawl mode
    # Starter (crawl_mode="sitemap") → sitemap only
    # Pro/Business (crawl_mode="full") → full algorithmic crawl
    urls, discovery_method, total_discovered = await discover_website_urls(data, user, notification_email, base_url)

    check_batch_limit(user, len(urls))
    check_conversion_limit(user, url_count=len(urls))
    check_storage_limit(user)

    batch_id = str(uuid.uuid4())
    activity_ids = await log_batch_activity_start(
        project_id=user["id"], endpoint="/v1/convert/url-to-pdf",
        urls=urls, batch_id=batch_id,
    )

    _capture_batch_requested(
        user, request, "website-to-pdf", len(urls), total_discovered, discovery_method,
    )

    no_capture = header_opts_out(request.headers)
    background_tasks.add_task(
        process_batch_async, urls, activity_ids, data,
        "url-to-pdf", user, ".pdf", batch_id, data.get("output_filename"),
        notification_email, data.get("callback_url"), no_capture,
    )

    return JSONResponse(
        status_code=202,
        content={
            "status": "processing",
            "batch_id": batch_id,
            "url_count": len(urls),
            "total_discovered": total_discovered,
            "discovery_method": discovery_method,
            "output_format": "zip",
        }
    )


@router.post("/website-to-screenshot")
async def website_to_screenshot(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Screenshot all pages of a website. Supports sitemap-only and full crawl modes. Async-only, returns ZIP."""
    data = await request.json()
    base_url = data.get("url", "").strip()
    if not base_url:
        raise HTTPException(400, "'url' must be provided")

    # SSRF: screen the crawl seed before we fetch its sitemap / crawl it.
    # Discovered URLs are re-screened at call_backend before rendering.
    await assert_public_http_url(base_url)

    notification_email = data.get("notification_email")
    if not notification_email:
        try:
            notification_email = get_project_owner_email(int(user["id"]))
        except (TypeError, ValueError):
            pass

    # Validate and gate auth/cookies/headers if provided
    validate_auth_cookies_headers(data)
    if data.get("auth") or data.get("cookies") or data.get("headers"):
        check_feature_access(user, "has_basic_auth")

    # Discover URLs based on user's plan and requested crawl mode
    # Starter (crawl_mode="sitemap") → sitemap only
    # Pro/Business (crawl_mode="full") → full algorithmic crawl
    urls, discovery_method, total_discovered = await discover_website_urls(data, user, notification_email, base_url)

    check_batch_limit(user, len(urls))
    check_conversion_limit(user, url_count=len(urls))
    check_storage_limit(user)

    batch_id = str(uuid.uuid4())
    activity_ids = await log_batch_activity_start(
        project_id=user["id"], endpoint="/v1/convert/url-to-screenshot",
        urls=urls, batch_id=batch_id,
    )

    _capture_batch_requested(
        user, request, "website-to-screenshot", len(urls), total_discovered, discovery_method,
    )

    no_capture = header_opts_out(request.headers)
    background_tasks.add_task(
        process_batch_async, urls, activity_ids, data,
        "url-to-screenshot", user, ".png", batch_id, data.get("output_filename"),
        notification_email, data.get("callback_url"), no_capture,
    )

    return JSONResponse(
        status_code=202,
        content={
            "status": "processing",
            "batch_id": batch_id,
            "url_count": len(urls),
            "total_discovered": total_discovered,
            "discovery_method": discovery_method,
            "output_format": "zip",
        }
    )


# Lightweight converters
@router.post("/json-to-xml")
async def json_to_xml(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert JSON to XML

    Args:
        file: JSON file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .xml extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/json-to-xml")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "json-to-xml", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/xml-to-json")
async def xml_to_json(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert XML to JSON

    Args:
        file: XML file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .json extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/xml-to-json")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "xml-to-json", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/json-to-yaml")
async def json_to_yaml(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert JSON to YAML

    Args:
        file: JSON file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .yaml extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/json-to-yaml")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "json-to-yaml", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/yaml-to-json")
async def yaml_to_json(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert YAML to JSON

    Args:
        file: YAML file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .json extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/yaml-to-json")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "yaml-to-json", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/csv-to-json")
async def csv_to_json(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert CSV to JSON

    Args:
        file: CSV file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .json extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/csv-to-json")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "csv-to-json", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/json-to-csv")
async def json_to_csv(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert json to csv

    Args:
        file: JSON file (.json) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "json-to-csv", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/json-to-toml")
async def json_to_toml(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert JSON to TOML

    Args:
        file: JSON file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .toml extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/json-to-toml")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "json-to-toml", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/toml-to-json")
async def toml_to_json(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert TOML to JSON

    Args:
        file: TOML file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .json extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/toml-to-json")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "toml-to-json", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/csv-to-xml")
async def csv_to_xml(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert CSV to XML

    Args:
        file: CSV file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .xml extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/csv-to-xml")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "csv-to-xml", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/xml-to-csv")
async def xml_to_csv(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert XML to CSV

    Args:
        file: XML file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .csv extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/xml-to-csv")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "xml-to-csv", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/markdown-to-html")
async def markdown_to_html(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert Markdown to HTML

    Args:
        file: Markdown file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .html extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/markdown-to-html")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "markdown-to-html", user, content, file.filename, output_filename, direct_download, job_id)

# Document converters
@router.post("/doc-to-pdf")
async def doc_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert Word document to PDF

    Args:
        file: Word document (.doc) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/doc-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "doc-to-pdf")

    return await forward_to_backend(request, "doc-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/excel-to-pdf")
async def excel_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert Excel spreadsheet to PDF

    Args:
        file: Excel spreadsheet (.excel) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/excel-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "excel-to-pdf")

    return await forward_to_backend(request, "excel-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/html-to-pdf")
async def html_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert HTML to PDF

    Args:
        file: HTML file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string with PDF output configuration (page size, margins, etc.)
    """
    #check_rate_limits(request, user, "/v1/convert/html-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = None
    if pdf_options:
        try:
            parsed_pdf_options = PdfOptions(**json_lib.loads(pdf_options))
        except (json_lib.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid pdf_options: {str(e)}")

    return await forward_to_backend(request, "html-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/markdown-to-pdf")
async def markdown_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert Markdown to PDF

    Args:
        file: Markdown file to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string with PDF output configuration (page size, margins, etc.)
    """
    #check_rate_limits(request, user, "/v1/convert/markdown-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = None
    if pdf_options:
        try:
            parsed_pdf_options = PdfOptions(**json_lib.loads(pdf_options))
        except (json_lib.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid pdf_options: {str(e)}")

    return await forward_to_backend(request, "markdown-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/anything-to-markdown")
async def anything_to_markdown(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert an uploaded file to clean Markdown (RAG ingestion building block).

    Auto-detects the input format by extension and dispatches to the right
    extractor: PDF, DOCX, PPTX, XLSX, CSV, HTML, EPUB, plain text/Markdown, and
    legacy/ODF office (DOC/PPT/XLS/ODT/ODS/ODP/RTF via unoserver). The output is
    a single .md file with heading-aware structure suited to semantic chunking.

    Args:
        file: The document to convert.
        output_filename: Optional custom output name (defaults to input name .md).
        direct_download: If True (default), return the file; else JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "anything-to-markdown", user, content, file.filename, output_filename, direct_download, job_id)


@router.post("/anything-to-pdf")
async def anything_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert an uploaded file of (almost) any format to PDF.

    File-based only (no URLs). Auto-detects the input format by extension and
    dispatches to the right engine: LibreOffice (DOC/DOCX/XLS/XLSX/PPT/PPTX/ODF/
    Pages/Numbers/RTF/CSV), WeasyPrint (HTML/Markdown/plain text/EPUB), Pillow
    (PNG/JPEG/GIF/BMP/TIFF/WebP/HEIC) or CairoSVG (SVG). A PDF upload is accepted
    and passed through, which — with pdf_options.grayscale — doubles as a PDF
    grayscale/normalise path.

    Args:
        file: The document or image to convert.
        output_filename: Optional custom output name (defaults to input name .pdf).
        direct_download: If True (default), return the file; else JSON metadata.
        pdf_options: Optional JSON string. Page geometry (page_size, page_width/
            height, orientation, margins, scale, header, footer) is honoured for
            HTML/Markdown/plain text/EPUB/image/SVG input. Office and PDF input
            support ``grayscale`` only — their page layout comes from the source
            document — and reject an explicitly-set geometry option with a 400.
    """
    # Pass `file` so the ceiling is checked against the bytes actually received
    # rather than the Content-Length header, which a chunked/HTTP2 client can
    # omit entirely. Must stay above the read() below.
    validate_file_size(request, user, file)

    content = await file.read()

    parsed_pdf_options = None
    if pdf_options:
        try:
            parsed_pdf_options = PdfOptions(**json_lib.loads(pdf_options))
        except (json_lib.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid pdf_options: {str(e)}")
        # Reject here rather than in the dispatcher: forward_to_backend burns
        # quota and logs a Failed activity row, neither of which a pure client
        # error should cause.
        try:
            assert_options_supported(file.filename, parsed_pdf_options)
        except UnsupportedOptionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return await forward_to_backend(
        request, "anything-to-pdf", user, content, file.filename,
        output_filename, direct_download, job_id, pdf_options=parsed_pdf_options,
    )

# Media converters
# @router.post("/image")
# async def convert_image(
#     request: Request,
#     file: UploadFile = File(...),
#     output_filename: Optional[str] = Form(None),
#     direct_download: Optional[bool] = Form(True),
#     job_id: Optional[str] = Form(None),
#     user: dict = Depends(get_current_user)
# ):
#     """Convert image format

#     Args:
#         file: Image file to convert
#         output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
#         direct_download: If True (default), return file directly. Set to False for JSON metadata.
#     """
#     #check_rate_limits(request, user, "/v1/convert/image")
    # check_abuse_patterns(request, user)
    # validate_file_size(request, user)

    # content = await file.read()
    # return await forward_to_backend(request, "image", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/thumbnail")
async def generate_thumbnail(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Generate thumbnail from media file

    Args:
        file: Media file to generate thumbnail from
        output_filename: Optional custom output filename. If not provided, uses input filename with thumbnail extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/thumbnail")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "thumbnail", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/video")
async def transcode_video(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Transcode video

    Args:
        file: Video file to transcode
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/video")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "video", user, content, file.filename, output_filename, direct_download, job_id)

# AI converters
@router.post("/ocr")
async def extract_text_ocr(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Extract text from image or PDF using OCR

    Args:
        file: Image or PDF file to extract text from
        output_filename: Optional custom output filename. If not provided, uses input filename with .txt extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/ocr")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "ocr", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/speech-to-text")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Transcribe audio to text

    Args:
        file: Audio file to transcribe
        output_filename: Optional custom output filename. If not provided, uses input filename with .txt extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/speech-to-text")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "speech-to-text", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/text-to-speech")
async def synthesize_speech(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert text to speech

    Args:
        file: Text file to convert to speech
        output_filename: Optional custom output filename. If not provided, uses input filename with audio extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/text-to-speech")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "text-to-speech", user, content, file.filename, output_filename, direct_download, job_id)


@router.get("/status/{job_id}")
async def get_conversion_status(
    job_id: str,
    user: dict = Depends(get_current_user),
):
    """Poll conversion job status. Used by Playground/Widget when the
    initial conversion request times out due to reverse proxy limits."""
    job = get_conversion_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.project_id != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    if job.status == "":
        return JSONResponse(status_code=200, content={"status": "processing"})

    if job.status == "success":
        presigned_url = generate_presigned_url(job.object_key, job.project_id)
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "presigned_url": presigned_url,
                "object_key": job.object_key,
            },
        )

    return JSONResponse(
        status_code=200,
        content={"status": "failed", "error": job.error_message},
    )


@router.get("/batch/{batch_id}")
async def get_batch_status_endpoint(
    batch_id: str,
    user: dict = Depends(get_current_user),
):
    """Poll the status of an async batch conversion job.

    Returns aggregate status, per-URL statuses, and presigned download
    URLs for completed files. Private keys only.
    """
    if user.get("key_type") in ("public", "dashboard"):
        raise HTTPException(
            status_code=403,
            detail="Batch status requires a private API key",
        )

    result = get_batch_status(batch_id, str(user["id"]))
    if result is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    return result


@router.get("/download/{object_key:path}")
async def download_file(
    object_key: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Proxy file download from DO Spaces through the API.
    Avoids CORS issues and prevents auto-download with garbled filenames
    by setting Content-Disposition: inline with the correct filename."""
    from utils.storage import DO_SPACES_BUCKET, DO_SPACES_REGION, DO_SPACES_KEY, DO_SPACES_SECRET, ENV_PREFIX
    import boto3
    from pathlib import Path

    expected_prefix = f"{ENV_PREFIX}/files/{str(user['id'])}/"
    if not object_key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Access denied")

    client = boto3.client(
        's3',
        region_name=DO_SPACES_REGION,
        endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
    )

    try:
        s3_obj = client.get_object(Bucket=DO_SPACES_BUCKET, Key=object_key)
    except client.exceptions.NoSuchKey:
        raise HTTPException(status_code=404, detail="File not found")

    content = s3_obj["Body"].read()
    content_type = s3_obj.get("ContentType", "application/octet-stream")
    filename = Path(object_key).name

    posthog_client.capture(
        posthog_client.distinct_id_for_project(user["id"]),
        "file_download_proxied",
        {
            "content_type": content_type,
            "file_size_bytes": len(content),
            "key_type": user.get("key_type", "unknown"),
            "source": posthog_client.source_from(user, request),
        },
        posthog_client.group_of(user["id"]),
    )

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Filename": filename,
        },
    )


@router.post("/ppt-to-pdf")
async def ppt_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert power point to PDF

    Args:
        file: power point (.ppt) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/ppt-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "ppt-to-pdf")

    return await forward_to_backend(request, "ppt-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/odt-to-pdf")
async def odt_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert open office to PDF

    Args:
        file: open office (.odt) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/odt-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "odt-to-pdf")

    return await forward_to_backend(request, "odt-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/ods-to-pdf")
async def ods_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert open office to PDF

    Args:
        file: open office (.ods) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/ods-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "ods-to-pdf")

    return await forward_to_backend(request, "ods-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/odp-to-pdf")
async def odp_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert open office to PDF

    Args:
        file: open office (.odp) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/odp-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "odp-to-pdf")

    return await forward_to_backend(request, "odp-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/ots-to-pdf")
async def ots_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert open office to PDF

    Args:
        file: open office (.ots) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/ots-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "ots-to-pdf")

    return await forward_to_backend(request, "ots-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/pages-to-pdf")
async def pages_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert apple pages to PDF

    Args:
        file: apple pages (.pages) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/pages-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "pages-to-pdf")

    return await forward_to_backend(request, "pages-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/numbers-to-pdf")
async def numbers_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    pdf_options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert apple numbers to PDF

    Args:
        file: apple numbers (.numbers) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
        pdf_options: Optional JSON string. Supports 'grayscale' only — this
            format is rendered by LibreOffice, whose page layout comes from the
            source document — so an explicitly-set geometry option (page_size,
            page_width/height, orientation, margins, scale, header, footer) is
            rejected with a 400.
    """
    #check_rate_limits(request, user, "/v1/convert/numbers-to-pdf")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()

    parsed_pdf_options = _parse_office_pdf_options(pdf_options, file.filename, "numbers-to-pdf")

    return await forward_to_backend(request, "numbers-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

# @router.post("/key-to-pdf")
# async def key_to_pdf(
#     request: Request,
#     file: UploadFile = File(...),
#     output_filename: Optional[str] = Form(None),
#     direct_download: Optional[bool] = Form(True),
#     job_id: Optional[str] = Form(None),
#     pdf_options: Optional[str] = Form(None),
#     user: dict = Depends(get_current_user)
# ):
#     """Convert apple pages to PDF

#     Args:
#         file: apple pages (.key) to convert
#         output_filename: Optional custom output filename. If not provided, uses input filename with .pdf extension.
#         direct_download: If True (default), return file directly. Set to False for JSON metadata.
#         pdf_options: Optional JSON string with PDF output configuration (page size, margins, etc.)
#     """
#     #check_rate_limits(request, user, "/v1/convert/key-to-pdf")
#     # check_abuse_patterns(request, user)
#     validate_file_size(request, user)

#     content = await file.read()

#     parsed_pdf_options = None
#     if pdf_options:
#         try:
#             parsed_pdf_options = PdfOptions(**json_lib.loads(pdf_options))
#         except (json_lib.JSONDecodeError, ValueError) as e:
#             raise HTTPException(status_code=400, detail=f"Invalid pdf_options: {str(e)}")

#     return await forward_to_backend(request, "key-to-pdf", user, content, file.filename, output_filename, direct_download, job_id, pdf_options=parsed_pdf_options)

@router.post("/jpeg-to-png")
async def jpeg_to_png(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert jpeg to png

    Args:
        file: jpeg image file (.jpeg) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/jpeg-to-png")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "jpeg-to-png", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/png-to-jpeg")
async def png_to_jpeg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert png to jpeg

    Args:
        file: png image file (.png) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    #check_rate_limits(request, user, "/v1/convert/png-to-jpeg")
    # check_abuse_patterns(request, user)
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "png-to-jpeg", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/jpeg-to-svg")
async def jpeg_to_svg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert jpeg to svg

    Args:
        file: JPEG image file (.jpeg/.jpeg) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "jpeg-to-svg", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/svg-to-jpeg")
async def svg_to_jpeg(
    request: Request,
    file: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert svg to jpeg

    Args:
        file: SVG image file (.svg) to convert
        width: Optional output width in pixels (1-10000). Alone, height is derived from the SVG's aspect ratio.
        height: Optional output height in pixels (1-10000). Alone, width is derived from the SVG's aspect ratio.
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    validate_svg_dimensions(width, height, content)
    size_kwargs = {k: v for k, v in (("width", width), ("height", height)) if v is not None}
    return await forward_to_backend(request, "svg-to-jpeg", user, content, file.filename, output_filename, direct_download, job_id, converter_kwargs=size_kwargs or None)


@router.post("/jpeg-to-heic")
async def jpeg_to_heic(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert jpeg to heic

    Args:
        file: JPEG image file (.jpeg/.jpeg) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "jpeg-to-heic", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/heic-to-jpeg")
async def heic_to_jpeg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert heic to jpeg

    Args:
        file: HEIC image file (.heic/.heif) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "heic-to-jpeg", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/jpeg-to-webp")
async def jpeg_to_webp(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert jpeg to webp

    Args:
        file: JPEG image file (.jpeg/.jpeg) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "jpeg-to-webp", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/webp-to-jpeg")
async def webp_to_jpeg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert webp to jpeg

    Args:
        file: WEBP image file (.webp) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "webp-to-jpeg", user, content, file.filename, output_filename, direct_download, job_id)    
  

@router.post("/png-to-svg")
async def png_to_svg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert png to svg

    Args:
        file: PNG image file (.png) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "png-to-svg", user, content, file.filename, output_filename, direct_download, job_id)    
  
@router.post("/svg-to-png")
async def svg_to_png(
    request: Request,
    file: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert svg to png

    Args:
        file: SVG image file (.svg) to convert
        width: Optional output width in pixels (1-10000). Alone, height is derived from the SVG's aspect ratio.
        height: Optional output height in pixels (1-10000). Alone, width is derived from the SVG's aspect ratio.
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    validate_svg_dimensions(width, height, content)
    size_kwargs = {k: v for k, v in (("width", width), ("height", height)) if v is not None}
    return await forward_to_backend(request, "svg-to-png", user, content, file.filename, output_filename, direct_download, job_id, converter_kwargs=size_kwargs or None)
    
@router.post("/png-to-heic")
async def png_to_heic(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert png to heic

    Args:
        file: PNG image file (.png) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "png-to-heic", user, content, file.filename, output_filename, direct_download, job_id)    
  
@router.post("/heic-to-png")
async def heic_to_png(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert heic to png

    Args:
        file: HEIC image file (.heic/.heif) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "heic-to-png", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/png-to-webp")
async def png_to_webp(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert png to webp

    Args:
        file: PNG image file (.png) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "png-to-webp", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/webp-to-png")
async def webp_to_png(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert webp to png

    Args:
        file: WebP image file (.webp) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "webp-to-png", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/svg-to-heic")
async def svg_to_heic(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert svg to heic

    Args:
        file: SVG image file (.svg) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "svg-to-heic", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/heic-to-svg")
async def heic_to_svg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert heic to svg

    Args:
        file: HEIC image file (.heic/.heif) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "heic-to-svg", user, content, file.filename, output_filename, direct_download, job_id)

 
@router.post("/svg-to-webp")
async def svg_to_webp(
    request: Request,
    file: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert svg to webp

    Args:
        file: SVG image file (.svg) to convert
        width: Optional output width in pixels (1-10000). Alone, height is derived from the SVG's aspect ratio.
        height: Optional output height in pixels (1-10000). Alone, width is derived from the SVG's aspect ratio.
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    validate_svg_dimensions(width, height, content)
    size_kwargs = {k: v for k, v in (("width", width), ("height", height)) if v is not None}
    return await forward_to_backend(request, "svg-to-webp", user, content, file.filename, output_filename, direct_download, job_id, converter_kwargs=size_kwargs or None)

@router.post("/webp-to-svg")
async def webp_to_svg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert webp to svg

    Args:
        file: WebP image file (.webp) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "webp-to-svg", user, content, file.filename, output_filename, direct_download, job_id) 


@router.post("/heic-to-webp")
async def heic_to_webp(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert heic to webp

    Args:
        file: HEIC image file (.heic/.heif) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "heic-to-webp", user, content, file.filename, output_filename, direct_download, job_id)

@router.post("/webp-to-heic")
async def webp_to_heic(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert webp to heic

    Args:
        file: WebP image file (.webp) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "webp-to-heic", user, content, file.filename, output_filename, direct_download, job_id)


@router.post("/pdf-to-jpeg")
async def pdf_to_jpeg(
    request: Request,
    file: UploadFile = File(...),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Convert pdf to jpeg

    Args:
        file: PDF file (.pdf) to convert
        output_filename: Optional custom output filename. If not provided, uses input filename with new extension.
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(request, "pdf-to-jpeg", user, content, file.filename, output_filename, direct_download, job_id)


@router.post("/compress-image")
async def compress_image(
    request: Request,
    file: UploadFile = File(...),
    target_size_kb: Optional[int] = Form(None),
    output_filename: Optional[str] = Form(None),
    direct_download: Optional[bool] = Form(True),
    job_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Compress a PNG, JPEG or WebP image without changing its format.

    Lossless-first: metadata is stripped (ICC profile and EXIF orientation are
    preserved) and the image is re-encoded with the format's strongest
    lossless settings; the result is never larger than the input. When
    target_size_kb is given and lossless compression alone can't reach it, the
    image is downscaled with the aspect ratio locked until it fits (best
    effort — an unreachable target returns the smallest achievable file).

    Args:
        file: PNG, JPEG or WebP image file (.png/.jpg/.jpeg/.webp) to compress
        target_size_kb: Optional size budget in KB. Omitted -> lossless-only.
        output_filename: Optional custom output filename. If not provided, uses input filename (extension is preserved).
        direct_download: If True (default), return file directly. Set to False for JSON metadata.
    """
    # Pure client error: reject before quota burn / activity logging, same
    # placement rationale as _parse_office_pdf_options.
    if target_size_kb is not None and target_size_kb < 1:
        raise HTTPException(status_code=400, detail="target_size_kb must be a positive integer")
    validate_file_size(request, user)

    content = await file.read()
    return await forward_to_backend(
        request, "compress-image", user, content, file.filename, output_filename,
        direct_download, job_id,
        converter_kwargs={"target_size_kb": target_size_kb} if target_size_kb else None,
    )
