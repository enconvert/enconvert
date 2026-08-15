"""Open-source fallback for the /v2/perceive flow.

This module ships ONLY in the public mirror. The private/cloud build has the
real perceive engine at this path (stealth ladder, page-quality chains,
render-quality scoring, Tier-3 LLM extraction); the mirror replaces it with a
plain headless-Chromium render that still produces the honest basics:
markdown, cleaned/raw HTML, links, images, PDF, screenshots and heuristic
structured extraction — persisted through the same open operations/usage/
storage helpers, so the API contract (PerceiveResponse, GET status,
batch worker) is unchanged.

Capabilities that need the cloud engine (mobile emulation, action chains,
geolocation, proxies, LLM schema extraction) raise ``CloudEngineRequired``
(HTTP 501) instead of silently degrading.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from playwright.async_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from api.v2.schemas.perceive import (
    SUPPORTED_EXTRACTS,
    OutputArtifact,
    PerceiveAuth,
    PerceiveRequest,
    PerceiveResponse,
    PerceiveTokens,
)
from models import PAGE_SIZES, PdfOptions, PerceiveOperation
from services._engine_fallback import CloudEngineRequired
from services.v2_engine import operations, usage
from services.v2_engine.crawl4ai_processors import (
    extract_headings,
    extract_json_ld,
    generate_markdown_bytes,
    scrap_html,
    serialize_images,
    serialize_links,
    serialize_tables,
)
from services.markdown.html_md import html_to_markdown
from services.v2_engine.main_content import (
    ContentCandidate,
    extract_main_content,
    select_main_content,
)
from services.v2_engine.url_safety import assert_public_http_url
from utils.pdf_postprocess import convert_to_grayscale
from utils.robots_parser import fetch_robots_info
from utils.storage import generate_presigned_url, upload_to_gcs

logger = logging.getLogger(__name__)

V2_PERCEIVE_ENDPOINT = "v2-perceive"  # Spaces path segment

_EXTENSIONS: dict[str, str] = {
    "markdown": ".md",
    "html_cleaned": ".html",
    "html_raw": ".html",
    "screenshot": ".png",
    "screenshot_full_page": ".png",
    "pdf": ".pdf",
    "links": ".json",
    "images": ".json",
}

_CONTENT_TYPES: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".pdf": "application/pdf",
    ".json": "application/json",
}

# Default heuristic extraction when the caller asks for "structured"
# output without an explicit extract[] list.
_DEFAULT_EXTRACTS: tuple[str, ...] = ("metadata", "structured_data")

_MAIN_CONTENT_MAX_CHARS = 50_000

_GOTO_TIMEOUT_MS = int(os.environ.get("FALLBACK_GOTO_TIMEOUT_MS", "60000"))
_SETTLE_TIMEOUT_MS = 5_000

# ── Shared plain-Chromium browser (lazy singleton) ───────────────────────

_playwright: Optional[Any] = None
_browser: Optional[Browser] = None
_browser_lock = asyncio.Lock()
_render_semaphore = asyncio.Semaphore(
    max(1, int(os.environ.get("FALLBACK_RENDER_CONCURRENCY", "1")))
)


async def _get_browser() -> Browser:
    """Launch (or reuse) one plain headless Chromium for all renders."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        if _playwright is None:
            _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        return _browser


def _reject_cloud_features(request: PerceiveRequest) -> None:
    """Fail fast (501) on request knobs only the cloud engine serves."""
    if request.mobile:
        raise CloudEngineRequired("mobile emulation")
    if request.extraction_schema is not None:
        raise CloudEngineRequired("LLM schema extraction")
    if request.action_chain:
        raise CloudEngineRequired("browser action chains")
    if request.geolocation is not None:
        raise CloudEngineRequired("geolocation emulation")
    if request.proxy_url is not None:
        raise CloudEngineRequired("proxy routing")


