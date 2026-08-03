"""Request timeout middleware (raw ASGI).

Rewritten from ``BaseHTTPMiddleware`` after the 2026-07-17 wedge incident:
``asyncio.wait_for(call_next(request))`` timed out the HTTP response but
never cancelled the underlying request task — a hung render kept holding
the semaphore=1 browser slot for ~2.9 days (PostHog: perceive operations
completing with ``duration_seconds`` ≈ 254,000) and every conversion behind
it 504'd. As a raw ASGI middleware the timeout now cancels the actual
request task, so cancellation propagates into the endpoint and releases any
held conversion slot on the way out (``crawler_slot`` releases in
``finally``).

Semantics preserved from the old middleware:

* The budget bounds time-to-response-start, not body streaming — once the
  app has sent ``http.response.start`` the timer is disarmed (the download
  proxy may legitimately stream past the ceiling to a slow client, exactly
  as ``call_next`` returning at response start allowed before).
* On timeout the stuck conversion's Activity row is transitioned to Failed
  (BUG FIX A) and a ``conversion_timed_out`` PostHog event is captured.
"""
import asyncio
import logging
from typing import Any, Callable

from fastapi.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger(__name__)


class TimeoutMiddleware:
    def __init__(self, app: Callable[..., Any], timeout: float = 300.0):
        self.app = app
        self.timeout = timeout

    async def __call__(
        self, scope: dict, receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = asyncio.Event()

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_started.set()
            await send(message)

        app_task = asyncio.ensure_future(self.app(scope, receive, send_wrapper))
        started_task = asyncio.ensure_future(response_started.wait())
        try:
            done, _ = await asyncio.wait(
                {app_task, started_task},
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if app_task in done or response_started.is_set():
                # Finished within budget, or the response already started
                # streaming (the timer only bounds time-to-first-byte).
                # ``await`` re-raises app exceptions so the server's error
                # handling stays exactly as it was with BaseHTTPMiddleware.
                await app_task
                return

            # Timed out with nothing sent: cancel the request task FOR REAL
            # so a hung render is torn down and its conversion slot released,
            # then answer 504 ourselves.
            app_task.cancel()
            try:
                await app_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — already timing out; log only
                logger.warning(
                    "request task raised during timeout cancellation",
                    exc_info=True,
                )
            await self._fail_timed_out_activity(Request(scope))
            response = JSONResponse(
                status_code=504, content={"error": "Request timeout"}
            )
            await response(scope, receive, send)
        finally:
            started_task.cancel()
            if not app_task.done():
                # Outer cancellation (client disconnect / server shutdown)
                # must not leave the request task running detached.
                app_task.cancel()

    async def _fail_timed_out_activity(self, request: Request) -> None:
        # BUG FIX A: a timed-out conversion had its Activity row left
        # 'In Progress' forever because the inner request task is cancelled
        # before its own except/finally can run. The conversion handlers
        # stash the activity_id on request.state (shared with the endpoint
        # through the ASGI scope) right after log_activity_start, so we can
        # transition exactly that row to Failed here.
        elapsed_ms = int(self.timeout * 1000)
        activity_id = getattr(request.state, "activity_id", None)
        if activity_id is not None:
            try:
                from monitoring.metrics import update_activity_status
                from utils.error_capture import error_fields
                endpoint = getattr(request.state, "endpoint", None) or request.url.path
                fallback_message = (
                    f"Request exceeded the {self.timeout:g}s gateway timeout "
                    f"({endpoint})"
                )
                await update_activity_status(
                    activity_id, "Failed", duration=self.timeout,
                    **error_fields(
                        None,
                        fallback_message=fallback_message,
                        fallback_type="TimeoutError",
                    ),
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not mask the 504
                logger.warning(
                    "timeout sweep: failed to mark activity %s Failed",
                    activity_id, exc_info=True,
                )

        try:
            from monitoring import posthog_client
            project_id = getattr(request.state, "project_id", None)
            distinct_id = (
                posthog_client.distinct_id_for_project(project_id)
                if project_id is not None else "anonymous"
            )
            posthog_client.capture(
                distinct_id,
                "conversion_timed_out",
                {
                    "path": request.url.path,
                    "endpoint": getattr(request.state, "endpoint", None),
                    "elapsed_ms": elapsed_ms,
                    "project_id": project_id,
                    "activity_id": activity_id,
                },
                posthog_client.group_of(project_id) if project_id is not None else None,
            )
        except Exception:  # noqa: BLE001
            logger.debug("timeout event capture failed", exc_info=True)
