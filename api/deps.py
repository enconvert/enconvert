import os
import re
import threading
from typing import Optional
from fastapi import HTTPException, Header, Request, UploadFile
import logging

# Status-polling allowlist is scoped to the exact operation-id shape so a
# future /v2/perceive/<other> route can't silently bypass the
# allowed_endpoints check (it would need its own explicit entry).
_PERCEIVE_STATUS_PATH_RE = re.compile(r"/v2/perceive/per_[0-9a-f]+")
# Batch job status uses 'batch_<hex>' ids (batch_worker.py), which the per_
# regex above deliberately does NOT match — without this entry a key scoped to
# ['/v2/perceive', '/v2/perceive/batch'] could submit a batch but would 403 on
# GET /v2/perceive/batch/{job_id} when polling it.
_PERCEIVE_BATCH_STATUS_PATH_RE = re.compile(r"/v2/perceive/batch/batch_[0-9a-f]+")
# Same scoping for /v2/ingest job status/cancel/retry: a token restricted to
# '/v2/ingest' must still reach the jobs it created — GET/DELETE
# /v2/ingest/{job_id} and POST /v2/ingest/{job_id}/retry-webhook (H.8) — without
# each path being its own allowlist entry. The action suffix is enumerated
# EXPLICITLY (not a wildcard) so a future, possibly more-sensitive per-job
# sub-route does not auto-inherit this bypass — adding one forces a deliberate
# regex edit. The static /v2/ingest list + /v2/ingest/webhook-secret management
# routes do not match and stay gated to broad/dashboard tokens.
_INGEST_STATUS_PATH_RE = re.compile(
    r"/v2/ingest/ing_[0-9a-f]+(?:/retry-webhook)?"
)
# Same scoping for /v2/watch per-watcher management: a token restricted to
# '/v2/watch' must still reach GET/PATCH/DELETE /v2/watch/{watcher_id} for the
# watchers it created, without each verb being its own allowlist entry.
_WATCH_STATUS_PATH_RE = re.compile(r"/v2/watch/wat_[0-9a-f]+(?:/snapshots)?")

from datetime import datetime, timezone, timedelta

from auth.api_key import validate_api_key
from auth.jwt_handler import validate_token
from models import Project, Subscription
from monitoring import posthog_client
from sqlmodel import select
from utils.postgres import get_db
from utils.subscription import ADMIN_SUBSCRIPTION, get_subscription, get_current_usage_period, is_admin_default_project, get_project_owner_email, is_project_owner_active
from utils.email_notifier import send_quota_reached_email
from rate_limiting.limiter import enforce as enforce_rate_limits

logger = logging.getLogger("conversion-api-gateway")


def _stamp_request_identity(request: Request, user: dict) -> None:
    """Attach the resolved project identity to request.state and the active
    PostHog context so downstream events + exception autocapture are keyed to
    ``project_<project_id>`` (the machine distinct-id from the shared identity
    contract)."""
    project_id = user.get("id")
    distinct_id = posthog_client.distinct_id_for_project(project_id)
    request.state.project_id = project_id
    request.state.posthog_distinct_id = distinct_id
    request.state.posthog_group = posthog_client.group_of(project_id)
    request.state.source = posthog_client.source_from(user, request)
    posthog_client.identify_context(distinct_id)


def _capture_auth_failed(
    request: Request, failure_reason: str, status_code: int, key_type: str
) -> None:
    """Emit auth_failed. No project is resolvable at this point, so the event
    is anonymous (no project group — it must not bill against a project)."""
    posthog_client.capture(
        _ANONYMOUS_AUTH_DISTINCT_ID,
        "auth_failed",
        {
            "failure_reason": failure_reason,
            "status_code": status_code,
            "key_type": key_type,
            "path": request.url.path,
        },
    )


_ANONYMOUS_AUTH_DISTINCT_ID = "anonymous"


def _gate_capture(user: dict, event: str, properties: dict) -> None:
    """Emit a plan/quota gate event keyed to the project (account-relevant, so
    it carries the project group). These fire BEFORE any row is created, so
    they are pure new signal about demand the plan blocked."""
    project_id = user.get("id")
    posthog_client.capture(
        posthog_client.distinct_id_for_project(project_id),
        event,
        properties,
        posthog_client.group_of(project_id),
    )