def _viewport(request: PerceiveRequest) -> tuple[int, int]:
    if request.viewport is not None:
        return request.viewport.width, request.viewport.height
    if request.mobile:
        return 390, 844
    return 1920, 1080


def _effective_extracts(request: PerceiveRequest) -> tuple[list[str], list[str]]:
    """(supported extracts to run, warnings for unsupported ones)."""
    if "structured" not in request.outputs:
        return [], []
    requested = list(request.extract) or list(_DEFAULT_EXTRACTS)
    if "all" in requested:
        requested = list(SUPPORTED_EXTRACTS) + [
            name for name in requested if name != "all"
        ]
    seen: list[str] = []
    warnings: list[str] = []
    for name in requested:
        if name in seen:
            continue
        if name in SUPPORTED_EXTRACTS:
            seen.append(name)
        else:
            warnings.append(
                f"extract '{name}' is not available in the self-hosted "
                "build; omitted from structured."
            )
    return seen, warnings


def _fingerprint(request: PerceiveRequest, outputs: list[str]) -> str:
    """Cache key over the render-affecting request surface."""
    cookies = request.cookies or []
    sorted_cookies = sorted(
        cookies,
        key=lambda c: (str(c.get("name", "")), str(c.get("domain", ""))),
    )
    payload: dict[str, Any] = {
        "url": str(request.url),
        "outputs": sorted(outputs),
        # only_main_content reshapes the markdown artifact; direct_download
        # is delivery-only and deliberately absent.
        "only_main_content": request.only_main_content,
        "extract": sorted(request.extract),
        "schema": request.extraction_schema,
        "pdf_options": request.pdf_options.model_dump()
        if request.pdf_options
        else None,
        "viewport": request.viewport.model_dump() if request.viewport else None,
        "mobile": request.mobile,
        "js_code": request.js_code,
        "wait_for": request.wait_for,
        "wait_timeout_ms": request.wait_timeout_ms,
        "block_resources": sorted(request.block_resources),
        "headers": request.headers,
        "cookies": sorted_cookies,
        "auth": request.auth.model_dump() if request.auth else None,
    }
    return operations.request_fingerprint(payload)


def _options_echo(request: PerceiveRequest, outputs: list[str]) -> dict[str, Any]:
    """The honoured request options, secrets redacted to booleans (A3)."""
    return {
        "outputs": list(outputs),
        "only_main_content": request.only_main_content,
        "extract": list(request.extract),
        "cache_mode": request.cache_mode,
        "mobile": request.mobile,
        "respect_robots": request.respect_robots,
        "direct_download": request.direct_download,
        "wait_for": request.wait_for,
        "wait_timeout_ms": request.wait_timeout_ms,
        "viewport": request.viewport.model_dump() if request.viewport else None,
        "block_resources": list(request.block_resources),
        "js_code_provided": bool(request.js_code),
        "schema_provided": request.extraction_schema is not None,
        "pdf_options_provided": request.pdf_options is not None,
        "auth_provided": request.auth is not None,
        "cookies_provided": bool(request.cookies),
        "headers_provided": bool(request.headers),
    }


async def _check_robots(url: str) -> None:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    info = await fetch_robots_info(origin)
    if not info.can_fetch(url):
        raise HTTPException(
            status_code=403,
            detail="robots.txt disallows fetching this URL "
            "(request sent respect_robots=true).",
        )


async def _apply_wait_for(
    page: Page, wait_for: str, timeout_ms: int, warnings: list[str]
) -> None:
    """Best-effort wait_for: a timeout degrades to a warning."""
    try:
        if wait_for.startswith("js:"):
            await page.wait_for_function(wait_for[3:], timeout=timeout_ms)
        else:
            selector = wait_for[4:] if wait_for.startswith("css:") else wait_for
            await page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        warnings.append(
            f"wait_for did not complete within {timeout_ms}ms; "
            "captured the page as-is."
        )


def _artifact_filename(operation_id: str, name: str) -> str:
    return f"{operation_id}_{name}{_EXTENSIONS[name]}"


