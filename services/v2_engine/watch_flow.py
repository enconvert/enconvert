"""/v2/watch orchestration (Tasks I.1/I.2, plan section 4 + section 8).

Two responsibilities, one per sprint:

* I.2 — the CRUD service the handlers delegate to: create (SSRF screen + id +
  hourly-floor clamp), response shaping, and PATCH resolution (status toggles
  re-arm or stop the schedule).
* I.1 — ``run_check``: one scheduled check end-to-end. It renders the watched
  URL through the shared Crawl4AI singleton (``perceive_flow.render_html`` —
  no persistence, no perceive quota), then reschedules. Three consecutive
  render failures pause the watcher and email the owner (plan Task I.1 step 4).
* I.3 — change detection: ``run_check`` builds a capture (main-content text +
  extracted structure) from the render, diffs it against the previous baseline
  with the four-strategy engine (``services.v2_engine.quality.diff``), persists
  the capture body to Spaces + the diff verdict to the snapshot row. A render
  that scores below the quality floor (or is blocked) is recorded as an
  audit-only check and never becomes a baseline (plan Task I.3 step 3).

Task I.4 still fires the webhook + change email where marked below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from api.v2.schemas.watch import (
    MIN_FREQUENCY_MINUTES,
    WatchCreateRequest,
    WatcherResponse,
    WatcherSnapshotResponse,
    WatcherSummary,
    WatchUpdateRequest,
)
from models import Watcher, WatcherSnapshot
from monitoring import posthog_client
from services.v2_engine import perceive_flow, watch_store
from services.v2_engine.crawl4ai_processors import (
    extract_json_ld,
    generate_fit_markdown,
    scrap_html,
    serialize_links,
    serialize_tables,
)
from services.v2_engine.quality.diff import Capture, DiffResult, diff_captures
from services.v2_engine.url_safety import assert_public_http_url
from services.v2_engine.watch_store import ClaimedWatcher
from utils import webhook_secret
from utils.callback_notifier import deliver_signed_webhook
from utils.email_notifier import (
    send_watcher_change_email,
    send_watcher_paused_email,
)
from utils.storage import download_from_storage, upload_to_gcs
from utils.subscription import get_project_owner_email

logger = logging.getLogger(__name__)

# Plan Task I.1 step 4: pause (no reschedule) after this many consecutive
# render failures.
MAX_CONSECUTIVE_ERRORS = 3

# Plan Task I.3 step 3: a render scoring below this (or flagged blocked) is the
# F.7 scorer saying "the page did not really render" — record an audit-only
# check, do not diff, do not let it become a baseline.
QUALITY_FLOOR = 0.4

# Spaces path segment for persisted capture bodies.
WATCH_SNAPSHOT_ENDPOINT = "v2-watch-snapshots"

# Cap the main-content body we diff + persist, so a multi-MB page bounds both
# the SequenceMatcher cost and the Spaces object size (mirrors perceive_flow's
# main-content cap). NOTE: superseded snapshot bodies are not deleted here —
# pruning old capture objects is deferred to the retention sweep (plan 6.3).
MAX_CAPTURE_TEXT_CHARS = 200_000

WATCHER_ID_PREFIX = "wat_"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claimed_notification_channel(claimed: ClaimedWatcher) -> str:
    """Which channels a claimed watcher notifies on: webhook / email / both / none."""
    has_webhook = bool(getattr(claimed, "webhook_url", None))
    has_email = bool(getattr(claimed, "notify_email", None))
    if has_webhook and has_email:
        return "both"
    if has_webhook:
        return "webhook"
    if has_email:
        return "email"
    return "none"


def _compute_next_check_at(now: datetime, frequency_minutes: int) -> datetime:
    """Next fire time with the hourly floor applied (plan Task I.1 step 3).

    ``max(frequency, 60)`` reproduces the plan's
    ``max(next_check_at, last_check_at + 1h)`` because the check that just ran
    IS ``last_check_at`` (== ``now``). The floor is enforced here as well as at
    create/update so the cadence is safe even for a hand-edited row.
    """
    interval = max(int(frequency_minutes), MIN_FREQUENCY_MINUTES)
    return now + timedelta(minutes=interval)


# ── CRUD service (Task I.2) ──────────────────────────────────────────────────


async def create_watcher(body: WatchCreateRequest, project_id: int) -> Watcher:
    """Create a watcher: SSRF-screen the URL, then persist it active.

    ``assert_public_http_url`` raises HTTPException (4xx) BEFORE anything is
    written, so a blocked/internal target never leaves a row behind. The first
    ``next_check_at`` is ``now`` — the poller picks it up on its next tick.
    """
    url = str(body.url).strip()
    await assert_public_http_url(url)

    now = _utcnow()
    watcher_id = f"{WATCHER_ID_PREFIX}{uuid.uuid4().hex}"
    frequency = max(int(body.frequency_minutes), MIN_FREQUENCY_MINUTES)
    return await asyncio.to_thread(
        watch_store.create_watcher,
        watcher_id=watcher_id,
        project_id=project_id,
        url=url,
        frequency_minutes=frequency,
        diff_mode=body.diff_mode,
        track_fields=body.track_fields,
        webhook_url=body.webhook_url,
        notify_email=body.notify_email,
        next_check_at=now,
        now=now,
    )


async def update_watcher(
    watcher_id: str, project_id: int, body: WatchUpdateRequest
) -> Optional[Watcher]:
    """Resolve a PATCH into concrete column updates and apply them.

    Only the fields present in the body change. A ``status`` toggle also moves
    the schedule: pausing clears ``next_check_at`` (the poller stops claiming
    it); resuming re-arms it to fire on the next tick.
    """
    updates: dict[str, Any] = {}
    if body.frequency_minutes is not None:
        updates["frequency_minutes"] = max(
            int(body.frequency_minutes), MIN_FREQUENCY_MINUTES
        )
    if body.diff_mode is not None:
        updates["diff_mode"] = body.diff_mode
    if body.track_fields is not None:
        updates["track_fields"] = body.track_fields
    if body.webhook_url is not None:
        # "" is the explicit clear signal (schema-normalized); store NULL.
        updates["webhook_url"] = body.webhook_url or None
    if body.notify_email is not None:
        updates["notify_email"] = body.notify_email
    if body.status is not None:
        updates["status"] = body.status
        updates["next_check_at"] = _utcnow() if body.status == "active" else None

    if not updates:
        return await asyncio.to_thread(
            watch_store.get_watcher_for_project, watcher_id, project_id
        )
    return await asyncio.to_thread(
        watch_store.apply_updates, watcher_id, project_id, updates
    )


def watcher_response(watcher: Watcher) -> WatcherResponse:
    """Full API view of one watcher."""
    return WatcherResponse(
        watcher_id=watcher.watcher_id,
        url=watcher.url,
        status=watcher.status,  # type: ignore[arg-type]
        frequency_minutes=watcher.frequency_minutes,
        diff_mode=watcher.diff_mode,  # type: ignore[arg-type]
        track_fields=watcher.track_fields,
        webhook_url=watcher.webhook_url,
        notify_email=watcher.notify_email,
        consecutive_errors=watcher.consecutive_errors,
        checks_count=watcher.checks_count,
        last_check_at=watcher.last_check_at,
        next_check_at=watcher.next_check_at,
        last_change_at=watcher.last_change_at,
        created_at=watcher.created_at,
        updated_at=watcher.updated_at,
    )


def watcher_summary(watcher: Watcher) -> WatcherSummary:
    """Compact view for the dashboard list (Task I.4)."""
    return WatcherSummary(
        watcher_id=watcher.watcher_id,
        url=watcher.url,
        status=watcher.status,  # type: ignore[arg-type]
        frequency_minutes=watcher.frequency_minutes,
        checks_count=watcher.checks_count,
        consecutive_errors=watcher.consecutive_errors,
        last_check_at=watcher.last_check_at,
        next_check_at=watcher.next_check_at,
        last_change_at=watcher.last_change_at,
        created_at=watcher.created_at,
    )


# ── Scheduled check (Task I.1) ───────────────────────────────────────────────


async def run_check(claimed: ClaimedWatcher) -> None:
    """Render a due watcher once, detect change, persist, reschedule.

    Never raises: a render (or capture/diff) failure routes to
    ``_handle_failure`` (which may auto-pause), and a persistence failure is
    logged — the worker loop must survive one bad watcher. The row was already
    provisionally rescheduled at claim time, so even an unexpected death here
    only skips one cycle.
    """
    try:
        # allow_tls=False: a watch check is a COMPARISON against a stored
        # baseline, so the capture method must not change underneath it. The
        # no-browser TLS rung returns raw un-hydrated HTML; letting a watcher
        # whose baseline was captured by Chromium silently flip to it would
        # drop text similarity under TEXT_SIMILARITY_THRESHOLD and fire a
        # change webhook + email for a page that never changed.
        page = await perceive_flow.render_html(
            str(claimed.url), allow_tls=False
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a bad render is a watcher error
        await _handle_failure(claimed, str(exc))
        return

    now = _utcnow()
    next_at = _compute_next_check_at(now, claimed.frequency_minutes)

    # Plan I.3 step 3: a blocked / low-quality render did not really capture the
    # monitored content. Record the check for audit (no content_hash, so it is
    # never picked as a baseline) and reschedule — but do not diff or notify.
    if page.is_blocked or page.render_quality < QUALITY_FLOOR:
        await _record_check(
            claimed, now, next_at,
            content_hash=None, snapshot_key=None, structured_data=None,
            render_quality=page.render_quality,
            diff=DiffResult(False, 1.0, ()),
        )
        return

    try:
        capture = await asyncio.to_thread(
            _build_capture,
            page.html or "",
            page.final_url or str(claimed.url),
            content_category=page.content_category,
        )
        content_hash = await asyncio.to_thread(_capture_hash, capture)
        diff = await _diff_against_baseline(claimed, capture)
        snapshot_key = await asyncio.to_thread(
            _upload_capture_body, capture.text, claimed.project_id,
            claimed.watcher_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a capture/diff failure is a check error
        logger.exception("watch capture/diff failed for %s", claimed.watcher_id)
        await _handle_failure(claimed, str(exc))
        return

    persisted = await _record_check(
        claimed, now, next_at,
        content_hash=content_hash, snapshot_key=snapshot_key,
        structured_data=capture.structured,
        render_quality=page.render_quality, diff=diff,
    )

    # Task I.4: when the page changed, fire the HMAC webhook + change email.
    if persisted and diff.has_changes:
        posthog_client.capture_project_event(
            claimed.project_id, "v2_watch_change_detected", {
                "watcher_id": claimed.watcher_id,
                "diff_mode": claimed.diff_mode,
                "change_count": len(diff.changes),
                "similarity": round(diff.similarity, 4),
                "render_quality_score": page.render_quality,
                "notification_channel": _claimed_notification_channel(claimed),
            },
        )
        await _notify_change(claimed, diff, now)


# ── Notifications (Task I.4) ─────────────────────────────────────────────────


async def _notify_change(
    claimed: ClaimedWatcher, diff: DiffResult, checked_at: datetime
) -> None:
    """Fire the change webhook + owner email concurrently; both best-effort.

    Each branch swallows its own failures, and ``return_exceptions=True`` means
    a crash in one never sinks the other (or the worker loop).
    """
    logger.info(
        "watch %s detected %d change(s) (similarity %.4f)",
        claimed.watcher_id, len(diff.changes), diff.similarity,
    )
    results = await asyncio.gather(
        _deliver_change_webhook(claimed, diff, checked_at),
        _email_owner_change(claimed, diff, checked_at),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result  # propagate shutdown; never swallow cancellation
        if isinstance(result, BaseException):
            logger.warning(
                "watch %s: a notify branch raised unexpectedly: %r",
                claimed.watcher_id, result,
            )


def _change_payload(
    claimed: ClaimedWatcher, diff: DiffResult, checked_at: datetime
) -> dict[str, Any]:
    """The change event shared by the webhook body and (loosely) the email."""
    return {
        "event": "change_detected",
        "watcher_id": claimed.watcher_id,
        "url": str(claimed.url),
        "checked_at": checked_at.isoformat(),
        "similarity": round(diff.similarity, 4),
        "change_count": len(diff.changes),
        "changes": diff.to_change_dicts(),
    }


async def _deliver_change_webhook(
    claimed: ClaimedWatcher, diff: DiffResult, checked_at: datetime
) -> None:
    """SSRF-screen, sign and POST the change webhook (reuses H.8's delivery).

    The stored URL was only scheme-checked at create time, so it is screened
    again here, immediately before the send (a private/metadata host is inert
    until we POST to it). Retries + back-off live in ``deliver_signed_webhook``;
    a dead endpoint is a logged non-delivery, never a worker failure.
    """
    url = claimed.webhook_url
    if not url:
        return
    try:
        await assert_public_http_url(url)
    except HTTPException:
        logger.warning(
            "watch %s: change webhook URL blocked by SSRF guard", claimed.watcher_id
        )
        return
    secret = await asyncio.to_thread(
        webhook_secret.get_or_create_webhook_secret, claimed.project_id
    )
    if not secret:
        logger.error(
            "watch %s: webhook secret unavailable for project %s",
            claimed.watcher_id, claimed.project_id,
        )
        return
    body = json.dumps(
        _change_payload(claimed, diff, checked_at),
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    try:
        result = await deliver_signed_webhook(url, body, secret)
    except Exception:  # noqa: BLE001 — delivery is best-effort
        logger.warning(
            "watch %s: change webhook delivery raised", claimed.watcher_id,
            exc_info=True,
        )
        return
    if not result.delivered:
        logger.warning(
            "watch %s: change webhook not delivered (%s)",
            claimed.watcher_id, result.error,
        )


async def _email_owner_change(
    claimed: ClaimedWatcher, diff: DiffResult, checked_at: datetime
) -> None:
    """Best-effort owner email on a detected change; never propagates."""
    if not claimed.notify_email:
        return
    try:
        email = await asyncio.to_thread(
            get_project_owner_email, claimed.project_id
        )
        if not email:
            return
        await asyncio.to_thread(
            send_watcher_change_email,
            email,
            claimed.watcher_id,
            str(claimed.url),
            diff.similarity,
            diff.to_change_dicts(),
            checked_at.isoformat(),
        )
    except Exception:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "watch: change-email delivery failed for %s",
            claimed.watcher_id, exc_info=True,
        )


def snapshot_response(snapshot: WatcherSnapshot) -> WatcherSnapshotResponse:
    """Dashboard view of one snapshot (timeline + structured diff, Task I.4)."""
    changes = snapshot.changes or []
    return WatcherSnapshotResponse(
        checked_at=snapshot.created_at,
        has_changes=snapshot.has_changes,
        similarity=snapshot.similarity,
        render_quality=snapshot.render_quality_score,
        change_count=len(changes),
        changes=changes,
    )


async def _record_check(
    claimed: ClaimedWatcher,
    now: datetime,
    next_at: datetime,
    *,
    content_hash: Optional[str],
    snapshot_key: Optional[str],
    structured_data: Optional[dict[str, Any]],
    render_quality: Optional[float],
    diff: DiffResult,
) -> bool:
    """Persist the snapshot + reschedule. Returns False (logged) on DB error."""
    try:
        await asyncio.to_thread(
            watch_store.apply_successful_check,
            watcher_id=claimed.watcher_id,
            project_id=claimed.project_id,
            now=now,
            content_hash=content_hash,
            snapshot_key=snapshot_key,
            structured_data=structured_data,
            render_quality_score=render_quality,
            has_changes=diff.has_changes,
            similarity=diff.similarity,
            changes=diff.to_change_dicts() if diff.changes else None,
            next_check_at=next_at,
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never let one watcher kill the loop
        logger.exception(
            "watch check persistence failed for %s", claimed.watcher_id
        )
        return False


async def _diff_against_baseline(
    claimed: ClaimedWatcher, capture: Capture
) -> DiffResult:
    """Diff the fresh capture against the latest baseline (content) snapshot.

    The first ever check (or the first after only audit/low-quality rows) has no
    baseline: it establishes one and reports no change.
    """
    prior = await asyncio.to_thread(
        watch_store.latest_content_snapshot, claimed.watcher_id
    )
    if prior is None:
        return DiffResult(False, 1.0, ())
    prior_capture = await asyncio.to_thread(
        _load_prior_capture, prior, claimed.project_id
    )
    # diff_captures is pure but CPU-heavy (SequenceMatcher over the full
    # capture); offload it so a large page never blocks the event loop.
    return await asyncio.to_thread(
        diff_captures,
        prior_capture,
        capture,
        mode=claimed.diff_mode or "auto",
        track_fields=_track_terms(claimed.track_fields),
    )


def _build_capture(
    html: str, final_url: str, *, content_category: Optional[str] = None
) -> Capture:
    """Extract the diffable capture (main-content text + structure) from a
    render. CPU-bound (bs4 parse) — the caller offloads it to a worker thread,
    mirroring perceive_flow._process_outputs.

    ``content_category`` is set when the watched URL answers with a non-HTML
    body (``text/plain``, ``application/json``); ``html`` is then that body
    VERBATIM. Running the HTML pipeline over it would let a bare ``<`` in the
    payload swallow everything after it, so the whole watched document is
    diffed as text and no DOM structure is claimed for it. A JSON API
    endpoint is exactly the kind of URL people watch, so this path has to be
    lossless."""
    if content_category is not None:
        text = html
        if len(text) > MAX_CAPTURE_TEXT_CHARS:
            text = text[:MAX_CAPTURE_TEXT_CHARS]
        return Capture(
            text=text,
            structured={
                "metadata": {},
                "tables": [],
                "links": {"internal": [], "external": []},
                "structured_data": [],
            },
        )

    scraping = scrap_html(final_url, html)
    try:
        text = generate_fit_markdown(html, final_url).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — main-content extraction is best-effort
        text = ""
    if len(text) > MAX_CAPTURE_TEXT_CHARS:
        text = text[:MAX_CAPTURE_TEXT_CHARS]
    structured: dict[str, Any] = {
        "metadata": dict(scraping.metadata or {}),
        "tables": serialize_tables(scraping),
        "links": serialize_links(scraping),
        "structured_data": extract_json_ld(html),
    }
    return Capture(text=text, structured=structured)


def _capture_hash(capture: Capture) -> str:
    """Stable content hash over the whole capture (text + structure)."""
    canonical = json.dumps(
        {"text": capture.text, "structured": capture.structured},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_prior_capture(snapshot: WatcherSnapshot, project_id: int) -> Capture:
    """Rebuild the baseline capture: text body from Spaces + structure from the
    row. A missing/unreadable body degrades to empty text (structure still
    diffs); the diff simply reports the text as changed if it later returns."""
    text = ""
    key = snapshot.snapshot_key
    # Defence in depth: we only ever write keys under this project's
    # watch-snapshots namespace ({prefix}/files/{project_id}/v2-watch-snapshots/).
    # Refuse a key (a corrupt/foreign DB value) that is not in it — this anchors
    # the segment AND enforces cross-project isolation at the read layer.
    expected = f"/files/{project_id}/{WATCH_SNAPSHOT_ENDPOINT}/"
    if key and expected not in key:
        logger.warning("watch: refusing to load out-of-namespace snapshot key %s", key)
        key = None
    if key:
        try:
            text = download_from_storage(key).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — a stale body must not fail the check
            logger.warning(
                "watch: could not load prior snapshot body %s", key, exc_info=True
            )
    return Capture(text=text, structured=snapshot.structured_data or {})


def _upload_capture_body(text: str, project_id: int, watcher_id: str) -> str:
    """Persist the capture's text body to Spaces; returns its object key."""
    filename = f"{watcher_id}_{uuid.uuid4().hex}.txt"
    result = upload_to_gcs(
        text.encode("utf-8"), str(project_id), WATCH_SNAPSHOT_ENDPOINT, filename
    )
    return result["object_key"]


def _track_terms(track_fields: Any) -> Optional[list[str]]:
    """Coerce the watcher's stored ``track_fields`` (a JSONB dict or list) into
    the flat list of terms the diff engine filters on. A dict contributes its
    keys plus any list values (so ``{"metadata": ["title"]}`` tracks both
    ``metadata`` and ``title``); a bare list is used as-is."""
    if not track_fields:
        return None
    if isinstance(track_fields, list):
        terms = [str(term).strip() for term in track_fields if str(term).strip()]
        return terms or None
    if isinstance(track_fields, dict):
        terms: list[str] = []
        for key, value in track_fields.items():
            if str(key).strip():
                terms.append(str(key).strip())
            if isinstance(value, list):
                terms.extend(
                    str(item).strip() for item in value if str(item).strip()
                )
        return terms or None
    return None


async def _handle_failure(claimed: ClaimedWatcher, error: str) -> None:
    """Count a failed check; pause + email after three in a row.

    ``claimed.consecutive_errors`` is the value at claim time. Under the
    at-most-once claim semantics (a row is claimed only when due, and a failed
    check advances next_check_at AFTER the claim) it equals the live DB value,
    so ``new_errors`` is exact. If the poller is ever parallelized, re-read the
    count inside ``apply_failed_check`` instead of trusting the snapshot.
    """
    now = _utcnow()
    new_errors = claimed.consecutive_errors + 1
    pause = new_errors >= MAX_CONSECUTIVE_ERRORS
    next_at = (
        None if pause else _compute_next_check_at(now, claimed.frequency_minutes)
    )
    try:
        paused = await asyncio.to_thread(
            watch_store.apply_failed_check,
            watcher_id=claimed.watcher_id,
            project_id=claimed.project_id,
            now=now,
            new_consecutive_errors=new_errors,
            pause=pause,
            next_check_at=next_at,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception(
            "watch failure bookkeeping failed for %s", claimed.watcher_id
        )
        return

    logger.warning(
        "watch check failed for %s (errors=%d, paused=%s): %s",
        claimed.watcher_id,
        new_errors,
        paused,
        error,
    )
    if paused:
        posthog_client.capture_project_event(
            claimed.project_id, "v2_watch_paused", {
                "watcher_id": claimed.watcher_id,
                "consecutive_errors": new_errors,
                "frequency_minutes": claimed.frequency_minutes,
                "notification_channel": _claimed_notification_channel(claimed),
            },
        )
        await _email_owner_paused(claimed, error)


async def _email_owner_paused(claimed: ClaimedWatcher, error: str) -> None:
    """Best-effort owner email on auto-pause; never propagates."""
    if not claimed.notify_email:
        return
    try:
        email = await asyncio.to_thread(
            get_project_owner_email, claimed.project_id
        )
        if not email:
            return
        await asyncio.to_thread(
            send_watcher_paused_email,
            email,
            claimed.watcher_id,
            str(claimed.url),
            error,
        )
    except Exception:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "watch: paused-email delivery failed for %s",
            claimed.watcher_id,
            exc_info=True,
        )
