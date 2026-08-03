"""Shared HTML -> Markdown core.

This is the single tag-mapping/normalisation pipeline used by BOTH:

* the web path (``services/browser/converters/url_markdown.py``), which extracts
  the main article with Readability first (``extract_article=True``), and
* the file path (``services/markdown`` — DOCX via mammoth, uploaded HTML, EPUB
  chapters), which converts the document FAITHFULLY (``extract_article=False``)
  without article extraction.

The two modes differ only in (a) whether Readability runs first and (b) which
tags are treated as boilerplate. ``BOILERPLATE_TAGS`` (aggressive: also strips
nav/footer/aside/form/button) suits article extraction; ``NON_CONTENT_TAGS``
(structural noise only) suits faithful full-document conversion where nav/aside
content may be meaningful.

The converter subclass and the ``_preprocess_html`` / ``_postprocess_markdown``
helpers were extracted here VERBATIM from ``url_markdown`` so the web path stays
byte-for-byte identical (locked by tests/v2/test_html_markdown.py). ``url_markdown``
now imports them from this module.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

# Aggressive boilerplate set for ARTICLE extraction (web path): these tags are
# non-article noise and are removed before conversion.
BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "nav",
    "footer",
    "aside",
    "button",
    "svg",
    "canvas",
    "template",
)

# Minimal set for FAITHFUL document conversion (file path): strip only truly
# non-content tags. nav/footer/aside/form/button are KEPT because a standalone
# uploaded document may carry meaningful content in them.
NON_CONTENT_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "template",
)

# Attributes stripped from every surviving element before conversion.
NOISE_ATTRS = ("style", "class", "id")

_LANG_CLASS_RE = re.compile(r"(?:^|\s)(?:language|lang|highlight-source|brush:)[-_]?([\w+#-]+)")

# XML processing instructions (e.g. the ``<?xml version="1.0"?>`` declaration on
# XHTML/EPUB documents) would otherwise leak into Markdown as literal text.
_XML_PI_RE = re.compile(r"<\?[^>]*\?>")


class _ArticleMarkdownConverter(MarkdownConverter):
    """Custom MarkdownConverter with tag-by-tag mapping tuned for web articles.

    Overrides rendering for links, images, code, and horizontal rules so the
    output is clean GFM rather than a naive text dump.
    """

    class Options(MarkdownConverter.DefaultOptions):
        heading_style = "ATX"
        bullets = "-"
        strong_em_symbol = "*"
        newline_style = "SPACES"
        autolinks = True
        default_title = False
        escape_asterisks = True
        escape_underscores = True
        keep_inline_images_in = ["figure", "figcaption"]

    def convert_a(self, el, text, convert_as_inline):
        href = (el.get("href") or "").strip()
        text = (text or "").strip()
        # Drop anchor-only and empty-href links; unwrap to plain text.
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            return text
        if not text:
            # Autolink form when we only have a URL.
            return f"<{href}>"
        title = (el.get("title") or "").strip()
        title_part = f' "{title}"' if title else ""
        return f"[{text}]({href}{title_part})"

    def convert_img(self, el, text, convert_as_inline):
        src = (el.get("src") or el.get("data-src") or "").strip()
        if not src:
            return ""
        alt = (el.get("alt") or "").strip()
        title = (el.get("title") or "").strip()
        title_part = f' "{title}"' if title else ""
        return f"![{alt}]({src}{title_part})"

    def convert_pre(self, el, text, convert_as_inline):
        if not text:
            return ""
        language = _detect_code_language(el)
        fence = "```"
        # Strip trailing newlines from text so the closing fence sits tight.
        body = text.rstrip("\n")
        return f"\n\n{fence}{language}\n{body}\n{fence}\n\n"

    def convert_hr(self, el, text, convert_as_inline):
        return "\n\n---\n\n"


def _detect_code_language(pre_el) -> str:
    """Extract a language hint from a <pre> or its nested <code> element.

    Looks at class names like 'language-python', 'lang-py', 'highlight-source-js',
    and data-lang / data-language attributes.
    """
    candidates = [pre_el]
    code = pre_el.find("code")
    if code is not None:
        candidates.append(code)

    for el in candidates:
        for attr in ("data-lang", "data-language"):
            val = (el.get(attr) or "").strip()
            if val:
                return val
        classes = el.get("class") or []
        for cls in classes:
            m = _LANG_CLASS_RE.search(cls)
            if m:
                return m.group(1)
    return ""


def _preprocess_soup(
    html: str, base_url: str, tags: tuple = BOILERPLATE_TAGS
) -> BeautifulSoup:
    """Normalise article/document HTML before Markdown conversion.

    - Resolves relative <a href> and <img src> against base_url (a no-op when
      base_url is "" — relative URLs are then left as-is for a standalone file).
    - Removes ``tags`` (boilerplate for articles, non-content noise for files).
    - Drops empty / fragment-only links by unwrapping them.
    - Strips noisy attributes (style, class, id) and any on* event handlers.

    Returns the live soup so callers can convert it directly (convert_soup)
    without a serialize + re-parse round trip.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag_name in tags:
        for el in soup.find_all(tag_name):
            el.decompose()

    # Resolve relative URLs on links.
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        a["href"] = urljoin(base_url, href)

    # Resolve relative URLs on images. Favour data-src over src for lazy-loaded.
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if not src:
            img.decompose()
            continue
        img["src"] = urljoin(base_url, src)

    # Strip noisy attributes and any on* event handlers.
    for el in soup.find_all(True):
        for attr in list(el.attrs):
            if attr in NOISE_ATTRS or attr.startswith("on"):
                del el[attr]

    return soup