def _content_type_for(name: str) -> str:
    return _CONTENT_TYPES[_EXTENSIONS[name]]


def artifact_from_entry(
    name: str, entry: Any, project_id: int
) -> Optional[OutputArtifact]:
    """Build an OutputArtifact (fresh signed URL) from an output_keys
    entry. Shared with the GET status handler. Tolerates both the dict
    shape and a bare object-key string."""
    if isinstance(entry, dict):
        key = entry.get("key")
        size_bytes = int(entry.get("size_bytes", 0) or 0)
        content_type = entry.get("content_type") or _CONTENT_TYPES.get(
            _EXTENSIONS.get(name, ""), "application/octet-stream"
        )
    else:
        key = entry
        size_bytes = 0
        content_type = "application/octet-stream"
    if not key:
        return None
    try:
        url = generate_presigned_url(key, str(project_id))
    except Exception:  # noqa: BLE001 — a stale key must not 500 the GET
        logger.warning("presign failed for %s", key, exc_info=True)
        url = None
    return OutputArtifact(
        url=url,
        object_key=key,
        size_bytes=size_bytes,
        content_type=content_type,
    )


def outputs_from_keys(
    output_keys: Optional[dict[str, Any]], project_id: int
) -> Dict[str, OutputArtifact]:
    """Rebuild the response outputs map from a persisted row."""
    result: Dict[str, OutputArtifact] = {}
    for name, entry in (output_keys or {}).items():
        if name.startswith("_"):
            continue
        artifact = artifact_from_entry(name, entry, project_id)
        if artifact is not None:
            result[name] = artifact
    return result


# ── Rendering ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Captured:
    """What one plain render produced (mirror of the closed RenderResult
    surface that ``_process_outputs`` reads)."""

    html: str
    final_url: str
    pdf_bytes: Optional[bytes] = None
    screenshot_bytes: Optional[bytes] = None
    screenshot_viewport_bytes: Optional[bytes] = None


def _pdf_kwargs(pdf_options: Optional[PdfOptions]) -> dict[str, Any]:
    """Map the public PdfOptions surface onto ``page.pdf`` kwargs."""
    if pdf_options is None:
        return {"format": "A4", "print_background": True}
    kwargs: dict[str, Any] = {
        "print_background": True,
        "landscape": pdf_options.orientation == "landscape",
        "scale": pdf_options.scale,
        "margin": {
            "top": f"{pdf_options.margins.top}mm",
            "bottom": f"{pdf_options.margins.bottom}mm",
            "left": f"{pdf_options.margins.left}mm",
            "right": f"{pdf_options.margins.right}mm",
        },
    }
    if pdf_options.page_width and pdf_options.page_height:
        kwargs["width"] = f"{pdf_options.page_width}mm"
        kwargs["height"] = f"{pdf_options.page_height}mm"
    elif pdf_options.page_size in PAGE_SIZES:
        width_mm, height_mm = PAGE_SIZES[pdf_options.page_size]
        kwargs["width"] = f"{width_mm}mm"
        kwargs["height"] = f"{height_mm}mm"
    return kwargs


