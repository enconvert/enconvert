"""EPUB -> Markdown using only the standard library (no EbookLib).

An EPUB is a ZIP of XHTML content documents plus an OPF package manifest. This
reader is deliberately namespace-agnostic (matches tag local-names) because real
EPUBs vary in how they declare the OPF/container namespaces:

  META-INF/container.xml  ->  <rootfile full-path="…/content.opf">
  content.opf             ->  <manifest><item id= href= properties=> + <spine><itemref idref=>

Chapters are read in spine order (skipping the EPUB3 nav document) and each is
converted with the shared faithful HTML->Markdown pipeline. No EbookLib means no
AGPL dependency.

Untrusted-input safety: the archive is screened for decompression bombs; the
metadata XML is size-capped and rejected if it carries a DTD/entity declaration
(a portable defense against entity-expansion attacks independent of the runtime
expat version); and the assembled chapter text is bounded.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import urllib.parse
import zipfile
from xml.etree import ElementTree as ET

from .common import guard_zip_bomb, join_blocks
from .html_md import html_to_markdown

logger = logging.getLogger(__name__)

_XHTML_SUFFIXES = (".xhtml", ".html", ".htm")
_MAX_METADATA_BYTES = 10 * 1024 * 1024  # container.xml / OPF are tiny in practice
_MAX_TEXT_BYTES = 100 * 1024 * 1024  # bound on total decompressed chapter bytes
_DTD_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _local(tag: str) -> str:
    """Local name of a possibly namespaced XML tag ('{ns}item' -> 'item')."""
    return tag.rsplit("}", 1)[-1]


def _read_capped(archive: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """Read a named member, rejecting an oversized one before decompression."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise ValueError(f"Invalid EPUB: '{name}' not found in the archive.")
    if info.file_size > cap:
        raise ValueError(f"Invalid EPUB: '{name}' is unexpectedly large.")
    return archive.read(name)


def _parse_xml_safe(data: bytes) -> ET.Element:
    """Parse trusted-structure/untrusted-content XML, rejecting any DTD."""
    if _DTD_RE.search(data):
        raise ValueError("Invalid EPUB: XML declaring a DTD/entity is not allowed.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid EPUB: unreadable XML ({exc})")


def _find_opf_path(archive: zipfile.ZipFile) -> str:
    """Resolve the OPF package path from META-INF/container.xml."""
    root = _parse_xml_safe(
        _read_capped(archive, "META-INF/container.xml", _MAX_METADATA_BYTES)
    )
    for element in root.iter():
        if _local(element.tag) == "rootfile" and element.get("full-path"):
            return urllib.parse.unquote(element.get("full-path"))
    raise ValueError("Invalid EPUB: no rootfile declared in container.xml")


def _spine_hrefs(opf_bytes: bytes) -> list[str]:
    """Ordered, URL-decoded content hrefs from the OPF manifest + spine.

    The EPUB3 nav document (manifest item with ``properties`` containing ``nav``)
    is excluded — it is a table-of-contents link list, not chapter content.
    """
    root = _parse_xml_safe(opf_bytes)

    manifest: dict[str, str] = {}
    nav_ids: set[str] = set()
    for element in root.iter():
        if _local(element.tag) == "item":
            item_id = element.get("id")
            href = element.get("href")
            if item_id and href:
                manifest[item_id] = href
                if "nav" in (element.get("properties") or "").split():
                    nav_ids.add(item_id)

    hrefs: list[str] = []
    for element in root.iter():
        if _local(element.tag) == "itemref":
            idref = element.get("idref")
            if idref in nav_ids:
                continue
            href = manifest.get(idref)
            if href:
                hrefs.append(urllib.parse.unquote(href))
    return hrefs


def epub_to_markdown(file_bytes: bytes) -> str:
    """Convert an EPUB to Markdown, chapters in spine order."""
    guard_zip_bomb(file_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Invalid EPUB: the file is not a valid ZIP archive.")

    with archive:
        opf_path = _find_opf_path(archive)
        opf_dir = posixpath.dirname(opf_path)
        hrefs = _spine_hrefs(_read_capped(archive, opf_path, _MAX_METADATA_BYTES))

        chapters: list[str] = []
        budget = _MAX_TEXT_BYTES
        for href in hrefs:
            if not href.lower().endswith(_XHTML_SUFFIXES):
                continue
            full_path = posixpath.normpath(posixpath.join(opf_dir, href))
            try:
                raw = archive.read(full_path)
            except KeyError:
                logger.debug("epub: spine item missing in archive: %s", full_path)
                continue
            budget -= len(raw)
            if budget < 0:
                logger.warning("epub: chapter budget exceeded; truncating")
                break
            html = raw.decode("utf-8", errors="replace")
            chapter_md = html_to_markdown(html, extract_article=False)
            if chapter_md.strip():
                chapters.append(chapter_md)

    markdown = join_blocks(chapters)
    if not markdown.strip():
        raise ValueError("The EPUB contains no readable text content.")
    return markdown