async def get_current_user(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None)
) -> dict:
    """
    Dependency: Authenticate user via API key or JWT token

    Authentication rules:
    - Public API keys: Can ONLY be used for /v1/auth/token endpoint
    - Private API keys: Can be used for all endpoints
    - JWT tokens: Can be used for all endpoints (required for browser clients)

    Returns:
        dict: User info with keys: id, tier, plan_slug, key_type, allowed_domains, subscription
    """

    logger.info(f"get_current_user: path={request.url.path}, has_api_key={x_api_key is not None}, has_auth={authorization is not None}")

    # Try JWT token auth first
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        origin = request.headers.get("Origin")
        parent_origin = request.headers.get("X-Parent-Origin")
        logger.info(f"JWT auth: origin={origin}, parent_origin={parent_origin}")
        try:
            user = await validate_token(token, origin, current_parent_origin=parent_origin)
        except HTTPException as exc:
            _capture_auth_failed(request, str(exc.detail), exc.status_code, "jwt")
            raise

        # Enforce allowed_endpoints from JWT token
        # /v1/whoami is exempt so a deliberately-scoped key (non-empty
        # allowed_endpoints) can still resolve its own identity; the route
        # itself rejects non-private key_types, so a public JWT gets 403 there.
        auth_exempt_paths = ("/v1/auth/token", "/v1/auth/verify", "/v1/whoami")
        allowed_endpoints = user.get("allowed_endpoints", [])
        # '*' is the wildcard sentinel: grants every endpoint, including future ones.
        if allowed_endpoints and "*" not in allowed_endpoints and request.url.path not in allowed_endpoints:
            if request.url.path.startswith("/v1/convert/status/"):
                pass  # Always allow conversion job status polling
            elif request.url.path.startswith("/v1/convert/batch/"):
                pass  # Always allow batch status polling
            elif request.url.path.startswith("/v1/convert/download/"):
                pass  # Always allow file download proxy
            elif request.url.path.startswith("/v1/extension/"):
                pass  # Extension endpoints always allowed
            elif _PERCEIVE_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow perceive operation status polling
            elif _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow perceive batch status polling
            elif _INGEST_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow ingest job status polling / cancel
            elif _WATCH_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow per-watcher management (get/patch/delete)
            elif not request.url.path.endswith(auth_exempt_paths):
                logger.warning(f"JWT auth rejected: endpoint not allowed (path={request.url.path}, allowed={allowed_endpoints})")
                raise HTTPException(
                    status_code=403,
                    detail=f"Endpoint '{request.url.path}' not allowed for this token"
                )

        # Set allowed_domains on request state so CORS middleware can use it
        request.state.allowed_domains = user.get("allowed_domains", [])

        # Attach subscription data
        _attach_subscription(user)

        _stamp_request_identity(request, user)
        logger.info(f"JWT auth successful: user={user['id']}, key_type={user['key_type']}")
        check_rate_limits(request, user)
        return user

    # Fall back to API key auth
    if x_api_key:
        logger.info(f"API key auth: prefix={x_api_key[:7]}...")
        try:
            user = validate_api_key(x_api_key, request)
        except HTTPException as exc:
            key_type = (
                "private" if x_api_key.startswith("sk_")
                else "public" if x_api_key.startswith("pk_")
                else "unknown"
            )
            _capture_auth_failed(request, str(exc.detail), exc.status_code, key_type)
            raise

        # Auth endpoints are always accessible regardless of allowed_endpoints.
        # /v1/whoami is exempt so a deliberately-scoped sk_ key (non-empty
        # allowed_endpoints) can still resolve its own identity for the MCP.
        auth_exempt_paths = ("/v1/auth/token", "/v1/auth/verify", "/v1/whoami")

        # Check if endpoint is allowed for this API key
        allowed_endpoints = user.get("allowed_endpoints", [])
        # '*' is the wildcard sentinel: grants every endpoint, including future ones.
        if allowed_endpoints and "*" not in allowed_endpoints and request.url.path not in allowed_endpoints:
            if request.url.path.startswith("/v1/convert/status/"):
                pass  # Always allow conversion job status polling
            elif request.url.path.startswith("/v1/convert/batch/"):
                pass  # Always allow batch status polling
            elif request.url.path.startswith("/v1/convert/download/"):
                pass  # Always allow file download proxy
            elif _PERCEIVE_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow perceive operation status polling
            elif _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow perceive batch status polling
            elif _INGEST_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow ingest job status polling / cancel
            elif _WATCH_STATUS_PATH_RE.fullmatch(request.url.path):
                pass  # Always allow per-watcher management (get/patch/delete)
            elif not request.url.path.endswith(auth_exempt_paths):
                logger.warning(f"API key auth rejected: endpoint not allowed (path={request.url.path}, allowed={allowed_endpoints})")
                raise HTTPException(
                    status_code=403,
                    detail=f"Endpoint '{request.url.path}' not allowed for this API key"
                )

        # Public keys can ONLY be used for token exchange and read-only branding lookup
        if user["key_type"] == "public":
            public_allowed_suffixes = ("/auth/token", "/auth/branding")
            if not any(request.url.path.endswith(suffix) for suffix in public_allowed_suffixes):
                logger.warning(f"API key auth rejected: public key used for non-public endpoint (path={request.url.path})")
                raise HTTPException(
                    status_code=403,
                    detail="Public API keys can only be used to generate JWT tokens or fetch widget branding. "
                           "Please exchange your public key for a JWT token at /v1/auth/token, "
                           "then use the token for API calls."
                )

        # Set allowed_domains on request state so CORS middleware can use it
        request.state.allowed_domains = user.get("allowed_domains", [])

        # Attach subscription data
        _attach_subscription(user)

        _stamp_request_identity(request, user)
        logger.info(f"API key auth successful: user={user['id']}, key_type={user['key_type']}, path={request.url.path}")
        check_rate_limits(request, user)
        return user

    logger.warning(f"Authentication failed: no API key or JWT token provided for {request.url.path}")
    _capture_auth_failed(request, "no_credentials", 401, "none")
    raise HTTPException(status_code=401, detail="Authentication required")