async def _render_basic(
    request: PerceiveRequest, url: str, outputs: list[str]
) -> tuple[_Captured, list[str]]:
    """One plain-Chromium render: goto + content/pdf/screenshot.

    No stealth, no quality chains, no engine ladder — this is the honest
    open-source render path.
    """
    warnings: list[str] = []
    browser = await _get_browser()
    width, height = _viewport(request)

    context_kwargs: dict[str, Any] = {
        "viewport": {"width": width, "height": height}
    }
    if request.headers:
        context_kwargs["extra_http_headers"] = dict(request.headers)
    if request.auth is not None:
        context_kwargs["http_credentials"] = {
            "username": request.auth.username,
            "password": request.auth.password,
        }

    async with _render_semaphore:
        context = await browser.new_context(**context_kwargs)
        try:
            if request.cookies:
                try:
                    await context.add_cookies(list(request.cookies))
                except Exception as exc:  # noqa: BLE001 — degrade, don't fail
                    warnings.append(f"cookies could not be applied: {exc}")

            page = await context.new_page()

            blocked_types = set(request.block_resources)
            if blocked_types:

                async def _block(route: Any) -> None:
                    if route.request.resource_type in blocked_types:
                        await route.abort()
                    else:
                        await route.fallback()

                await page.route("**/*", _block)

            try:
                await page.goto(url, wait_until="load", timeout=_GOTO_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                warnings.append(
                    "page load timed out; captured the page as-is."
                )

            if request.js_code:
                try:
                    await page.evaluate(request.js_code)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"js_code raised: {exc}")
            if request.wait_for:
                await _apply_wait_for(
                    page, request.wait_for, request.wait_timeout_ms, warnings
                )
            # Give late JS a short, bounded settle window.
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_SETTLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                pass

            html = await page.content()
            final_url = page.url

            pdf_bytes: Optional[bytes] = None
            ss_full: Optional[bytes] = None
            ss_viewport: Optional[bytes] = None
            if "pdf" in outputs:
                pdf_bytes = await page.pdf(**_pdf_kwargs(request.pdf_options))
            if "screenshot" in outputs:
                ss_viewport = await page.screenshot(full_page=False, type="png")
            if "screenshot_full_page" in outputs:
                ss_full = await page.screenshot(full_page=True, type="png")

            return (
                _Captured(
                    html=html,
                    final_url=final_url,
                    pdf_bytes=pdf_bytes,
                    screenshot_bytes=ss_full,
                    screenshot_viewport_bytes=ss_viewport,
                ),
                warnings,
            )
        finally:
            await context.close()


@dataclass(frozen=True)
class RenderedPage:
    """The DOM from one lightweight render (open-fallback shape).

    Same public fields the distill/ingest/watch flows read. The open build
    has no render-quality scorer, so ``render_quality`` is a coarse
    non-empty-DOM heuristic and ``is_blocked`` is always False.
    """

    html: str
    final_url: str
    render_quality: float
    is_blocked: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


async def render_html(
    url: str,
    *,
    respect_robots: bool = False,
    wait_for: Optional[str] = None,
    wait_timeout_ms: int = 30000,
    headers: Optional[dict[str, str]] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    auth: Optional[dict[str, Any]] = None,
    allow_tls: bool = True,
) -> RenderedPage:
    """Render one URL to HTML — no persistence, no quota, no uploads.

    The lightweight render entry point distill/ingest/watch depend on.
    Raises (RuntimeError / HTTPException) on a failed render exactly like
    ``run`` does; callers catch per URL.

    ``allow_tls`` is accepted and ignored. In the full build it drops the
    no-browser TLS rung from the render ladder, which ``watch_flow`` sets so a
    watch check never changes capture method mid-baseline. This open build has
    no engine ladder, so there is nothing to disable — the parameter exists
    only to keep this signature call-compatible with ``watch_flow``.
    """
    del allow_tls  # no ladder in the open build; see docstring
    clean = url.strip()
    await assert_public_http_url(clean)
    if respect_robots:
        await _check_robots(clean)

    request = PerceiveRequest(
        url=clean,
        outputs=["html_raw"],
        wait_for=wait_for,
        wait_timeout_ms=wait_timeout_ms,
        headers=headers,
        cookies=cookies,
        auth=PerceiveAuth(**auth) if auth else None,
        cache_mode="bypass",
    )
    captured, warnings = await _render_basic(request, clean, ["html_raw"])
    if not captured.html:
        raise RuntimeError(f"render captured no HTML for {clean}")
    return RenderedPage(
        html=captured.html,
        final_url=captured.final_url or clean,
        render_quality=1.0 if len(captured.html) > 500 else 0.5,
        is_blocked=False,
        warnings=tuple(warnings),
    )


# ── /v2/perceive orchestration ───────────────────────────────────────────


