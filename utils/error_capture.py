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

import traceback

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
    single_line = " ".join(text.split())
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
        chain = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip()
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
