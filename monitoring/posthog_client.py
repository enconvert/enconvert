"""PostHog analytics client for the API gateway.

Env-driven process singleton. The gateway's authenticated identity is
PROJECT-centric (``get_current_user`` returns ``user["id"] == project_id``),
so machine/API traffic is keyed as ``project_<project_id>`` per the shared
cross-repo identity contract, and every account-relevant event carries the
``{"project": <project_id>}`` group so PostHog can join gateway activity to
the same project across the other services.

Design rules baked in here:

* Analytics must NEVER break a conversion. Every public helper is
  best-effort: a missing key, an uninstalled ``posthog`` package, or a
  transport error degrades to a silent no-op, never an exception into the
  request path.
* Secrets come from the environment only (``POSTHOG_PROJECT_API_KEY``) — the
  key is never hardcoded.
* ``privacy_mode`` is deliberately NOT set globally: page content can carry
  user PII, so the LLM-observability call site opts into per-call privacy
  mode instead of blanket-masking every event's properties.

The exception-autocapture flag lets PostHog catch the droplet-local pollers'
unhandled errors (they run off-request, so nothing else would report them).
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# ── Event vocabulary (snake_case, object_action; fixed literals only) ────────
# Documented in one place per the cross-repo naming contract. Call sites pass
# the literal strings directly (they are fixed, never built dynamically); this
# block is the authoritative catalogue.
#
#   conversion_requested / conversion_completed / conversion_failed
#   conversion_timed_out
#   batch_conversion_requested / batch_conversion_completed
#   file_download_proxied
#   upload_rejected_oversized / upload_rejected_bad_format
#   conversion_limit_reached / storage_limit_reached
#   feature_gate_blocked / auth_failed
#   v2_perceive_requested / _completed / _failed
#   v2_lookup_performed / v2_distill_completed / v2_discover_completed
#   v2_ingest_started / v2_ingest_completed
#   v2_watch_created / _change_detected / _paused / _updated / _deleted
#   $ai_generation  (LLM observability)

DEFAULT_HOST = "https://us.i.posthog.com"

# Distinct-id prefixes from the shared identity model.
_PROJECT_PREFIX = "project_"
_ANONYMOUS_DISTINCT_ID = "anonymous"

_client: Any = None
_initialized: bool = False


def init() -> Any:
    """Build the process singleton from the environment (idempotent).

    Called once from the FastAPI lifespan on startup. Returns the client, or
    ``None`` when analytics is disabled (no key) or the SDK is unavailable —
    in which case every helper below no-ops.
    """
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    key = os.getenv("POSTHOG_PROJECT_API_KEY")
    if not key:
        logger.info("PostHog disabled: POSTHOG_PROJECT_API_KEY is not set")
        _client = None
        return None

    host = os.getenv("POSTHOG_HOST", DEFAULT_HOST)
    try:
        from posthog import Posthog

        try:
            _client = Posthog(
                project_api_key=key,
                host=host,
                enable_exception_autocapture=True,
                # SDK default is 10,000 buffered events (~10-40MB when egress
                # stalls — e.g. the 2026-07-16 Cloudflare-block incident
                # class). On the 1GB droplet cap the queue; overflow is
                # drop-on-full inside the SDK, so a stall costs analytics,
                # never memory.
                max_queue_size=int(os.getenv("POSTHOG_MAX_QUEUE_SIZE", "1000")),
            )
        except TypeError:
            # Older SDK without the autocapture kwarg — keep analytics rather
            # than losing every event over one unsupported option.
            _client = Posthog(project_api_key=key, host=host)
        logger.info("PostHog analytics initialized (host=%s)", host)
    except Exception:  # noqa: BLE001 — never let telemetry setup break boot
        logger.warning("PostHog init failed; analytics disabled", exc_info=True)
        _client = None
    return _client


def get_client() -> Any:
    """Return the singleton, lazily initializing it on first use.

    Off-request pollers and non-lifespan test paths reach the same shared
    instance through this accessor — there is nothing to inject.
    """
    if not _initialized:
        return init()
    return _client


def flush() -> None:
    """Best-effort flush of the buffered event queue."""
    client = _client
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # noqa: BLE001
        logger.debug("PostHog flush failed", exc_info=True)


def shutdown() -> None:
    """Flush and stop the background sender (called from lifespan shutdown)."""
    client = _client
    if client is None:
        return
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            logger.debug("PostHog shutdown/flush failed", exc_info=True)


# ── Identity + property helpers ──────────────────────────────────────────────


def distinct_id_for_project(project_id: Any) -> str:
    """Machine distinct-id for gateway traffic: ``project_<project_id>``."""
    return f"{_PROJECT_PREFIX}{project_id}"


def group_of(project_id: Any) -> dict[str, str]:
    """The universal ``project`` group join key for account-relevant events."""
    return {"project": str(project_id)}


def source_from(user: dict, request: Any = None) -> str:
    """Traffic source in {web, api, sdk, mcp, extension}.

    Derived from key_type and, when available, the request's User-Agent /
    Origin. Browser widget + dashboard keys are ``web``; a chrome-extension
    origin is ``extension``; private-key traffic defaults to ``api`` unless
    the User-Agent advertises the SDK or an MCP client.
    """
    key_type = (user or {}).get("key_type", "")
    ua = ""
    origin = ""
    if request is not None:
        try:
            ua = (request.headers.get("user-agent", "") or "").lower()
            origin = request.headers.get("origin", "") or ""
        except Exception:  # noqa: BLE001 — header access must never raise here
            ua = ""
            origin = ""
    if origin.startswith("chrome-extension://") or "enconvert-extension" in ua:
        return "extension"
    if "mcp" in ua:
        return "mcp"
    if "enconvert-sdk" in ua or "python-sdk" in ua or "node-sdk" in ua:
        return "sdk"
    if key_type in ("public", "dashboard"):
        return "web"
    return "api"


def converter_module_of(fn: Any) -> str:
    """Map a converter function to its module bucket for event properties."""
    mod = getattr(fn, "__module__", "") or ""
    for name in ("lightweight", "documents", "image", "browser"):
        if f".{name}." in mod or mod.endswith(f".{name}"):
            return name
    return "unknown"


def split_endpoint_formats(endpoint: str) -> tuple[str, str]:
    """('json-to-xml') -> ('json', 'xml'); ('url-to-pdf') -> ('url', 'pdf')."""
    if endpoint and "-to-" in endpoint:
        source, target = endpoint.split("-to-", 1)
        return source, target
    return endpoint or "", ""


# ── Capture helpers (all best-effort) ────────────────────────────────────────


def capture(
    distinct_id: str,
    event: str,
    properties: Optional[dict[str, Any]] = None,
    groups: Optional[dict[str, str]] = None,
) -> None:
    """Send one event. Silently no-ops when analytics is disabled.

    ``None``-valued properties are dropped so a partially-known event stays
    clean. ``groups`` is attached only for account-relevant events (the caller
    passes ``None`` for anonymous/trivial signals to avoid group billing).
    """
    client = get_client()
    if client is None:
        return
    props = {k: v for k, v in (properties or {}).items() if v is not None}
    try:
        # Keyword form works across posthog-python major versions (the
        # positional distinct_id/event order changed between them).
        client.capture(
            distinct_id=distinct_id,
            event=event,
            properties=props,
            groups=groups or {},
        )
    except TypeError:
        try:
            client.capture(distinct_id, event, properties=props, groups=groups or {})
        except Exception:  # noqa: BLE001
            logger.debug("PostHog capture failed for %s", event, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("PostHog capture failed for %s", event, exc_info=True)


def capture_project_event(
    project_id: Any,
    event: str,
    properties: Optional[dict[str, Any]] = None,
    source: Optional[str] = None,
) -> None:
    """Emit an account-relevant event keyed to a project.

    Convenience wrapper that derives the ``project_<id>`` distinct-id and the
    ``project`` group from a project id — the common shape for V2 engine and
    off-request worker events (which have no request to pass to ``source_from``,
    so callers pass ``source`` explicitly or leave it unset)."""
    props = dict(properties or {})
    if source is not None:
        props["source"] = source
    capture(
        distinct_id_for_project(project_id),
        event,
        props,
        group_of(project_id),
    )


def capture_exception(
    exc: BaseException,
    distinct_id: Optional[str] = None,
    properties: Optional[dict[str, Any]] = None,
    groups: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Report an exception with a stack trace; returns the PostHog event id.

    Used by the global exception handler so the client can quote the id when
    contacting support, and available to the pollers on top of autocapture.
    """
    client = get_client()
    if client is None:
        return None
    props = {k: v for k, v in (properties or {}).items() if v is not None}
    try:
        return client.capture_exception(
            exc, distinct_id=distinct_id, properties=props, groups=groups or {}
        )
    except TypeError:
        try:
            return client.capture_exception(exc, distinct_id=distinct_id)
        except Exception:  # noqa: BLE001
            logger.debug("PostHog capture_exception failed", exc_info=True)
            return None
    except Exception:  # noqa: BLE001
        logger.debug("PostHog capture_exception failed", exc_info=True)
        return None


