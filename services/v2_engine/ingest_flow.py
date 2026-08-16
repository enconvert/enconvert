"""/v2/ingest orchestration (Task H.7, plan sections 4 + 8).

``/v2/ingest`` (Firecrawl ``/crawl`` + chunking) turns a site or an explicit
URL list into RAG-ready chunks and emits ONE JSONL file. The job lifecycle is
durable end-to-end:

    queued -> discovering -> processing -> completed | failed | canceled

``process_job`` is the single worker entry point (driven by ``ingest_worker``,
the droplet-local in-process queue — F.8's no-GCP pattern, made durable here
because H.7 finally owns the ch_ingest_jobs/ch_ingest_pages tables F.8
lacked). It is RESTART-SAFE and IDEMPOTENT: each phase reads its own progress
from the DB, so a process that died mid-job is re-enqueued at boot and
continues from where it stopped — already-completed pages keep their staged
per-page JSONL in Spaces and are not re-rendered.

Cost & quota model (coexistence rule 3): ingest bills its OWN ``ingest_pages``
counter, one per page whose render+chunk+stage completed. It uses
``perceive_flow.render_html`` (the persistence-free render entry point, like
H.5 distill) — NOT a full /v2/perceive operation — so a page render never
double-charges perceive quota nor litters ch_perceive_operations. Rendering
is sequential through the shared Chromium singleton (plan A5).

Per-page staging: each completed page's chunks are written to a deterministic
Spaces key (``v2-ingest-pages/{job}_{md5(url)}.jsonl``) re-derivable from
(project, job, url) via ``storage.build_object_key`` — that is what makes
partial recovery cheap. On completion the per-page objects are concatenated
into the final ``v2-ingest/{job}.jsonl`` and the now-redundant staging objects
are deleted.

Security: ingest renders are credential-free (no auth/cookies/headers in the
request), so nothing secret is persisted for the durable resume. SSRF is
screened in the worker (discover_flow on the seed, render_html per page), not
the handler, keeping submit instant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections import Counter
from typing import Any, BinaryIO, List, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from fastapi import HTTPException

from api.deps import check_ops_quota
from api.v2.schemas.discover import DiscoverRequest
from api.v2.schemas.ingest import (
    IngestJobResponse,
    IngestJobSummary,
    IngestRequest,
)
from models import IngestJob, IngestPage
from monitoring import posthog_client
from services.v2_engine import ingest_store, usage
from services.v2_engine.url_safety import assert_public_http_url
from utils import webhook_secret
from utils.callback_notifier import (
    WebhookDeliveryResult,
    deliver_signed_webhook,
)
from services.v2_engine.chunking.semantic import (
    DEFAULT_MAX_WORDS,
    DEFAULT_SENTENCE_OVERLAP,
    chunk_markdown,
)
from services.browser.converters.arun_flow import CONTENT_JSON
from services.markdown.nonhtml import fenced_document
from services.v2_engine.crawl4ai_processors import generate_markdown_bytes
from services.v2_engine.page_markdown import (
    full_page_markdown,
    main_content_markdown,
)
from services.v2_engine.markdown_jsonl import (
    encode_jsonl,
    page_records,
    safe_source_label,
)
from utils.storage import (
    DO_SPACES_BUCKET,
    build_object_key,
    delete_from_storage,
    download_from_storage,
    generate_presigned_url,
    get_s3_client,
    upload_fileobj_to_gcs,
    upload_to_gcs,
)
from utils.subscription import get_effective_subscription

logger = logging.getLogger(__name__)

# Spaces path segments (also fed to build_object_key for re-derivation).
PAGE_ENDPOINT = "v2-ingest-pages"   # transient per-page staging
FINAL_ENDPOINT = "v2-ingest"        # the deliverable JSONL

_TERMINAL_STATUSES = ("completed", "failed", "canceled")

# Recorded on a page row that resolved to content an earlier page in the same
# job already contributed. The row stays 'completed' with chunk_count=0: it is
# not a failure (nothing went wrong, and counting it as one would make a
# perfectly healthy job report pages_failed > 0), it simply adds nothing.
DUPLICATE_NOTE = "duplicate content: already ingested from an earlier page in this job"

# Ceiling on a raw (text/plain, JSON) body surfaced verbatim. An HTML page is
# bounded by extraction; a raw body is not, and it is copied through markdown,
# chunks, JSONL records and the staged upload. 8 MB is far above the real
# cases (an llms.txt is ~100 KB, an llms-full.txt a few MB) and far below what
# would matter on a 1 GB droplet.
MAX_NON_HTML_CHARS = 8 * 1024 * 1024


_SELF_URL_PLACEHOLDER = "\x00self\x00"


def content_fingerprint(markdown: str, *urls: Optional[str]) -> str:
    """sha256 of a page's markdown with its OWN address neutralised.

    The same document served at two addresses is byte-different only
    because its relative links resolve against different bases — the
    LlamaIndex docs home appeared at ``/en/stable/`` and at ``/``, whose
    markdown differed solely in six anchor hrefs. A raw hash calls those
    two distinct pages and ships every chunk twice.

    Only the page's OWN urls are folded, so two genuinely different pages
    can never collide: their prose differs, and every other link is left
    exactly as it is.
    """
    text = markdown
    variants: set[str] = set()
    for url in urls:
        if not url:
            continue
        variants.add(url)
        variants.add(url.rstrip("/"))
    # Longest first so a shorter variant cannot pre-empt a longer one. The
    # lookahead matches the url only as a COMPLETE url (end, fragment, or a
    # markdown/quote delimiter) — never as the prefix of a longer path, or a
    # page served at the site root would also fold every other link on the
    # site and two genuinely different pages could collide.
    for variant in sorted(variants, key=len, reverse=True):
        if not variant:
            continue
        text = re.sub(
            re.escape(variant) + r"(?=[#)\s\"']|$)",
            _SELF_URL_PLACEHOLDER,
            text,
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PageIdentitySet:
    """The pages a job has already contributed, by identity and by content.

    Two layers, because neither alone is sufficient:

    * ``final_url`` — the post-redirect address. Catches a redirect
      collision even when the two renders are not byte-identical (rotating
      nonces, timestamps, A/B variants). Only known after the render, so it
      cannot move upstream into discovery.
    * ``content_hash`` — sha256 of the page markdown. Catches identical
      content served from genuinely different addresses (print views,
      mirrored paths), and is the layer that survives a worker restart:
      it is re-seeded from the completed page rows, which already persist
      ``content_hash``, so no new column and no migration is needed.

    Seeded from completed rows ONLY, so a page that failed before it could
    contribute never suppresses a later identical page.
    """

    def __init__(self, pages: Optional[List[IngestPage]] = None) -> None:
        self._urls: set[str] = set()
        self._hashes: set[str] = set()
        for page in pages or []:
            if page.status == "completed" and page.content_hash:
                self._hashes.add(page.content_hash)

    def contains(self, final_url: Optional[str], content_hash: str) -> bool:
        if final_url and final_url in self._urls:
            return True
        return content_hash in self._hashes

    def add(self, final_url: Optional[str], content_hash: str) -> None:
        if final_url:
            self._urls.add(final_url)
        self._hashes.add(content_hash)


# ── Config (persisted in ch_ingest_jobs.chunk_options JSONB) ─────────────────


def build_job_config(request: IngestRequest) -> dict[str, Any]:
    """Serialize the resume-relevant request surface for the worker.

    Only non-secret options are persisted (ingest has no auth/cookies/headers
    by design), so the durable resume never stores credentials.
    """
    return {
        "chunk": {
            "max_words": request.chunk.max_words,
            "sentence_overlap": request.chunk.sentence_overlap,
        },
        "markdown": {
            "only_main_content": request.only_main_content,
            # Tri-state resolved at submit so a resumed job keeps the
            # caller's effective choice even if the default ever changes.
            "truncate_data_arrays": (
                request.only_main_content
                if request.truncate_data_arrays is None
                else request.truncate_data_arrays
            ),
        },
        "render": {
            "wait_for": request.wait_for,
            "wait_timeout_ms": request.wait_timeout_ms,
            "respect_robots": request.respect_robots,
        },
        "discovery": {
            "max_pages": request.max_pages,
            "max_depth": request.max_depth,
            "same_domain_only": request.same_domain_only,
            "include_patterns": list(request.include_patterns),
            "exclude_patterns": list(request.exclude_patterns),
            "respect_robots": request.respect_robots,
        },
    }


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _page_filename(job_id: str, url: str) -> str:
    return f"{job_id}_{hashlib.md5(url.encode('utf-8')).hexdigest()}.jsonl"


def _final_filename(job_id: str) -> str:
    return f"{job_id}.jsonl"


def page_object_key(project_id: int, job_id: str, url: str) -> str:
    """Deterministic staging key for one page's JSONL (re-derivable on resume)."""
    return build_object_key(str(project_id), PAGE_ENDPOINT, _page_filename(job_id, url))