def _attach_subscription(user: dict):
    """Attach subscription data to the user dict. Admin users get unlimited bypass."""
    try:
        project_id = int(user.get("id"))
    except (TypeError, ValueError):
        return

    db = get_db()
    try:
        if not is_project_owner_active(db, project_id):
            raise HTTPException(status_code=403, detail="Account suspended")
        if is_admin_default_project(db, project_id):
            # Shared with background workers via utils.subscription
            # .get_effective_subscription — see ADMIN_SUBSCRIPTION's note.
            user["subscription"] = dict(ADMIN_SUBSCRIPTION)
            return
    finally:
        db.close()

    sub = get_subscription(project_id)
    if sub:
        user["subscription"] = sub
    else:
        # Fallback: no subscription found, use free defaults
        user["subscription"] = {
            "plan_slug": "free",
            "conversion_limit": 100,
            "max_file_size": 5242880,
            "file_retention_hours": 1,
            "batch_limit": 0,
            "storage_bytes": 0,
            "has_async_mode": False,
            "has_webhook": False,
            "has_zip_output": False,
            "has_basic_auth": False,
            "crawl_mode": "none",
            "widget_branding": True,
            "overage_enabled": False,
            "overage_allowed": False,
            # V2: mirror the migration-012 free-plan defaults.
            "perceive_enabled": True,
            "perceive_operations_month": 50,
            "discover_enabled": True,
            "lookup_enabled": True,
            "lookup_queries_month": 25,
            "distill_enabled": False,
            "distill_operations_month": 0,
            "ingest_enabled": False,
            "ingest_pages_month": 0,
            "watch_enabled": False,
            "max_watchers": 0,
            "llm_extraction_enabled": False,
            "agent_model_tier": "none",
        }


def check_rate_limits(
    request: Request,
    user: dict,
    endpoint: Optional[str] = None,
):
    """Enforce per-key/per-plan request-rate limits (HTTP 429).

    Delegates to rate_limiting.limiter, which self-gates on
    RATE_LIMITING_ENABLED and only throttles billable POSTs (never status
    polling). ``endpoint`` is accepted for backward compatibility with the
    legacy call sites; the path is read from ``request``.
    """
    enforce_rate_limits(request, user)


