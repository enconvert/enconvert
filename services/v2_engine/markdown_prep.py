"""DOM hygiene applied before EVERY markdown conversion (QA round 3).

The 2026-08-12 QA round filed 14 findings against /v2/perceive markdown.
Grouped by mechanism they are only two bugs:

1. The chrome strip runs on ONE candidate. ``only_main_content``'s
   ensemble usually elects ``main_region`` or ``readability``, and
   neither ever saw the B1/B2/B6 strip — so every artifact that lives
   INSIDE ``<main>`` (feedback widgets, "Copy page" buttons, sr-only
   anchor labels, aria-hidden number badges) sailed through.
2. The HTML->Markdown converters have no DOM hygiene. Card ``<a>``
   elements wrapping a heading plus a description collapse into one
   mashed link (``[DatabaseSupabase provides...]``), block elements
   inside a heading orphan its ``##`` marker, adjacent inline siblings
   concatenate (``YesNo``, ``AI PromptCLI``), empty icon elements emit
   ``__``, zero-width-space anchors emit ``[](#x)``, and the code
   language is never read off the DOM even though the generator emits
   a tagged fence when ``<pre data-language>`` is present.

This module fixes both classes ONCE, on the HTML, before any candidate
is produced — so the result is identical no matter which extractor wins
the election.

Two modes:

* ``strip_chrome=True`` (the ``only_main_content`` path) — everything
  below. Rules A1-A4 remove things a *reader never sees* (screen-reader
  text, interactive controls, hidden nodes, ``data-nosnippet`` blocks).
* ``strip_chrome=False`` (the full-page path) — only the
  conversion-quality rules A5-A8, which fix how the DOM is rendered
  without deciding what belongs on the page. A caller who asked for the
  full page still gets the full page.

Rule map (letters match the QA fix plan):

* A1 screen-reader-only text (``.sr-only``, ``.visually-hidden``, clip
  rects) — "Section titled X" after every heading, skip links,
  "Terminal window", duplicated link labels.
* A2 interactive controls (``button``/``input``/``[role=tab]``/...) —
  "Copy page", "On this page", "Was this page helpful? Yes/No",
  "Get started" x9, keyboard hints, tab strips. Controls that carry
  real content (a heading, a code block, 25+ words, or an
  ``aria-expanded`` disclosure label) are UNWRAPPED, not deleted.
* A3 hidden nodes (``[hidden]``/``aria-hidden``/``inert``/CSS-hidden) —
  aria-hidden ordinal badges ("1. 1"), icon-font glyphs, mega-menu
  clones. Collapsed DISCLOSURE panels inside the content region
  (tabpanels, ``aria-controls`` targets) are KEPT: an inactive tab is
  deferred content, not chrome, and dropping it lost Supabase's only
  real ``<pre>`` block.
* A4 ``[data-nosnippet]`` — content the site itself marks as
  not-for-extraction.
* A5 invisible characters (zero-width, soft hyphen, Private Use Area
  icon-font glyphs), vector graphics, and elements left empty by any of
  the above (empty ``<i>`` icons that rendered as ``__``, zero-width
  anchors that rendered as ``[](#x)``, alt-less logo links).
* A6 heading flattening — block children inside ``h1``-``h6`` orphan the
  marker, leaving a bare ``##`` and the text on its own line.
* A7 block-level links + sibling separation — a card ``<a>`` becomes a
  linked heading followed by its description instead of one mashed
  link, and adjacent element siblings that would concatenate get one
  space between them.
* A8 code-fence languages — the language lives in
  ``class="language-x"``, ``data-lang``, a bare ``language`` attribute
  or a wrapper element depending on the doc framework; normalise all of
  them onto ``<pre data-language>``, which both markdown generators
  understand.

Every rule degrades: a parse failure returns the original HTML, because
an unpolished page is a quality issue while a lost page is an incident.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from services.v2_engine.main_content import (
    _contains_content_region,
    _style_hides,
    controlled_ids,
    is_deferred_disclosure,
    is_streaming_ssr_payload,
)

logger = logging.getLogger(__name__)

__all__ = ["prepare_html", "detect_code_language"]


# --- A1: screen-reader-only ------------------------------------------------

# Class-name vocabulary for "present for assistive tech, invisible to a
# reader". Matched against the class token with every non-alphanumeric
# character removed, so CSS-module hashes (``visuallyHidden_a1b2c3``) and
# Tailwind variants (``md:sr-only``) hit the same markers.
_SR_ONLY_MARKERS: tuple[str, ...] = (
    "sronly",
    "visuallyhidden",
    "visiblyhidden",
    "screenreader",
    "a11yhidden",
    "hiddenvisually",
    "assistivetext",
)

# ``focus:not-sr-only`` (the standard skip-link reveal) contains the
# marker but negates it — an element carrying ONLY the negation is
# visible. Elements carrying both (every real skip link) still match on
# the plain ``sr-only`` token.
_SR_ONLY_NEGATIONS: tuple[str, ...] = ("notsronly", "notvisuallyhidden")

# The pre-utility-class idiom for the same thing: clip the box to
# nothing. Matched on the style attribute with spaces removed.
_SR_ONLY_STYLE_MARKERS: tuple[str, ...] = (
    "clip:rect(0",
    "clip:rect(1px",
    "clip-path:inset(50%)",
    "clip-path:inset(100%)",
)


# --- A2: interactive controls ----------------------------------------------

_CONTROL_TAGS: tuple[str, ...] = ("button", "input", "select", "textarea", "dialog")

# ARIA roles whose elements are controls even when the tag is a ``<div>``
# (every modern design system builds its tabs and menus this way).
_CONTROL_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "tab",
        "tablist",
        "menu",
        "menubar",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "toolbar",
        "dialog",
        "alertdialog",
        "search",
        "searchbox",
        "switch",
        "checkbox",
        "radio",
        "radiogroup",
        "slider",
        "spinbutton",
        "combobox",
        "listbox",
        "option",
        "tooltip",
        "progressbar",
        "scrollbar",
    }
)

# A control holding this many words is a content block wearing a control
# role (an expandable FAQ answer, a clickable card body) — unwrap it
# instead of deleting it.
_CONTROL_CONTENT_WORDS: int = 25

# A disclosure trigger (``aria-expanded``) with a real phrase in it is an
# FAQ question or a section title; below this it is "Show more"/"Menu".
_DISCLOSURE_LABEL_WORDS: int = 4


# A control's prompt has no meaning once the control is gone: "Was this
# page helpful?" without its Yes/No buttons, a search placeholder
# without its box, a "⌘I" shortcut hint without its input. After the
# controls are removed, a container left holding only a short phrase —
# no heading, link, media, list or code — goes with them. The ceiling
# keeps this to labels: any real paragraph is longer.
_ORPHAN_PROMPT_MAX_WORDS: int = 12

# Controls whose removal leaves a prompt: a GROUP of them (a Yes/No vote
# bar, a tab strip) or any form field. One button beside a paragraph is
# a call-to-action next to real copy, and must not take the copy with it.
_ORPHAN_PROMPT_MIN_CONTROLS: int = 2
_FIELD_TAGS: frozenset[str] = frozenset({"input", "select", "textarea"})

# How far above a removed control to look for that prompt. Design
# systems nest the buttons one or two wrappers below the prompt text.
_ORPHAN_PROMPT_DEPTH: int = 3

# Elements whose presence proves a container is content, not a prompt.
_PROMPT_DISQUALIFIERS: tuple[str, ...] = (
    "a",
    "img",
    "pre",
    "code",
    "table",
    "ul",
    "ol",
    "video",
    "audio",
    "iframe",
)


# --- A5: invisible characters ---------------------------------------------

# Zero-width space, word joiner, BOM, soft hyphen, Mongolian vowel
# separator, and the Private Use Area blocks (icon fonts render their
# glyphs there; extracted as text they are unprintable tokens).
# ZWNJ/ZWJ (U+200C/U+200D) are deliberately NOT here — they are
# meaningful letters in Indic, Persian and Arabic scripts.
_INVISIBLE_RE = re.compile(
    "[\u200b\u2060\ufeff\u00ad\u180e]"  # ZWSP, word joiner, BOM, soft hyphen
    "|[\ue000-\uf8ff]"  # Private Use Area (icon-font glyphs)
    "|[\U000f0000-\U000ffffd]"  # Supplementary PUA-A
    "|[\U00100000-\U0010fffd]"  # Supplementary PUA-B
)

# Inline elements that carry nothing once their icon/zero-width content
# is gone. ``a`` is handled with them but keeps images that have alt text.
_EMPTY_PRUNE_TAGS: tuple[str, ...] = (
    "i",
    "b",
    "em",
    "strong",
    "span",
    "u",
    "small",
    "mark",
    "sup",
    "sub",
    "abbr",
    "kbd",
    "cite",
    "q",
    "figcaption",
    "label",
)


# --- A6/A7: block structure ------------------------------------------------

_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")

# Block elements that must not sit inside a heading or inside a link's
# text run.
_BLOCK_TAGS: tuple[str, ...] = (
    "div",
    "p",
    "section",
    "article",
    "header",
    "footer",
    "figure",
    "figcaption",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    "main",
    "aside",
    "nav",
    "table",
    "blockquote",
)

_BLOCK_IN_LINK: tuple[str, ...] = _HEADING_TAGS + _BLOCK_TAGS

_BLOCK_NAMES: frozenset[str] = frozenset(_BLOCK_TAGS + _HEADING_TAGS)
# Cells flatten their whole subtree onto one markdown line, so block
# structure inside them stops separating anything (see
# _insert_sibling_separators).
_TABLE_CELL_NAMES: tuple[str, ...] = ("td", "th")

# Tags whose text must never be reflowed or separated.
_VERBATIM_TAGS: tuple[str, ...] = ("pre", "code", "script", "style", "textarea")


# --- A8: code-fence languages ---------------------------------------------

# Attribute names doc frameworks use for the language, in preference
# order. ``language`` (bare, non-standard) is what Mintlify/shiki SSR
# emits and was the reason langchain's fences came out untagged.
_LANG_ATTRS: tuple[str, ...] = (
    "data-language",
    "data-lang",
    "language",
    "lang",
    "data-code-language",
    "data-highlight-language",
    "data-syntax",
    "data-ec-language",
)

# ``language-python``, ``lang-py``, ``highlight-source-js``,
# ``sourceCode-haskell``. Longest prefixes first so
# ``highlight-source-js`` does not match the bare ``highlight`` branch.
_LANG_CLASS_RE = re.compile(
    r"^(?:highlight-source|highlight-text|language|lang|highlight|brush|sourcecode)"
    r"[-_:]([\w+#.]+)$",
    re.IGNORECASE,
)

_LANG_VALID_RE = re.compile(r"^[a-z][a-z0-9+#._-]{0,19}$")

# Tokens the patterns above can produce that are not languages: theme
# names, layout modifiers, framework markers.
_LANG_REJECT: frozenset[str] = frozenset(
    {
        "auto",
        "block",
        "blocks",
        "code",
        "container",
        "default",
        "dark",
        "false",
        "highlight",
        "hljs",
        "inline",
        "latte",
        "light",
        "line",
        "lines",
        "mocha",
        "none",
        "null",
        "numbers",
        "pre",
        "prism",
        "shiki",
        "snippet",
        "themes",
        "true",
        "undefined",
        "wrapper",
    }
)

# How far above a ``<pre>`` to look for the language marker (shiki and
# Expressive Code put it on an outer wrapper div/figure).
_LANG_ANCESTOR_DEPTH: int = 3


def _class_tokens(tag: Any) -> list[str]:
    """Class list of ``tag`` as a list of strings (bs4 gives str or list)."""
    classes = tag.get("class") or []
    if isinstance(classes, str):
        return classes.split()
    return [str(item) for item in classes]


def _normalize_token(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _text_of(tag: Any) -> str:
    try:
        return tag.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001 — a detached node has no text
        return ""


def _is_screen_reader_only(tag: Any) -> bool:
    """True when ``tag`` exists only for assistive tech (A1)."""
    for token in _class_tokens(tag):
        normalized = _normalize_token(token)
        if any(negation in normalized for negation in _SR_ONLY_NEGATIONS):
            continue
        if any(marker in normalized for marker in _SR_ONLY_MARKERS):
            return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    return any(marker in style for marker in _SR_ONLY_STYLE_MARKERS)


def _drop_screen_reader_only(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        if not _is_screen_reader_only(tag):
            continue
        if _contains_content_region(tag):
            continue
        tag.decompose()


# Attributes with which a site declares "this is not page content" to
# crawlers and search indexers. ``data-nosnippet`` is Google's,
# ``data-pagefind-ignore`` is Pagefind's (the static-site search used by
# Astro/Starlight docs), ``data-noindex`` is the generic spelling. They
# all mark the same thing, and honouring them is the least
# presumptuous chrome rule we have: the site said so.
_NOT_CONTENT_ATTRS: tuple[str, ...] = (
    "[data-nosnippet]",
    "[data-pagefind-ignore]",
    "[data-noindex]",
)


def _drop_nosnippet(soup: BeautifulSoup) -> None:
    """A4 — honour the site's own not-for-extraction markers."""
    for selector in _NOT_CONTENT_ATTRS:
        try:
            nodes = soup.select(selector)
        except Exception:  # noqa: BLE001 — selector engine failure
            continue
        for tag in nodes:
            if tag.decomposed or _contains_content_region(tag):
                continue
            # These attributes are also used to keep code samples and
            # long reference tables OUT of a search index while they
            # remain very much part of the page — never let the hint
            # delete substantial content.
            if tag.find(_HEADING_TAGS + ("pre", "table")) is not None:
                continue
            tag.decompose()


