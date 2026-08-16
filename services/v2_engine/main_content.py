"""Main-content extraction for /v2/perceive markdown (``only_main_content``).

Two layers:

1. ``extract_main_content`` — the structural strip (fixes B1+B2+B6),
   guarded by a retained-words ratio.
2. ``select_main_content`` — the B4 candidate ensemble: given several
   markdown candidates for the same page (structural strip, Readability
   article extraction, the full page), score each by how much of the
   page's real prose it RETAINS versus how much link-chrome it carries,
   and pick the cleanest candidate that keeps the content. Measured on
   the QA corpus no single extractor wins every page class — Readability
   is cleanest but collapses docs hubs and landing pages to a stub; the
   structural strip is safe but leaves chrome on div-soup sites when its
   own guard aborts. The ensemble is the only shape that scored both
   clean and complete.

Implements fixes B1+B2+B5+B6 from the 2026-08-06 QA root-cause report,
behind a fidelity guard:

* B1 — remove semantic boilerplate tags (``nav``/``header``/``footer``/
  ``aside``/``form``).
* B2 — remove div-soup boilerplate by role/class/id selector (Mintlify,
  Tailwind-style docs where nothing semantic marks the chrome).
* B6 — remove CSS-hidden nodes (``display:none``, ``visibility:hidden``,
  off-screen, ``opacity:0``): font-metric probes ("word word word…",
  "mmMwWLliI0fiflO&1") and tracking pixels that survive into markdown.
* Fidelity guard — stripping is measured, not trusted. If the visible
  word count after stripping falls below ``MIN_RETAINED_RATIO`` of the
  original, the whole strip is ABORTED and the original HTML returned
  with ``aborted=True`` so the caller can emit a warning. The report's
  section on filter tuning shows why this is non-negotiable: every
  aggressive config scored a perfect clutter metric by deleting 5 of 7
  real pages down to a single newline.

B5 (``images_to_alt``) lives at the markdown-generation call site in
``crawl4ai_processors.generate_markdown_bytes`` — it is an html2text
option, not a DOM transform.

The ensemble selector is PURE (text in, choice out) so it is fully
testable without a render; the orchestration that produces the actual
candidates lives in ``perceive_flow._main_content_markdown``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from utils.url_registrable import registered_domain_from_url

logger = logging.getLogger(__name__)

# B1 — semantic boilerplate. ``form`` is included deliberately: on the QA
# corpus forms are search boxes and newsletter signups; pages whose real
# content lives in a form (login walls) are flagged by the quality scorer,
# not served as "main content".
BOILERPLATE_TAGS: tuple[str, ...] = (
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "template",
    "iframe",
)


# B2 — div-soup boilerplate. Roles first (spec-defined, lowest false-positive
# risk), then the class/id vocabulary the corpus actually uses. Every entry
# here is protected by the fidelity guard below: a selector that eats a
# page's real content aborts the whole strip rather than shipping a stub.
BOILERPLATE_SELECTORS: str = ", ".join(
    (
        "[role=navigation]",
        "[role=banner]",
        "[role=contentinfo]",
        "[role=search]",
        "[role=complementary]",
        "[aria-hidden=true]",
        "[hidden]",
        "nav",
        ".sidebar",
        "#sidebar",
        ".side-bar",
        ".toc",
        "#toc",
        ".table-of-contents",
        ".breadcrumb",
        ".breadcrumbs",
        ".pagination",
        ".skip-link",
        ".skip-nav",
        "[class*=cookie-banner]",
        "[class*=cookie-consent]",
        "[id*=cookie-banner]",
        "[class*=announcement-bar]",
        "[class*=newsletter]",
        # Page-rating widgets ("Was this page helpful? Yes/No"). The
        # buttons themselves are removed by markdown_prep; this takes
        # the prompt paragraph that would otherwise be left stranded.
        "[class*=feedback]",
        "[id*=feedback]",
        # Docusaurus/Mintlify heading self-anchors, whose label is a
        # zero-width space.
        ".hash-link",
        ".anchor-link",
    )
)

# B6 — inline styles that make a node invisible to a human. Matched on the
# style attribute with whitespace stripped, so "display: none" and
# "display:none" both hit.
_HIDDEN_STYLE_MARKERS: tuple[str, ...] = (
    "display:none",
    "visibility:hidden",
    "opacity:0;",
    "opacity:0}",
    "left:-999",
    "left:-9999",
    "top:-999",
    "top:-9999",
    "text-indent:-999",
)

# Fidelity guard: abort the strip when it retains less than this share of
# the page's visible words. 0.45 is calibrated on the QA corpus: the one
# page the guard fires on (Mintlify/Anthropic docs, whose entire article
# sits inside containers the selector list also matches) retains ~1% and
# every legitimate strip retains 55%+, so the threshold has wide margin on
# both sides.
MIN_RETAINED_RATIO: float = 0.45


@dataclass(frozen=True)
class MainContentResult:
    """Outcome of one main-content strip.

    ``html`` keeps its original contract: the stripped page on success, the
    ORIGINAL page when the guard aborted (callers that only read ``html``
    are unaffected by an abort). ``stripped_html`` additionally carries the
    strip output even when the guard fired, so the ensemble can let an
    aborted strip COMPETE under the election's stricter prose-retention
    floor instead of discarding it outright (the Pinecone/Mintlify fix:
    on link-dense pages every legitimate strip trips the word-count guard,
    yet retains all the page's prose).
    """

    html: str
    aborted: bool
    words_before: int
    words_after: int
    stripped_html: str = ""


def _visible_word_count(soup: BeautifulSoup) -> int:
    for tag in soup(("script", "style", "noscript", "template")):
        tag.decompose()
    return len(soup.get_text(" ").split())


def _style_hides(style: str) -> bool:
    compact = style.replace(" ", "").lower()
    return any(marker in compact for marker in _HIDDEN_STYLE_MARKERS)


def extract_main_content(html: str) -> MainContentResult:
    """Strip site chrome from rendered HTML, guarded by fidelity.

    Returns the stripped HTML, or the ORIGINAL html with ``aborted=True``
    when stripping would delete more than ``1 - MIN_RETAINED_RATIO`` of
    the page's visible words. An article's own title block (link-poor
    ``<header>`` inside ``<main>``/``<article>``) is protected — see
    ``_protected_article_header``. Never raises: any parse failure
    degrades to the original HTML (an un-stripped page is a quality
    issue, a lost page is an incident).
    """
    if not html or not isinstance(html, str):
        return MainContentResult(html=html or "", aborted=False,
                                 words_before=0, words_after=0)
    try:
        base_soup = BeautifulSoup(html, "lxml")
        words_before = _visible_word_count(BeautifulSoup(html, "lxml"))

        # B6 first: hidden nodes are junk regardless of what they are, and
        # removing them BEFORE the guard keeps font-probe filler ("word
        # word word…") from inflating the retained-word denominator.
        # ``find_all`` materializes its result list up front, so when a
        # hidden ancestor is decomposed here its (also-matched) descendants
        # stay in the list as dead nodes with ``attrs=None`` — calling
        # ``.get`` on one raised AttributeError and silently degraded the
        # WHOLE strip to the original page (observed on figma.com, whose
        # chrome nests style-hidden nodes). Skip already-decomposed tags.
        for tag in base_soup.find_all(style=True):
            if tag.decomposed:
                continue
            if not _style_hides(tag.get("style") or ""):
                continue
            if _contains_content_region(tag):
                continue
            # A streaming-SSR payload is deferred content whichever way the
            # framework hides it. Guarding only the [hidden]-selector pass
            # below would still lose a payload that also carries
            # display:none — the same node, stripped one pass earlier.
            if is_streaming_ssr_payload(tag):
                continue
            tag.decompose()

        # B1 — semantic tags. An article's own title block (link-poor
        # <header> inside <main>/<article>) is content, not chrome — see
        # _protected_article_header.
        for tag_name in BOILERPLATE_TAGS:
            for tag in base_soup.find_all(tag_name):
                if tag.decomposed:
                    continue
                if tag_name == "header" and _protected_article_header(tag):
                    _trim_header_labels(tag)
                    continue
                tag.decompose()

        # B2 — role/class/id selectors. soupsieve raises on a bad selector;
        # the list above is static and covered by tests, but guard anyway.
        # Same decompose-while-iterating hazard as the B6 loop: ``select``
        # materializes its list, so skip nodes a matched ancestor already
        # took down. Nodes that CONTAIN the content region (streaming-SSR
        # hidden wrappers) are never removed — see _contains_content_region.
        try:
            deferred = controlled_ids(base_soup)
            for tag in base_soup.select(BOILERPLATE_SELECTORS):
                if tag.decomposed or _contains_content_region(tag):
                    continue
                # A collapsed tab panel inside the content region is
                # deferred CONTENT, not chrome: the [aria-hidden] and
                # [hidden] selectors above would otherwise delete the
                # inactive half of every tabbed code sample.
                if is_deferred_disclosure(tag, deferred):
                    continue
                # Likewise a React streaming-SSR payload that has not been
                # moved into place yet — hidden, but holding real article
                # content rather than chrome.
                if is_streaming_ssr_payload(tag):
                    continue
                tag.decompose()
        except Exception:  # noqa: BLE001 — selector engine failure
            logger.warning("boilerplate selector pass failed", exc_info=True)

        _prune_link_farms(base_soup, words_before)

        stripped = str(base_soup)
        words_after = _visible_word_count(BeautifulSoup(stripped, "lxml"))

        if words_before > 0 and (words_after / words_before) < MIN_RETAINED_RATIO:
            return MainContentResult(
                html=html,
                aborted=True,
                words_before=words_before,
                words_after=words_after,
                stripped_html=stripped,
            )
        return MainContentResult(
            html=stripped,
            aborted=False,
            words_before=words_before,
            words_after=words_after,
            stripped_html=stripped,
        )
    except Exception:  # noqa: BLE001 — extraction must degrade, never 500
        logger.warning("main-content extraction failed", exc_info=True)
        return MainContentResult(html=html, aborted=False,
                                 words_before=0, words_after=0,
                                 stripped_html=html)


# --- Off-site tool/share widget pruning -------------------------------------
#
# Some pages park a link hub INSIDE the content region: arXiv's abstract
# page keeps Google Scholar / NASA ADS / DBLP / BibSonomy / Reddit
# widgets inside <main>, as a sibling of the paper itself. No semantic
# tag marks it, so the tag and selector passes cannot see it.
#
# The discriminating signal is where the links POINT, and nothing else.
# Link DENSITY was tried first and measured as unsafe on real pages:
#   * arXiv's tool sidebar scores 0.62 link-words/words — and the paper's
#     AUTHOR LIST directly above it scores 0.67, so no density threshold
#     separates them;
#   * a 62-row "document loaders" reference table on the LangChain docs
#     scores 0.98, and pruning by density deleted it outright.
# Pointing at many unrelated sites, with almost no prose per link, is
# what a "find this elsewhere" bar does and what a content block does
# not: an author list or a docs card grid links to its own site, and a
# genuine further-reading list carries descriptive text per entry.
#
# Tables are exempt unconditionally. Tabular data is content by
# construction — the LangChain table above is 23 external domains at one
# word per link, which is to say: indistinguishable from a share bar by
# every metric except being a table.
_LINKFARM_TAGS: tuple[str, ...] = ("div", "section", "ul", "ol", "dl")
_LINKFARM_MIN_LINKS: int = 6
_LINKFARM_MAX_PROSE_SHARE: float = 0.25
_SHARE_MIN_DOMAINS: int = 4
_SHARE_MAX_WORDS_PER_LINK: float = 4.0

# Minimum visible words for a semantic main region to be worth proposing
# as a candidate — below this the region is a stub (an empty SPA <main>
# shell) and would only lose the election anyway.
_MIN_REGION_WORDS: int = 10

# High-confidence chrome, excluded from the retention BASELINE (not from
# any candidate): hidden nodes plus spec-defined navigation containers.
# A page's real content never legitimately lives in these, so removing
# them from the baseline cannot mask content loss — while leaving them in
# poisoned the floor with chrome prose (mega-menu product blurbs, hidden
# search-dialog code snippets, announcement banners) that no clean
# extraction can retain.
_BASELINE_CHROME_SELECTORS: tuple[str, ...] = (
    "[hidden]",
    "[aria-hidden=true]",
    "nav",
    "header",
    "footer",
    "[role=navigation]",
    "[role=banner]",
    "[role=contentinfo]",
)


# Roles that mark DEFERRED content — a closed tab, a collapsed section —
# rather than chrome. Docs sites ship the Python example in the active
# tab and the JavaScript one in an inert sibling; both are content, and
# dropping the inactive one made the extracted page depend on which tab
# happened to be selected at render time.
_DEFERRED_ROLES: frozenset[str] = frozenset({"tabpanel", "region"})


def controlled_ids(soup: BeautifulSoup) -> set[str]:
    """Every element id referenced by an ``aria-controls`` on the page."""
    ids: set[str] = set()
    try:
        for tag in soup.find_all(attrs={"aria-controls": True}):
            value = tag.get("aria-controls") or ""
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            ids.update(str(value).split())
    except Exception:  # noqa: BLE001 — an unusable index, not an error
        logger.warning("aria-controls scan failed", exc_info=True)
    return ids


def is_deferred_disclosure(tag: Any, controlled: set[str]) -> bool:
    """True for a collapsed tab/section that holds deferred CONTENT.

    Requires the node to sit inside the semantic content region, so a
    closed mega-menu in the site header — which frameworks also mark
    ``aria-hidden`` — is still treated as chrome.
    """
    try:
        role = (tag.get("role") or "").strip().lower()
        tag_id = tag.get("id")
        is_panel = role in _DEFERRED_ROLES or (
            bool(tag_id) and str(tag_id) in controlled
        )
        if not is_panel:
            return False
        return _inside_main_region(tag)
    except Exception:  # noqa: BLE001 — treat as chrome on any parse quirk
        return False


def _inside_main_region(tag: Any) -> bool:
    """True when ``tag`` sits inside the page's semantic content region."""
    try:
        if tag.find_parent(("main", "article")) is not None:
            return True
        return tag.find_parent(attrs={"role": "main"}) is not None
    except Exception:  # noqa: BLE001 — treat as chrome on any parse quirk
        return False


# React's streaming SSR (React 18+, Next.js app router, Remix) ships each
# Suspense boundary's payload as ``<div hidden id="S:0">…</div>`` and moves
# it into place from an inline script once the boundary resolves. Before
# that script runs — which is ALWAYS the case for a no-JavaScript fetch, and
# briefly true for a browser render captured mid-hydration — that container
# is marked hidden while holding real article content. The id shape is the
# framework's own (a letter plus a colon plus the boundary counter), so this
# recognises the convention rather than any particular site.
_SSR_PAYLOAD_ID_RE = re.compile(r"^[A-Za-z]:[0-9]+$")


def is_streaming_ssr_payload(tag: Any) -> bool:
    """True for a React streaming-SSR payload container.

    Such a node is deferred CONTENT, never chrome: deleting it deletes
    whatever the page had not finished hydrating (on platform.claude.com
    that was the whole code sample plus 2.3 KB of prose).
    """
    try:
        if tag.get("hidden") is None:
            return False
        tag_id = tag.get("id")
        return bool(tag_id) and bool(_SSR_PAYLOAD_ID_RE.match(str(tag_id)))
    except Exception:  # noqa: BLE001 — treat as ordinary on any parse quirk
        return False


def _contains_content_region(tag: Any) -> bool:
    """True when ``tag`` CONTAINS the page's semantic content region.

    Streaming-SSR frameworks (Next.js) deliver the whole app inside a
    ``<div hidden id="S:0">`` placeholder that client JS un-hides; on a
    non-hydrated snapshot (TLS-engine fetch of platform.claude.com) that
    wrapper is "hidden" yet holds the entire article. A hidden node that
    contains ``<main>``/``<article>``/``[role=main]`` is therefore never
    junk — deleting it deletes the page. Real chrome (cookie banners,
    mega-menus, sidebars) never wraps the content region.
    """
    try:
        if tag.find("main") is not None or tag.find("article") is not None:
            return True
        return tag.find(attrs={"role": "main"}) is not None
    except Exception:  # noqa: BLE001 — keep the node when in doubt
        return True


# An in-main <header> counts as the ARTICLE's title block only when it is
# link-poor — a title + intro carries at most a couple of anchor links,
# while a site mega-header nested inside a page-wide <main> wrapper
# carries dozens.
_ARTICLE_HEADER_MAX_LINKS: int = 4


_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")

# Inside a protected article header, a short text block BEFORE the title
# is a breadcrumb trail, an eyebrow ("Get started"), or a category
# label — navigation, not the article. Anything longer than this is a
# standfirst/subtitle and stays. Blocks AFTER the title are never
# touched, which is where subtitles normally live.
_EYEBROW_MAX_WORDS: int = 8


def _trim_header_labels(header: Any) -> None:
    """Drop breadcrumb/eyebrow blocks preceding an article's title.

    Called only for headers ``_protected_article_header`` kept. Those
    headers earn protection by carrying the page title, but modern docs
    themes put the breadcrumb trail in the same element — with no
    ``nav``, no ``aria-label`` and no ``breadcrumb`` class to match on
    (verified on two Mintlify sites), so only its POSITION identifies
    it.
    """
    try:
        heading = header.find(_HEADING_TAGS)
        if heading is None:
            return
        for child in header.children:
            if not isinstance(child, Tag) or child.decomposed:
                continue
            if child is heading or heading in child.descendants:
                return  # reached the title; everything after it stays
            if child.find(("pre", "table", "img")) is not None:
                continue
            text = child.get_text(" ", strip=True)
            if text and len(text.split()) <= _EYEBROW_MAX_WORDS:
                child.decompose()
    except Exception:  # noqa: BLE001 — trimming is best-effort
        logger.warning("header label trim failed", exc_info=True)


def _protected_article_header(tag: Any) -> bool:
    """True for a ``<header>`` that is an article's own title block.

    Mintlify-style docs put the page h1 + intro paragraph in a
    ``<header>`` INSIDE ``<main>`` — deleting it deletes the article's
    opening. Protection requires all three: it is a ``header`` tag, it
    sits inside the semantic main region, and it is link-poor (so a
    site-wide header nested in a page-spanning ``<main>`` wrapper is
    still treated as chrome).
    """
    try:
        if getattr(tag, "name", None) != "header":
            return False
        if not _inside_main_region(tag):
            return False
        return len(tag.find_all("a")) <= _ARTICLE_HEADER_MAX_LINKS
    except Exception:  # noqa: BLE001 — treat as chrome on any parse quirk
        return False


def _is_share_widget(tag: Any, links: list, words: int) -> bool:
    """True for a 'find/share this elsewhere' bar (see ``_SHARE_*``).

    Counts DISTINCT registrable domains among absolute link targets.
    Relative and same-site links collapse to nothing/one domain, so a
    docs card grid or an author list can never reach the threshold —
    only a block pointing at many unrelated sites can.
    """
    del tag  # signature kept uniform with the other block predicates
    if words / max(len(links), 1) > _SHARE_MAX_WORDS_PER_LINK:
        return False
    domains: set[str] = set()
    for link in links:
        href = (link.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        domain = registered_domain_from_url(href)
        if domain:
            domains.add(domain)
    return len(domains) >= _SHARE_MIN_DOMAINS


def _prune_link_farms(soup: BeautifulSoup, total_words: int) -> None:
    """Remove in-content off-site tool/share widgets (see ``_LINKFARM_*``).

    Runs as part of the guarded strip, so an over-eager prune costs the
    candidate the election rather than the page. Never touches a block
    that contains the semantic content region or a table.
    """
    try:
        for tag in soup.find_all(_LINKFARM_TAGS):
            if tag.decomposed:
                continue
            links = tag.find_all("a")
            if len(links) < _LINKFARM_MIN_LINKS:
                continue
            if _contains_content_region(tag):
                continue
            if tag.find("table") is not None:
                continue  # tabular data is content, whatever it links to
            words = len(tag.get_text(" ").split())
            if words == 0:
                continue
            if (
                total_words > 0
                and words / total_words > _LINKFARM_MAX_PROSE_SHARE
            ):
                continue  # too big to be a widget; this may be the page
            if _is_share_widget(tag, links, words):
                tag.decompose()
    except Exception:  # noqa: BLE001 — pruning is best-effort
        logger.warning("link-farm pruning failed", exc_info=True)


def guard_baseline_html(html: str) -> str:
    """The page as a reader sees it, minus declared navigation chrome.

    This is the retention-floor BASELINE for the ensemble: candidates are
    scored on how much of THIS prose they keep. It is never returned to a
    caller — the full page remains the fallback output. Chrome containers
    nested inside the semantic main region are KEPT (see
    ``_inside_main_region``), so deleting an article's own header can
    never be masked by the baseline. Degrades to the original html on any
    parse failure.
    """
    if not html or not isinstance(html, str):
        return html or ""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(style=True):
            if tag.decomposed:
                continue
            if not _style_hides(tag.get("style") or ""):
                continue
            if _contains_content_region(tag) or is_streaming_ssr_payload(tag):
                continue
            tag.decompose()
        for selector in _BASELINE_CHROME_SELECTORS:
            for tag in soup.select(selector):
                if tag.decomposed or _protected_article_header(tag):
                    continue
                if _contains_content_region(tag):
                    continue
                # The [hidden] selector would otherwise take the streaming-SSR
                # payload out of the yardstick, and a candidate that dropped
                # that content would then score full retention against it.
                if is_streaming_ssr_payload(tag):
                    continue
                tag.decompose()
        return str(soup)
    except Exception:  # noqa: BLE001 — baseline must degrade, never 500
        logger.warning("guard-baseline extraction failed", exc_info=True)
        return html


def extract_main_region(html: str) -> str | None:
    """The page's semantic main region (``<main>``/``[role=main]``/``<article>``).

    A POSITIVE-selection counterpart to the negative strip above: on pages
    whose chrome dwarfs the prose (docs sidebars, marketing mega-menus,
    footer link farms) the strip's word-count guard aborts, but the page
    itself already labels its content region. Returns the serialized
    region, or None when the page declares none (callers then simply do
    not get this candidate — behavior identical to before this fix).

    When several regions match (blog-index ``<article>`` cards), the
    wordiest one is proposed; the ensemble's prose-retention floor rejects
    it if it is not actually the page's content.
    """
    if not html or not isinstance(html, str):
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(("script", "style", "noscript", "template")):
            tag.decompose()
        # Pool ALL region markers and take the wordiest: on
        # platform.claude.com the <main> is a 121-word scroll-shell while
        # the real article is an <article> SIBLING of it — a tag-priority
        # order would lock onto the shell. When regions nest (article
        # inside main) the outermost is wordiest and wins, which is the
        # safe direction.
        nodes = list(soup.find_all(("main", "article")))
        nodes.extend(soup.select("[role=main]"))
        best = None
        best_words = 0
        for node in nodes:
            words = len(node.get_text(" ").split())
            if words > best_words:
                best, best_words = node, words
        if best is None or best_words < _MIN_REGION_WORDS:
            return None
        return str(best)
    except Exception:  # noqa: BLE001 — a missing candidate, never an error
        logger.warning("main-region extraction failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# B4 — the candidate ensemble selector
# ---------------------------------------------------------------------------

# A candidate must retain at least this share of the page's prose words
# (measured against the full-page markdown) to be eligible. Calibrated on
# the QA corpus: legitimate extractions retain 0.85+, while Readability's
# failure mode (a docs hub collapsed to a one-line stub) retains < 0.10 —
# the floor sits in the wide gap between them.
RETENTION_FLOOR: float = 0.70

# A markdown line contributes prose words only when, after link markup is
# reduced to its text, it still carries at least this many words — this
# keeps bare nav labels ("Home", "Pricing") out of the retention
# denominator so removing them never reads as content loss.
_PROSE_MIN_WORDS: int = 3

# Words shorter than this are ignored by the retention metric (articles,
# pronouns, markdown syntax residue) — the signal lives in content words.
_RETENTION_MIN_WORD_LEN: int = 4

# Below this many distinct prose words the baseline is too small to score
# against and the ensemble defers to the structural candidate.
_MIN_BASELINE_WORDS: int = 25

# Code blocks get their own retention gate, at BLOCK granularity: the
# prose word-set metric barely notices a dropped snippet (a page's prose
# dwarfs its code), and word-level code counting misses a lost one-line
# `pip install` fence when a bigger example survives. "Code blocks
# intact" is an explicit product requirement for the docs/RAG audience,
# so: a baseline fenced block is "retained" when at least half its words
# appear in the candidate's code; a candidate that retains fewer than
# _CODE_BLOCK_RETENTION of the baseline's blocks is ineligible no matter
# how clean it is. Tiny fences (< _MIN_CODE_BLOCK_WORDS words) carry no
# signal and are not scored.
_MIN_CODE_BLOCK_WORDS: int = 3
_CODE_BLOCK_RETENTION: float = 0.6

_LINK_MD_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]*)\)")