def check_storage_limit(user: dict):
    """Reject request upfront if project has reached its storage limit."""
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    # If no storage plan purchased (storage_bytes == 0), no storage limit enforcement
    # Files will be auto-deleted after retention period
    if sub.get("storage_bytes", 0) == 0:
        return

    try:
        project_id = int(user.get("id"))
    except (TypeError, ValueError):
        return

    db = get_db()
    try:
        project = db.exec(select(Project).where(Project.id == project_id)).first()
        if project and (project.storage_used or 0) >= sub.get("storage_bytes", 0):
            _gate_capture(user, "storage_limit_reached", {
                "storage_used_bytes": int(project.storage_used or 0),
                "storage_limit_bytes": int(sub.get("storage_bytes", 0) or 0),
                "plan_tier": sub.get("plan_slug", "free"),
            })
            raise HTTPException(
                status_code=402,
                detail="Storage limit reached. Delete files or upgrade your storage plan to continue."
            )
    finally:
        db.close()


def _maybe_alert_quota_reached(project_id: int, used: int, limit: int, plan_slug: str) -> None:
    """Best-effort, throttled (once/24h) owner alert when the monthly conversion
    quota hits 100%. Runs only on the limit-reached path, so the extra DB read
    and email send never touch a normal (under-limit) request. The email itself
    (owner lookup + Brevo HTTP) runs on a daemon thread — this function is
    called synchronously inside async route handlers, and a blocking send here
    would stall the single-worker event loop."""
    try:
        db = get_db()
        try:
            sub = db.exec(select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.status.in_(["active", "past_due"]),
            ).order_by(Subscription.id.desc())).first()
            if not sub:
                return
            now = datetime.now(timezone.utc)
            last = sub.last_quota_alert_at
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last) < timedelta(hours=24):
                    return
            sub.last_quota_alert_at = now
            db.add(sub)
            db.commit()
        finally:
            db.close()

        def _send_alert():
            try:
                email = get_project_owner_email(project_id)
                if email:
                    send_quota_reached_email(email, plan_slug, used, limit)
            except Exception:
                logger.exception("Failed to send quota-reached alert")

        threading.Thread(target=_send_alert, daemon=True).start()
    except Exception:
        logger.exception("Failed to send quota-reached alert")


