"""Open-source fallback for ``services.v2_engine.quality.diff``.

The cloud build ships a four-strategy semantic diff engine (text similarity,
keyed structured-list matching, context-heading table matching, metadata
comparison). This fallback keeps the public surface the watcher reads —
:class:`Capture`, :class:`Change`, :class:`DiffResult`, ``diff_captures`` —
with a NAIVE content-hash comparison: two captures differ iff the SHA-256 of
their canonical JSON serialisations differ. Every function is pure: no I/O,
no globals, no input mutation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# Long string values are truncated at serialisation time so a change record
# cannot bloat the JSONB row it is persisted into.
MAX_VALUE_CHARS = 2_000


@dataclass(frozen=True)
class Change:
    """One structured change. JSON-serialisable via :meth:`to_dict`.

    Field-compatible with the cloud engine; the naive fallback only ever
    emits ``section='text', kind='modified', field='content_hash'``.
    """

    section: str
    kind: str  # "added" | "removed" | "modified"
    key: Optional[str] = None
    field: Optional[str] = None
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        """JSONB-ready dict. ``before`` / ``after`` may carry untrusted page
        content — consumers that render them MUST HTML-escape them."""
        return {
            "section": self.section,
            "kind": self.kind,
            "key": self.key,
            "field": self.field,
            "before": _cap_value(self.before),
            "after": _cap_value(self.after),
        }


@dataclass(frozen=True)
class DiffResult:
    """Verdict for one capture comparison."""

    has_changes: bool
    similarity: float
    changes: tuple[Change, ...] = ()

    def to_change_dicts(self) -> list[dict[str, Any]]:
        """JSONB-ready list for ``ch_watcher_snapshots.changes``."""
        return [change.to_dict() for change in self.changes]


@dataclass
class Capture:
    """One rendered capture of a watched URL: main text + extracted structure.

    ``structured`` carries whichever sections the watcher extracted
    (``metadata``, ``tables``, ``links``, ``structured_data``, ...); the
    naive engine hashes the whole payload rather than diffing per section.
    """

    text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capture":
        return cls(
            text=data.get("text") or "",
            structured=data.get("structured") or {},
        )


def _cap_value(value: Any) -> Any:
    """Truncate an over-long string before/after value for storage."""
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


def _capture_hash(capture: Capture) -> str:
    """Stable SHA-256 over the canonical JSON form of a capture."""
    canonical = json.dumps(
        {"text": capture.text, "structured": capture.structured},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diff_captures(
    before: Capture,
    after: Capture,
    mode: str = "auto",
    track_fields: Optional[Sequence[str]] = None,
) -> DiffResult:
    """Naive content-hash comparison of two captures.

    ``mode`` and ``track_fields`` are accepted for signature compatibility
    with the cloud engine but ignored: the open build cannot attribute a
    change to a specific section or field, so any content difference is
    reported as a single whole-capture change.

    Returns:
        ``DiffResult(False, 1.0, ())`` when the captures hash identically,
        else ``DiffResult(True, 0.0, (change,))`` where the single change
        carries the before/after content hashes.
    """
    del mode, track_fields  # Signature compatibility only (naive engine).

    before_hash = _capture_hash(before)
    after_hash = _capture_hash(after)
    if before_hash == after_hash:
        return DiffResult(False, 1.0, ())

    change = Change(
        section="text",
        kind="modified",
        field="content_hash",
        before=before_hash,
        after=after_hash,
    )
    return DiffResult(True, 0.0, (change,))
