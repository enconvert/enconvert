import io
import os
import re
import xml.etree.ElementTree as ET
from fastapi import HTTPException

# Single source of truth for the anything-to-markdown extension allowlist.
from services.markdown.dispatch import SUPPORTED_EXTENSIONS as _MARKDOWN_EXTENSIONS
# Single source of truth for the anything-to-pdf extension allowlist.
from services.pdf.dispatch import SUPPORTED_EXTENSIONS as _PDF_EXTENSIONS

# Allowed input file extensions per endpoint
ALLOWED_EXTENSIONS = {
    "json-to-xml": [".json"],
    "xml-to-json": [".xml"],
    "json-to-yaml": [".json"],
    "yaml-to-json": [".yaml", ".yml"],
    "csv-to-json": [".csv"],
    "json-to-csv": [".json"],
    "ppt-to-pdf": [".ppt", ".pptx"],
    "markdown-to-html": [".md", ".markdown"],
    "doc-to-pdf": [".docx", ".doc"],
    "excel-to-pdf": [".xlsx", ".xls"],
    "html-to-pdf": [".html", ".htm"],
    "markdown-to-pdf": [".md", ".markdown"],
    "anything-to-markdown": list(_MARKDOWN_EXTENSIONS),
    "anything-to-pdf": list(_PDF_EXTENSIONS),
    # NOTE: no "image" key -- POST /v1/convert/image is commented out in
    # api/v1/convert.py, so an entry here would gate nothing. Restore it in the
    # same commit that uncomments the route (test_no_allowed_extension_key_is_dead
    # enforces this).
    "thumbnail": [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg", ".mp4", ".avi", ".mov", ".mkv", ".webm"],
    "video": [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v"],
    "ocr": [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".pdf"],
    "speech-to-text": [".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".webm"],
    "text-to-speech": [".txt"],
    "odt-to-pdf": [".odt"],
    "ods-to-pdf": [".ods"],
    "odp-to-pdf": [".odp"],
    "ots-to-pdf": [".ots"],
    "pages-to-pdf": [".pages"],
    "numbers-to-pdf": [".numbers"],
    "json-to-toml": [".json"],
    "toml-to-json": [".toml"],
    "csv-to-xml": [".csv"],
    "xml-to-csv": [".xml"],
    "jpeg-to-png": [".jpeg", ".jpg"],
    "png-to-jpeg": [".png"],
    # NOTE: the endpoint is "jpeg-to-svg" (CONVERTER_MAP + the route in
    # api/v1/convert.py). The old "jpg-to-svg" key here matched no endpoint, so
    # it gated nothing while the real endpoint fell through the fail-open branch
    # in validate_file_format.
    "jpeg-to-svg": [".jpeg", ".jpg"],
    "svg-to-jpeg": [".svg"],
    "svg-to-png": [".svg"],
    "svg-to-webp": [".svg"],
    "compress-image": [".png", ".jpg", ".jpeg", ".webp"],
    # Every remaining image endpoint in CONVERTER_MAP. These were absent, and
    # validate_file_format fails OPEN on a missing key, so nothing rejected a
    # wrong extension before dispatch -- the converter's own bare string gate
    # (e.g. png_to_svg's "Expected a PNG file (.png)") became the only check and
    # surfaced as a raw internal message. tests/v2/test_converter_gate_parity.py
    # now fails the build if this dict and CONVERTER_MAP ever drift again.
    "png-to-svg": [".png"],
    "png-to-webp": [".png"],
    "png-to-heic": [".png"],
    "webp-to-png": [".webp"],
    "webp-to-jpeg": [".webp"],
    "webp-to-svg": [".webp"],
    "webp-to-heic": [".webp"],
    "heic-to-png": [".heic", ".heif"],
    "heic-to-jpeg": [".heic", ".heif"],
    "heic-to-svg": [".heic", ".heif"],
    "heic-to-webp": [".heic", ".heif"],
    "jpeg-to-webp": [".jpeg", ".jpg"],
    "jpeg-to-heic": [".jpeg", ".jpg"],
    "svg-to-heic": [".svg"],
    "pdf-to-jpeg": [".pdf"],
}

# ---------------------------------------------------------------------------
# SVG output-dimension validation (svg-to-png / svg-to-jpeg / svg-to-webp)
# ---------------------------------------------------------------------------
# Route-level gate, mirroring _parse_office_pdf_options' placement rationale:
# a pure client error (absurd dimensions) must 400 BEFORE quota is burned or a
# Failed activity row is logged. The per-dimension cap bounds the cairo render
# surface; the total-pixel cap is the real memory bound (25 MP RGBA ~= 100 MB).

MAX_IMAGE_DIMENSION = 10000
MAX_OUTPUT_PIXELS = 25_000_000

# Absolute SVG lengths convertible to CSS px. Relative units (%, em, ex) are
# unmatchable here on purpose -> treated as "ratio unknown" (fail-open).
_SVG_LENGTH_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(px|pt|pc|mm|cm|in|q)?\s*$", re.I)
_SVG_UNIT_TO_PX = {
    "": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0,
    "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "q": 96 / 101.6,
}
# Root <svg> attrs live in the first KBs; slicing also caps how much hostile
# XML (e.g. entity-expansion DTDs) the sniffer will ever feed to expat.
_SVG_SNIFF_BYTES = 65536


def _parse_svg_length(value: str | None) -> float | None:
    if not value:
        return None
    match = _SVG_LENGTH_RE.match(value)
    if not match:
        return None
    px = float(match.group(1)) * _SVG_UNIT_TO_PX[(match.group(2) or "").lower()]
    return px if px > 0 else None


def svg_intrinsic_size(svg_bytes: bytes) -> tuple[float, float] | None:
    """Best-effort (width, height) in px of the root <svg> element.

    Prefers the width/height attributes (what cairosvg sizes the canvas from),
    falls back to the viewBox. Returns None when the ratio cannot be
    determined — callers must fail open, matching this module's philosophy.
    """
    try:
        iterator = ET.iterparse(io.BytesIO(svg_bytes[:_SVG_SNIFF_BYTES]), events=("start",))
        _, root = next(iterator)
    except Exception:
        return None
    # Attribute names are not namespaced on the svg root; viewBox is camelCase.
    attrs = {key.rsplit("}", 1)[-1]: val for key, val in root.attrib.items()}
    width = _parse_svg_length(attrs.get("width"))
    height = _parse_svg_length(attrs.get("height"))
    if width and height:
        return (width, height)
    view_box = attrs.get("viewBox") or attrs.get("viewbox")
    if view_box:
        try:
            parts = [float(p) for p in re.split(r"[\s,]+", view_box.strip()) if p]
        except ValueError:
            return None
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return (parts[2], parts[3])
    return None


def validate_svg_dimensions(width: int | None, height: int | None, svg_bytes: bytes) -> None:
    """Validate requested raster dimensions for an SVG conversion.

    Semantics (cairosvg native behavior, verified empirically):
    - width only  -> height derived from the SVG's aspect ratio
    - height only -> width derived from the SVG's aspect ratio
    - both        -> exact output size (may change the aspect ratio)

    When only one dimension is given, the derived one is estimated from the
    SVG's intrinsic ratio so an extreme-ratio document can't request an
    unbounded render surface. Unknown ratio -> fail open (cairo errors are
    caught downstream as a 400).
    """
    for name, value in (("width", width), ("height", height)):
        if value is not None and not (1 <= value <= MAX_IMAGE_DIMENSION):
            raise HTTPException(
                status_code=400,
                detail=f"{name} must be between 1 and {MAX_IMAGE_DIMENSION} pixels",
            )
    if width is not None and height is not None:
        if width * height > MAX_OUTPUT_PIXELS:
            raise HTTPException(
                status_code=400,
                detail=f"Requested output ({width}x{height}) exceeds the "
                       f"{MAX_OUTPUT_PIXELS} pixel limit",
            )
        return
    given = width if width is not None else height
    if given is None:
        return
    intrinsic = svg_intrinsic_size(svg_bytes)
    if not intrinsic:
        # Ratio unknown (e.g. the SVG sizes itself in em/ex/%, which our sniffer
        # cannot resolve but cairosvg CAN — potentially into a huge canvas). We
        # cannot bound the derived dimension, so fail CLOSED: require both
        # dimensions. Note this only affects a single-dimension request; the
        # no-parameter default path returned earlier and is unaffected.
        raise HTTPException(
            status_code=400,
            detail="Could not determine the SVG's aspect ratio to derive the "
                   "other dimension safely. Pass both width and height to set "
                   "the output size explicitly.",
        )
    intrinsic_w, intrinsic_h = intrinsic
    derived = given * (intrinsic_h / intrinsic_w if width is not None else intrinsic_w / intrinsic_h)
    if given * derived > MAX_OUTPUT_PIXELS:
        raise HTTPException(
            status_code=400,
            detail=f"Requested output would be ~{given}x{max(1, round(derived))} "
                   f"(derived from the SVG's aspect ratio), exceeding the "
                   f"{MAX_OUTPUT_PIXELS} pixel limit. Pass both width and height "
                   f"to set the size explicitly.",
        )


def validate_file_format(endpoint: str, filename: str):
    """Check that the uploaded file extension is valid for the endpoint."""
    allowed = ALLOWED_EXTENSIONS.get(endpoint)
    if not allowed:
        return
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file format '{ext or 'unknown'}' for {endpoint}. Allowed: {', '.join(allowed)}"
        )


# BUG FIX B: content-based (magic-byte) validation.
#
# The extension check above trusts the filename; a caller can rename a PNG to
# .pdf and slip past it. We add a conservative content check that keys off the
# uploaded file's OWN extension (already validated as allowed) and rejects only
# on a HIGH-CONFIDENCE mismatch: a declared binary type whose bytes are clearly
# a DIFFERENT known binary type. Anything indeterminate (an unknown signature,
# a text-based format with no reliable magic) is ALLOWED — fail-open — so valid
# conversions are never broken.
#
# Text formats (json/csv/xml/yaml/toml/md/html/svg) have no dependable
# signature and are intentionally absent from _EXT_EXPECTED, so they skip the
# byte check entirely.

# Which magic-byte GROUP each binary input extension is expected to be. Office
# formats collapse into an "office" group because a single endpoint (e.g.
# doc-to-pdf) legitimately accepts both the ZIP-container form (.docx) and the
# legacy OLE2 form (.doc); we must not reject one for looking like the other.
_EXT_EXPECTED: dict[str, str] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
    ".heic": "heic",
    ".heif": "heic",
    ".pdf": "pdf",
    # ZIP-container office/e-book formats (all start with PK\x03\x04).
    ".docx": "office",
    ".xlsx": "office",
    ".pptx": "office",
    ".odt": "office",
    ".ods": "office",
    ".odp": "office",
    ".ots": "office",
    ".epub": "office",
    ".pages": "office",
    ".numbers": "office",
    # Legacy OLE2 office formats (D0 CF 11 E0 ...).
    ".doc": "office",
    ".xls": "office",
    ".ppt": "office",
}

# Concrete signature detectors. Each returns True when `content` starts with a
# recognizable signature for that type. Kept as a tiny local table so no new
# dependency (python-magic / libmagic) is pulled in.
_HEIC_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm",
    b"hevs", b"mif1", b"msf1", b"heif",
})