def _preprocess_html(html: str, base_url: str, tags: tuple = BOILERPLATE_TAGS) -> str:
    """Serialized form of ``_preprocess_soup`` — kept for the web path
    (``url_markdown``), whose string pipeline is locked byte-for-byte by tests."""
    return str(_preprocess_soup(html, base_url, tags))


def _postprocess_markdown(md: str) -> str:
    """Tidy Markdown output: trim trailing whitespace and collapse blank lines."""
    # Trim trailing whitespace on each line (preserve trailing two-space <br>).
    lines = [line.rstrip() if not line.endswith("  ") else line for line in md.splitlines()]
    text = "\n".join(lines)
    # Collapse runs of 3+ blank lines to exactly one blank line.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def html_to_markdown(
    html: str, base_url: str = "", *, extract_article: bool = False
) -> str:
    """Convert an HTML fragment/document to clean GFM Markdown.

    Args:
        html: Source HTML.
        base_url: Absolute base for resolving relative links/images ("" leaves
            relative URLs untouched, which is the norm for an uploaded file).
        extract_article: When True (web path), run Readability first to keep only
            the main article and strip the aggressive ``BOILERPLATE_TAGS``. When
            False (file path), convert the whole document faithfully, stripping
            only ``NON_CONTENT_TAGS``.

    Returns:
        Markdown text (no frontmatter). Empty input yields "".
    """
    if not html or not html.strip():
        return ""
    if extract_article:
        from readability import Document

        html = Document(html).summary(html_partial=True)
        tags = BOILERPLATE_TAGS
    else:
        # Faithful file path (XHTML/EPUB uploads): drop XML declarations/PIs so
        # they do not surface as literal text. The web path never sees these.
        html = _XML_PI_RE.sub("", html)
        tags = NON_CONTENT_TAGS
    # convert_soup (public markdownify API) consumes the preprocessed tree
    # directly — ``convert()`` would serialize it and re-parse with html.parser,
    # holding two full DOMs at peak on a 1GB box for identical output.
    soup = _preprocess_soup(html, base_url, tags)
    markdown = _ArticleMarkdownConverter().convert_soup(soup)
    return _postprocess_markdown(markdown)
