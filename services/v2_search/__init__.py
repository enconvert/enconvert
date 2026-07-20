"""Provider-neutral web search for /v2/lookup (Task H.3).

``adapter`` defines the neutral contract (SearchResults / SearchResult +
the SearchAdapter ABC and error taxonomy); ``serper`` is the concrete
serper.dev implementation. Everything above this package speaks only the
neutral vocabulary so a future Brave/Tavily swap is one new module.
"""
