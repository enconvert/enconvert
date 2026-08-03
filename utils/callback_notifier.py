"""
Optional customer callback webhook utility for async job completion.
Sends HTTP POST notifications to customer-provided endpoints.

Two delivery surfaces live here:

* ``send_callback_notification`` — the original V1 async-conversion callback
  (``utils/processor.py``). Unsigned, unchanged, frozen public behavior.
* HMAC-signed V2 delivery (Task H.8) — ``sign_payload`` / ``verify_signature``
  / ``deliver_signed_webhook`` plus the ``X-Enconvert-Signature`` /
  ``X-Enconvert-Timestamp`` header pair. Used by ``/v2/ingest`` completion
  (and, from Sprint I.4, ``/v2/watch``). The timestamp is bound INTO the MAC,
  so a replayed delivery can be rejected by a consumer that enforces a freshness
  window.

This module is intentionally transport-only: it never resolves DNS or screens
URLs. Callers MUST SSRF-screen a customer-supplied URL
(``services.v2_engine.url_safety.assert_public_http_url``) at delivery time
before handing it here.
"""
import asyncio
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

# ── HMAC webhook signing (Task H.8) ──────────────────────────────────────────

#: Header carrying the hex HMAC, prefixed with the scheme: ``sha256=<hex>``.
SIGNATURE_HEADER = "X-Enconvert-Signature"
#: Header carrying the unix-seconds timestamp that is bound into the MAC.
TIMESTAMP_HEADER = "X-Enconvert-Timestamp"
#: Scheme label prefixing the signature value (future-proofs the algorithm).
SIGNATURE_SCHEME = "sha256"
#: Default consumer freshness window — a timestamp older/newer than this many
#: seconds is a replay (or a badly skewed clock) and should be rejected.
DEFAULT_REPLAY_TOLERANCE_SECONDS = 300
#: Back-off delays (seconds) applied BEFORE each retry. Three entries == three
#: retries after the initial attempt (4 deliveries worst case: 0, 1, 4, 16).
#: Each attempt is re-signed with a fresh timestamp so a slow retry chain never
#: drifts past the consumer's replay window.
WEBHOOK_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0, 16.0)


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Outcome of a (possibly retried) signed webhook delivery."""

    delivered: bool
    status_code: Optional[int]
    attempts: int
    error: Optional[str]


def _signed_payload(timestamp: str, body: bytes) -> bytes:
    """Canonical signing input: ``<timestamp>.<raw body>``.

    Binding the timestamp into the MAC (Stripe's scheme) is what makes the
    separate ``X-Enconvert-Timestamp`` header tamper-evident — a consumer that
    rejects stale timestamps gets replay protection for free, because an
    attacker cannot move the timestamp without invalidating the signature.
    """
    return timestamp.encode("utf-8") + b"." + body


def sign_payload(body: bytes, secret: str, timestamp: str) -> str:
    """Return the hex HMAC-SHA256 of ``<timestamp>.<body>`` under ``secret``.

    Uses the optimized one-shot ``hmac.digest`` (C path) — faster than building
    an HMAC object for these small payloads, same result.
    """
    return hmac.digest(
        secret.encode("utf-8"), _signed_payload(timestamp, body), "sha256"
    ).hex()


def build_signature_headers(
    body: bytes, secret: str, *, timestamp: Optional[int] = None
) -> Dict[str, str]:
    """Build the ``X-EnConvert-{Signature,Timestamp}`` header pair for ``body``.

    ``timestamp`` defaults to the current unix second; it is injectable so a
    delivery's signature can be reproduced deterministically in tests.
    """
    ts = str(int(timestamp if timestamp is not None else time.time()))
    signature = sign_payload(body, secret, ts)
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: f"{SIGNATURE_SCHEME}={signature}",
    }


def verify_signature(
    body: bytes,
    secret: str,
    signature_header: Optional[str],
    timestamp_header: Optional[str],
    *,
    tolerance_seconds: Optional[int] = DEFAULT_REPLAY_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Constant-time verification of an inbound signed webhook.

    This is the reference a customer consumer (or a test) runs against the raw
    request body and the two headers we send. Returns False — never raises — on
    any malformed input, a stale/early timestamp (replay), or a signature
    mismatch. The comparison uses ``hmac.compare_digest`` so it does not leak
    the correct signature through timing.

    ``tolerance_seconds=None`` disables the freshness check (signature only).
    """
    if not signature_header or not timestamp_header:
        return False

    try:
        ts_int = int(timestamp_header)
    except (TypeError, ValueError):
        return False

    if tolerance_seconds is not None:
        current = now if now is not None else time.time()
        if abs(current - ts_int) > tolerance_seconds:
            return False

    provided = signature_header.strip()
    scheme_prefix = f"{SIGNATURE_SCHEME}="
    if provided.startswith(scheme_prefix):
        provided = provided[len(scheme_prefix):]

    expected = sign_payload(body, secret, str(ts_int))
    return hmac.compare_digest(expected, provided)


async def _post_once(
    client: Optional[httpx.AsyncClient],
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> httpx.Response:
    """POST the exact signed bytes once. Reuses ``client`` when provided (tests
    inject a mock transport), else opens a short-lived client."""
    if client is not None:
        return await client.post(url, content=body, headers=dict(headers))
    async with httpx.AsyncClient(timeout=timeout) as owned:
        return await owned.post(url, content=body, headers=dict(headers))


async def deliver_signed_webhook(
    url: str,
    body: bytes,
    secret: str,
    *,
    extra_headers: Optional[Mapping[str, str]] = None,
    backoff: tuple[float, ...] = WEBHOOK_RETRY_BACKOFF_SECONDS,
    timeout: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    client: Optional[httpx.AsyncClient] = None,
) -> WebhookDeliveryResult:
    """POST ``body`` to ``url`` HMAC-signed, retrying on failure with back-off.

    A 2xx response is success. Each attempt is re-signed with a fresh timestamp
    (so the consumer's replay window is measured from the actual send, not the
    first try). Network errors, timeouts and non-2xx responses are retried up to
    ``len(backoff)`` times; the final outcome is returned, never raised, so the
    durable worker treats a dead endpoint as a recorded non-delivery rather than
    a job failure.

    SECURITY: ``url`` MUST already be SSRF-screened by the caller (delivery-time
    ``assert_public_http_url``); this function does not resolve or validate it.
    ``secret`` MUST be non-empty.
    """
    if not secret:
        return WebhookDeliveryResult(False, None, 0, "no_secret")

    delays = (0.0, *backoff)  # first attempt fires immediately
    attempts = 0
    last_status: Optional[int] = None
    last_error: Optional[str] = None

    for delay in delays:
        if delay:
            await sleep(delay)
        attempts += 1

        headers: Dict[str, str] = build_signature_headers(body, secret)
        headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = await _post_once(client, url, body, headers, timeout)
        except httpx.TimeoutException:
            last_status, last_error = None, "timeout"
            continue
        except httpx.RequestError as exc:
            last_status, last_error = None, f"request_error: {exc.__class__.__name__}"
            continue

        last_status = response.status_code
        if 200 <= response.status_code < 300:
            return WebhookDeliveryResult(True, response.status_code, attempts, None)
        last_error = f"http_{response.status_code}"

    return WebhookDeliveryResult(False, last_status, attempts, last_error)


async def send_callback_notification(
    callback_url: str,
    job_id: str,
    job_status: str,
    batch_id: Optional[str] = None,
    gcs_uri: Optional[str] = None,
    filename: Optional[str] = None,
    file_size: Optional[int] = None,
    tasks: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Send callback notification to customer's endpoint when async job completes.

    Args:
        callback_url: Customer's webhook URL to POST to
        job_id: Unique job/activity ID
        job_status: Overall job status ("success" or "failed")
        batch_id: Optional batch ID for grouped tasks
        gcs_uri: Optional GCS URI of output file
        filename: Optional output filename
        file_size: Optional output file size in bytes
        tasks: Optional list of individual task results for bulk jobs
        metadata: Optional additional metadata to include

    Returns:
        True if callback sent successfully, False otherwise
    """
    if not callback_url:
        logger.warning("No callback URL provided, skipping callback notification")
        return False

    try:
        # Build callback payload
        payload = {
            "job_id": job_id,
            "status": job_status,
        }

        # Add optional fields if present
        if batch_id:
            payload["batch_id"] = batch_id

        if gcs_uri:
            payload["gcs_uri"] = gcs_uri

        if filename:
            payload["filename"] = filename

        if file_size is not None:
            payload["file_size"] = file_size

        # Add bulk task summary if present
        if tasks:
            success_count = sum(1 for t in tasks if t.get("status") == "success")
            failed_count = len(tasks) - success_count

            payload["total_tasks"] = len(tasks)
            payload["successful_tasks"] = success_count
            payload["failed_tasks"] = failed_count
            payload["tasks"] = tasks

        # Add custom metadata
        if metadata:
            payload["metadata"] = metadata

        # Send POST request to customer's callback URL
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                callback_url,
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            # Log response status
            if response.status_code in [200, 201, 202, 204]:
                logger.info(
                    f"Callback notification sent successfully to {callback_url} "
                    f"(status: {response.status_code})"
                )
                return True
            else:
                logger.warning(
                    f"Callback notification received non-success status {response.status_code} "
                    f"from {callback_url}"
                )
                return False

    except httpx.TimeoutException:
        logger.error(f"Callback notification timed out for {callback_url}")
        return False
    except httpx.RequestError as e:
        logger.error(f"Callback notification failed for {callback_url}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending callback notification: {e}")
        return False
