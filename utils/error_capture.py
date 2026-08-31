"""Format conversion failures for persistence on the activity row.

Why this exists: when a conversion fails the only record of *why* used to
be the droplet's journal, so diagnosing a customer report meant grepping
server logs by timestamp. Migration 025 added ``ch_activity.error_message``
so the failure detail travels with the row the admin dashboard already
shows.

What gets stored is the COMPLETE exception chain — a one-line summary
first (so the UI can truncate to something readable without parsing),
then the full formatted traceback including every ``raise ... from ...``
cause, oldest cause first. The root cause is usually several frames below
the handler that caught it, so storing only ``str(exc)`` would throw away
the part that actually identifies the bug.

Nothing here may raise: a failure to describe a failure must never turn
into a second failure on the error path.
"""

from __future__ import annotations

import os
import re
import tempfile
import traceback

# Scratch directories the converters write uploads into. Anything under one
# of these is a server path, never something a caller should be shown or an
# admin should have to read past.
_TEMP_DIRS = tuple(
    d for d in dict.fromkeys((tempfile.gettempdir(), "/tmp", "/var/tmp")) if d
)

# Both spellings of each directory: libraries quote the path with repr(),
# which DOUBLES backslashes, so the stored text reads
# ``C:\\Users\\...\\Temp\\tmp89wyefon.png`` while the directory itself has
# single ones. Matching only the raw form silently missed every Windows
# path (POSIX ``/tmp`` has no separator to escape, so it always matched).
_TEMP_DIR_VARIANTS = tuple(
    dict.fromkeys(
        variant
        for d in _TEMP_DIRS
        for variant in (d, d.replace("\\", "\\\\"))
    )
)

# Anchored on those directories only, so ordinary prose is never touched.
_TEMP_PATH_RE = re.compile(
    "(?:"
    + "|".join(re.escape(v) for v in _TEMP_DIR_VARIANTS)
    + r")[\\/]{1,2}[^\s'\"]*",
    re.IGNORECASE,
)

_PATH_PLACEHOLDER = "the uploaded file"

# Upper bound on what we persist. Comfortably fits a deep Playwright or
# SQLAlchemy traceback; guards against a pathological exception (e.g. one
# carrying a whole HTTP body in its message) bloating the row and the
# admin list payload.
MAX_ERROR_CHARS = 8000

# Exception class names are short; the column is VARCHAR(128).
MAX_ERROR_TYPE_CHARS = 128

_OMITTED = "\n\n... [{dropped} characters omitted] ...\n\n"


def exception_type_name(exc: BaseException) -> str:
    """Exception class name, clipped to the ``error_type`` column width."""
    try:
        return type(exc).__name__[:MAX_ERROR_TYPE_CHARS]
    except Exception:  # noqa: BLE001 - never raise from the error path
        return "Exception"


