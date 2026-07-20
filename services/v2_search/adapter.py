"""Neutral web-search adapter interface (Task H.3).

``/v2/lookup`` is provider-agnostic by design: Serper backs it today, but
the plan calls for a clean swap path to Brave / Tavily / SerpAPI later.
Everything above this module (the lookup flow, the handler, the API
schema) speaks ONLY this neutral vocabulary -- ``SearchResults`` of
``SearchResult`` -- so swapping providers is a one-file change
(``serper.py`` -> ``brave.py``) with no ripple into the flow or the API
contract.

Pricing lives with the adapter: each ``SearchResults`` carries the
``cost_cents`` the provider charged for that call, because only the
provider knows its own price list -- the flow just records what it is
told.

Error taxonomy (the flow maps each to an HTTP status; raw text never
reaches the client):

* ``SearchConfigError``      -> the server is misconfigured (missing key)
                                -- a 5xx that is OUR fault (-> 503).
* ``SearchUnavailableError`` -> provider tripped our circuit breaker or
                                rate-limited us; retry later (-> 503).
* ``SearchUpstreamError``    -> provider returned an error response or a
                                non-retryable transport fault (-> 502).
"""

from __future__ import annotations

import abc
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# Categories the plan (section 8, Task H.3) exposes through /v2/lookup.
SearchCategory = Literal["web", "news", "images", "scholar", "patents", "maps"]

# Neutral recency filter; the adapter maps this to the provider's syntax.
TimeFilter = Literal["hour", "day", "week", "month", "year"]


class SearchError(Exception):
    """Base class for every search-provider failure."""


class SearchConfigError(SearchError):
    """The provider cannot be called because the server is misconfigured
    (e.g. no API key in the environment). A failure that is OUR fault."""


class SearchUnavailableError(SearchError):
    """The provider is temporarily unreachable: the circuit breaker is
    open, or the provider rate-limited us (HTTP 429). The caller should
    retry later."""


class SearchUpstreamError(SearchError):
    """The provider returned an error response, or the request failed at
    the transport layer in a non-retryable way."""


class SearchResult(BaseModel):
    """One provider-neutral search hit.

    Only the universal fields are typed; anything category-specific
    (ratings, coordinates, citation counts, ...) lands in ``extra`` so
    the neutral contract never grows a column per provider quirk.
    """

    title: Optional[str] = None
    url: Optional[str] = Field(
        default=None,
        description="Canonical page link for the hit (the thing you would "
        "perceive). None for results with no navigable URL.",
    )
    snippet: Optional[str] = None
    position: Optional[int] = None
    source: Optional[str] = None
    date: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider/category-specific fields not in the neutral "
        "set (e.g. rating, address, latitude, citedBy).",
    )


class SearchResults(BaseModel):
    """Everything one search call returned, provider-neutral."""

    provider: str
    category: SearchCategory
    query: str
    results: list[SearchResult] = Field(default_factory=list)
    total: int = 0
    credits: Optional[int] = Field(
        default=None, description="Provider credits consumed by this call."
    )
    cost_cents: Decimal = Field(
        default=Decimal("0"),
        description="Monetary cost of this call, in cents, per the "
        "provider's price list.",
    )
    answer_box: Optional[dict[str, Any]] = None
    knowledge_graph: Optional[dict[str, Any]] = None
    warnings: list[str] = Field(default_factory=list)


class SearchAdapter(abc.ABC):
    """The contract every search provider implements."""

    name: str = "search"

    @abc.abstractmethod
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
        """Run one search and return provider-neutral results.

        Implementations raise the ``SearchError`` subclasses above for
        failures; they never return a partial/empty result to signal an
        error (an empty ``results`` list means the query genuinely had no
        hits).
        """
        raise NotImplementedError
