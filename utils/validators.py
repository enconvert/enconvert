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
    "image": [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".svg", ".ico"],
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
    "jpg-to-svg": [".jpeg", ".jpg"],
    "svg-to-jpeg": [".svg"],
    "svg-to-png": [".svg"],
    "svg-to-webp": [".svg"],
    "compress-image": [".png", ".jpg", ".jpeg", ".webp"],
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