def _is_content_control(tag: Any) -> bool:
    """True when a control element actually carries page content (A2)."""
    if tag.find(_HEADING_TAGS) is not None:
        return True
    if tag.find(("pre", "table", "img")) is not None and _text_of(tag):
        return True
    words = len(_text_of(tag).split())
    if words >= _CONTROL_CONTENT_WORDS:
        return True
    return (
        tag.get("aria-expanded") is not None and words >= _DISCLOSURE_LABEL_WORDS
    )


def _is_orphan_prompt(tag: Any, *, require_question: bool) -> bool:
    """True for a container left holding only a control's label (A2b).

    ``require_question`` applies when the removed controls were buttons
    rather than form fields: a rating widget's leftover is a QUESTION
    ("Was this page helpful?"), whereas a short statement beside a
    button group is ordinary copy introducing it. Containers left with
    no text at all are always prunable — they render nothing.
    """
    if tag is None or tag.decomposed or tag.parent is None:
        return False
    if tag.name in ("body", "html", "main", "article"):
        return False
    if _contains_content_region(tag):
        return False
    # A heading is never a prompt, and neither is the wrapper INSIDE a
    # heading that held its copy-link button — that wrapper holds the
    # heading's own text, and taking it (then its parent) deleted six
    # section titles from platform.claude.com in testing.
    if tag.name in _HEADING_TAGS or tag.find_parent(_HEADING_TAGS) is not None:
        return False
    if tag.find(_HEADING_TAGS) is not None:
        return False
    if tag.find(_PROMPT_DISQUALIFIERS) is not None:
        return False
    text = _text_of(tag)
    if not text:
        return True
    if len(text.split()) > _ORPHAN_PROMPT_MAX_WORDS:
        return False
    return text.endswith("?") if require_question else True