# --- Heading integrity ------------------------------------------------------
#
# A candidate that keeps a section's BODY but loses its HEADING has
# corrupted the document, and the prose-word metric cannot see it: a
# heading is two or three short words against thousands of body words.
# This is exactly how Readability failed on the QA corpus — it kept
# every paragraph of platform.claude.com and dropped six of its nine
# heading texts, which is where the bare "##" lines came from.
#
# The test is deliberately narrow: a heading counts as ORPHANED only
# when the candidate lost the heading text but KEPT the body that
# followed it. Legitimate chrome removal takes a heading and its section
# together and is therefore never penalised — which matters, because
# some pages carry real headings inside widgets we want gone (arXiv's
# "Bibliographic Tools").
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_EMPTY_HEADING_LINE_RE = re.compile(r"^#{1,6}\s*$")

# Lines scanned after a heading for a body probe, and the probe's
# minimum length in words (short lines match too easily by accident).
_HEADING_BODY_LOOKAHEAD: int = 6
_HEADING_BODY_MIN_WORDS: int = 5

# Orphaned headings tolerated before a candidate is disqualified. One
# can be a formatting quirk; two is a pattern.
_MAX_ORPHANED_HEADINGS: int = 2

# An orphaned H1 is the page's TITLE — losing it while keeping the
# article is on its own enough to disqualify a candidate. Readability
# does exactly this on docs hubs: it kept every paragraph of the
# LlamaIndex front page and dropped "Welcome to LlamaIndex".
_ORPHANED_H1_WEIGHT: int = _MAX_ORPHANED_HEADINGS


