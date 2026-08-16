"""Shared page -> Markdown rendition used by every V2 flow.

This is the ONE implementation of "turn rendered page HTML into the
Markdown we ship". It was born inside ``perceive_flow`` as the
``only_main_content`` path; ``/v2/ingest`` used to run Crawl4AI's Fit
Markdown (``PruningContentFilter``) instead, which is why the same page
came out of the two endpoints looking like two different products. It
lives here, flow-agnostic (pure HTML/Markdown in, bytes out, no request
objects, no DB, no storage), so perceive, ingest and any future consumer
share one behaviour and one set of fixes.

Two pieces do the work:

* ``main_content_markdown`` — the B4 candidate ensemble (structural
  chrome strip / Readability / semantic main region / full page), scored
  by ``main_content.select_main_content`` against a chrome-free fidelity
  baseline, then tidied by ``markdown_post.tidy_markdown``.
* ``full_page_markdown`` — the un-curated page, for callers that asked
  for it verbatim (``only_main_content=false``).

Why the ensemble and not Fit Markdown (measured on the ETL-076..084
corpus, 21 real documentation pages):

* ``PruningContentFilter`` hard-codes ``header`` in ``excluded_tags``
  and decomposes it by tag NAME on the whole page, so every doc site
  that wraps its own title block in an HTML5 ``<header>`` (Mintlify,
  RSC/Next.js docs, ...) lost its ``<h1>`` and standfirst on EVERY page.
* Its per-node score prunes ``<td>``/``<li>`` individually, so a cell or
  list item whose entire content is a short link scores under the
  threshold and is deleted — producing ragged tables and list items that
  begin with a bare ``:`` where the label used to be.
* It has no chrome vocabulary of its own, so cookie banners, "Was this
  page helpful", "Skip to main content", "Copy page" and skeleton
  "Loading" labels survived into the chunks.
* It never removes Private Use Area glyphs (icon-font artifacts) or
  empty ``##`` headings left behind when a heading's text is separated
  from its marker by a nested block element.
* It has no numeric-array truncation, so one notebook page's embedding
  dumps produced 168 KB of markdown where the tidy pass produces 11 KB.
"""

from __future__ import annotations

import logging
from typing import List

from services.markdown.html_md import html_to_markdown
from services.v2_engine.crawl4ai_processors import generate_markdown_bytes
from services.v2_engine.main_content import (
    ContentCandidate,
    extract_main_content,
    extract_main_region,
    guard_baseline_html,
    select_main_content,
)
from services.v2_engine.markdown_post import tidy_markdown
from services.v2_engine.markdown_prep import prepare_html

logger = logging.getLogger(__name__)

__all__ = ["main_content_markdown", "full_page_markdown"]


