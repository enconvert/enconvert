"""
Shared processor for all conversion endpoints.
Handles: sync/async, direct download, batch processing, ZIP bundling, custom filenames.
"""
import io
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

from api.deps import check_conversion_limit, check_storage_limit, check_feature_access, check_batch_limit, check_crawl_access
from monitoring import posthog_client
from monitoring.metrics import (
    log_activity_start, log_batch_activity_start, update_activity_status,
)
from utils.storage import upload_to_gcs, upload_rendered_html, generate_presigned_url
from utils.retention import schedule_file_cleanup
from utils.conversion_jobs import (
    create_conversion_job, update_conversion_job_success, update_conversion_job_failure,
)
from utils.email_notifier import send_job_completion_email
from utils.callback_notifier import send_callback_notification
from utils.subscription import get_project_owner_email
from utils.sitemap import fetch_sitemap_urls

from services.browser.converters import (
    url_to_pdf as _url_to_pdf,
    url_to_png as _url_to_png,
    url_to_markdown as _url_to_markdown,
)
from services.page_quality.instrumentation import PageInstrumentation, header_opts_out
from services.v2_engine.url_safety import assert_public_http_url
from models import PdfOptions

# V2 Phase 0: retain rendered HTML in DO Spaces for 90 days. See the
# Privacy policy for the public disclosure of this retention window
# and the X-EnConvert-No-Capture opt-out header.
RENDERED_HTML_RETENTION_HOURS = 90 * 24

import logging
logger = logging.getLogger(__name__)

# Map short endpoint names to browser converter functions
BROWSER_CONVERTER_MAP = {
    "url-to-pdf": _url_to_pdf,
    "url-to-screenshot": _url_to_png,
    "url-to-markdown": _url_to_markdown,
}


def _capture_url_event(
    user: dict, event: str, endpoint: str, *,
    input_size: int, is_async: bool, is_batch: bool,
    request=None, duration: float = None, output_size: int = None,
    url_count: int = None, error_type: str = None, error_code: int = None,
) -> None:
    """Emit a conversion_* event for a browser (URL-based) conversion.

    ``converter_module`` is always 'browser' for these endpoints. Background
    tasks have no request object, so ``source`` falls back to the key_type.
    """
    project_id = user.get("id")
    source_format, target_format = posthog_client.split_endpoint_formats(endpoint)
    props = {
        "endpoint": f"/v1/convert/{endpoint}",
        "converter_module": "browser",
        "source_format": source_format,
        "target_format": target_format,
        "input_file_size_bytes": input_size,
        "is_async": is_async,
        "is_batch": is_batch,
        "plan_tier": user.get("subscription", {}).get(
            "plan_slug", user.get("plan_slug", "free")
        ),
        "key_type": user.get("key_type", "unknown"),
        "source": posthog_client.source_from(user, request),
    }
    if duration is not None:
        props["duration_ms"] = int(duration * 1000)
    if output_size is not None:
        props["output_file_size_bytes"] = output_size
    if url_count is not None:
        props["url_count"] = url_count
    if error_type is not None:
        props["error_type"] = error_type
    if error_code is not None:
        props["error_code"] = error_code
    posthog_client.capture(
        posthog_client.distinct_id_for_project(project_id),
        event, props, posthog_client.group_of(project_id),
    )


def extract_name_from_url(url: str) -> str:
    """Extract domain name from URL for filename."""
    try:
        hostname = urlparse(url).netloc.split(':')[0]
        if hostname.startswith('www.'):
            after_www = hostname[4:]
            return after_www.split('.')[0] if '.' in after_www else after_www
        return hostname.split('.')[0] or "output"
    except:
        return "output"