def _prune_orphan_prompts(hosts: list[Any]) -> None:
    """A2b — remove the label text a deleted control leaves behind.

    For each container that lost a control, climbs up to
    ``_ORPHAN_PROMPT_DEPTH`` levels and removes the OUTERMOST ancestor
    that still qualifies as a bare prompt — design systems put the
    prompt one or two wrappers above the buttons themselves.

    Callers pass only containers that lost a control GROUP or a form
    field. A lone button beside a paragraph does not qualify: that
    shape is a call-to-action next to real copy, and treating it as a
    widget deleted the copy.
    """
    for host, require_question in hosts:
        node = host
        target = None
        depth = 0
        while isinstance(node, Tag) and depth < _ORPHAN_PROMPT_DEPTH:
            if not _is_orphan_prompt(node, require_question=require_question):
                break
            target = node
            node = node.parent
            depth += 1
        if target is not None and not target.decomposed:
            target.decompose()


def _drop_interactive_controls(soup: BeautifulSoup) -> None:
    """A2 — remove UI controls; unwrap the ones carrying real content."""
    # Per container: how many controls it lost, and whether any was a
    # form field. Both feed the A2b prompt rule below.
    removed: dict[int, tuple[Any, int, bool]] = {}

    def _record(parent: Any, *, is_field: bool) -> None:
        if parent is None:
            return
        node, count, field = removed.get(id(parent), (parent, 0, False))
        removed[id(parent)] = (node, count + 1, field or is_field)

    # Labels first: once the inputs are gone their form association is
    # unrecoverable, and a label without its control is a bare word.
    for label in soup.find_all("label"):
        if label.decomposed:
            continue
        if label.get("for") is not None or label.find(
            ("input", "select", "textarea")
        ):
            _record(label.parent, is_field=True)
            label.decompose()

    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        role = (tag.get("role") or "").strip().lower()
        if tag.name not in _CONTROL_TAGS and role not in _CONTROL_ROLES:
            continue
        if _contains_content_region(tag):
            continue
        if _is_content_control(tag):
            tag.unwrap()
            continue
        _record(tag.parent, is_field=tag.name in _FIELD_TAGS)
        tag.decompose()

    # A control GROUP (vote bar, tab strip) or a form field leaves a
    # prompt behind; a single button does not. Button groups additionally
    # require the leftover to be a question — see _prune_orphan_prompts.
    _prune_orphan_prompts(
        [
            (node, not field)
            for node, count, field in removed.values()
            if count >= _ORPHAN_PROMPT_MIN_CONTROLS or field
        ]
    )