@dataclass(frozen=True)
class ContentCandidate:
    """One markdown rendition of a page, produced by a named extractor."""

    source: str
    markdown: str


@dataclass(frozen=True)
class SelectedContent:
    """The ensemble's choice for one page."""

    markdown: str
    source: str
    retention: float
    nav_ratio: float
    fell_back_to_full_page: bool


def _lines(markdown: str) -> list[str]:
    return [line.strip() for line in markdown.split("\n") if line.strip()]


def _nav_ratio(lines: list[str]) -> float:
    """Share of lines that are pure link chrome (no meaningful prose)."""
    if not lines:
        return 0.0
    nav = 0
    for line in lines:
        if not _LINK_MD_RE.search(line):
            continue
        residue = _LINK_MD_RE.sub("", line).strip(" *-|>#\t")
        if len(residue) <= 3:
            nav += 1
    return nav / len(lines)


def _is_nav_line(line: str) -> bool:
    """A line that is pure link markup with no meaningful prose residue."""
    if not _LINK_MD_RE.search(line):
        return False
    residue = _LINK_MD_RE.sub("", line).strip(" *-|>#\t")
    return len(residue) <= 3


def _prose_words(lines: list[str]) -> set[str]:
    """Content words from prose-bearing lines.

    Pure-link lines are excluded ENTIRELY — a docs sidebar is hundreds of
    multi-word link labels ("Starter Tutorial (Using OpenAI)"), and
    counting those as prose would make removing the sidebar read as
    content loss, which is exactly backwards. Links inside real
    sentences are reduced to their anchor text and their words count.
    """
    words: set[str] = set()
    for line in lines:
        if _is_nav_line(line):
            continue
        text = _LINK_MD_RE.sub(lambda m: m.group(1), line)
        tokens = text.split()
        if len(tokens) < _PROSE_MIN_WORDS:
            continue
        for token in tokens:
            token = token.strip(".,;:!?()[]{}`'\"*").lower()
            if len(token) >= _RETENTION_MIN_WORD_LEN and "http" not in token:
                words.add(token)
    return words