def make_filename(custom_name: str, url: str, ext: str) -> str:
    """Generate filename with timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]
    if custom_name:
        base = custom_name[:-len(ext)] if custom_name.lower().endswith(ext) else custom_name
        return f"{base}_{ts}{ext}"
    return f"{extract_name_from_url(url)}_{ts}{ext}"


async def call_backend(
    endpoint: str,
    data: dict,
    instrumentation: PageInstrumentation | None = None,
) -> bytes:
    """Call browser converter directly — no HTTP round-trip.

    `instrumentation`, when provided, is threaded into the converter so
    rendered HTML, console errors, and page-load timing are captured for
    persistence by the caller after upload succeeds.
    """
    converter_fn = BROWSER_CONVERTER_MAP.get(endpoint)
    if not converter_fn:
        raise HTTPException(status_code=503, detail=f"Converter not available: {endpoint}")

    # SSRF guard on the live browser path. This is the single chokepoint every
    # URL conversion flows through (sync, async, batch, and website-crawl
    # discovered URLs), so screening here blocks loopback / RFC1918 / cloud
    # metadata targets before Chromium ever fetches them. Raises HTTPException
    # (400) which propagates as a clean rejection on the sync path and marks
    # the row Failed on the background paths. handle_url_conversion also
    # screens up front for early rejection; this is the last line of defense.
    await assert_public_http_url(data["url"])

    # Parse pdf_options from request data
    raw_pdf_opts = data.get("pdf_options")
    pdf_options = PdfOptions(**raw_pdf_opts) if raw_pdf_opts else None

    kwargs = dict(
        url=data["url"],
        load_media=data.get("load_media", True),
        enable_scroll=data.get("enable_scroll", True),
        handle_sticky_header=data.get("handle_sticky_header", True),
        handle_cookies=data.get("handle_cookies", True),
        wait_for_images=data.get("wait_for_images", True),
        viewport_width=data.get("viewport_width", 1920),
        viewport_height=data.get("viewport_height", 1080),
        auth=data.get("auth"),
        cookies=data.get("cookies"),
        headers=data.get("headers"),
        instrumentation=instrumentation,
        wait_for_selector=data.get("wait_for_selector"),
        wait_for_selector_timeout=data.get("wait_for_selector_timeout", 10000),
        block_ads=data.get("block_ads", False),
        block_media=data.get("block_media", False),
    )
    if endpoint in ("url-to-pdf", "url-to-markdown"):
        kwargs["single_page"] = data.get("single_page", True)
        kwargs["pdf_options"] = pdf_options
    return await converter_fn(**kwargs)


async def _persist_instrumentation(
    instrumentation: PageInstrumentation | None,
    activity_id: int,
    project_id: str,
) -> dict:
    """Upload captured HTML to DO Spaces, schedule 90-day cleanup, and return
    the field updates to merge into `update_activity_status`.

    Failures are intentionally swallowed — V2 instrumentation is observability
    and must never break a successful V1 conversion. Returns an empty dict
    when there is nothing to persist (no instrumentation, opt-out, or
    capture failed before any data was recorded).
    """
    if instrumentation is None or instrumentation.skip:
        return {}

    updates: dict = {}
    # Counters are useful even when the HTML capture itself failed.
    updates["console_error_count"] = instrumentation.console_error_count
    if instrumentation.page_load_time_ms:
        updates["page_load_time_ms"] = instrumentation.page_load_time_ms

    if instrumentation.rendered_html and instrumentation.content_hash:
        try:
            html_key = upload_rendered_html(
                instrumentation.rendered_html,
                project_id=str(project_id),
                job_id=str(activity_id),
            )
            updates["rendered_html_key"] = html_key
            updates["content_hash"] = instrumentation.content_hash

            # Best-effort 90-day deletion. Existing schedule_file_cleanup
            # expects an int project_id; fall back silently for the
            # public-key path where the id may not be numeric.
            try:
                project_id_int = int(project_id)
            except (TypeError, ValueError):
                project_id_int = None
            if project_id_int is not None:
                try:
                    schedule_file_cleanup(
                        html_key, project_id_int, RENDERED_HTML_RETENTION_HOURS
                    )
                except Exception as cleanup_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to schedule HTML cleanup for %s: %s",
                        html_key, cleanup_err,
                    )
        except Exception as upload_err:  # noqa: BLE001
            logger.warning(
                "Failed to upload rendered HTML for activity %s: %s",
                activity_id, upload_err,
            )

    return updates


async def process_single_async(
    request_data: dict, endpoint: str, user: dict, ext: str,
    activity_id: int | None = None, batch_id: str | None = None,
    custom_filename: str = None,
    notification_email: str = None,
    callback_url: str = None,
    no_capture: bool = False,
):
    """Background task: convert single URL, upload, send notifications.
    If activity_id is provided (batch mode), uses it. Otherwise creates one.
    Sends email notification and optional customer callback when complete.

    `no_capture` skips the V2 Phase 0 rendered-HTML capture for callers
    that sent `X-EnConvert-No-Capture: true`. Counters are still skipped
    because instrumentation is not created in that path.
    """
    url = request_data.get("url", "")
    full_endpoint = f"/v1/convert/{endpoint}"

    if activity_id is None:
        activity_id = await log_activity_start(
            project_id=user["id"], endpoint=full_endpoint,
            input_file_size=len(url), batch_id=batch_id, source_url=url,
        )

    start = datetime.now(timezone.utc)
    job_status = "failed"
    object_key = None
    file_size = None
    filename = None

    is_batch = batch_id is not None
    _capture_url_event(
        user, "conversion_requested", endpoint,
        input_size=len(url), is_async=True, is_batch=is_batch,
    )

    instrumentation = None if no_capture else PageInstrumentation()

    try:
        output_bytes = await call_backend(endpoint, request_data, instrumentation=instrumentation)
        filename = make_filename(custom_filename, url, ext)
        result = upload_to_gcs(output_bytes, user["id"], endpoint, filename)
        duration = (datetime.now(timezone.utc) - start).total_seconds()

        object_key = result["object_key"]
        file_size = result["file_size"]
        job_status = "success"

        instr_updates = await _persist_instrumentation(
            instrumentation, activity_id, user["id"],
        )
        await update_activity_status(
            activity_id, "Success",
            output_file_size=file_size, object_key=object_key, duration=duration,
            **instr_updates,
        )
        _capture_url_event(
            user, "conversion_completed", endpoint,
            input_size=len(url), is_async=True, is_batch=is_batch,
            duration=duration, output_size=file_size,
        )
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        try:
            instr_updates = await _persist_instrumentation(
                instrumentation, activity_id, user["id"],
            )
            await update_activity_status(
                activity_id, "Failed", duration=duration, **instr_updates,
            )
        except Exception:
            pass
        _capture_url_event(
            user, "conversion_failed", endpoint,
            input_size=len(url), is_async=True, is_batch=is_batch,
            duration=duration, error_type=type(e).__name__, error_code=500,
        )
        logger.error(f"Async conversion failed: {e}")

    # Send email notification if provided (no URL - user downloads from dashboard)
    if notification_email:
        send_job_completion_email(
            recipient_email=notification_email,
            job_id=str(activity_id),
            job_status=job_status,
            batch_id=batch_id
        )

    # Send optional customer callback if provided
    if callback_url:
        await send_callback_notification(
            callback_url=callback_url,
            job_id=str(activity_id),
            job_status=job_status,
            batch_id=batch_id,
            gcs_uri=object_key,
            filename=filename,
            file_size=file_size
        )


async def process_batch_async(
    urls: list, activity_ids: list[int], request_data: dict,
    endpoint: str, user: dict, ext: str, batch_id: str,
    custom_filename: str = None,
    notification_email: str = None,
    callback_url: str = None,
    no_capture: bool = False,
):
    """Background task: convert multiple URLs, create ZIP, upload, send notifications.
    Each URL has a pre-created activity row (activity_ids[i] <-> urls[i]).
    Sends email notification and optional customer callback when complete.

    `no_capture` propagates the V2 Phase 0 opt-out across all per-URL
    converter calls in the batch.
    """
    files, results = [], []
    job_status = "failed"
    zip_key = None
    zip_filename = None
    zip_size = None

    try:
        for i, url in enumerate(urls):
            aid = activity_ids[i]
            start = datetime.now(timezone.utc)
            _capture_url_event(
                user, "conversion_requested", endpoint,
                input_size=len(url), is_async=True, is_batch=True,
            )
            instrumentation = None if no_capture else PageInstrumentation()
            try:
                output_bytes = await call_backend(
                    endpoint, {**request_data, "url": url},
                    instrumentation=instrumentation,
                )
                duration = (datetime.now(timezone.utc) - start).total_seconds()
                filename = make_filename(None, url, ext)
                files.append((filename, output_bytes, aid, len(output_bytes)))
                results.append({"url": url, "status": "success", "filename": filename})
                # Mark individual row as Success (url will be updated after ZIP).
                # Persist V2 Phase 0 instrumentation for THIS url here so we
                # don't clobber other rows' html keys later in the loop.
                instr_updates = await _persist_instrumentation(
                    instrumentation, aid, user["id"],
                )
                await update_activity_status(
                    aid, "Success",
                    output_file_size=len(output_bytes), duration=duration,
                    **instr_updates,
                )
                _capture_url_event(
                    user, "conversion_completed", endpoint,
                    input_size=len(url), is_async=True, is_batch=True,
                    duration=duration, output_size=len(output_bytes),
                )
            except Exception as e:
                duration = (datetime.now(timezone.utc) - start).total_seconds()
                results.append({"url": url, "status": "failed", "error": str(e)})
                _capture_url_event(
                    user, "conversion_failed", endpoint,
                    input_size=len(url), is_async=True, is_batch=True,
                    duration=duration, error_type=type(e).__name__, error_code=500,
                )
                try:
                    instr_updates = await _persist_instrumentation(
                        instrumentation, aid, user["id"],
                    )
                    await update_activity_status(
                        aid, "Failed", duration=duration, **instr_updates,
                    )
                except Exception:
                    pass

        if files:
            # Create ZIP from successful files
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for name, data, _, _ in files:
                    zf.writestr(name, data)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:-3]
            zip_name = f"{custom_filename}_{ts}.zip" if custom_filename else f"batch_{ts}.zip"
            if custom_filename and custom_filename.lower().endswith('.zip'):
                zip_name = f"{custom_filename[:-4]}_{ts}.zip"

            result = upload_to_gcs(buf.getvalue(), user["id"], endpoint, zip_name)
            zip_key = result["object_key"]
            zip_filename = result["filename"]
            zip_size = result["file_size"]

            # Set the ZIP key on all successful rows
            for _, _, aid, _ in files:
                await update_activity_status(
                    aid, "Success", object_key=zip_key,
                )

            # Overall job status based on results
            if len(files) > 0:
                job_status = "success"

    except Exception as e:
        # Mark any remaining In Progress rows as Failed
        for i, url in enumerate(urls):
            try:
                await update_activity_status(activity_ids[i], "Failed")
            except Exception:
                pass
        logger.error(f"Batch conversion failed: {e}")

    succeeded = sum(1 for r in results if r.get("status") == "success")
    posthog_client.capture(
        posthog_client.distinct_id_for_project(user.get("id")),
        "batch_conversion_completed",
        {
            "endpoint": f"/v1/convert/{endpoint}",
            "url_count": len(urls),
            "succeeded_count": succeeded,
            "failed_count": len(urls) - succeeded,
            "job_status": job_status,
            "output_file_size_bytes": zip_size,
            "plan_tier": user.get("subscription", {}).get(
                "plan_slug", user.get("plan_slug", "free")
            ),
            "key_type": user.get("key_type", "unknown"),
            "source": posthog_client.source_from(user, None),
        },
        posthog_client.group_of(user.get("id")),
    )

    # Send email notification if provided (no URL - user downloads from dashboard)
    if notification_email:
        send_job_completion_email(
            recipient_email=notification_email,
            job_id=batch_id,
            job_status=job_status,
            tasks=results,
            batch_id=batch_id
        )

    # Send optional customer callback if provided
    if callback_url:
        await send_callback_notification(
            callback_url=callback_url,
            job_id=batch_id,
            job_status=job_status,
            batch_id=batch_id,
            gcs_uri=zip_key,
            filename=zip_filename,
            file_size=zip_size,
            tasks=results
        )


# Headers that must not be overridden via the custom headers parameter
_BLOCKED_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "upgrade", "te", "trailer",
})


def validate_auth_cookies_headers(data: dict):
    """Validate the optional auth, cookies and headers fields in the request body."""
    auth = data.get("auth")
    cookies = data.get("cookies")
    headers = data.get("headers")

    if auth:
        if not isinstance(auth, dict) or not auth.get("username") or not auth.get("password"):
            raise HTTPException(400, "'auth' must be an object with 'username' and 'password'")

    if cookies:
        if not isinstance(cookies, list):
            raise HTTPException(400, "'cookies' must be an array of cookie objects")
        if len(cookies) > 50:
            raise HTTPException(400, "'cookies' array must not exceed 50 entries")
        for i, cookie in enumerate(cookies):
            if not isinstance(cookie, dict):
                raise HTTPException(400, f"Cookie at index {i} must be an object")
            if not cookie.get("name") or not cookie.get("value"):
                raise HTTPException(400, f"Cookie at index {i} must have 'name' and 'value'")
            if not cookie.get("domain") and not cookie.get("url"):
                raise HTTPException(400, f"Cookie at index {i} must have 'domain' or 'url'")
            # Default path to "/" when domain is provided but path is missing
            if cookie.get("domain") and not cookie.get("path"):
                cookie["path"] = "/"

    if headers:
        if not isinstance(headers, dict):
            raise HTTPException(400, "'headers' must be an object of header name/value pairs")
        if len(headers) > 20:
            raise HTTPException(400, "'headers' must not exceed 20 entries")
        for name, value in headers.items():
            if name.lower() in _BLOCKED_HEADERS:
                raise HTTPException(400, f"Header '{name}' cannot be overridden")
            if not isinstance(value, str):
                raise HTTPException(400, f"Header '{name}' value must be a string")

    # Reject conflicting auth mechanisms
    if auth and headers and any(k.lower() == "authorization" for k in headers):
        raise HTTPException(
            400,
            "Cannot use both 'auth' and an 'Authorization' header. "
            "Use 'auth' for HTTP Basic Auth or 'headers' for Bearer/custom auth, not both."
        )


def validate_render_options(data: dict):
    """Validate the optional per-request render knobs.

    ``wait_for_selector`` (str), ``wait_for_selector_timeout`` (positive int
    ms, capped so a caller cannot pin a conversion slot indefinitely), and the
    ``block_ads`` / ``block_media`` booleans.
    """
    selector = data.get("wait_for_selector")
    if selector is not None:
        if not isinstance(selector, str):
            raise HTTPException(400, "'wait_for_selector' must be a string")
        if len(selector) > 1000:
            raise HTTPException(400, "'wait_for_selector' is too long (max 1000 chars)")

    timeout = data.get("wait_for_selector_timeout")
    if timeout is not None:
        # bool is a subclass of int — reject it explicitly.
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise HTTPException(
                400, "'wait_for_selector_timeout' must be a positive integer (ms)"
            )
        if timeout > 60000:
            raise HTTPException(
                400, "'wait_for_selector_timeout' must not exceed 60000 ms"
            )

    for flag in ("block_ads", "block_media"):
        value = data.get(flag)
        if value is not None and not isinstance(value, bool):
            raise HTTPException(400, f"'{flag}' must be a boolean")


async def discover_website_urls(
    data: dict, user: dict, notification_email: str | None, base_url: str,
) -> tuple[list[str], str, int]:
    """
    Shared URL discovery logic for website capture endpoints.

    Determines crawl mode based on:
    1. User's subscription crawl_mode (none / sitemap / full)
    2. Requested crawl_mode from request body (auto / sitemap / full)

    - Starter plans (crawl_mode="sitemap"): sitemap-only discovery
    - Pro/Business plans (crawl_mode="full"): full algorithmic crawl (sitemap + Crawlee BFS)
    - Free plans (crawl_mode="none"): blocked by check_crawl_access

    Returns:
        (urls, discovery_method, total_discovered)
    """
    crawl_mode_requested = data.get("crawl_mode", "auto")
    sub = user.get("subscription", {})
    user_crawl_mode = sub.get("crawl_mode", "none")

    # Determine effective mode
    if crawl_mode_requested == "full":
        # User explicitly requested full crawl — check they have access
        check_crawl_access(user, "full")
        effective_mode = "full"
    elif crawl_mode_requested == "sitemap":
        # User explicitly requested sitemap only
        check_crawl_access(user, "sitemap")
        effective_mode = "sitemap"
    else:
        # "auto" — use the highest mode available to the user
        check_crawl_access(user, "sitemap")  # At minimum they need sitemap access
        effective_mode = user_crawl_mode  # "sitemap" for Starter, "full" for Pro/Business

    if effective_mode == "full":
        from utils.crawler import discover_urls
        urls = await discover_urls(
            base_url=base_url,
            user=user,
            include_patterns=data.get("include_patterns"),
            exclude_patterns=data.get("exclude_patterns"),
        )
        if not urls:
            if notification_email:
                send_job_completion_email(
                    recipient_email=notification_email, job_id="N/A",
                    job_status="failed", batch_id=None,
                )
            raise HTTPException(400, f"No pages discovered on {base_url}")
        return urls, "full_crawl", len(urls)

    # effective_mode == "sitemap"
    try:
        urls = await fetch_sitemap_urls(base_url)
    except HTTPException:
        if notification_email:
            send_job_completion_email(
                recipient_email=notification_email, job_id="N/A",
                job_status="failed", batch_id=None,
            )
        raise
    return urls, "sitemap", len(urls)


async def handle_url_conversion(
    request, background_tasks, user: dict, endpoint: str, ext: str, media_type: str
):
    """
    Unified handler for URL-based conversions (url-to-pdf, url-to-screenshot, etc.)
    Handles: sync/async, single/multiple URLs, direct download, ZIP bundling.
    """
    data = await request.json()
    job_id = data.get("job_id")

    # V2 Phase 0 opt-out: consumers can disable rendered-HTML capture
    # per-request with `X-EnConvert-No-Capture: true`. The Privacy policy
    # documents this header alongside the 90-day retention window.
    no_capture = header_opts_out(request.headers)

    # Normalize URLs to list
    urls = data.get("url", [])
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        raise HTTPException(400, "'url' must be provided")

    # SSRF pre-screen every URL up front so a private/internal/metadata target
    # is rejected with a clean 400 BEFORE any activity rows or jobs are
    # created. call_backend re-checks each URL as the last line of defense
    # (and covers website-crawl discovered URLs that never reach here).
    for candidate in urls:
        await assert_public_http_url(candidate)

    # Validate optional per-request render knobs.
    validate_render_options(data)

    # Check conversion limit with full batch size
    check_conversion_limit(user, url_count=len(urls))
    check_storage_limit(user)

    # Feature gates for private key features
    if user.get("key_type") not in ("public", "dashboard"):
        async_mode_requested = data.get("async_mode", False)
        output_format_requested = data.get("output_format", False)
        callback_url_requested = data.get("callback_url")

        # Batch limit check
        if len(urls) > 1:
            check_feature_access(user, "has_async_mode")  # batch requires async
            check_batch_limit(user, len(urls))

        # Async mode check
        if async_mode_requested:
            check_feature_access(user, "has_async_mode")

        # ZIP output check
        if output_format_requested:
            check_feature_access(user, "has_zip_output")

        # Webhook/callback check
        if callback_url_requested:
            check_feature_access(user, "has_webhook")

        # Basic auth, cookies & custom headers check
        if data.get("auth") or data.get("cookies") or data.get("headers"):
            check_feature_access(user, "has_basic_auth")

    # Validate auth, cookies and headers structures
    validate_auth_cookies_headers(data)

    # TODO: Add crawl access gating when website crawl features are built
    # if crawl_type requested:
    #     check_crawl_access(user, crawl_type)

    # Public/dashboard keys: enforce sync mode, direct download, and single URL only
    is_browser_key = user.get("key_type") in ("public", "dashboard")
    if is_browser_key:
        if len(urls) > 1:
            raise HTTPException(400, "Public keys only support a single URL input")
        async_mode = False
        direct_download = True
        output_format = False
        output_filename = data.get("output_filename")
        notification_email = None
        callback_url = None
    else:
        async_mode = data.get("async_mode", False)
        direct_download = data.get("direct_download", False)
        output_format = data.get("output_format", False)  # ZIP bundling
        output_filename = data.get("output_filename")
        # Optional notification settings (only available for private keys)
        notification_email = data.get("notification_email")
        if not notification_email:
            try:
                notification_email = get_project_owner_email(int(user["id"]))
            except (TypeError, ValueError):
                pass
        callback_url = data.get("callback_url")

    # Validation
    if output_format and len(urls) == 1:
        raise HTTPException(400, "output_format=True requires multiple URLs")
    if len(urls) > 1:
        async_mode = True
        if direct_download:
            raise HTTPException(400, "direct_download not supported for multiple URLs")
    if direct_download and async_mode:
        raise HTTPException(400, "direct_download only works in sync mode")

    full_endpoint = f"/v1/convert/{endpoint}"

    if endpoint not in BROWSER_CONVERTER_MAP:
        raise HTTPException(503, "Converter not available")

    # ASYNC MODE
    if async_mode:
        batch_id = str(uuid.uuid4())

        if output_format:
            # ZIP mode: create all activity rows up front, process in one background task
            activity_ids = await log_batch_activity_start(
                project_id=user["id"], endpoint=full_endpoint,
                urls=urls, batch_id=batch_id,
            )
            background_tasks.add_task(
                process_batch_async, urls, activity_ids, data,
                endpoint, user, ext, batch_id, output_filename,
                notification_email, callback_url, no_capture,
            )
            return JSONResponse(
                status_code=202,
                content={
                    "status": "processing", "batch_id": batch_id,
                    "url_count": len(urls), "output_format": "zip",
                }
            )
        else:
            # Individual mode: create all activity rows, then one background task per URL
            activity_ids = await log_batch_activity_start(
                project_id=user["id"], endpoint=full_endpoint,
                urls=urls, batch_id=batch_id,
            )
            for i, url in enumerate(urls):
                background_tasks.add_task(
                    process_single_async, {**data, "url": url},
                    endpoint, user, ext, activity_ids[i], batch_id, output_filename,
                    notification_email, callback_url, no_capture,
                )
            return JSONResponse(
                status_code=202,
                content={
                    "status": "processing", "batch_id": batch_id,
                    "url_count": len(urls), "output_format": "individual",
                }
            )

    # SYNC MODE (single URL only)
    url = urls[0]
    activity_id = await log_activity_start(
        project_id=user["id"], endpoint=full_endpoint, input_file_size=len(url)
    )
    # BUG FIX A: hand the activity id to the TimeoutMiddleware (via the ASGI
    # scope) so a 300s timeout on a slow browser render can fail this row
    # instead of leaving it stuck 'In Progress'.
    request.state.activity_id = activity_id
    request.state.endpoint = full_endpoint

    _capture_url_event(
        user, "conversion_requested", endpoint,
        input_size=len(url), is_async=False, is_batch=False, request=request,
    )

    # Create conversion job row (enables polling on timeout)
    if job_id:
        create_conversion_job(job_id, str(user["id"]))

    start = datetime.now(timezone.utc)
    instrumentation = None if no_capture else PageInstrumentation()

    try:
        output_bytes = await call_backend(
            endpoint, {**data, "url": url}, instrumentation=instrumentation,
        )
        filename = make_filename(output_filename, url, ext)
        result = upload_to_gcs(output_bytes, user["id"], endpoint, filename)
        duration = (datetime.now(timezone.utc) - start).total_seconds()

        try:
            instr_updates = await _persist_instrumentation(
                instrumentation, activity_id, user["id"],
            )
            await update_activity_status(
                activity_id, "Success",
                output_file_size=result["file_size"], object_key=result["object_key"], duration=duration,
                **instr_updates,
            )
        except Exception:
            pass

        _capture_url_event(
            user, "conversion_completed", endpoint,
            input_size=len(url), is_async=False, is_batch=False, request=request,
            duration=duration, output_size=result["file_size"],
        )

        # Update conversion job on success
        if job_id:
            update_conversion_job_success(job_id, str(user["id"]), result["object_key"])
    except Exception as exc:
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        try:
            instr_updates = await _persist_instrumentation(
                instrumentation, activity_id, user["id"],
            )
            await update_activity_status(
                activity_id, "Failed", duration=duration, **instr_updates,
            )
        except Exception:
            pass
        _capture_url_event(
            user, "conversion_failed", endpoint,
            input_size=len(url), is_async=False, is_batch=False, request=request,
            duration=duration,
            error_type=type(exc).__name__,
            error_code=getattr(exc, "status_code", 500),
        )
        # Update conversion job on failure
        if job_id:
            update_conversion_job_failure(job_id, str(exc))
        raise

    if direct_download:
        if is_browser_key:
            # Browser-based requests (playground/widgets/dashboard): return a presigned DO Spaces URL instead of
            # streaming the raw bytes through the gateway. Large PDFs from heavy websites can
            # take 60–120 s to convert, which exceeds typical reverse-proxy idle timeouts
            # (Nginx default 60 s, DO LB default 60 s), causing "Failed to fetch" on the
            # frontend even though the file was stored successfully. Returning a tiny JSON
            # response eliminates the proxy timeout; the client fetches the file directly
            # from DO Spaces CDN.
            presigned_url = generate_presigned_url(result["object_key"], str(user["id"]))
            return JSONResponse(
                status_code=200,
                content={
                    "presigned_url": presigned_url,
                    "object_key": result["object_key"],
                    "filename": filename,
                    "file_size": result["file_size"],
                    "conversion_time_seconds": duration,
                    "job_id": job_id,
                },
                headers={
                    "X-Object-Key": result["object_key"],
                    "X-File-Size": str(result["file_size"]),
                    "X-Conversion-Time": str(duration),
                    "X-Filename": filename,
                }
            )
        # Private keys with explicit direct_download=True: stream bytes directly
        return Response(
            content=output_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(output_bytes)),
                "Cache-Control": "no-transform",
                "X-Object-Key": result["object_key"],
                "X-File-Size": str(result["file_size"]),
                "X-Conversion-Time": str(duration),
                "X-Filename": filename,
            }
        )

    presigned_url = generate_presigned_url(result["object_key"], str(user["id"]))
    return JSONResponse(
        status_code=200,
        content={
            "presigned_url": presigned_url,
            "object_key": result["object_key"], "filename": result["filename"],
            "file_size": result["file_size"], "conversion_time_seconds": duration
        }
    )