def _is_hidden(tag: Any) -> bool:
    if tag.get("hidden") is not None:
        return True
    if tag.get("inert") is not None:
        return True
    if (tag.get("aria-hidden") or "").strip().lower() == "true":
        return True
    return _style_hides(tag.get("style") or "")


def _drop_hidden_nodes(soup: BeautifulSoup) -> None:
    """A3 — remove nodes invisible to a reader, keeping deferred content.

    ``is_deferred_disclosure`` is shared with ``extract_main_content``:
    both passes carry ``[aria-hidden]``/``[hidden]`` rules, and a panel
    spared here only to be deleted there would still lose the inactive
    half of every tabbed code sample.
    """
    controlled = controlled_ids(soup)
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        if not _is_hidden(tag):
            continue
        if _contains_content_region(tag):
            continue
        if is_streaming_ssr_payload(tag):
            continue
        if is_deferred_disclosure(tag, controlled):
            continue
        tag.decompose()


def _drop_vector_graphics(soup: BeautifulSoup, *, strip_chrome: bool) -> None:
    """A5 — SVG/canvas text is icon plumbing, never prose."""
    names = ["svg", "canvas"]
    if strip_chrome:
        # A <noscript> body is what a JS-less client would see; we
        # rendered WITH JavaScript, so it is a duplicate at best.
        names.append("noscript")
    for tag in soup.find_all(names):
        if not tag.decomposed:
            tag.decompose()