def _code_blocks(lines: list[str]) -> list[set[str]]:
    """Word set per fenced code block (``` ... ```), scorable blocks only."""
    blocks: list[set[str]] = []
    current: set[str] = set()
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            if in_fence and len(current) >= _MIN_CODE_BLOCK_WORDS:
                blocks.append(current)
            current = set()
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        for token in line.split():
            token = token.strip(".,;:!?()[]{}'\"").lower()
            if len(token) >= 3:
                current.add(token)
    if in_fence and len(current) >= _MIN_CODE_BLOCK_WORDS:
        blocks.append(current)  # unterminated fence at EOF
    return blocks


def _code_block_retention(
    candidate_lines: list[str], baseline_blocks: list[set[str]]
) -> float:
    """Fraction of baseline code blocks the candidate retained."""
    if not baseline_blocks:
        return 1.0
    candidate_code: set[str] = set()
    for block in _code_blocks(candidate_lines):
        candidate_code |= block
    retained = sum(
        1
        for block in baseline_blocks
        if len(block & candidate_code) / len(block) >= 0.5
    )
    return retained / len(baseline_blocks)


def _nav_line_count(lines: list[str]) -> int:
    """Absolute count of pure link-chrome lines (see ``_is_nav_line``)."""
    return sum(1 for line in lines if _is_nav_line(line))