# ── Request-scoped context (for exception autocapture attribution) ───────────


@contextmanager
def _request_context() -> Iterator[None]:
    """Open a PostHog context so autocaptured exceptions during the request
    inherit whatever distinct-id ``identify_context`` later sets. No-ops when
    the SDK/contexts API is unavailable."""
    client = get_client()
    factory = None
    if client is not None:
        try:
            import posthog as _posthog

            factory = getattr(_posthog, "new_context", None)
        except Exception:  # noqa: BLE001
            factory = None
    if factory is None:
        yield
        return
    # Only ENTERING the context is best-effort. The body must never sit inside
    # an `except` that yields again: contextlib calls gen.throw() when the body
    # raises, and a second yield there makes it raise
    # `RuntimeError: generator didn't stop after throw()`, destroying the real
    # traceback. This is the outermost middleware, so that bug replaced EVERY
    # unhandled request exception with that RuntimeError.
    try:
        cm = factory()
        cm.__enter__()
    except Exception:  # noqa: BLE001 — contexts are a best-effort convenience
        yield
        return
    try:
        yield
    finally:
        # sys.exc_info() reports whatever is propagating (including
        # BaseException/CancelledError on client disconnect). The return value
        # is intentionally ignored: a PostHog context must never SUPPRESS a
        # request exception.
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:  # noqa: BLE001
            logger.debug("PostHog context exit failed", exc_info=True)


def identify_context(distinct_id: str) -> None:
    """Attach a distinct-id to the active PostHog context (best-effort)."""
    if get_client() is None:
        return
    try:
        import posthog as _posthog

        fn = getattr(_posthog, "identify_context", None)
        if fn is not None:
            fn(distinct_id)
    except Exception:  # noqa: BLE001
        logger.debug("PostHog identify_context failed", exc_info=True)


class PostHogContextMiddleware:
    """Pure-ASGI middleware that wraps each HTTP request in a PostHog context.

    Auth runs later (in the ``get_current_user`` dependency), which resolves
    the project and calls ``identify_context`` to stamp
    ``project_<project_id>`` onto this context — so any exception autocaptured
    during the request is attributed to the right project. Non-HTTP scopes
    pass straight through.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        with _request_context():
            await self.app(scope, receive, send)