def _scrub_text_nodes(soup: BeautifulSoup) -> None:
    """A5 — strip invisible characters from every text node."""
    for text in list(soup.find_all(string=True)):
        raw = str(text)
        cleaned = _INVISIBLE_RE.sub("", raw)
        if cleaned != raw:
            text.replace_with(NavigableString(cleaned))


def _drop_empty_elements(soup: BeautifulSoup) -> None:
    """A5 — prune elements left with nothing to render.

    Whitespace-only elements are UNWRAPPED, not deleted: syntax
    highlighters emit the spaces between code tokens as their own
    ``<span> </span>`` elements, and deleting those ran every command
    in a code block together (``npminstall-gsupabase``). Unwrapping
    drops the markup and keeps the space — which is also the right
    answer for a card's whitespace-only overlay anchor, since it
    removes the empty link without gluing its neighbours.

    Two passes: removing an empty icon ``<span>`` can leave its ``<a>``
    parent empty in turn.
    """
    for _ in range(2):
        for tag in soup.find_all(list(_EMPTY_PRUNE_TAGS) + ["a"]):
            if tag.decomposed or tag.parent is None:
                continue
            # Never reach inside verbatim text: its whitespace is data.
            if tag.find_parent(_VERBATIM_TAGS) is not None:
                continue
            if _text_of(tag):
                continue
            if _contains_content_region(tag):
                continue
            images = tag.find_all("img")
            if any((image.get("alt") or "").strip() for image in images):
                continue
            if tag.find(("pre", "table", "video", "audio", "iframe")) is not None:
                continue
            if tag.get_text():  # whitespace-only: keep the whitespace
                tag.unwrap()
                continue
            tag.decompose()