def _empty_heading_count(lines: list[str]) -> int:
    """Headings the candidate emitted with no text (``##`` alone)."""
    return sum(1 for line in lines if _EMPTY_HEADING_LINE_RE.match(line))


def _flatten_text(line: str) -> str:
    """One markdown line reduced to comparable plain text."""
    text = _LINK_MD_RE.sub(lambda m: m.group(1), line)
    text = re.sub(r"[`*_#>|~\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _heading_probes(lines: list[str]) -> list[tuple[int, str, str]]:
    """``(level, heading text, body probe)`` for every heading found.

    The body probe is the first substantial line under the heading and
    before the next one — the evidence that this section survived.
    """
    probes: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_LINE_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        heading = _flatten_text(match.group(2))
        if len(heading) < 3:
            continue
        body = ""
        window = lines[index + 1 : index + 1 + _HEADING_BODY_LOOKAHEAD]
        for follower in window:
            if _HEADING_LINE_RE.match(follower):
                break
            probe = _flatten_text(follower)
            if len(probe.split()) >= _HEADING_BODY_MIN_WORDS:
                body = probe
                break
        if body:
            probes.append((level, heading, body))
    return probes


def _orphaned_heading_count(
    lines: list[str], probes: list[tuple[int, str, str]]
) -> int:
    """Weighted count of headings the candidate orphaned.

    A heading is orphaned when the candidate KEPT the section's body but
    LOST its title — the signature of a lossy article extractor, and
    invisible to the prose-word metric. Removing a heading together
    with its section (ordinary chrome stripping) scores zero.
    """
    if not probes:
        return 0
    blob = " ".join(_flatten_text(line) for line in lines)
    score = 0
    for level, heading, body in probes:
        if heading in blob or body not in blob:
            continue
        score += _ORPHANED_H1_WEIGHT if level == 1 else 1
    return score


def select_main_content(
    candidates: list[ContentCandidate],
    full_page_markdown: str,
    baseline_markdown: str | None = None,
) -> SelectedContent:
    """Pick the cleanest candidate that keeps the page's real content.

    ``full_page_markdown`` is the final fallback: it is always eligible
    (retention 1.0 by construction), so the ensemble can never return
    less content than the page has — an over-aggressive extractor loses
    the election instead of shipping a stub.

    ``baseline_markdown`` (optional) is the retention yardstick — the
    markdown of the VISIBLE, chrome-free page (``guard_baseline_html``).
    When omitted, the full page doubles as the baseline (the original
    behavior, kept for callers/tests that pass two arguments).

    Eligibility has three gates, each guarding a different kind of
    damage a clean-looking extraction can do: prose retention
    (``RETENTION_FLOOR``), code-block retention
    (``_CODE_BLOCK_RETENTION``), and heading integrity
    (``_MAX_ORPHANED_HEADINGS`` — a candidate that keeps a section but
    drops its title is disqualified).

    Selection: among eligible candidates, minimise the ABSOLUTE number
    of junk lines (nav chrome plus text-less headings); ties go to the
    shorter text. The count (not the ratio) is deliberate: on link-hub
    pages whose content IS links, stripping non-link chrome (footer
    taglines, search boxes) RAISED the ratio and handed the election to
    the unstripped page.
    """
    baseline_lines = _lines(
        baseline_markdown if baseline_markdown else full_page_markdown
    )
    baseline_words = _prose_words(baseline_lines)
    baseline_blocks = _code_blocks(baseline_lines)
    baseline_probes = _heading_probes(baseline_lines)

    scored: list[SelectedContent] = []
    for candidate in candidates:
        if not candidate.markdown.strip():
            continue  # an empty candidate is never a valid answer
        lines = _lines(candidate.markdown)
        if baseline_blocks and (
            _code_block_retention(lines, baseline_blocks)
            < _CODE_BLOCK_RETENTION
        ):
            continue  # dropped the page's code blocks — never eligible
        if (
            _orphaned_heading_count(lines, baseline_probes)
            >= _MAX_ORPHANED_HEADINGS
        ):
            continue  # kept the sections, lost their titles
        words = _prose_words(lines)
        retention = (
            len(words & baseline_words) / len(baseline_words)
            if baseline_words
            else 1.0
        )
        scored.append(
            SelectedContent(
                markdown=candidate.markdown,
                source=candidate.source,
                retention=retention,
                nav_ratio=_nav_ratio(lines),
                fell_back_to_full_page=False,
            )
        )

    full_page_lines = _lines(full_page_markdown)
    full_page = SelectedContent(
        markdown=full_page_markdown,
        source="full_page",
        retention=1.0,
        nav_ratio=_nav_ratio(full_page_lines),
        fell_back_to_full_page=True,
    )

    if len(baseline_words) < _MIN_BASELINE_WORDS:
        # Too little prose to score against; prefer the structural strip
        # (its own fidelity guard already ran) over an unscoreable vote.
        # main_region and the unguarded (aborted-strip) candidate are
        # deliberately NOT trusted here — with no baseline to score
        # retention against, nothing would catch a region that is really
        # a nav shell (observed on platform.claude.com before the
        # streaming-SSR baseline fix).
        for entry in scored:
            if entry.source == "structural":
                return entry
        return full_page

    eligible = [e for e in scored if e.retention >= RETENTION_FLOOR]
    eligible.append(full_page)

    def _junk_score(entry: SelectedContent) -> tuple[int, int]:
        lines = _lines(entry.markdown)
        junk = _nav_line_count(lines) + _empty_heading_count(lines)
        return junk, len(entry.markdown)

    return min(eligible, key=_junk_score)
