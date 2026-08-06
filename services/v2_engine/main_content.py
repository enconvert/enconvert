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

from bs4 import BeautifulSoup

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
    """Outcome of one main-content strip."""

    html: str
    aborted: bool
    words_before: int
    words_after: int


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
    the page's visible words. Never raises: any parse failure degrades to
    the original HTML (an un-stripped page is a quality issue, a lost
    page is an incident).
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
        for tag in base_soup.find_all(style=True):
            if _style_hides(tag.get("style") or ""):
                tag.decompose()

        # B1 — semantic tags.
        for tag_name in BOILERPLATE_TAGS:
            for tag in base_soup.find_all(tag_name):
                tag.decompose()

        # B2 — role/class/id selectors. soupsieve raises on a bad selector;
        # the list above is static and covered by tests, but guard anyway.
        try:
            for tag in base_soup.select(BOILERPLATE_SELECTORS):
                tag.decompose()
        except Exception:  # noqa: BLE001 — selector engine failure
            logger.warning("boilerplate selector pass failed", exc_info=True)

        stripped = str(base_soup)
        words_after = _visible_word_count(BeautifulSoup(stripped, "lxml"))

        if words_before > 0 and (words_after / words_before) < MIN_RETAINED_RATIO:
            return MainContentResult(
                html=html,
                aborted=True,
                words_before=words_before,
                words_after=words_after,
            )
        return MainContentResult(
            html=stripped,
            aborted=False,
            words_before=words_before,
            words_after=words_after,
        )
    except Exception:  # noqa: BLE001 — extraction must degrade, never 500
        logger.warning("main-content extraction failed", exc_info=True)
        return MainContentResult(html=html, aborted=False,
                                 words_before=0, words_after=0)


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


def select_main_content(
    candidates: list[ContentCandidate],
    full_page_markdown: str,
) -> SelectedContent:
    """Pick the cleanest candidate that keeps the page's real content.

    ``full_page_markdown`` is both the retention baseline AND the final
    fallback: it is always eligible (retention 1.0 by construction), so
    the ensemble can never return less content than the page has — an
    over-aggressive extractor loses the election instead of shipping a
    stub. Selection: among candidates retaining >= RETENTION_FLOOR of
    the baseline's prose words, minimise nav-chrome ratio; ties go to
    the shorter text.
    """
    baseline_lines = _lines(full_page_markdown)
    baseline_words = _prose_words(baseline_lines)
    baseline_blocks = _code_blocks(baseline_lines)

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

    full_page = SelectedContent(
        markdown=full_page_markdown,
        source="full_page",
        retention=1.0,
        nav_ratio=_nav_ratio(baseline_lines),
        fell_back_to_full_page=True,
    )

    if len(baseline_words) < _MIN_BASELINE_WORDS:
        # Too little prose to score against; prefer the structural strip
        # (its own fidelity guard already ran) over an unscoreable vote.
        for entry in scored:
            if entry.source == "structural":
                return entry
        return full_page

    eligible = [e for e in scored if e.retention >= RETENTION_FLOOR]
    eligible.append(full_page)
    winner = min(eligible, key=lambda e: (e.nav_ratio, len(e.markdown)))
    return winner