def _flatten_headings(soup: BeautifulSoup) -> None:
    """A6 — a heading must render as one line with its marker.

    Mintlify-style headings wrap their text in a positioned ``<div>``
    that also holds a copy-link button. The block child ends the
    heading line during conversion, leaving a bare ``##`` and the text
    stranded on the next line (the "empty H2" finding on three sites).
    """
    for heading in soup.find_all(_HEADING_TAGS):
        if heading.decomposed:
            continue
        for child in heading.find_all(_BLOCK_TAGS):
            if child.decomposed or child.parent is None:
                continue
            child.unwrap()
        for text in list(heading.find_all(string=True)):
            raw = str(text)
            collapsed = re.sub(r"\s+", " ", raw)
            if collapsed != raw:
                text.replace_with(NavigableString(collapsed))


def _restructure_block_links(soup: BeautifulSoup) -> None:
    """A7 — split a card link into a linked title plus loose content.

    A card is one ``<a>`` wrapping a heading, a description paragraph
    and often a list; converted verbatim it becomes a single link whose
    text is every descendant run together
    (``[DatabaseSupabase provides...]``). Restructured, the title keeps
    the href and the rest of the card stays as normal blocks — which
    also preserves the destination URL that flattening used to lose.
    """
    for anchor in soup.find_all("a"):
        if anchor.decomposed or anchor.parent is None:
            continue
        if anchor.find_parent(_VERBATIM_TAGS) is not None:
            continue
        # The INNERMOST leading block with text. Taking the outermost
        # would hand the whole card back as one label: Supabase wraps
        # title and description together in a positioning <div>, so the
        # first matching block is the card itself.
        block = next(
            (
                node
                for node in anchor.find_all(_BLOCK_IN_LINK)
                if _text_of(node)
                and not any(
                    _text_of(inner) for inner in node.find_all(_BLOCK_IN_LINK)
                )
            ),
            None,
        )
        if block is None:
            continue
        href = (anchor.get("href") or "").strip()
        if not href:
            anchor.unwrap()
            continue
        heading = anchor.find(_HEADING_TAGS)
        title_host = heading if heading is not None else block
        title_text = _text_of(title_host)
        if not title_text:
            anchor.unwrap()
            continue
        new_anchor = soup.new_tag("a", href=href)
        title = (anchor.get("title") or "").strip()
        if title:
            new_anchor["title"] = title
        new_anchor.string = title_text
        title_host.clear()
        title_host.append(new_anchor)
        if heading is not None:
            # Promote the heading ahead of the rest of the card so the
            # description reads as the heading's body.
            anchor.insert_before(heading.extract())
        anchor.unwrap()


def _edge_text(tag: Any) -> str:
    """Text that would render for ``tag`` at a sibling boundary."""
    if tag.name == "img":
        return (tag.get("alt") or "").strip()
    return _text_of(tag)


def _insert_sibling_separators(soup: BeautifulSoup) -> None:
    """A7 — one space between element siblings that would concatenate.

    Flex/grid layouts space their children with CSS, not whitespace, so
    the DOM holds ``<button>Yes</button><button>No</button>`` and text
    extraction yields ``YesNo``. The same shape produces
    ``AI PromptCLI`` (tab strip) and
    ``EvaluationDeploymentProduction`` (badge row).

    Single-character siblings are left alone: per-character animation
    spans (Linear's text-scramble headings) are exactly that shape, and
    gluing them would turn one word into a column of letters.

    Only INLINE pairs are considered — EXCEPT inside a table cell. Block
    siblings normally convert to separate markdown blocks, so gluing them
    buys nothing, but a pipe table flattens a cell onto one line: an
    invoice or newsletter laid out as a table (``<td><p>Invoice 4471</p>
    <p>Issued 12 March</p></td>``) came out as ``Invoice 4471Issued 12
    March``, with the word boundary destroyed.
    """
    for parent in soup.find_all(True):
        if parent.name in _VERBATIM_TAGS:
            continue
        if parent.find_parent(_VERBATIM_TAGS) is not None:
            continue
        in_table_cell = parent.name in _TABLE_CELL_NAMES or (
            parent.find_parent(_TABLE_CELL_NAMES) is not None
        )
        children = list(parent.children)
        for index in range(len(children) - 1):
            current, following = children[index], children[index + 1]
            if not isinstance(current, Tag) or not isinstance(following, Tag):
                continue
            if not in_table_cell and (
                current.name in _BLOCK_NAMES or following.name in _BLOCK_NAMES
            ):
                continue
            left, right = _edge_text(current), _edge_text(following)
            if len(left) < 2 or len(right) < 2:
                continue
            current.insert_after(NavigableString(" "))