async def run(
    request: PerceiveRequest,
    operation_id: str,
    user: dict,
    batch_id: Optional[str] = None,
) -> PerceiveResponse:
    """Execute one /v2/perceive operation end-to-end (basic path).

    Renders (or serves from the 1 h cache), processes outputs, uploads
    artifacts, persists ch_perceive_operations, bumps the V2 usage
    counter, and returns the standard response. Cloud-only request knobs
    raise ``CloudEngineRequired`` before any row is written.
    """
    start = time.monotonic()
    project_id = int(user["id"])
    url = str(request.url)
    outputs = list(dict.fromkeys(request.outputs))

    _reject_cloud_features(request)
    await assert_public_http_url(url)
    if request.respect_robots:
        await _check_robots(url)

    extracts, warnings = _effective_extracts(request)
    fingerprint = _fingerprint(request, outputs)

    if request.cache_mode == "enabled":
        cached = operations.find_cached_operation(
            project_id=project_id, url=url, fingerprint=fingerprint
        )
        if cached is not None:
            try:
                return _serve_from_cache(
                    cached, operation_id, project_id, url, outputs, start,
                    batch_id=batch_id,
                    request=request,
                    request_warnings=warnings,
                )
            except Exception as exc:
                operations.fail_operation(
                    operation_id=operation_id,
                    error_message=str(exc),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                raise

    operations.create_operation(
        operation_id=operation_id,
        project_id=project_id,
        url=url,
        outputs_requested=outputs,
        batch_id=batch_id,
    )

    try:
        captured, render_warnings = await _render_basic(request, url, outputs)
        warnings.extend(render_warnings)
        if not captured.html:
            raise RuntimeError(f"/v2/perceive render captured no HTML for {url}")
        if "pdf" in outputs and captured.pdf_bytes is None:
            raise RuntimeError(f"/v2/perceive produced no PDF for {url}")

        artifacts, structured = await asyncio.to_thread(
            _process_outputs, request, outputs, extracts, captured, warnings
        )
        if (
            "pdf" in artifacts
            and request.pdf_options
            and request.pdf_options.grayscale
        ):
            artifacts["pdf"] = await convert_to_grayscale(artifacts["pdf"])

        uploads = await _upload_artifacts(artifacts, operation_id, project_id)

        content_hash = hashlib.sha256(
            captured.html.encode("utf-8")
        ).hexdigest()
        duration_ms = int((time.monotonic() - start) * 1000)
        output_keys: dict[str, Any] = dict(uploads)
        output_keys[operations.FINGERPRINT_KEY] = fingerprint

        operations.complete_operation(
            operation_id=operation_id,
            url_final=captured.final_url,
            content_hash=content_hash,
            output_keys=output_keys,
            structured_data=structured,
            extraction_tier="heuristic",
            cache_hit=False,
            duration_ms=duration_ms,
        )
        # Unified ops billing: the operation_id makes a retry/replay of this
        # exact operation a no-op at the ledger (contract item 5).
        usage.increment_perceive_usage(
            project_id, idempotency_key=f"v2:op:perceive:{operation_id}"
        )
        usage.record_storage_and_retention(
            project_id, uploads, user.get("subscription", {})
        )

        return PerceiveResponse(
            operation_id=operation_id,
            status="completed",
            url=url,
            url_final=captured.final_url,
            content_hash=content_hash,
            render_quality=None,
            options_echo=_options_echo(request, outputs),
            cache_hit=False,
            outputs=outputs_from_keys(output_keys, project_id),
            structured=structured,
            extraction_tier="heuristic",
            tokens=PerceiveTokens(),
            cost_cents=0.0,
            duration_ms=duration_ms,
            warnings=warnings,
        )
    except HTTPException:
        raise
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        operations.fail_operation(
            operation_id=operation_id,
            error_message=str(exc),
            duration_ms=duration_ms,
        )
        raise


def _serve_from_cache(
    cached: PerceiveOperation,
    operation_id: str,
    project_id: int,
    url: str,
    outputs: list[str],
    start: float,
    batch_id: Optional[str] = None,
    request: Optional[PerceiveRequest] = None,
    request_warnings: Optional[list[str]] = None,
) -> PerceiveResponse:
    """Record a cache-hit operation and answer from the cached row."""
    operations.create_operation(
        operation_id=operation_id,
        project_id=project_id,
        url=url,
        outputs_requested=outputs,
        batch_id=batch_id,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    cached_keys = dict(cached.output_keys or {})
    # F1: the hit row must NOT inherit the render row's fingerprint —
    # otherwise a hit row (fresh created_at, live fingerprint) is itself
    # a cache candidate and an hourly poller renews the TTL forever.
    hit_row_keys = dict(cached_keys)
    hit_row_keys.pop(operations.FINGERPRINT_KEY, None)
    operations.complete_operation(
        operation_id=operation_id,
        url_final=cached.url_final,
        content_hash=cached.content_hash,
        output_keys=hit_row_keys,
        structured_data=cached.structured_data,
        extraction_tier=cached.extraction_tier,
        cache_hit=True,
        duration_ms=duration_ms,
        render_quality_score=cached.render_quality_score,
    )
    # Cache hits bill like any operation (the quota gates operations, not
    # renders); the hit row's OWN operation_id keys the ledger, so a fresh
    # render and its later cache hits each bill exactly once.
    usage.increment_perceive_usage(
        project_id, idempotency_key=f"v2:op:perceive:{operation_id}"
    )
    return PerceiveResponse(
        operation_id=operation_id,
        status="completed",
        url=url,
        url_final=cached.url_final,
        content_hash=cached.content_hash,
        status_code=cached_keys.get(operations.HTTP_STATUS_KEY),
        render_quality=cached.render_quality_score,
        deductions=dict(cached_keys.get(operations.DEDUCTIONS_KEY) or {}),
        options_echo=(
            _options_echo(request, outputs) if request is not None else None
        ),
        cache_hit=True,
        outputs=outputs_from_keys(cached_keys, project_id),
        structured=cached.structured_data,
        extraction_tier=cached.extraction_tier,  # type: ignore[arg-type]
        tokens=PerceiveTokens(),
        cost_cents=0.0,
        duration_ms=duration_ms,
        warnings=list(request_warnings or []),
    )


def _process_outputs(
    request: PerceiveRequest,
    outputs: list[str],
    extracts: list[str],
    carried: _Captured,
    warnings: list[str],
) -> tuple[dict[str, bytes], Optional[dict[str, Any]]]:
    """CPU-bound output materialization (runs in a worker thread)."""
    html = carried.html or ""
    final_url = carried.final_url or str(request.url)

    scraping = None
    # The scrap pass feeds links/images/html_cleaned/metadata/tables and
    # the FULL-page markdown; the main-content markdown path has its own
    # extractor and does not need it.
    needs_scrap = bool(extracts) or any(
        name in outputs for name in ("html_cleaned", "links", "images")
    ) or ("markdown" in outputs and not request.only_main_content)
    if needs_scrap:
        scraping = scrap_html(final_url, html)

    artifacts: dict[str, bytes] = {}
    cleaned_html = (scraping.cleaned_html or "") if scraping else ""
    main_bytes: Optional[bytes] = None

    wants_main = request.only_main_content and "markdown" in outputs
    if wants_main or "main_content" in extracts:
        # B4 candidate ensemble: structural strip vs Readability vs the
        # full page, scored by prose retention + code-block retention vs
        # nav chrome; the full page always remains eligible so an
        # over-aggressive extractor can never ship a stub.
        candidates: list[ContentCandidate] = []
        extraction = extract_main_content(html)
        if not extraction.aborted:
            structural_md = generate_markdown_bytes(
                extraction.html, final_url, images_to_alt=True
            ).decode("utf-8", errors="replace")
            if structural_md.strip():
                candidates.append(
                    ContentCandidate(
                        source="structural", markdown=structural_md
                    )
                )
        try:
            readability_md = html_to_markdown(
                html, final_url, extract_article=True
            )
            if readability_md.strip():
                candidates.append(
                    ContentCandidate(
                        source="readability", markdown=readability_md
                    )
                )
        except Exception:  # noqa: BLE001 — a candidate failing is fine
            logger.warning(
                "readability candidate failed for %s", final_url,
                exc_info=True,
            )
        full_page_md = generate_markdown_bytes(
            html, final_url, images_to_alt=True
        ).decode("utf-8", errors="replace")
        selected = select_main_content(candidates, full_page_md)
        if selected.fell_back_to_full_page and candidates:
            warnings.append(
                "only_main_content: no extraction retained enough of the "
                "page's content; returned the full page instead "
                "(fidelity guard)."
            )
        main_bytes = selected.markdown.encode("utf-8")

    if "markdown" in outputs:
        if request.only_main_content and main_bytes is not None:
            artifacts["markdown"] = main_bytes
        else:
            artifacts["markdown"] = generate_markdown_bytes(
                cleaned_html or html, final_url
            )
    if "html_cleaned" in outputs:
        artifacts["html_cleaned"] = (cleaned_html or html).encode("utf-8")
    if "html_raw" in outputs:
        artifacts["html_raw"] = html.encode("utf-8")
    if "links" in outputs and scraping is not None:
        artifacts["links"] = json.dumps(
            serialize_links(scraping), ensure_ascii=False
        ).encode("utf-8")
    if "images" in outputs and scraping is not None:
        artifacts["images"] = json.dumps(
            serialize_images(scraping), ensure_ascii=False
        ).encode("utf-8")
    if "pdf" in outputs and carried.pdf_bytes is not None:
        artifacts["pdf"] = carried.pdf_bytes
    if "screenshot_full_page" in outputs and carried.screenshot_bytes is not None:
        artifacts["screenshot_full_page"] = carried.screenshot_bytes
    if "screenshot" in outputs and carried.screenshot_viewport_bytes is not None:
        artifacts["screenshot"] = carried.screenshot_viewport_bytes

    structured: Optional[dict[str, Any]] = None
    if "structured" in outputs:
        structured = {}
        if "metadata" in extracts and scraping is not None:
            structured["metadata"] = scraping.metadata or {}
        if "structured_data" in extracts:
            structured["structured_data"] = extract_json_ld(html)
        if "headings" in extracts:
            structured["headings"] = extract_headings(cleaned_html or html)
        if "tables" in extracts and scraping is not None:
            structured["tables"] = serialize_tables(scraping)
        if "main_content" in extracts and main_bytes is not None:
            text = main_bytes.decode("utf-8", errors="replace")
            if not text.strip():
                warnings.append(
                    "main_content extraction produced no text for this "
                    "page; the field is empty."
                )
            if len(text) > _MAIN_CONTENT_MAX_CHARS:
                text = text[:_MAIN_CONTENT_MAX_CHARS]
                warnings.append(
                    "main_content truncated to "
                    f"{_MAIN_CONTENT_MAX_CHARS} characters."
                )
            structured["main_content"] = text

    return artifacts, structured


async def _upload_artifacts(
    artifacts: dict[str, bytes], operation_id: str, project_id: int
) -> dict[str, dict[str, Any]]:
    """Upload every artifact to storage; boto3 is sync, so each call runs
    in a worker thread. Returns {output: {key, size_bytes, content_type}}."""
    uploads: dict[str, dict[str, Any]] = {}
    for name, payload in artifacts.items():
        filename = _artifact_filename(operation_id, name)
        result = await asyncio.to_thread(
            upload_to_gcs, payload, str(project_id), V2_PERCEIVE_ENDPOINT, filename
        )
        uploads[name] = {
            "key": result["object_key"],
            "size_bytes": result["file_size"],
            "content_type": _content_type_for(name),
        }
    return uploads