def main_content_markdown(
    html: str,
    final_url: str,
    warnings: List[str],
    *,
    truncate_arrays: bool = True,
    images_to_alt: bool = True,
) -> bytes:
    """Markdown of the page's main content (QA fixes A+B+C).

    The B4 candidate ensemble: up to four markdown renditions compete —
    the structural chrome strip, Readability article extraction, the
    semantic main region, and the full page — and
    ``select_main_content`` picks the cleanest one that retains the
    page's real prose, code blocks and headings. The full page is always
    eligible, so an over-aggressive extractor loses the election instead
    of shipping a stub; when the full page wins, a warning says
    stripping was not usable.

    ``markdown_prep.prepare_html`` runs ONCE, before any candidate is
    built, so every candidate is free of UI chrome and DOM artifacts no
    matter which one wins. This is the round-3 QA fix: the ensemble
    elects ``main_region`` or ``readability`` on most real pages, and
    neither of those ever saw the chrome strip — which is why buttons,
    screen-reader labels and feedback widgets kept reaching the output
    on pages where ``only_main_content`` was set.

    ``images_to_alt`` renders long image URLs as their alt text (B5): a
    CDN URL is routinely 120+ characters of pure token bloat, and
    /v2/perceive keeps the full image list available via
    ``outputs=["images"]``. A caller with no such second channel — an
    uploaded HTML file — passes False so the references survive.
    """
    candidates: list[ContentCandidate] = []
    html = prepare_html(html, strip_chrome=True)

    extraction = extract_main_content(html)
    if not extraction.aborted:
        structural_md = generate_markdown_bytes(
            extraction.html, final_url, images_to_alt=images_to_alt
        ).decode("utf-8", errors="replace")
        if structural_md.strip():
            candidates.append(
                ContentCandidate(source="structural", markdown=structural_md)
            )

    try:
        readability_md = html_to_markdown(
            html, final_url, extract_article=True
        )
        if readability_md.strip():
            candidates.append(
                ContentCandidate(source="readability", markdown=readability_md)
            )
    except Exception:  # noqa: BLE001 — a candidate failing is not an error
        logger.warning(
            "readability candidate failed for %s", final_url, exc_info=True
        )

    # Semantic main region (<main>/[role=main]/<article>) — positive
    # selection for chrome-dominated pages where the negative strip's
    # word-count guard aborts (Mintlify docs, marketing mega-menus).
    # The region gets the same strip pass to drop in-region chrome
    # (breadcrumbs, "on this page" TOCs); if even that trips the guard,
    # the raw region competes as-is. Appended AFTER the existing
    # candidates so score ties keep today's winner.
    region_html = extract_main_region(html)
    if region_html:
        region_extraction = extract_main_content(region_html)
        region_source = (
            region_extraction.html
            if not region_extraction.aborted
            else region_html
        )
        region_md = generate_markdown_bytes(
            region_source, final_url, images_to_alt=images_to_alt
        ).decode("utf-8", errors="replace")
        if region_md.strip():
            candidates.append(
                ContentCandidate(source="main_region", markdown=region_md)
            )

    # An aborted strip still competes, under the election's stricter
    # prose-retention floor: on link-dense pages the guard's raw word
    # ratio reads chrome removal as content loss, while the election
    # (which ignores pure-link lines) can tell prose loss from chrome
    # loss. The catastrophic case the guard exists for (the article
    # itself deleted) fails the 0.70 retention floor and never wins.
    if extraction.aborted and extraction.stripped_html:
        unguarded_md = generate_markdown_bytes(
            extraction.stripped_html, final_url, images_to_alt=images_to_alt
        ).decode("utf-8", errors="replace")
        if unguarded_md.strip():
            candidates.append(
                ContentCandidate(
                    source="structural_unguarded", markdown=unguarded_md
                )
            )

    full_page_md = generate_markdown_bytes(
        html, final_url, images_to_alt=images_to_alt
    ).decode("utf-8", errors="replace")

    # Retention yardstick: the VISIBLE, chrome-free page (never returned,
    # only scored against). The raw full page stays the fallback output.
    baseline_md = generate_markdown_bytes(
        guard_baseline_html(html), final_url, images_to_alt=images_to_alt
    ).decode("utf-8", errors="replace")

    selected = select_main_content(
        candidates, full_page_md, baseline_markdown=baseline_md
    )
    logger.info(
        "only_main_content ensemble for %s: %s (retention %.2f, nav %.2f, "
        "%d candidates)",
        final_url,
        selected.source,
        selected.retention,
        selected.nav_ratio,
        len(candidates),
    )
    if selected.fell_back_to_full_page:
        # Warn on EVERY full-page fallback — the old `and candidates`
        # guard silently returned an unstripped page when no candidate
        # was produced at all (observed on figma.com when the strip
        # crashed), which is exactly when the caller most needs to know.
        warnings.append(
            "only_main_content: no extraction retained enough of the "
            "page's content; returned the full page instead "
            "(fidelity guard)."
        )
    tidied = tidy_markdown(selected.markdown, truncate_arrays=truncate_arrays)
    if truncate_arrays and len(tidied) < len(selected.markdown) * 0.5:
        warnings.append(
            "long numeric data arrays were truncated in the markdown "
            "(set truncate_data_arrays=false to keep them in full)."
        )
    return tidied.encode("utf-8")


def full_page_markdown(
    html: str,
    final_url: str,
    *,
    truncate_arrays: bool = False,
) -> bytes:
    """Markdown of the WHOLE page — nothing a caller asked to keep is cut.

    ``prepare_html(strip_chrome=False)`` still runs: those rules are
    conversion-quality fixes (code-language stamping, invisible-character
    scrubbing, block-link restructuring), not content removal.
    """
    prepped = prepare_html(html, strip_chrome=False)
    markdown = generate_markdown_bytes(prepped, final_url).decode(
        "utf-8", errors="replace"
    )
    return tidy_markdown(markdown, truncate_arrays=truncate_arrays).encode(
        "utf-8"
    )