def _valid_language(value: str) -> Optional[str]:
    token = (value or "").strip().lower()
    if not token or token in _LANG_REJECT:
        return None
    if not _LANG_VALID_RE.match(token):
        return None
    return token


def _language_from(tag: Any) -> Optional[str]:
    """Language marker carried by one element's attributes or classes."""
    for attr in _LANG_ATTRS:
        raw = tag.get(attr)
        if isinstance(raw, list):
            raw = " ".join(str(item) for item in raw)
        language = _valid_language(str(raw or ""))
        if language:
            return language
    for token in _class_tokens(tag):
        match = _LANG_CLASS_RE.match(token.strip())
        if not match:
            continue
        language = _valid_language(match.group(1))
        if language:
            return language
    return None


def detect_code_language(pre: Any) -> Optional[str]:
    """The fence language for a ``<pre>``, or None.

    Checks the element, its ``<code>`` child, then up to
    ``_LANG_ANCESTOR_DEPTH`` ancestors — doc frameworks put the marker
    at every one of those levels depending on the highlighter.
    """
    code = pre.find("code")
    for element in (pre, code):
        if element is None:
            continue
        language = _language_from(element)
        if language:
            return language
    parent = pre.parent
    depth = 0
    while isinstance(parent, Tag) and depth < _LANG_ANCESTOR_DEPTH:
        language = _language_from(parent)
        if language:
            return language
        parent = parent.parent
        depth += 1
    return None


def _stamp_code_languages(soup: BeautifulSoup) -> None:
    """A8 — normalise every language marker onto ``<pre data-language>``.

    Both markdown generators read this attribute (crawl4ai's html2text
    emits ```` ```lang ```` from it directly; the markdownify path reads
    it in ``_detect_code_language``), and it is the only place that
    survives the class-stripping both pipelines perform.
    """
    for pre in soup.find_all("pre"):
        if pre.decomposed:
            continue
        if _valid_language(str(pre.get("data-language") or "")):
            continue
        language = detect_code_language(pre)
        if language:
            pre["data-language"] = language


def prepare_html(html: str, *, strip_chrome: bool = True) -> str:
    """Clean ``html`` so markdown conversion renders it faithfully.

    Args:
        html: Rendered page HTML (or any fragment of it).
        strip_chrome: When True (the ``only_main_content`` path) also
            remove what a reader never sees — screen-reader-only text,
            interactive controls, hidden nodes, ``data-nosnippet``
            blocks. When False (the full-page path) only the
            conversion-quality rules run, so nothing a caller asked to
            keep is dropped.

    Returns:
        The prepared HTML, or the input unchanged if parsing failed.
    """
    if not html or not isinstance(html, str):
        return html or ""
    try:
        soup = BeautifulSoup(html, "lxml")

        # A8 first: the language markers live in class names that later
        # passes (and both converters' own preprocessing) discard.
        _stamp_code_languages(soup)

        if strip_chrome:
            _drop_nosnippet(soup)
            _drop_screen_reader_only(soup)
            _drop_interactive_controls(soup)
            _drop_hidden_nodes(soup)

        _drop_vector_graphics(soup, strip_chrome=strip_chrome)
        _scrub_text_nodes(soup)
        _drop_empty_elements(soup)
        _flatten_headings(soup)
        _restructure_block_links(soup)
        _insert_sibling_separators(soup)
        return str(soup)
    except Exception:  # noqa: BLE001 — prep must degrade, never 500
        logger.warning("markdown prep failed", exc_info=True)
        return html