def _detect_binary_type(content: bytes) -> str | None:
    """Best-effort magic-byte detection. Returns a group key
    (png/jpeg/gif/webp/heic/pdf/office) or None when nothing is recognized."""
    if not content:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    # WebP: RIFF container with a 'WEBP' form type at bytes 8..12.
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    # HEIC/HEIF: ISO-BMFF 'ftyp' box with a HEIF-family brand at bytes 4..12.
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in _HEIC_BRANDS:
        return "heic"
    if content.startswith(b"%PDF-"):
        return "pdf"
    # ZIP container (docx/xlsx/pptx/odt/... and empty/spanned variants) OR
    # legacy OLE2 compound file — both map to the "office" group.
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "office"
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "office"
    return None


def validate_file_content(endpoint: str, filename: str, content: bytes) -> str | None:
    """Conservative magic-byte check for the uploaded bytes.

    Returns a mismatch-reason string on a HIGH-CONFIDENCE rejection (the caller
    raises 400 and emits the analytics event), or None to allow. Fail-open:
    unknown/indeterminate content and text-based formats always pass.

    Keyed off the UPLOADED file's extension (a binary format maps to exactly one
    signature group in _EXT_EXPECTED), so it also protects file endpoints that
    are not in ALLOWED_EXTENSIONS. Text-based / unmapped extensions skip the
    byte sniff entirely.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    expected = _EXT_EXPECTED.get(ext)
    if expected is None:
        return None  # text-based or unmapped extension -> skip byte sniffing

    detected = _detect_binary_type(content)
    if detected is None:
        return None  # indeterminate -> allow (do not block on uncertainty)
    if detected == expected:
        return None  # matches the declared type

    # A recognizable, DIFFERENT known binary type: high-confidence mismatch.
    return (
        f"declared '{ext}' ({expected}) but the file content is '{detected}'"
    )


# Canonical extension to attach when the bytes identify a type but the supplied
# filename does not. The "office" group is absent here on purpose: one ZIP
# signature covers .docx/.xlsx/.odt/.epub/..., so it is resolved separately by
# _zip_container_ext, which reads the container's own self-description.
_GROUP_TO_EXT: dict[str, str] = {
    "png": ".png",
    "jpeg": ".jpg",
    "gif": ".gif",
    "webp": ".webp",
    "heic": ".heic",
    "pdf": ".pdf",
}

# OOXML declares its type in [Content_Types].xml; ODF and EPUB declare theirs in
# a "mimetype" entry. Both are cheap to read and unambiguous, so an office
# upload named "blob" or "file" (the n8n / FormData defaults) can be resolved
# exactly rather than rejected.
_OOXML_CONTENT_TYPE_TO_EXT = (
    ("wordprocessingml.document", ".docx"),
    ("spreadsheetml.sheet", ".xlsx"),
    ("presentationml.presentation", ".pptx"),
)
_MIMETYPE_TO_EXT = {
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.oasis.opendocument.spreadsheet-template": ".ots",
    "application/epub+zip": ".epub",
}
# Cap on what we will read out of an untrusted archive member. The manifests we
# care about are a few KB; anything larger is not one of them, and refusing to
# inflate past this bounds a zip bomb to a harmless read.
_ZIP_MANIFEST_MAX_BYTES = 256 * 1024


def _zip_container_ext(content: bytes) -> str | None:
    """Resolve a ZIP-container office/e-book format to its exact extension.

    Returns None for a plain ZIP (or anything unreadable) so the caller falls
    back to leaving the filename alone. Never raises: a malformed or hostile
    archive must not turn a conversion into a 500.
    """
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())

            # ODF/EPUB: a "mimetype" member holding the media type verbatim.
            if "mimetype" in names:
                info = archive.getinfo("mimetype")
                if info.file_size <= _ZIP_MANIFEST_MAX_BYTES:
                    declared = archive.read("mimetype").decode("ascii", "ignore").strip()
                    if declared in _MIMETYPE_TO_EXT:
                        return _MIMETYPE_TO_EXT[declared]

            # OOXML: [Content_Types].xml names the part's own content type.
            if "[Content_Types].xml" in names:
                info = archive.getinfo("[Content_Types].xml")
                if info.file_size <= _ZIP_MANIFEST_MAX_BYTES:
                    manifest = archive.read("[Content_Types].xml").decode("utf-8", "ignore")
                    for marker, ext in _OOXML_CONTENT_TYPE_TO_EXT:
                        if marker in manifest:
                            return ext
    except Exception:  # noqa: BLE001 - detection must never raise
        return None
    return None


def _looks_like_svg(content: bytes) -> bool:
    """True when the bytes parse as XML whose ROOT element is <svg>.

    Checking the root (rather than searching for the substring "<svg") is what
    keeps HTML out: an HTML page embedding an inline SVG has root <html>. Reuses
    the same bounded slice as svg_intrinsic_size, which also caps how much
    hostile XML expat is ever handed.
    """
    try:
        iterator = ET.iterparse(io.BytesIO(content[:_SVG_SNIFF_BYTES]), events=("start",))
        _, root = next(iterator)
    except Exception:  # noqa: BLE001 - not XML, truncated, or hostile -> not an SVG
        return False
    # Strip the namespace: "{http://www.w3.org/2000/svg}svg" -> "svg".
    return root.tag.rsplit("}", 1)[-1].lower() == "svg"


# Every extension the product recognizes as a declared input type, derived from
# the endpoint tables so it can never drift from them. A name carrying one of
# these already states what it is -- including text types with no magic bytes
# (.svg/.txt/.csv/.json/...) -- so the normalizer leaves it alone and lets the
# gates judge it. Only names carrying NO recognized type get repaired.
def _declared_input_extensions() -> frozenset[str]:
    known = set(_EXT_EXPECTED)
    for extensions in ALLOWED_EXTENSIONS.values():
        known.update(extensions)
    return frozenset(known)


_DECLARED_INPUT_EXTS = _declared_input_extensions()


def _detect_upload_extension(content: bytes) -> str | None:
    """The canonical extension the CONTENT warrants, or None when undecidable.

    Layered cheapest-first: binary magic, then the ZIP container's own manifest,
    then a bounded XML root-element check for SVG.
    """
    group = _detect_binary_type(content)
    if group == "office":
        return _zip_container_ext(content)
    if group is not None:
        return _GROUP_TO_EXT.get(group)
    if _looks_like_svg(content):
        return ".svg"
    return None


def normalize_upload_filename(filename: str | None, content: bytes) -> str:
    """Repair an upload filename whose extension is missing or non-indicative.

    The converters and ``validate_file_format`` both re-derive the INPUT TYPE
    from the filename string, but a filename is client-controlled metadata that
    routinely arrives without a usable extension through our own clients:

      - ``FormData.append('file', blob)`` with no third argument -> "blob"
      - the n8n node's ``binaryData.fileName ?? 'file'`` fallback -> "file"
      - ``enconvert convert shot.dat --from png`` -> "shot.dat"
      - mobile photo pickers and renamed/extension-less downloads -> "IMG_0042"
      - a multipart part sent with ``filename=""`` -> ""

    In every one of those cases the BYTES are a perfectly valid PNG/JPEG/WebP/
    HEIC/GIF/PDF/SVG/DOCX/..., so rejecting them was our bug, not bad input.
    This runs before both gates and re-attaches the extension the content
    actually warrants.

    Conservative by construction -- the name is returned untouched unless BOTH:
      - it declares no extension the product recognizes as an input type (a
        declared type is left alone so ``validate_file_format`` /
        ``validate_file_content`` can judge it and a genuinely wrong format
        still earns its clean "Invalid file format" 400), and
      - the content resolves to exactly one extension (see
        ``_detect_upload_extension``).

    Formats with no dependable signature and no recognized extension
    (json/csv/txt/md/yaml/toml named "blob") are never rewritten: nothing
    identifies them, so the name is left as sent.
    """
    name = (filename or "").strip()

    ext = os.path.splitext(name)[1].lower()
    if ext in _DECLARED_INPUT_EXTS:
        # Already states its type -- ".png", but also text types like ".svg" and
        # ".csv" that carry no magic bytes. Leave it: validate_file_format
        # decides whether that type belongs on this endpoint, and
        # validate_file_content decides whether the bytes back the claim.
        return name

    canonical = _detect_upload_extension(content)
    if canonical is None:
        # Unrecognized content (or a signature-less text format) -> fail open
        # and leave the caller's name exactly as sent.
        return name

    # Names that carry no usable base. os.path.splitext ignores leading dots, so
    # ".png" splits to (".png", "") and "." to (".", "") -- appending to either
    # yields a name whose extension splitext STILL cannot read ("..png"), which
    # is the very failure this function exists to prevent. Anything that is only
    # dots, or is itself just an extension, gets a real base name instead.
    base = name
    if not base.strip(".") or base.lower() in _DECLARED_INPUT_EXTS:
        base = "upload"
    return base + canonical


def is_safe_filename(filename: str) -> bool:
    """Validate filename to prevent path traversal and injection attacks"""
    dangerous_patterns = ["..", "/", "\\", "\x00", "|", "&", ";", "`", "$", "<", ">"]
    
    for pattern in dangerous_patterns:
        if pattern in filename:
            return False
        
    # Only allow alphanumeric, dots, dashes, underscores
    if not re.match(r'^[a-zA-Z0-9._-]+$', filename):
        return False
    
    return True

def is_domain_allowed(origin: str, allowed_domains: list) -> bool:
    """Check if origin mathces allowed domains (supports wildcards)"""
    from urllib.parse import urlparse
    
    if not origin or not allowed_domains:
        return False
    
    origin_domain = urlparse(origin).netloc
    
    if not origin_domain:
        return False
    
    for allowed in allowed_domains:
        # Remove protocol if present in allowed domain
        if '://' in allowed:
            allowed = urlparse(allowed).netloc
            
        #Exact match
        if origin_domain == allowed:
            return True
        
        #Wildcard subdomain: *.example.com
        if allowed.startswith("*."):
            base_domain = allowed[2:]
            if origin_domain.endswith(f".{base_domain}") or origin_domain == base_domain:
                return True
            
    return False
