"""Pure tests for the allowed_endpoints bypass regexes in api/deps.py.

The gateway matches allowed_endpoints by EXACT full path; dynamic per-id
sub-routes are reachable only through these bypass regexes. Each regex is
deliberately scoped to its id prefix (per_/batch_/ing_/wat_) so a future
sub-route cannot silently inherit a bypass.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# api.deps transitively imports utils.postgres, which calls create_engine at
# import time. Engine creation is lazy (no connection), but it requires a
# non-None URL — provide a dummy when the test env has none set.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_dummy")

from api.deps import (  # noqa: E402
    _INGEST_STATUS_PATH_RE,
    _PERCEIVE_BATCH_STATUS_PATH_RE,
    _PERCEIVE_STATUS_PATH_RE,
    _WATCH_STATUS_PATH_RE,
)

HEX = "a3f09b1c2d4e5f6a7b8c9d0e1f2a3b4c"


class TestPerceiveStatusBypass:
    def test_matches_operation_id(self):
        assert _PERCEIVE_STATUS_PATH_RE.fullmatch(f"/v2/perceive/per_{HEX}")

    def test_rejects_batch_paths(self):
        # batch ids must NOT ride the per_ bypass
        assert not _PERCEIVE_STATUS_PATH_RE.fullmatch(f"/v2/perceive/batch/batch_{HEX}")
        assert not _PERCEIVE_STATUS_PATH_RE.fullmatch("/v2/perceive/batch")

    def test_rejects_arbitrary_suffix(self):
        assert not _PERCEIVE_STATUS_PATH_RE.fullmatch(f"/v2/perceive/per_{HEX}/delete")
        assert not _PERCEIVE_STATUS_PATH_RE.fullmatch("/v2/perceive/other")


class TestPerceiveBatchStatusBypass:
    def test_matches_batch_id(self):
        assert _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch(f"/v2/perceive/batch/batch_{HEX}")

    def test_rejects_static_batch_submit_path(self):
        # POST /v2/perceive/batch needs its own exact allowlist entry
        assert not _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch("/v2/perceive/batch")

    def test_rejects_wrong_id_prefix(self):
        assert not _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch(f"/v2/perceive/batch/per_{HEX}")
        assert not _PERCEIVE_BATCH_STATUS_PATH_RE.fullmatch(f"/v2/perceive/batch/batch_{HEX}/x")


class TestIngestStatusBypass:
    def test_matches_job_id_and_retry_webhook(self):
        assert _INGEST_STATUS_PATH_RE.fullmatch(f"/v2/ingest/ing_{HEX}")
        assert _INGEST_STATUS_PATH_RE.fullmatch(f"/v2/ingest/ing_{HEX}/retry-webhook")

    def test_rejects_webhook_secret_management(self):
        # webhook-secret routes stay gated to broad/dashboard tokens
        assert not _INGEST_STATUS_PATH_RE.fullmatch("/v2/ingest/webhook-secret")
        assert not _INGEST_STATUS_PATH_RE.fullmatch("/v2/ingest/webhook-secret/rotate")


class TestWatchStatusBypass:
    def test_matches_watcher_id_and_snapshots(self):
        assert _WATCH_STATUS_PATH_RE.fullmatch(f"/v2/watch/wat_{HEX}")
        assert _WATCH_STATUS_PATH_RE.fullmatch(f"/v2/watch/wat_{HEX}/snapshots")

    def test_rejects_static_list_path(self):
        assert not _WATCH_STATUS_PATH_RE.fullmatch("/v2/watch")
