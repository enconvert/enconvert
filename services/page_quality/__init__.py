"""Open-source fallback for ``services.page_quality``.

The private/cloud build ships a full page-quality toolkit here (cookie-banner
handling, modal dismissal, viewport normalisation, render hooks). The open
self-hosted build only needs the two names the open code imports from the
package: the capture opt-out header check and the per-render instrumentation
shim.
"""

from .instrumentation import PageInstrumentation, header_opts_out

__all__ = [
    "PageInstrumentation",
    "header_opts_out",
]