def _extract_title(html: str) -> str:
    """Best-effort page title: <title>, else first <h1>, else ''."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — title is metadata, never fail the page
        return ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title:
            return title[:512]
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(" ", strip=True)
        if text:
            return text[:512]
    return ""


def _markdown_for(
    html: str,
    base_url: str,
    *,
    content_category: Optional[str] = None,
    content_type: Optional[str] = None,
    only_main_content: bool = True,
    truncate_arrays: bool = True,
) -> str:
    """The page's Markdown for chunking — the SAME rendition /v2/perceive ships.

    Ingest used to run Crawl4AI's Fit Markdown here, which is why the same
    URL came out of the two endpoints looking like two different products:
    the pruning filter deletes every ``<header>`` by tag name (so modern doc
    sites lost their ``<h1>`` and standfirst on every page), prunes
    link-only table cells and list labels individually (ragged tables, list
    items starting with a bare ``:``), and has no notion of cookie banners,
    icon-font glyphs, empty headings or numeric-array bloat. See
    ``page_markdown`` for the measured comparison.

    Non-HTML bodies never reach an HTML parser:

    * ``text/plain`` is ALREADY a line-structured document — it is passed
      through verbatim so the heading-aware chunker sees its real structure.
      (Fencing it would make the whole file one atomic chunk, which is the
      opposite of what a RAG pipeline wants.)
    * ``application/json`` has no meaningful split point, so it is fenced as
      one atomic block via the shared ``nonhtml`` helper, whose fence is
      always longer than any backtick run in the body.
    """
    if content_category is not None:
        body = html
        if len(body) > MAX_NON_HTML_CHARS:
            # A raw body has no structure to prune, so nothing downstream
            # bounds it: before this path existed the newline collapse
            # accidentally capped it by producing zero chunks. Truncate
            # visibly rather than let one llms-full.txt drive the whole
            # per-page pipeline (markdown -> chunks -> records -> upload)
            # to several times its size on a small droplet.
            body = body[:MAX_NON_HTML_CHARS]
            body += (
                f"\n\n[truncated: the source exceeded "
                f"{MAX_NON_HTML_CHARS} characters]\n"
            )
        if content_category == CONTENT_JSON:
            return fenced_document(body, language="json", frontmatter=False)
        return body

    warnings: List[str] = []
    if only_main_content:
        main = main_content_markdown(
            html, base_url, warnings, truncate_arrays=truncate_arrays
        )
        if main and main.strip():
            return main.decode("utf-8")
    full = full_page_markdown(html, base_url, truncate_arrays=truncate_arrays)
    if full and full.strip():
        return full.decode("utf-8")
    # Last resort: the raw conversion, un-prepped. Reached only when both
    # curated paths produced nothing at all.
    return generate_markdown_bytes(html, base_url).decode("utf-8")


# ── Discovery ────────────────────────────────────────────────────────────────


# Schema ceiling on DiscoverRequest.max_urls — the sitemap probe asks for
# this instead of max_pages so pages_found reports the site's TRUE unique
# URL count (sitemap parsing is pure HTTP; nothing extra is fetched).
_SITEMAP_PROBE_MAX_URLS = 1000


def _fold_www_duplicates(urls: List[str]) -> List[str]:
    """Drop URLs that differ from an earlier one only by a ``www.`` host
    prefix (or a trailing slash).

    Discovery seeds the map with the request URL, so an apex seed plus a
    www-canonical sitemap yields the same homepage twice (live QA:
    enconvert.com ingested ``https://enconvert.com/`` AND
    ``https://www.enconvert.com/`` as two pages). First occurrence wins;
    the render follows redirects, so keeping either variant is correct.
    """
    seen: set[str] = set()
    out: List[str] = []
    for url in urls:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        folded_host = host[4:] if host.startswith("www.") else host
        key = f"{folded_host}|{parts.path.rstrip('/')}|{parts.query}"
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


async def _discover_urls(
    job: IngestJob, discovery: dict[str, Any], user: dict
) -> tuple[List[str], Optional[int], bool]:
    """Resolve the URL list to ingest, plus honest discovery stats.

    Returns ``(urls, pages_found, truncated)``:

    * ``urls`` — the ordered, de-duplicated, ``max_pages``-capped list the
      job will actually process (drives ``pages_discovered``, unchanged
      semantics);
    * ``pages_found`` — unique eligible URLs discovery yielded BEFORE the
      cap (sitemap: true unique count up to the schema ceiling; crawl:
      bounded by the crawl budget; None for explicit-urls mode);
    * ``truncated`` — True when at least one more unique URL existed past
      the cap/budget (drives the response warning).

    ``urls`` mode returns the explicit list; ``sitemap``/``crawl`` run
    ``discover_flow`` (which SSRF-screens the seed and raises HTTPException
    on a private/blocked host).
    """
    max_pages = int(discovery.get("max_pages", 50))
    if job.mode == "urls":
        # Explicit list: de-dup order-preserving; the schema already bounds
        # the count, so max_pages (a discovery knob) does not apply here.
        explicit = list(dict.fromkeys(job.source_urls or []))
        return explicit, None, False

    from services.v2_engine import discover_flow

    # Sitemap probing is pure HTTP over already-parsed sitemap files, so ask
    # for the schema ceiling to learn the site's real size; crawl fetches a
    # page per URL, so its probe stays at max_pages (the fetch budget).
    probe_max = (
        _SITEMAP_PROBE_MAX_URLS if job.mode == "sitemap" else max_pages
    )
    discover_request = DiscoverRequest(
        url=job.source_url or "",
        mode=job.mode,  # "sitemap" | "crawl"
        max_urls=probe_max,
        max_depth=int(discovery.get("max_depth", 2)),
        same_domain_only=bool(discovery.get("same_domain_only", True)),
        include_patterns=list(discovery.get("include_patterns", [])),
        exclude_patterns=list(discovery.get("exclude_patterns", [])),
        respect_robots=bool(discovery.get("respect_robots", False)),
    )
    result = await discover_flow.run(discover_request, user)
    unique = _fold_www_duplicates(list(dict.fromkeys(result.urls)))
    urls = unique[:max_pages]
    truncated = bool(result.truncated) or len(unique) > len(urls)
    return urls, len(unique), truncated


# ── Per-page processing ──────────────────────────────────────────────────────


async def _url_to_markdown(
    page: IngestPage, render: dict[str, Any], markdown: dict[str, Any]
) -> tuple[str, str, str, str]:
    """Render a URL page to (markdown, title, source_ref, final_url).

    ``allow_tls=False`` is deliberate. The engine ladder's TLS rung is a
    plain HTTP fetch with no JavaScript, and the render-quality scorer
    cannot tell a complete server-rendered page from a shell that has not
    hydrated — so it scored those bodies 1.00 and never escalated. On the
    ETL corpus that meant ingest captured pre-hydration HTML on every
    JS-rendered documentation site: React Suspense payloads still parked
    in ``<div hidden id="S:0">``, skeleton loaders whose screen-reader
    label ("Loading") leaked into the chunks, un-dismissed cookie banners,
    and (measured on docs.pinecone.io) less than half the page's text.
    Ingest builds a durable retrieval corpus, so it pays for the real
    browser render — the same trade /v2/watch already makes to keep its
    baselines comparable.
    """
    from services.v2_engine import perceive_flow

    rendered = await perceive_flow.render_html(
        page.url,
        respect_robots=bool(render.get("respect_robots", False)),
        wait_for=render.get("wait_for"),
        wait_timeout_ms=int(render.get("wait_timeout_ms", 30000)),
        allow_tls=False,
    )
    html = rendered.html or ""
    final_url = rendered.final_url or page.url
    # Both are CPU-bound bs4/markdown passes over a page that can be
    # megabytes: offload them so the shared event loop keeps serving.
    text = await asyncio.to_thread(
        _markdown_for,
        html,
        final_url,
        content_category=rendered.content_category,
        content_type=rendered.content_type,
        only_main_content=bool(markdown.get("only_main_content", True)),
        truncate_arrays=bool(markdown.get("truncate_data_arrays", True)),
    )
    title = await asyncio.to_thread(_extract_title, html)
    # source_ref stays the REQUESTED url (that is the provenance the caller
    # asked for and what metadata.source_url has always carried); final_url
    # is returned alongside it because the post-redirect address is the
    # page's real identity — two discovered URLs that redirect to the same
    # place are one page (see _process_one_page's duplicate handling).
    return text, title, page.url, final_url


async def _file_to_markdown(page: IngestPage) -> tuple[str, str, str]:
    """Download an uploaded file and convert it to (markdown, title, source_ref).

    ``page.url`` holds the uploaded file's Spaces object key (migration 020);
    ``page.filename`` is the original name used for format detection and as the
    chunk source label. The same heading-aware chunker then runs over the
    resulting Markdown exactly as for a rendered web page.
    """
    from services.markdown import convert_to_markdown

    data = await asyncio.to_thread(download_from_storage, page.url)
    md_bytes = await convert_to_markdown(data, page.filename or "upload")
    markdown = md_bytes.decode("utf-8")
    # page.filename is the RAW upload name (kept raw in the DB for audit).
    # Basename + normalize it for the deliverable; never fall back to page.url,
    # which is an internal Spaces object key and must not leak to the customer.
    label = safe_source_label(page.filename or "upload")
    return markdown, label, label


class EmptyPageError(Exception):
    """A page yielded no extractable text (blocked, a JS shell, a 404 body).

    Distinguished from a crash so the caller records an honest SKIP instead
    of counting an empty page as a successful one — a job whose only page was
    empty used to report ``completed`` with ``total_chunks: 0`` and ship an
    empty JSONL.
    """


async def _process_one_page(
    job: IngestJob,
    page: IngestPage,
    *,
    max_words: int,
    sentence_overlap: int,
    render: dict[str, Any],
    markdown_cfg: Optional[dict[str, Any]] = None,
    seen: Optional["PageIdentitySet"] = None,
) -> tuple[int, bool]:
    """(URL render | file convert) -> markdown -> chunk -> stage JSONL -> complete.

    Returns ``(chunk_count, is_duplicate)``. Raises ``EmptyPageError`` when
    the page produced no text at all, and re-raises fetch/convert/upload
    failures so the caller records a failed page; one bad page never sinks
    the job.

    Duplicate handling: discovery works on URL strings, so it cannot know
    that two structurally different URLs redirect to the same resource
    (``docs.llamaindex.ai/en/stable/`` and ``docs.llamaindex.ai/`` both land
    on ``developers.llamaindex.ai/python/framework/``, which shipped every
    chunk twice). That fact only exists after the render, so the check lives
    here: a page whose post-redirect URL or whose markdown content hash was
    already produced by an earlier page in this job is completed with zero
    chunks rather than duplicated into the deliverable.
    """
    seen = PageIdentitySet() if seen is None else seen
    final_url: Optional[str] = None
    if page.source_type == "file":
        markdown, title, source_ref = await _file_to_markdown(page)
    else:
        markdown, title, source_ref, final_url = await _url_to_markdown(
            page, render, markdown_cfg or {}
        )

    if not markdown.strip():
        raise EmptyPageError("the page returned no extractable content")

    content_hash = content_fingerprint(markdown, final_url, page.url)
    is_duplicate = seen.contains(final_url, content_hash)

    # Offloaded like every other CPU-bound step here: the ingest worker runs
    # on the same event loop that serves HTTP, so parsing a multi-megabyte
    # page inline would stall every other request on the droplet.
    chunks = (
        []
        if is_duplicate
        else await asyncio.to_thread(
            chunk_markdown,
            markdown,
            max_words=max_words,
            sentence_overlap=sentence_overlap,
        )
    )
    # id_seed is the page's unique identity (URL, or the uploaded file's object
    # key); source_ref is only the display label and is NOT unique for files.
    records = page_records(
        chunks, source_url=source_ref, title=title, id_seed=page.url
    )
    blob = encode_jsonl(records)

    await asyncio.to_thread(
        upload_to_gcs,
        blob,
        str(job.project_id),
        PAGE_ENDPOINT,
        _page_filename(job.job_id, page.url),
    )

    word_count = sum(chunk.word_count for chunk in chunks)
    await asyncio.to_thread(
        ingest_store.complete_page,
        page.id,
        chunk_count=len(chunks),
        word_count=word_count,
        content_hash=content_hash,
        note=DUPLICATE_NOTE if is_duplicate else None,
    )
    if not is_duplicate:
        seen.add(final_url, content_hash)
    return len(chunks), is_duplicate


# ── Worker entry point ───────────────────────────────────────────────────────


async def process_job(job_id: str) -> None:
    """Run (or resume) one ingest job end-to-end. Never raises for a job-level
    fault — it records the failure on the row; only asyncio.CancelledError
    (shutdown) propagates so the job is resumed on the next boot."""
    job = await asyncio.to_thread(ingest_store.get_job, job_id)
    if job is None:
        logger.error("ingest process_job: %s not found", job_id)
        return
    if job.status in _TERMINAL_STATUSES:
        return  # already done / canceled — idempotent

    # Off-request signal: the worker (not the submit handler) is what actually
    # begins the durable work, so v2_ingest_started fires here.
    posthog_client.capture_project_event(job.project_id, "v2_ingest_started", {
        "job_id": job.job_id,
        "mode": job.mode,
    })

    # Effective (request-path-equivalent) subscription: the admin default
    # project must get its unlimited plan here too, or the per-page quota
    # re-check below denies every page the submit handler accepted.
    sub = await asyncio.to_thread(get_effective_subscription, job.project_id)
    if sub is None:
        await asyncio.to_thread(
            ingest_store.fail_job, job_id, "subscription unavailable"
        )
        return
    user = {"id": job.project_id, "subscription": sub}

    config = job.chunk_options or {}
    chunk_cfg = config.get("chunk", {})
    render_cfg = config.get("render", {})
    discovery_cfg = config.get("discovery", {})
    # Jobs submitted before the markdown options existed have no "markdown"
    # block; the defaults inside _markdown_for match what they used to get
    # once the pipeline itself was fixed, so a resumed old job is consistent.
    markdown_cfg = config.get("markdown", {})
    max_words = int(chunk_cfg.get("max_words", DEFAULT_MAX_WORDS))
    sentence_overlap = int(chunk_cfg.get("sentence_overlap", DEFAULT_SENTENCE_OVERLAP))

    # ── Discovery (skipped on resume once pages exist) ───────────────────────
    pages = await asyncio.to_thread(ingest_store.list_pages, job_id)
    if not pages:
        # allowed_from includes "discovering" so a job that crashed mid-discovery
        # (status already 'discovering', no page rows yet) resumes cleanly;
        # re-running discovery is safe because create_pages is idempotent. A
        # canceled/terminal job is not in the set, so this still aborts on cancel.
        if not await asyncio.to_thread(
            ingest_store.transition_status, job_id, "discovering",
            allowed_from=("queued", "discovering"),
        ):
            return  # canceled or already advanced by another path
        try:
            urls, pages_found, truncated = await _discover_urls(
                job, discovery_cfg, user
            )
        except HTTPException as exc:
            await asyncio.to_thread(
                ingest_store.fail_job, job_id, f"discovery rejected: {exc.detail}"
            )
            return
        except Exception:  # noqa: BLE001 — discovery fault fails the job
            # Full detail to server logs only; the job's error_message is
            # returned to the client, so it must not echo library/internal text.
            logger.exception("ingest %s: discovery crashed", job_id)
            await asyncio.to_thread(
                ingest_store.fail_job,
                job_id,
                "discovery failed: the site could not be crawled.",
            )
            return
        if not urls:
            await asyncio.to_thread(
                ingest_store.fail_job, job_id, "no URLs to ingest"
            )
            return
        await asyncio.to_thread(ingest_store.create_pages, job_id, urls)
        await asyncio.to_thread(
            ingest_store.set_pages_discovered,
            job_id,
            len(urls),
            pages_found=pages_found,
            truncated=truncated,
        )
        pages = await asyncio.to_thread(ingest_store.list_pages, job_id)
    else:
        # Resume: the page rows are authoritative; leave the migration-026
        # discovery stats untouched (they were written by the original
        # discovery pass).
        await asyncio.to_thread(
            ingest_store.set_pages_discovered, job_id, len(pages)
        )

    if not await asyncio.to_thread(ingest_store.transition_status, job_id, "processing"):
        return  # canceled during discovery

    # ── Processing (only pages this job still owes) ──────────────────────────
    # Running counters drive live progress (GET /v2/ingest/:id); already-done
    # pages from a resume are seeded so progress never goes backwards.
    done_pages = [p for p in pages if p.status == "completed"]
    processed = len(done_pages)
    failed = len([p for p in pages if p.status in ("failed", "skipped")])
    chunks_total = sum(p.chunk_count for p in done_pages)
    quota_denied: Optional[str] = None  # 402 detail once the quota trips
    # Re-seeded from the DB, so a resumed job still recognises the pages the
    # interrupted attempt already contributed (see PageIdentitySet).
    seen = PageIdentitySet(pages)

    async def _sync_progress() -> None:
        # EVERY page outcome (processed, failed, skipped) must reach the job
        # row — a skip that bypassed this left pages_failed at 0 while the
        # terminal error said all pages failed (the docs.carverjs.dev bug).
        await asyncio.to_thread(
            ingest_store.update_job_progress,
            job_id,
            pages_processed=processed,
            pages_failed=failed,
            total_chunks=chunks_total,
        )

    for page in pages:
        if page.status not in ingest_store.RESUMABLE_PAGE_STATUSES:
            continue

        # Cancel check FIRST — the quota-denied skip tail below must observe
        # a mid-tail cancel too, or a 1000-page tail churns on unstoppably.
        current = await asyncio.to_thread(ingest_store.get_job, job_id)
        if current is None or current.status == "canceled":
            # Cancellation observed: stop, leave the page rows for the record —
            # but the uploaded sources are now unreachable, so drop them here.
            # _assemble is never reached on this path.
            if current is not None:
                await _cleanup_source_files(job_id, pages)
            return

        if quota_denied is not None:
            await asyncio.to_thread(ingest_store.skip_page, page.id, quota_denied)
            failed += 1
            await _sync_progress()
            continue

        try:
            # check_ops_quota opens a sync DB session (current usage period) —
            # offload it so the event loop is never blocked per page.
            await asyncio.to_thread(check_ops_quota, user, 1)
        except HTTPException as exc:
            # Keep the real denial — it is our own client-safe 402 text and
            # _assemble surfaces it on the job.
            quota_denied = (
                exc.detail
                if isinstance(exc.detail, str)
                else "monthly ops quota exhausted"
            )
            await asyncio.to_thread(ingest_store.skip_page, page.id, quota_denied)
            failed += 1
            await _sync_progress()
            continue

        await asyncio.to_thread(ingest_store.mark_page_processing, page.id)
        try:
            chunk_count, is_duplicate = await _process_one_page(
                job,
                page,
                max_words=max_words,
                sentence_overlap=sentence_overlap,
                render=render_cfg,
                markdown_cfg=markdown_cfg,
                seen=seen,
            )
            if not is_duplicate:
                # job_id + hashed page URL (or object key for file pages) is
                # unique per page, so a worker crash-and-resume that
                # re-processes a page bills it exactly once at the ledger.
                # A duplicate contributed nothing, so it is not billed — and
                # because its row is left 'completed', a resume never
                # re-enters it and can never bill it later either.
                page_digest = hashlib.md5(page.url.encode("utf-8")).hexdigest()[:16]
                await asyncio.to_thread(
                    usage.increment_ingest_usage,
                    job.project_id,
                    idempotency_key=f"v2:op:ingest:{job_id}:{page_digest}",
                )
            processed += 1
            chunks_total += chunk_count
        except asyncio.CancelledError:
            raise  # shutdown: leave the row 'processing'; resumed next boot
        except EmptyPageError as exc:
            # Not a crash and not a success: the render/convert worked but
            # there was nothing to chunk. Recorded as a skip so the job's
            # counters stay honest instead of reporting a completed page
            # that contributed an empty file.
            logger.info(
                "ingest %s: no extractable content for %s", job_id, _safe(page.url)
            )
            await asyncio.to_thread(ingest_store.skip_page, page.id, str(exc))
            failed += 1
        except Exception as exc:  # noqa: BLE001 — isolate per-page failures
            logger.exception("ingest %s: page failed for %s", job_id, _safe(page.url))
            await asyncio.to_thread(ingest_store.fail_page, page.id, str(exc))
            failed += 1

        await _sync_progress()

    # ── Assembly ─────────────────────────────────────────────────────────────
    await _assemble(job, sub)


async def _cleanup_source_files(job_id: str, pages: List[IngestPage]) -> None:
    """Delete a file-mode job's uploaded SOURCE objects (best-effort).

    ``page.url`` holds the upload's Spaces object key for file pages. Called on
    EVERY terminal path — assembled, all-failed, lost-before-assembly and
    canceled — because the submit-time ch_scheduled_deletions backstop is a 24h
    safety net, not the prompt path. Guarded by source_type so URL-mode pages
    (whose url is a real URL, not a key) are never touched. Never raises:
    cleanup must not sink an otherwise-finished job.
    """
    for page in pages:
        if page.source_type != "file":
            continue
        try:
            await asyncio.to_thread(delete_from_storage, page.url)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ingest %s: source cleanup failed for %s",
                job_id,
                _safe(page.url),
                exc_info=True,
            )


def _stream_object_to_file(object_key: str, sink: BinaryIO) -> None:
    """Stream one Spaces object into ``sink`` in 64KB chunks (blocking).

    Bypasses download_from_storage (whole-object bytes) so assembly never
    holds a full page in RAM — run via asyncio.to_thread. A failure rolls
    the partial write back so the spool stays a clean concatenation of
    complete pages, then re-raises for the caller's per-page handling.
    """
    client = get_s3_client()
    offset = sink.tell()
    try:
        body = client.get_object(Bucket=DO_SPACES_BUCKET, Key=object_key)["Body"]
        for chunk in body.iter_chunks(65536):
            sink.write(chunk)
    except Exception:
        sink.seek(offset)
        sink.truncate()
        raise


async def _assemble(job: IngestJob, subscription: dict) -> None:
    """Concatenate completed per-page JSONL into the final file, or fail the
    job if nothing completed. Skips when the job was canceled mid-flight."""
    job_id = job.job_id
    project_id = job.project_id

    current = await asyncio.to_thread(ingest_store.get_job, job_id)
    if current is None or current.status == "canceled":
        if current is not None:
            # Canceled mid-flight: the uploads are dead weight — drop them now
            # rather than waiting for the retention backstop.
            await _cleanup_source_files(
                job_id, await asyncio.to_thread(ingest_store.list_pages, job_id)
            )
        return

    pages = await asyncio.to_thread(ingest_store.list_pages, job_id)
    completed = [p for p in pages if p.status == "completed"]
    failed_or_skipped = [p for p in pages if p.status in ("failed", "skipped")]

    if not completed:
        # Surface the dominant SKIP reason (skip reasons are our own
        # client-safe strings — e.g. the 402 quota detail); failed-render
        # exceptions stay in logs/page rows, never on the job (see the
        # discovery-failure note above about echoing internal text).
        skip_reasons = [
            p.error_message
            for p in pages
            if p.status == "skipped" and p.error_message
        ]
        detail = ""
        if skip_reasons:
            top_reason, _ = Counter(skip_reasons).most_common(1)[0]
            detail = f" — {top_reason}"
        await asyncio.to_thread(
            ingest_store.fail_job,
            job_id,
            f"all {len(pages)} page(s) failed or were skipped{detail}",
        )
        await _cleanup_source_files(job_id, pages)
        return

    # Stream each completed page's staged JSONL straight into one on-disk
    # spool (2026-07-28 memory incident): the old path held every page's
    # bytes in a list, doubled them with b"".join() and kept the blob
    # resident through the upload — ~3x the final file on the 1GB droplet.
    # If an object is missing (it must not be, since complete_page runs only
    # after a successful upload), demote that page to 'failed' so
    # total_chunks stays consistent with the bytes actually assembled —
    # never report chunks the final file does not contain.
    spool = tempfile.NamedTemporaryFile(
        prefix="ingest_final_", suffix=".jsonl", delete=False
    )
    try:
        assembled_pages = 0
        assembled_chunks = 0
        missing = 0
        for page in completed:
            key = page_object_key(project_id, job_id, page.url)
            try:
                await asyncio.to_thread(_stream_object_to_file, key, spool)
                assembled_pages += 1
                assembled_chunks += page.chunk_count
            except Exception:  # noqa: BLE001 — a missing stage object drops one page
                logger.warning(
                    "ingest %s: staged page object missing (%s)", job_id, _safe(page.url)
                )
                await asyncio.to_thread(
                    ingest_store.fail_page, page.id, "staged object missing at assembly"
                )
                missing += 1

        if not assembled_pages:
            await asyncio.to_thread(
                ingest_store.fail_job,
                job_id,
                "all completed pages were lost before assembly",
            )
            await _cleanup_source_files(job_id, pages)
            return

        total_chunks = assembled_chunks
        pages_done = len(completed) - missing
        pages_failed = len(failed_or_skipped) + missing

        # upload_fileobj_to_gcs derives the key via build_object_key exactly
        # like upload_to_gcs did — the FINAL_ENDPOINT key shape is unchanged.
        spool.flush()
        spool.seek(0)
        upload = await asyncio.to_thread(
            upload_fileobj_to_gcs,
            spool,
            str(project_id),
            FINAL_ENDPOINT,
            _final_filename(job_id),
            file_size=os.path.getsize(spool.name),
        )
    finally:
        spool.close()
        try:
            os.unlink(spool.name)
        except OSError:
            pass
    output_key = upload["object_key"]

    committed = await asyncio.to_thread(
        ingest_store.complete_job,
        job_id,
        output_key=output_key,
        total_chunks=total_chunks,
        pages_processed=pages_done,
        pages_failed=pages_failed,
    )

    if committed:
        await asyncio.to_thread(
            usage.record_storage_and_retention,
            project_id,
            {"jsonl": {"key": output_key, "size_bytes": upload["file_size"]}},
            subscription,
        )
        # H.8: fire the signed completion webhook. Re-fetch so the payload
        # carries the committed output_key + final counts. Best-effort — a dead
        # endpoint records a non-delivery + alert, it never sinks a done job.
        final_job = await asyncio.to_thread(ingest_store.get_job, job_id)
        if final_job is not None and final_job.webhook_url:
            try:
                await deliver_ingest_webhook(final_job)
            except Exception:  # noqa: BLE001 — delivery must not fail completion
                logger.exception("ingest %s: completion webhook crashed", job_id)
            final_job = await asyncio.to_thread(ingest_store.get_job, job_id)

        posthog_client.capture_project_event(project_id, "v2_ingest_completed", {
            "job_id": job_id,
            "mode": job.mode,
            "pages_processed": pages_done,
            "pages_failed": pages_failed,
            "total_chunks": total_chunks,
            "webhook_delivered": bool(getattr(final_job, "webhook_delivered", False))
            if final_job is not None else False,
        })
    else:
        # Canceled during assembly: the final object is orphaned — remove it.
        await asyncio.to_thread(delete_from_storage, output_key)

    # The per-page staging objects are redundant once assembled; best-effort
    # cleanup so a no-storage-plan project does not accumulate them.
    for page in completed:
        await asyncio.to_thread(
            delete_from_storage, page_object_key(project_id, job_id, page.url)
        )

    # File-mode jobs: the uploaded source files (page.url is their object key)
    # are consumed once the JSONL is assembled — remove them too (best-effort).
    await _cleanup_source_files(job_id, pages)


# ── Webhook delivery (Task H.8) ──────────────────────────────────────────────


async def deliver_ingest_webhook(job: IngestJob) -> WebhookDeliveryResult:
    """SSRF-screen, sign, and POST a job's completion webhook.

    Shared by the auto-fire path (``_assemble`` on completion) and the manual
    retry handler, so signing, screening and bookkeeping live in exactly one
    place. ``webhook_delivered`` is set to the outcome; on retry exhaustion a
    dashboard alert is raised (verification (d)). Returns the delivery result
    so the retry handler can shape its HTTP response; the worker ignores it.

    Payload (plan H.8 step 3): ``{job_id, status, output_url, pages_processed,
    total_chunks}`` as compact, key-sorted JSON. ``output_url`` is a
    freshly-signed link to the final JSONL (``None`` if it cannot be signed).
    Never raises for a delivery-level fault — the worker treats a dead endpoint
    as a recorded non-delivery, not a job failure.
    """
    url = job.webhook_url
    if not url:
        return WebhookDeliveryResult(False, None, 0, "no_webhook")

    # SSRF screen at DELIVERY time (see schemas/ingest.py webhook_url note): the
    # URL was only scheme-checked at submit, so a private / metadata host is
    # inert until we POST to it. Screen immediately before the send.
    try:
        await assert_public_http_url(url)
    except HTTPException:
        logger.warning("ingest %s: webhook URL blocked by SSRF guard", job.job_id)
        await asyncio.to_thread(
            ingest_store.set_webhook_delivered, job.job_id, False
        )
        # A permanently-misconfigured (internal) URL never reaches the network,
        # so surface it as an alert too — otherwise the dashboard shows
        # "not delivered" with no explanation (CR finding).
        await asyncio.to_thread(
            ingest_store.record_webhook_failure_alert,
            job,
            0,
            reason="the endpoint URL resolves to a private or internal address.",
        )
        return WebhookDeliveryResult(False, None, 0, "blocked_url")

    secret = await asyncio.to_thread(
        webhook_secret.get_or_create_webhook_secret, job.project_id
    )
    if not secret:
        logger.error(
            "ingest %s: webhook secret unavailable for project %s",
            job.job_id,
            job.project_id,
        )
        return WebhookDeliveryResult(False, None, 0, "no_secret")

    output_url: Optional[str] = None
    if job.output_key:
        try:
            output_url = generate_presigned_url(
                job.output_key, str(job.project_id)
            )
        except Exception:  # noqa: BLE001 — a stale key must not abort delivery
            logger.warning(
                "ingest %s: webhook output presign failed", job.job_id, exc_info=True
            )

    payload = {
        "job_id": job.job_id,
        "status": job.status,
        "output_url": output_url,
        "pages_processed": job.pages_processed,
        "total_chunks": job.total_chunks,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    result = await deliver_signed_webhook(url, body, secret)

    await asyncio.to_thread(
        ingest_store.set_webhook_delivered, job.job_id, result.delivered
    )
    if not result.delivered:
        await asyncio.to_thread(
            ingest_store.record_webhook_failure_alert, job, result.attempts
        )
    return result


# ── Response shaping ─────────────────────────────────────────────────────────


def _signed_output_url(job: IngestJob) -> Optional[str]:
    """Sign the final JSONL URL for a completed job (None otherwise / on error)."""
    if job.status == "completed" and job.output_key:
        try:
            return generate_presigned_url(job.output_key, str(job.project_id))
        except Exception:  # noqa: BLE001 — a stale key must not 500 the caller
            logger.warning(
                "ingest %s: output presign failed", job.job_id, exc_info=True
            )
    return None


def _truncation_warning(job: IngestJob) -> Optional[str]:
    """Human-readable truncation note (migration 026 stats), or None.

    Central so POST, GET and the MCP passthrough all explain the
    pages_found vs pages_discovered gap — the live-QA complaint was a
    719-URL sitemap silently reported as "pages_discovered: 50".
    """
    found = getattr(job, "pages_found", None)
    if found is not None and found > (job.pages_discovered or 0):
        discovery = (job.chunk_options or {}).get("discovery", {})
        cap = discovery.get("max_pages", 50)
        return (
            f"discovery found {found} unique URLs; the job was capped at "
            f"max_pages={cap}, so {job.pages_discovered} pages were "
            "enqueued. Raise max_pages to ingest more of the site."
        )
    if getattr(job, "discovery_truncated", False):
        return (
            "discovery stopped at the max_pages cap; the site has more "
            "URLs than this job enqueued. Raise max_pages to ingest more."
        )
    return None


def wants_duplicate_scan(job: IngestJob) -> bool:
    """True when ``job_response`` could produce a duplicate warning for this
    job, so a caller knows whether loading its page rows is worth a query.

    Only terminal jobs can have duplicates, and clients stop polling once a
    job is terminal — so the extra read happens roughly once per job, not
    once per poll.
    """
    return job.status in ("completed", "failed")


def _duplicate_warning(job: IngestJob, pages: List[IngestPage]) -> Optional[str]:
    """Note how many pages resolved to already-ingested content, or None.

    Silence here would be misleading in the other direction: pages_processed
    counts a duplicate (nothing failed), so without this the caller sees
    "10 pages processed" and a deliverable built from fewer.

    Pure: the caller supplies the rows. ``job_response`` runs inline in async
    request handlers, and every DB read in this module is deliberately
    offloaded with ``asyncio.to_thread`` — a synchronous query here would
    block the event loop the ingest worker shares with the HTTP server.
    """
    if not wants_duplicate_scan(job):
        return None
    duplicates = sum(
        1
        for page in pages
        if page.status == "completed" and page.error_message == DUPLICATE_NOTE
    )
    if not duplicates:
        return None
    return (
        f"{duplicates} of {len(pages)} page(s) resolved to content already "
        "ingested from another page in this job (usually a redirect to the "
        "same destination) and were excluded from the output."
    )


def job_response(
    job: IngestJob,
    *,
    warnings: Optional[List[str]] = None,
    pages: Optional[List[IngestPage]] = None,
) -> IngestJobResponse:
    """Build the API view of a job, signing the output URL when present.

    ``pages`` are the job's page rows when the caller has already loaded
    them (off-thread); without them the duplicate note is simply omitted
    rather than fetched synchronously from an async handler.
    """
    combined = list(warnings or [])
    note = _truncation_warning(job)
    if note is not None:
        combined.append(note)
    if pages is not None:
        duplicate_note = _duplicate_warning(job, pages)
        if duplicate_note is not None:
            combined.append(duplicate_note)
    return IngestJobResponse(
        job_id=job.job_id,
        status=job.status,  # type: ignore[arg-type]
        mode=job.mode,  # type: ignore[arg-type]
        pages_discovered=job.pages_discovered,
        pages_found=getattr(job, "pages_found", None),
        discovery_truncated=bool(getattr(job, "discovery_truncated", False)),
        pages_processed=job.pages_processed,
        pages_failed=job.pages_failed,
        total_chunks=job.total_chunks,
        output_url=_signed_output_url(job),
        error_message=job.error_message,
        webhook_url=job.webhook_url,
        webhook_delivered=job.webhook_delivered,
        created_at=job.created_at,
        completed_at=job.completed_at,
        warnings=combined,
    )


def job_summary(job: IngestJob) -> IngestJobSummary:
    """Compact dashboard-list view of a job (GET /v2/ingest, Task H.8).

    ``webhook_url`` is reduced to a ``webhook_configured`` boolean — the list
    never echoes the raw endpoint back into the table, only whether one is set.
    """
    return IngestJobSummary(
        job_id=job.job_id,
        status=job.status,  # type: ignore[arg-type]
        mode=job.mode,  # type: ignore[arg-type]
        pages_discovered=job.pages_discovered,
        pages_found=getattr(job, "pages_found", None),
        discovery_truncated=bool(getattr(job, "discovery_truncated", False)),
        pages_processed=job.pages_processed,
        pages_failed=job.pages_failed,
        total_chunks=job.total_chunks,
        output_url=_signed_output_url(job),
        error_message=job.error_message,
        webhook_configured=bool(job.webhook_url),
        webhook_delivered=job.webhook_delivered,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


def _safe(url: str) -> str:
    """Truncate an attacker-influenceable URL before logging."""
    return (url or "")[:256]
