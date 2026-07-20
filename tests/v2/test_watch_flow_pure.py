"""Sprint I.3 unit tests for watch_flow's pure helpers.

Only the pure ``_track_terms`` coercion is covered here (the watcher's JSONB
track_fields -> flat term list the diff engine filters on). The I/O paths of
run_check are covered by the scratch-DB flow-integration harness.

Run: .venv/bin/python -m pytest tests/v2/test_watch_flow_pure.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from services.v2_engine.watch_flow import _track_terms  # noqa: E402


def test_track_terms_none_and_empty():
    assert _track_terms(None) is None
    assert _track_terms([]) is None
    assert _track_terms({}) is None


def test_track_terms_list_passthrough():
    assert _track_terms(["price", "title"]) == ["price", "title"]


def test_track_terms_dict_keys_and_nested_lists():
    assert _track_terms({"metadata": ["title"], "tables": []}) == [
        "metadata",
        "title",
        "tables",
    ]


def test_track_terms_strips_empty_terms():
    assert _track_terms(["", "  ", "price"]) == ["price"]
    assert _track_terms({"": ["title"]}) == ["title"]


def test_track_terms_unknown_type():
    assert _track_terms(42) is None