def check_conversion_limit(user: dict, url_count: int = 1):
    """
    Enforce monthly conversion limits based on subscription.
    Accepts url_count to pre-check batch jobs against the remaining quota.
    """
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    try:
        project_id = int(user.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Project not found")

    usage = get_current_usage_period(project_id)
    if not usage:
        return

    limit = sub.get("conversion_limit", 100)
    used = usage.conversions_used
    remaining = limit - used

    if remaining <= 0:
        # Check if overage is allowed and enabled
        if sub.get("overage_allowed") and sub.get("overage_enabled"):
            return  # Allow overage (will be billed)
        _gate_capture(user, "conversion_limit_reached", {
            "conversions_used": used,
            "conversion_limit": limit,
            "plan_tier": sub.get("plan_slug", "free"),
            "url_count": url_count,
        })
        _maybe_alert_quota_reached(project_id, used, limit, sub.get("plan_slug", "free"))
        raise HTTPException(
            status_code=402,
            detail=f"Monthly conversion limit reached ({used}/{limit}). Upgrade your plan to continue."
        )

    if url_count > remaining:
        if sub.get("overage_allowed") and sub.get("overage_enabled"):
            return  # Allow overage (will be billed)
        _gate_capture(user, "conversion_limit_reached", {
            "conversions_used": used,
            "conversion_limit": limit,
            "plan_tier": sub.get("plan_slug", "free"),
            "url_count": url_count,
        })
        raise HTTPException(
            status_code=402,
            detail=f"Batch of {url_count} URLs would exceed your monthly limit. "
                   f"You have {remaining} conversions remaining out of {limit}."
        )


def check_abuse_patterns(
    request: Request,
    user: dict
):
    """
    Dependency: Check for abuse patterns
    Logs suspicious activity
    """
    pass


def _uploaded_size(file: UploadFile) -> Optional[int]:
    """Exact byte count of an uploaded part, independent of any header.

    Starlette's MultiPartParser builds every file part as ``UploadFile(size=0,
    ...)`` and accumulates ``size`` on each write as the body streams in, so
    this is already correct for chunked / HTTP2 requests that carry no
    Content-Length. The seek fallback covers an ``UploadFile`` constructed by
    hand (tests, non-multipart callers), where ``size`` may be None.
    """
    if file.size is not None:
        return file.size
    try:
        size = file.file.seek(0, os.SEEK_END)
        file.file.seek(0)
        return size
    except (AttributeError, OSError):
        return None


def validate_file_size(
    request: Request,
    user: dict,
    file: Optional[UploadFile] = None,
) -> None:
    """Dependency: validate upload size against the subscription ceiling.

    ``file`` is authoritative when supplied and MUST be passed by any route that
    accepts an upload. The Content-Length fallback exists only for the legacy
    call sites that do not yet thread their UploadFile through: it measures the
    whole multipart envelope rather than the file, and a client that sends no
    Content-Length at all (chunked / HTTP2) is not size-checked by it at all.
    See ``_uploaded_size``.
    """
    sub = user.get("subscription", {})
    max_size = sub.get("max_file_size", 5242880)

    size: Optional[int] = None
    if file is not None:
        size = _uploaded_size(file)

    if size is None:
        content_length = request.headers.get("content-length")
        if not content_length:
            return
        try:
            size = int(content_length)
        except ValueError:
            # h11 rejects a malformed Content-Length upstream; defensive only —
            # a ValueError here would surface as a 500.
            return

    if size > max_size:
        _gate_capture(user, "upload_rejected_oversized", {
            "attempted_file_size_bytes": size,
            "max_allowed_bytes": max_size,
            "plan_tier": sub.get("plan_slug", "free"),
            "key_type": user.get("key_type", "unknown"),
        })
        raise HTTPException(
            status_code=413,
            detail={
                "error": "File too large",
                "file_size": size,
                "max_size": max_size,
                "tier": sub.get("plan_slug", "free"),
                "key_type": user.get("key_type", "unknown")
            }
        )


# V2 quota registry (Task F.5; later sprints add lookup/distill/ingest/
# watch rows). Maps a ch_usage_periods counter to its ch_plans gate flag
# and monthly-limit column. Per migration 011's documented convention,
# limit == 0 with the flag TRUE means UNLIMITED (enterprise).
V2_QUOTAS: dict = {
    "perceive_operations": {
        "flag": "perceive_enabled",
        "limit_key": "perceive_operations_month",
        "label": "Perceive",
    },
    "lookup_queries": {
        "flag": "lookup_enabled",
        "limit_key": "lookup_queries_month",
        "label": "Lookup",
    },
    "distill_operations": {
        "flag": "distill_enabled",
        "limit_key": "distill_operations_month",
        "label": "Distill",
    },
    "ingest_pages": {
        "flag": "ingest_enabled",
        "limit_key": "ingest_pages_month",
        "label": "Ingest",
    },
}


def check_v2_quota(user: dict, counter: str, units: int = 1):
    """Enforce the V2 plan gate AND the monthly quota for `counter`.

    Both denials are 402 per plan Task F.5 verification (d)/(e) — the
    fix is the same either way: upgrade to a V2-inclusive plan. (V1's
    check_feature_access answers 403 for V1 features; the F.5 playbook
    pins 402 for V2, so V2 enforcement lives here, separately.)

    ``units`` is the number of operations this request will consume
    (F.8: one per batch URL). The default of 1 preserves the original
    single-operation semantics exactly (used + 1 > limit == used >= limit).
    """
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    spec = V2_QUOTAS[counter]
    label = spec["label"]

    if not sub.get(spec["flag"], False):
        raise HTTPException(
            status_code=402,
            detail=f"{label} is not available on your current plan. "
            "Upgrade to a V2-inclusive plan to access this endpoint."
        )

    limit = int(sub.get(spec["limit_key"], 0) or 0)
    if limit <= 0:
        return  # 0 + enabled flag = unlimited (migration 011 convention)

    try:
        project_id = int(user.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Project not found")

    usage = get_current_usage_period(project_id)
    if not usage:
        return

    # KNOWN TOLERANCE (matches V1 check_conversion_limit): this read and
    # the post-render increment in services.v2_engine.usage are separate
    # statements, so N requests racing at the boundary can overshoot the
    # cap by up to N-1. Renders are seconds-long and per-project
    # concurrency is low, so the bound is tiny; closing it fully needs an
    # optimistic reserve+rollback, deferred to keep V1 parity and avoid
    # the worse failure of an uncounted (free) completed render.
    used = int(getattr(usage, counter, 0) or 0)
    if used + units > limit:
        needed = f" This request needs {units} operations." if units > 1 else ""
        raise HTTPException(
            status_code=402,
            detail=f"Monthly {label} limit reached ({used}/{limit}).{needed} "
            "Upgrade your plan to continue."
        )


def check_v2_feature(user: dict, flag: str, label: str) -> None:
    """Gate a V2 endpoint on a boolean plan flag with NO quota counter.

    ``/v2/discover`` (Task H.1) is the first such endpoint: it is cheap
    (HTTP-only, no browser, no Spaces artifact) and has no per-operation
    counter on ch_usage_periods, so check_v2_quota does not fit. The
    denial is 402 to match the F.5 V2-gate convention (V1 feature gates
    answer 403 via check_feature_access; V2 gates answer 402 — the
    remedy is the same: upgrade to a V2-inclusive plan). Admin bypasses.
    """
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    if not sub.get(flag, False):
        _gate_capture(user, "feature_gate_blocked", {
            "gated_feature": flag,
            "plan_tier": sub.get("plan_slug", "free"),
        })
        raise HTTPException(
            status_code=402,
            detail=f"{label} is not available on your current plan. "
            "Upgrade to a V2-inclusive plan to access this endpoint.",
        )


def check_watcher_quota(user: dict, active_count: int) -> None:
    """Gate POST /v2/watch on the plan flag AND the concurrent watcher cap.

    Unlike the monthly counters in ``check_v2_quota``, ``max_watchers`` limits
    how many ACTIVE watchers a project may hold at once, so the caller passes
    the current active count. Both denials are 402 to match the F.5 V2-gate
    convention (the remedy is the same: upgrade). Per the migration 011
    convention, ``max_watchers == 0`` with the flag TRUE means UNLIMITED
    (enterprise/admin). Admin bypasses entirely.

    KNOWN TOLERANCE (matches check_v2_quota): the count read and the insert are
    separate statements, so requests racing at the boundary can overshoot the
    cap by up to N-1. Per-project concurrency on this endpoint is low, so the
    bound is tiny; an optimistic reserve is deferred.
    """
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    if not sub.get("watch_enabled", False):
        raise HTTPException(
            status_code=402,
            detail="Watch is not available on your current plan. "
            "Upgrade to a V2-inclusive plan to access this endpoint.",
        )

    limit = int(sub.get("max_watchers", 0) or 0)
    if limit <= 0:
        return  # 0 + enabled flag = unlimited (migration 011 convention)

    if active_count >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"Active watcher limit reached ({active_count}/{limit}). "
            "Delete an existing watcher or upgrade your plan to add more.",
        )


def check_feature_access(user: dict, feature: str):
    """Generic feature gate that checks boolean flags on the subscription.
    Returns 403 with upgrade message if feature not available."""
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    if not sub.get(feature, False):
        feature_names = {
            "has_async_mode": "Async processing",
            "has_webhook": "Webhook callbacks",
            "has_zip_output": "ZIP output bundling",
            "has_basic_auth": "Basic authentication, cookies & custom headers",
        }
        name = feature_names.get(feature, feature)
        _gate_capture(user, "feature_gate_blocked", {
            "gated_feature": feature,
            "plan_tier": sub.get("plan_slug", "free"),
        })
        raise HTTPException(
            status_code=403,
            detail=f"{name} is not available on your current plan. Please upgrade to access this feature."
        )


def check_batch_limit(user: dict, url_count: int):
    """Check url_count against subscription batch_limit.
    Returns 403 if exceeded or if batch_limit == 0 (not available)."""
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    batch_limit = sub.get("batch_limit", 0)
    if batch_limit == 0:
        raise HTTPException(
            status_code=403,
            detail="Batch processing is not available on your current plan. Please upgrade to access this feature."
        )

    if url_count > batch_limit:
        raise HTTPException(
            status_code=403,
            detail=f"Batch size {url_count} exceeds your plan's limit of {batch_limit} URLs per batch."
        )


def check_crawl_access(user: dict, crawl_type: str):
    """Check subscription crawl_mode against requested crawl type."""
    sub = user.get("subscription", {})
    if sub.get("plan_slug") == "admin":
        return

    crawl_mode = sub.get("crawl_mode", "none")
    # Hierarchy: none < sitemap < full
    if crawl_mode == "none":
        raise HTTPException(
            status_code=403,
            detail="Website crawling is not available on your current plan. Please upgrade to access this feature."
        )
    if crawl_type == "full" and crawl_mode == "sitemap":
        raise HTTPException(
            status_code=403,
            detail="Full website crawling requires a Pro plan or higher. Your plan supports sitemap-based crawling only."
        )