def truncate_error(text: str | None, *, max_chars: int = MAX_ERROR_CHARS) -> str | None:
    """Clip ``text`` to ``max_chars``, keeping both ends.

    Middle-truncation, not tail-truncation: the head carries the summary
    line and the entry point, the tail carries the innermost frame and the
    root-cause exception. Dropping either end loses the useful half.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001
            return None
    if len(text) <= max_chars:
        return text

    marker = _OMITTED.format(dropped=len(text) - max_chars)
    budget = max(max_chars - len(marker), 0)
    if budget <= 0:
        return text[:max_chars]
    head = int(budget * 0.6)
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def scrub_temp_paths(text: str | None, *paths: str) -> str:
    """Replace scratch-file paths with a neutral token.

    Two sources, because both leak the same thing:

    * ``paths`` — exact paths the caller knows it handed out, replaced
      literally (and by basename, which is what some libraries quote).
    * ``_TEMP_PATH_RE`` — anything under a system temp directory, which
      catches the paths a library found on its own, deep in a chained
      traceback the caller never sees.

    Applies to BOTH destinations: the ``ValueError`` the routes turn into
    the 400 body, and the traceback persisted on the activity row.
    Scrubbing only the first left ``/tmp/tmp89wyefon.png`` sitting in the
    admin error table.
    """
    if not text:
        return text or ""
    try:
        for path in paths:
            if not path:
                continue
            text = text.replace(path, _PATH_PLACEHOLDER)
            base = os.path.basename(path)
            if base:
                text = text.replace(base, _PATH_PLACEHOLDER)
        return _TEMP_PATH_RE.sub(_PATH_PLACEHOLDER, text)
    except Exception:  # noqa: BLE001 - never raise from the error path
        return text


def describe_image_error(exc: BaseException, *paths: str) -> str:
    """A caller-safe description of a failed image conversion.

    The routes surface a converter's ``ValueError`` as the 400 body, so
    whatever PIL or cairosvg said goes straight to the API caller. Two
    problems came out of that:

    * ``cannot identify image file '/tmp/tmp89wyefon.png'`` handed the
      caller an internal filesystem path.
    * It also told them nothing they could act on.

    Duck-typed on the exception's class name so this module keeps no
    imaging dependency.
    """
    if type(exc).__name__ == "UnidentifiedImageError":
        return (
            "the file could not be decoded as an image (its contents do not "
            "match its extension, or it is corrupt or truncated)"
        )
    # CairoSVG parses and renders recursively, so a deeply nested SVG -- or a
    # <use> cycle, which no pre-parse depth check can see -- comes back as a
    # bare "maximum recursion depth exceeded". That names a Python internal,
    # not anything the caller can act on, and it lands in the admin activity
    # table looking like a server crash rather than rejected input.
    if isinstance(exc, RecursionError):
        return (
            "the file is nested too deeply to render (an SVG whose elements "
            "nest beyond the renderer's limit, or whose <use> elements "
            "reference each other in a cycle)"
        )
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001
        return "the file could not be converted"
    return scrub_temp_paths(message, *paths)


def summarize_exception(exc: BaseException) -> str:
    """Single-line ``Type: message`` summary — the first stored line."""
    try:
        name = type(exc).__name__
    except Exception:  # noqa: BLE001
        return "Exception"
    try:
        text = str(exc).strip()
    except Exception:  # noqa: BLE001
        text = ""
    # HTTPException (Starlette/FastAPI) carries the useful part on
    # attributes; older Starlette versions have no informative __str__.
    status_code = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if status_code is not None and detail is not None:
        candidate = f"{status_code}: {detail}"
        if len(candidate) > len(text):
            text = candidate
    if not text:
        try:
            text = repr(exc)
        except Exception:  # noqa: BLE001
            text = ""
    single_line = scrub_temp_paths(" ".join(text.split()))
    return f"{name}: {single_line}" if single_line else name


def format_exception_detail(
    exc: BaseException, *, context: str | None = None, max_chars: int = MAX_ERROR_CHARS
) -> str:
    """Full failure detail for ``exc``: summary line + whole traceback chain.

    ``context`` prefixes a caller-supplied line (e.g. the URL or the step
    that was running) when the traceback alone wouldn't identify the input.
    """
    summary = summarize_exception(exc)
    try:
        # The chain carries every implicitly-chained cause, which is where
        # the raw library message (and its scratch path) survives even when
        # the outer ValueError was already cleaned.
        chain = scrub_temp_paths(
            "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).strip()
        )
    except Exception:  # noqa: BLE001 - never raise from the error path
        chain = ""

    # Summary FIRST, always: the admin table truncates to the first line,
    # so leading with the context (e.g. "url=...") would show the input
    # instead of the cause on exactly the batch rows that carry context.
    parts = [summary]
    if context:
        parts.append(" ".join(str(context).split()))
    if chain and chain != summary:
        parts.append(chain)
    return truncate_error("\n\n".join(parts), max_chars=max_chars) or summary


def _client_error_status(exc: BaseException) -> int | None:
    """The 4xx status of an HTTPException-like exception, else ``None``.

    Duck-typed on ``status_code`` so this module keeps no FastAPI import.
    """
    try:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return status
    except Exception:  # noqa: BLE001 — never raise from the error path
        pass
    return None


def _client_error_message(exc: BaseException, status: int) -> str:
    """One line describing a rejected request, with no stack trace.

    A 4xx is the caller's input being refused exactly as designed — an
    SSRF screen on a private address, an exhausted quota, an endpoint
    missing from a key's allowlist. Recording a full server traceback for
    it filled the admin Activity table with expected client errors that
    read like crashes, burying the real ones.
    """
    try:
        detail = getattr(exc, "detail", None)
        text = " ".join(str(detail).split()) if detail else ""
    except Exception:  # noqa: BLE001
        text = ""
    return f"HTTP {status}: {text}" if text else f"HTTP {status}"


def error_fields(
    exc: BaseException | None,
    *,
    context: str | None = None,
    fallback_message: str | None = None,
    fallback_type: str = "Error",
) -> dict[str, str]:
    """``update_activity_status`` kwargs describing a failure.

    Returns ``{"error_message": ..., "error_type": ...}``, or ``{}`` when
    there is nothing to record — so callers can splat it unconditionally:

        await update_activity_status(aid, "Failed", **error_fields(exc))

    ``fallback_message`` covers failures with no exception object (a
    middleware timeout, a sibling job aborting the batch).
    """
    try:
        if exc is not None:
            client_status = _client_error_status(exc)
            if client_status is not None:
                message = _client_error_message(exc, client_status)
                if context:
                    message = f"{message}\n\n{' '.join(str(context).split())}"
                return {
                    "error_message": truncate_error(message) or message,
                    "error_type": exception_type_name(exc),
                }
            return {
                "error_message": format_exception_detail(exc, context=context),
                "error_type": exception_type_name(exc),
            }
        if fallback_message:
            message = " ".join(str(fallback_message).split())
            if context:
                message = f"{message}\n\n{' '.join(str(context).split())}"
            return {
                "error_message": truncate_error(message) or message,
                "error_type": fallback_type[:MAX_ERROR_TYPE_CHARS],
            }
    except Exception:  # noqa: BLE001 - never raise from the error path
        pass
    return {}
