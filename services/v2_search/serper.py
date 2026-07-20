"""serper.dev implementation of the neutral SearchAdapter (Task H.3).

Serper is a thin Google-SERP proxy: one POST per category endpoint, auth
via the ``X-API-KEY`` header, JSON in / JSON out. This module:

* reads ``SERPER_API_KEY`` from the environment AT CALL TIME (never at
  import) so a missing key is a clean runtime 503 instead of a boot
  crash, and so tests can inject a key per case;
* routes each ``SearchCategory`` to its Serper endpoint and maps the
  neutral ``time_filter`` onto Google's ``tbs=qdr:*`` syntax;
* wraps the call in the shared process-wide circuit breaker
  (``middleware.circuit_breaker``) plus a bounded retry/backoff loop for
  transient faults (timeouts, 5xx, 429);
* normalises each category's idiosyncratic JSON into neutral
  ``SearchResult`` objects.

Cost: the plan (section 8) pins Serper at $0.0006/query == 0.06 cents
flat (``SERPER_COST_CENTS``). Serper actually bills 2 credits for some
verticals (scholar/patents), but the plan models a flat per-query cost;
revisit if billing ever needs per-credit accuracy.

Security: the API key is only ever sent in the request header and is
never logged. Transport/HTTP errors are logged without the key or the
response body.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any, Optional

import httpx

from middleware.circuit_breaker import circuit_breaker
from services.v2_search.adapter import (
    SearchAdapter,
    SearchCategory,
    SearchConfigError,
    SearchResult,
    SearchResults,
    SearchUnavailableError,
    SearchUpstreamError,
    TimeFilter,
)

logger = logging.getLogger(__name__)

_SERVICE_NAME = "serper"
_BASE_URL = "https://google.serper.dev"

# Category -> Serper endpoint path.
_ENDPOINTS: dict[str, str] = {
    "web": "/search",
    "news": "/news",
    "images": "/images",
    "scholar": "/scholar",
    "patents": "/patents",
    "maps": "/maps",
}

# Neutral recency filter -> Google ``tbs`` "query date range" value.
_TBS: dict[str, str] = {
    "hour": "qdr:h",
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}

# Plan section 8 (Task H.3): $0.0006 per query == 0.06 cents.
SERPER_COST_CENTS = Decimal("0.06")

_TIMEOUT_S = 15.0
_MAX_ATTEMPTS = 3
# Sleeps between attempts: index 0 is after the 1st failure, index 1 the
# 2nd. len == _MAX_ATTEMPTS - 1.
_BACKOFF_SCHEDULE_S: tuple[float, ...] = (0.5, 1.0)
# Statuses worth retrying (transient on the provider's side).
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Statuses that mean "our credential is wrong" -- not retryable, and not a
# reason to trip the breaker for every other request.
_AUTH_STATUS = frozenset({401, 403})


def _pruned(item: dict[str, Any], known: set[str]) -> dict[str, Any]:
    """Return the item's fields that are not already mapped to a neutral
    column, so nothing Serper sends is silently discarded."""
    return {key: value for key, value in item.items() if key not in known}


class SerperAdapter(SearchAdapter):
    """SearchAdapter backed by serper.dev."""

    name = "serper"

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Explicit injection is for tests; production reads the env at call
        # time so the key can rotate without a redeploy of this object.
        self._explicit_key = api_key

    def _api_key(self) -> str:
        key = self._explicit_key or os.environ.get("SERPER_API_KEY")
        if not key:
            raise SearchConfigError("SERPER_API_KEY is not configured")
        return key

    async def search(
        self,
        *,
        query: str,
        category: SearchCategory = "web",
        country: Optional[str] = None,
        locale: Optional[str] = None,
        time_filter: Optional[TimeFilter] = None,
        num: int = 10,
        page: int = 1,
        location: Optional[str] = None,
        autocorrect: bool = True,
    ) -> SearchResults:
        api_key = self._api_key()
        endpoint = _ENDPOINTS.get(category)
        if endpoint is None:
            # The schema constrains category; this is defence for direct callers.
            raise SearchUpstreamError(f"unsupported search category: {category!r}")

        payload: dict[str, Any] = {
            "q": query,
            "num": num,
            "page": page,
            "autocorrect": autocorrect,
        }
        if country:
            payload["gl"] = country
        if locale:
            payload["hl"] = locale
        if location:
            payload["location"] = location
        if time_filter:
            payload["tbs"] = _TBS[time_filter]

        data = await self._post(endpoint, payload, api_key)
        return self._normalize(category, query, data)

    async def _post(
        self, endpoint: str, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        """POST with bounded retry/backoff; translate faults to SearchError.

        One ``AsyncClient`` spans every retry so the connection pool is
        reused across attempts. The circuit breaker is re-checked at the
        top of each attempt -- if a concurrent caller trips it mid-retry,
        "open -> abort" holds for this in-flight request too, not just for
        new callers. Records exactly one breaker outcome per logical call:
        a success on the first 200, a single failure when it gives up.
        """
        url = f"{_BASE_URL}{endpoint}"
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        last_error: Optional[Exception] = None

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for attempt in range(_MAX_ATTEMPTS):
                if circuit_breaker.is_open(_SERVICE_NAME):
                    raise SearchUnavailableError("search provider circuit is open")
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    logger.warning(
                        "serper transport error (attempt %d/%d): %s",
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        exc.__class__.__name__,
                    )
                else:
                    status = response.status_code
                    if status == 200:
                        circuit_breaker.record_success(_SERVICE_NAME)
                        try:
                            return response.json()
                        except ValueError as exc:
                            circuit_breaker.record_failure(_SERVICE_NAME)
                            raise SearchUpstreamError(
                                "serper returned a non-JSON body"
                            ) from exc
                    if status in _AUTH_STATUS:
                        # Bad/expired key: our config problem, not a transient
                        # outage. Do not retry and do not trip the breaker.
                        raise SearchConfigError(
                            f"serper rejected the API key (HTTP {status})"
                        )
                    if status not in _RETRY_STATUS:
                        circuit_breaker.record_failure(_SERVICE_NAME)
                        raise SearchUpstreamError(f"serper returned HTTP {status}")
                    # Retryable status: remember it and fall through to backoff.
                    last_error = SearchUpstreamError(
                        f"serper returned HTTP {status}"
                    )
                    logger.warning(
                        "serper retryable status %d (attempt %d/%d)",
                        status,
                        attempt + 1,
                        _MAX_ATTEMPTS,
                    )

                if attempt < len(_BACKOFF_SCHEDULE_S):
                    await asyncio.sleep(_BACKOFF_SCHEDULE_S[attempt])

        circuit_breaker.record_failure(_SERVICE_NAME)
        raise SearchUnavailableError(
            f"serper unavailable after {_MAX_ATTEMPTS} attempts"
        ) from last_error

    def _normalize(
        self, category: SearchCategory, query: str, data: dict[str, Any]
    ) -> SearchResults:
        """Map a raw Serper response onto the neutral result shape."""
        if not isinstance(data, dict):
            raise SearchUpstreamError("serper returned an unexpected payload")

        if category == "images":
            results = [self._image_result(item) for item in data.get("images", [])]
        elif category == "news":
            results = [self._news_result(item) for item in data.get("news", [])]
        elif category == "maps":
            results = [self._place_result(item) for item in data.get("places", [])]
        else:
            # web / scholar / patents all return an ``organic`` array.
            results = [self._organic_result(item) for item in data.get("organic", [])]

        credits = data.get("credits")
        answer_box = data.get("answerBox")
        knowledge_graph = data.get("knowledgeGraph")
        return SearchResults(
            provider=self.name,
            category=category,
            query=query,
            results=results,
            total=len(results),
            credits=credits if isinstance(credits, int) else None,
            cost_cents=SERPER_COST_CENTS,
            answer_box=answer_box if isinstance(answer_box, dict) else None,
            knowledge_graph=knowledge_graph
            if isinstance(knowledge_graph, dict)
            else None,
        )

    @staticmethod
    def _organic_result(item: dict[str, Any]) -> SearchResult:
        return SearchResult(
            title=item.get("title"),
            url=item.get("link"),
            snippet=item.get("snippet"),
            date=item.get("date"),
            position=item.get("position"),
            extra=_pruned(item, {"title", "link", "snippet", "date", "position"}),
        )

    @staticmethod
    def _news_result(item: dict[str, Any]) -> SearchResult:
        return SearchResult(
            title=item.get("title"),
            url=item.get("link"),
            snippet=item.get("snippet"),
            date=item.get("date"),
            source=item.get("source"),
            image_url=item.get("imageUrl"),
            position=item.get("position"),
            extra=_pruned(
                item,
                {"title", "link", "snippet", "date", "source", "imageUrl", "position"},
            ),
        )

    @staticmethod
    def _image_result(item: dict[str, Any]) -> SearchResult:
        return SearchResult(
            title=item.get("title"),
            url=item.get("link"),
            image_url=item.get("imageUrl"),
            thumbnail_url=item.get("thumbnailUrl"),
            source=item.get("source"),
            position=item.get("position"),
            extra=_pruned(
                item,
                {"title", "link", "imageUrl", "thumbnailUrl", "source", "position"},
            ),
        )

    @staticmethod
    def _place_result(item: dict[str, Any]) -> SearchResult:
        return SearchResult(
            title=item.get("title"),
            url=item.get("website"),
            snippet=item.get("address"),
            position=item.get("position"),
            extra=_pruned(item, {"title", "website", "address", "position"}),
        )
