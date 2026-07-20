"""Open-source fallback for ``services.v2_engine.quality``.

Re-exports the naive render-quality scorer so call sites keep importing
``RenderQuality`` and ``score`` from the package root, matching the cloud
build.
"""

from services.v2_engine.quality.scorer import RenderQuality, score

__all__ = ["RenderQuality", "score"]
