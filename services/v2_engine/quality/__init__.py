"""Open-source fallback for ``services.v2_engine.quality``.

Re-exports the naive render-quality scorer so call sites keep importing
``RenderQuality`` and ``score`` from the package root, matching the cloud
build.

``QUALITY_FLOOR`` must stay exported here even though the naive scorer
never produces a sub-floor score on its own: ``distill_flow`` is OPEN
code that ships to the mirror and imports it from this package root, so
dropping it breaks the public build's import chain (main -> api.v2.router
-> handlers.distill -> distill_flow). Keep this module's public surface a
superset of what open code imports.
"""

from services.v2_engine.quality.scorer import RenderQuality, score

# Mirrors the cloud build's constant so open call sites gate identically.
QUALITY_FLOOR: float = 0.40

__all__ = ["RenderQuality", "QUALITY_FLOOR", "score"]
