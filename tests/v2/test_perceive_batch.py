"""Sprint F.8 — /v2/perceive/batch with a droplet-local async worker.

NO Cloud Tasks / NO Google services (owner decision 2026-06-07,
superseding the plan's F.8 Cloud Tasks dispatch): batches run through an
in-process asyncio worker that processes ONE URL AT A TIME via
perceive_flow.run(). V1 batch flows already work exactly this way
(Starlette BackgroundTasks + process_batch_async); F.8 mirrors that
pattern for V2.

What this file proves (offline analogues of plan F.8 verification a-d):
  (a) <= 10 URLs run inline and return 200 with one full PerceiveResponse
      per URL, all sharing a batch_id.
  (b) > 10 URLs return 202 with a job_id; the worker drains the queue one
      URL at a time; GET /v2/perceive/batch/{job_id} aggregates to
      completion.
  (c) Per-URL ch_perceive_operations rows: pre-created as 'queued' with
      the batch_id, claimed (not duplicated) by create_operation.
  (d) Plan gates: batch_limit=0 -> 403, over-limit -> 403; the new
      check_v2_quota units= argument denies a batch that would overshoot
      the monthly perceive quota (402).
  Plus: ZIP output mode bundles every successful URL's artifacts through
  the V1 plumbing (upload_to_gcs + Activity-row stamping), partial
  failure semantics, and the startup sweep for orphaned rows.

House conventions (tests/v2/test_perceive.py): synchronous ``def test_*``
driving coroutines via ``asyncio.run`` (no pytest-asyncio in this venv),
hand-rolled fakes over unittest.mock, no live DB / browser / network.

Run: .venv/bin/python -m pytest tests/v2/test_perceive_batch.py -v
"""

from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(GATEWAY_ROOT / ".env")

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from api.deps import check_v2_quota, get_current_user  # noqa: E402
from api.v2.router import router as v2_router  # noqa: E402
from api.v2.schemas.perceive import (  # noqa: E402
    OutputArtifact,
    PerceiveBatchRequest,
    PerceiveRequest,
    PerceiveResponse,
    PerceiveTokens,
)
from services.v2_engine import batch_worker, operations  # noqa: E402

INLINE_THRESHOLD = batch_worker.INLINE_THRESHOLD


# ─── Shared fakes ───────────────────────────────────────────────────────────


def make_user(
    batch_limit: int = 50,
    perceive_used: int = 0,
    perceive_limit: int = 1000,
) -> dict:
    return {
        "id": 1,
        "subscription": {
            "plan_slug": "pro",
            "batch_limit": batch_limit,
            "perceive_enabled": True,
            "perceive_operations_month": perceive_limit,
            "llm_extraction_enabled": False,
        },
        "_perceive_used": perceive_used,
    }


def ok_response(url: str, operation_id: str = "per_x") -> PerceiveResponse:
    return PerceiveResponse(
        operation_id=operation_id,
        status="completed",
        url=url,
        url_final=url,
        content_hash="h" * 64,
        render_quality=1.0,
        cache_hit=False,
        outputs={
            "markdown": OutputArtifact(
                url="https://signed.example/x.md",
                object_key=f"files/1/v2-perceive/{operation_id}_markdown.md",
                size_bytes=10,
                content_type="text/markdown; charset=utf-8",
            )
        },
        structured=None,
        extraction_tier="heuristic",
        tokens=PerceiveTokens(),
        cost_cents=0.0,
        duration_ms=100,
        warnings=[],
    )


class FakeOps:
    """In-memory stand-in for the operations persistence layer."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}

    def create_queued(
        self,
        *,
        batch_id: str,
        project_id: int,
        entries: list[tuple[str, str]],
        outputs_requested: list[str],
    ) -> None:
        for operation_id, url in entries:
            self.rows[operation_id] = SimpleNamespace(
                operation_id=operation_id,
                project_id=project_id,
                url=url,
                url_final=None,
                status="queued",
                batch_id=batch_id,
                outputs_requested=list(outputs_requested),
                output_keys=None,
                structured_data=None,
                extraction_tier=None,
                content_hash=None,
                render_quality_score=None,
                cache_hit=False,
                llm_input_tokens=0,
                llm_output_tokens=0,
                llm_cost_cents=0,
                duration_ms=None,
                error_message=None,
            )

    def fail(self, *, operation_id: str, error_message: str, **_: Any) -> None:
        row = self.rows.get(operation_id)
        if row is not None:
            row.status = "failed"
            row.error_message = error_message

    def complete(self, operation_id: str, response: Any) -> None:
        """Mirror what perceive_flow/complete_operation persist."""
        row = self.rows.get(operation_id)
        if row is not None:
            row.status = "completed"
            row.url_final = str(response.url_final or response.url)
            row.content_hash = response.content_hash
            row.output_keys = {
                name: {
                    "key": artifact.object_key,
                    "size_bytes": artifact.size_bytes,
                    "content_type": artifact.content_type,
                }
                for name, artifact in response.outputs.items()
            }

    def list_batch(self, batch_id: str, project_id: int) -> list[Any]:
        return [
            row
            for row in self.rows.values()
            if row.batch_id == batch_id and row.project_id == project_id
        ]

    def attach_zip(
        self, batch_id: str, project_id: int, zip_entry: dict
    ) -> None:
        for row in self.rows.values():
            if row.batch_id == batch_id and row.status == "completed":
                keys = dict(row.output_keys or {})
                keys["_batch_zip"] = zip_entry
                row.output_keys = keys


class FakeBatchStore:
    """In-memory stand-in for the durable batch envelope (ch_perceive_batches)."""

    ACTIVE_BATCH_STATUSES = ("queued", "processing")

    def __init__(self) -> None:
        self.batches: dict[str, SimpleNamespace] = {}

    def create_batch(
        self, batch_id, project_id, *, output_mode, options, total
    ) -> None:
        self.batches[batch_id] = SimpleNamespace(
            batch_id=batch_id,
            project_id=project_id,
            status="queued",
            output_mode=output_mode,
            options=options,
            total=total,
            completed=0,
            failed=0,
            zip_object_key=None,
        )

    def get_batch(self, batch_id):
        return self.batches.get(batch_id)

    def get_batch_for_project(self, batch_id, project_id):
        row = self.batches.get(batch_id)
        return row if row and row.project_id == project_id else None

    def list_active_batch_ids(self):
        return [
            b.batch_id
            for b in self.batches.values()
            if b.status in self.ACTIVE_BATCH_STATUSES
        ]

    def transition_status(
        self, batch_id, new_status, *, allowed_from=ACTIVE_BATCH_STATUSES,
        error_message=None, zip_object_key=None, completed=False,
    ) -> bool:
        row = self.batches.get(batch_id)
        if row is None or row.status not in allowed_from:
            return False
        row.status = new_status
        if zip_object_key is not None:
            row.zip_object_key = zip_object_key
        return True

    def update_progress(self, batch_id, *, completed, failed) -> None:
        row = self.batches.get(batch_id)
        if row is not None and row.status in self.ACTIVE_BATCH_STATUSES:
            row.completed, row.failed = completed, failed

    def finalize(
        self, batch_id, *, status, completed, failed, zip_object_key=None
    ) -> bool:
        row = self.batches.get(batch_id)
        if row is None or row.status not in self.ACTIVE_BATCH_STATUSES:
            return False
        row.status = status
        row.completed, row.failed = completed, failed
        if zip_object_key is not None:
            row.zip_object_key = zip_object_key
        return True

    def cancel_batch(self, batch_id, project_id):
        row = self.batches.get(batch_id)
        if row is None or row.project_id != project_id:
            return None
        if row.status in self.ACTIVE_BATCH_STATUSES:
            row.status = "canceled"
        return row


@pytest.fixture()
def fake_ops(monkeypatch: pytest.MonkeyPatch) -> FakeOps:
    ops = FakeOps()
    monkeypatch.setattr(
        batch_worker.operations, "create_queued_operations", ops.create_queued
    )
    monkeypatch.setattr(batch_worker.operations, "fail_operation", ops.fail)
    monkeypatch.setattr(
        batch_worker.operations, "list_batch_operations", ops.list_batch
    )
    monkeypatch.setattr(
        batch_worker.operations, "attach_batch_zip", ops.attach_zip
    )
    # Durable batch envelope: patch every batch_store method the worker uses.
    store = FakeBatchStore()
    for name in (
        "create_batch", "get_batch", "get_batch_for_project",
        "list_active_batch_ids", "transition_status", "update_progress",
        "finalize", "cancel_batch",
    ):
        monkeypatch.setattr(
            batch_worker.batch_store, name, getattr(store, name)
        )
    ops.batch_store = store  # expose for assertions
    return ops


@pytest.fixture()
def quiet_activity(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture activity calls; never touch the DB."""
    calls: list[dict] = []

    async def fake_start(
        project_id: Any, endpoint: str, urls: list[str], batch_id: str
    ) -> list[int]:
        calls.append({"start": urls, "batch_id": batch_id})
        return list(range(1, len(urls) + 1))

    async def fake_update(activity_id: int, status: str, **kwargs: Any) -> None:
        calls.append({"update": activity_id, "status": status, **kwargs})

    monkeypatch.setattr(batch_worker, "log_batch_activity_start", fake_start)
    monkeypatch.setattr(batch_worker, "update_activity_status", fake_update)
    return calls


def run_fake_flow(
    monkeypatch: pytest.MonkeyPatch,
    fail_urls: frozenset[str] = frozenset(),
    order: Optional[list[str]] = None,
    in_flight: Optional[list[int]] = None,
    fake_ops: Optional[FakeOps] = None,
) -> None:
    """Replace perceive_flow.run with a deterministic fake.

    ``in_flight`` records concurrency: it must never exceed 1 (the
    owner's one-URL-at-a-time directive). When ``fake_ops`` is given,
    successful runs persist to its rows exactly as the real flow's
    complete_operation would.
    """
    active = {"count": 0}

    async def fake_run(
        request: PerceiveRequest,
        operation_id: str,
        user: dict,
        batch_id: Optional[str] = None,
    ) -> PerceiveResponse:
        active["count"] += 1
        if in_flight is not None:
            in_flight.append(active["count"])
        try:
            await asyncio.sleep(0)
            if order is not None:
                order.append(str(request.url))
            if str(request.url) in fail_urls:
                raise RuntimeError(f"render failed for {request.url}")
            response = ok_response(str(request.url), operation_id)
            if fake_ops is not None:
                fake_ops.complete(operation_id, response)
            return response
        finally:
            active["count"] -= 1

    monkeypatch.setattr(batch_worker.perceive_flow, "run", fake_run)


def make_app(user: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(v2_router, prefix="/v2")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


# ─── Schemas ────────────────────────────────────────────────────────────────


def test_batch_request_requires_urls() -> None:
    with pytest.raises(ValidationError):
        PerceiveBatchRequest(urls=[])


def test_batch_request_defaults() -> None:
    body = PerceiveBatchRequest(urls=["https://example.com"])
    assert body.output_mode == "manifest"
    assert body.options.outputs == ["markdown", "structured"]


def test_batch_request_rejects_unknown_output_mode() -> None:
    with pytest.raises(ValidationError):
        PerceiveBatchRequest(urls=["https://example.com"], output_mode="tar")


def test_batch_options_carry_perceive_surface() -> None:
    body = PerceiveBatchRequest(
        urls=["https://example.com"],
        options={"outputs": ["pdf"], "mobile": True, "schema": {"type": "object"}},
    )
    assert body.options.outputs == ["pdf"]
    assert body.options.mobile is True
    assert body.options.extraction_schema == {"type": "object"}


def test_per_url_requests_are_validated() -> None:
    body = PerceiveBatchRequest(urls=["ftp://nope.example.com"])
    with pytest.raises(ValidationError):
        batch_worker.build_requests(body)


def test_build_requests_dedupes_preserving_order() -> None:
    body = PerceiveBatchRequest(
        urls=[
            " https://a.example.com ",
            "https://b.example.com",
            "https://a.example.com",
        ]
    )
    requests = batch_worker.build_requests(body)
    assert [str(r.url) for r in requests] == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_perceive_request_surface_unchanged() -> None:
    # The options-base refactor must not move PerceiveRequest's contract.
    request = PerceiveRequest(url="https://example.com")
    assert request.outputs == ["markdown", "structured"]
    assert request.cache_mode == "enabled"
    with pytest.raises(ValidationError):
        PerceiveRequest(url="javascript:alert(1)")


# ─── check_v2_quota units ───────────────────────────────────────────────────


def test_quota_units_denies_overshoot(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(perceive_limit=20)
    monkeypatch.setattr(
        "api.deps.get_current_usage_period",
        lambda project_id: SimpleNamespace(perceive_operations=10),
    )
    check_v2_quota(user, "perceive_operations", units=10)  # exactly fits
    with pytest.raises(HTTPException) as err:
        check_v2_quota(user, "perceive_operations", units=11)
    assert err.value.status_code == 402


def test_quota_units_default_matches_old_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(perceive_limit=10)
    monkeypatch.setattr(
        "api.deps.get_current_usage_period",
        lambda project_id: SimpleNamespace(perceive_operations=10),
    )
    with pytest.raises(HTTPException):
        check_v2_quota(user, "perceive_operations")


# ─── Worker: process_batch ──────────────────────────────────────────────────


def test_inline_batch_processes_sequentially(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    urls = [f"https://site{i}.example.com" for i in range(5)]
    order: list[str] = []
    in_flight: list[int] = []
    run_fake_flow(monkeypatch, order=order, in_flight=in_flight)

    body = PerceiveBatchRequest(urls=urls)
    job = batch_worker.make_job(body, make_user())
    items = asyncio.run(batch_worker.process_batch(job))

    assert [str(i.url) for i in items] == urls  # original order
    assert order == urls  # processed strictly one at a time, FIFO
    assert max(in_flight) == 1  # never concurrent
    assert all(i.status == "completed" for i in items)


def test_partial_failure_marks_failed_and_continues(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    urls = [f"https://site{i}.example.com" for i in range(4)]
    run_fake_flow(monkeypatch, fail_urls=frozenset({urls[1]}), fake_ops=fake_ops)

    body = PerceiveBatchRequest(urls=urls)
    job = batch_worker.make_job(body, make_user())
    items = asyncio.run(batch_worker.process_batch(job))

    statuses = {str(i.url): i.status for i in items}
    assert statuses[urls[1]] == "failed"
    assert [statuses[u] for u in urls].count("completed") == 3
    # The pre-created queued row for the failed URL was marked failed.
    failed_rows = [r for r in fake_ops.rows.values() if r.status == "failed"]
    assert len(failed_rows) == 1 and failed_rows[0].url == urls[1]


def test_queued_rows_precreated_with_batch_id(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    urls = [f"https://site{i}.example.com" for i in range(3)]
    run_fake_flow(monkeypatch)
    body = PerceiveBatchRequest(urls=urls)
    job = batch_worker.make_job(body, make_user())
    # Rows exist as 'queued' from job creation, before any processing.
    assert len(fake_ops.rows) == 3
    assert all(r.status == "queued" for r in fake_ops.rows.values())
    assert {r.batch_id for r in fake_ops.rows.values()} == {job.batch_id}
    asyncio.run(batch_worker.process_batch(job))


def test_zip_mode_bundles_successful_artifacts(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    urls = ["https://a.example.com", "https://b.example.com"]
    run_fake_flow(monkeypatch, fake_ops=fake_ops)

    downloads: list[str] = []
    uploads: dict[str, bytes] = {}

    def fake_download(object_key: str) -> bytes:
        downloads.append(object_key)
        return b"artifact-bytes-" + object_key.encode()

    def fake_upload(
        file_bytes: bytes, user_id: Any, endpoint: str, filename: str
    ) -> dict:
        uploads[filename] = file_bytes
        return {
            "object_key": f"files/1/v2-perceive-batch/{filename}",
            "file_size": len(file_bytes),
            "filename": filename,
        }

    monkeypatch.setattr(batch_worker, "download_from_storage", fake_download)
    monkeypatch.setattr(batch_worker, "upload_to_gcs", fake_upload)
    monkeypatch.setattr(
        batch_worker,
        "generate_presigned_url",
        lambda key, uid, expires_in=900: f"https://signed.example/{key}",
    )

    body = PerceiveBatchRequest(urls=urls, output_mode="zip")
    job = batch_worker.make_job(body, make_user())
    asyncio.run(batch_worker.process_batch(job))

    assert len(downloads) == 2  # one markdown artifact per URL
    assert len(uploads) == 1
    zip_bytes = next(iter(uploads.values()))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        assert len(archive.namelist()) == 2
    assert job.zip_artifact is not None
    assert job.zip_artifact.object_key.endswith(".zip")
    # Completed rows carry the reserved _batch_zip entry for status GETs.
    stamped = [
        r
        for r in fake_ops.rows.values()
        if (r.output_keys or {}).get("_batch_zip")
    ]
    assert len(stamped) == 2


# ─── operations: claim semantics ────────────────────────────────────────────


class FakeSession:
    """Hand-rolled session double for operations.create_operation."""

    def __init__(self, existing: Optional[Any]) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.committed = False

    def exec(self, statement: Any) -> Any:
        return SimpleNamespace(first=lambda: self.existing, all=lambda: [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        pass


def test_create_operation_claims_queued_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = SimpleNamespace(
        operation_id="per_q", status="queued", batch_id="batch_1"
    )
    session = FakeSession(existing=queued)
    monkeypatch.setattr(operations, "get_db", lambda: session)
    operations.create_operation(
        operation_id="per_q",
        project_id=1,
        url="https://example.com",
        outputs_requested=["markdown"],
        batch_id="batch_1",
    )
    assert queued.status == "processing"  # claimed in place
    assert session.added == [queued]  # no second row inserted
    assert session.committed


def test_create_operation_inserts_when_no_queued_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(existing=None)
    monkeypatch.setattr(operations, "get_db", lambda: session)
    operations.create_operation(
        operation_id="per_new",
        project_id=1,
        url="https://example.com",
        outputs_requested=["markdown"],
    )
    assert len(session.added) == 1
    assert session.added[0].operation_id == "per_new"
    assert session.added[0].status == "processing"


# ─── Handler: POST /v2/perceive/batch ───────────────────────────────────────


def quota_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.deps.get_current_usage_period",
        lambda project_id: SimpleNamespace(perceive_operations=0),
    )


def test_post_batch_limit_zero_is_403(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    quota_ok(monkeypatch)
    client = TestClient(make_app(make_user(batch_limit=0)))
    response = client.post(
        "/v2/perceive/batch", json={"urls": ["https://example.com"]}
    )
    assert response.status_code == 403


def test_post_batch_over_limit_is_403(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    quota_ok(monkeypatch)
    client = TestClient(make_app(make_user(batch_limit=2)))
    response = client.post(
        "/v2/perceive/batch",
        json={"urls": [f"https://site{i}.example.com" for i in range(3)]},
    )
    assert response.status_code == 403


def test_post_batch_quota_overshoot_is_402(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    monkeypatch.setattr(
        "api.deps.get_current_usage_period",
        lambda project_id: SimpleNamespace(perceive_operations=998),
    )
    client = TestClient(make_app(make_user(perceive_limit=1000)))
    response = client.post(
        "/v2/perceive/batch",
        json={"urls": [f"https://site{i}.example.com" for i in range(3)]},
    )
    assert response.status_code == 402


def test_post_batch_invalid_url_is_422(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    quota_ok(monkeypatch)
    client = TestClient(make_app(make_user()))
    response = client.post(
        "/v2/perceive/batch",
        json={"urls": ["https://ok.example.com", "ftp://bad.example.com"]},
    )
    assert response.status_code == 422


def test_post_small_batch_runs_inline(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    quota_ok(monkeypatch)
    run_fake_flow(monkeypatch)
    monkeypatch.setattr(batch_worker, "assert_urls_public", _noop_async)
    urls = [f"https://site{i}.example.com" for i in range(5)]
    client = TestClient(make_app(make_user()))
    response = client.post("/v2/perceive/batch", json={"urls": urls})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["total"] == 5
    assert len(payload["items"]) == 5
    assert payload["job_id"].startswith("batch_")
    assert all(i["status"] == "completed" for i in payload["items"])


def test_inline_batch_degrades_to_202_past_wait_budget(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    """TimeoutMiddleware guard: a slow inline batch must answer 202
    with the job_id instead of being killed at the gateway's 300 s."""
    quota_ok(monkeypatch)
    run_fake_flow(monkeypatch, fake_ops=fake_ops)
    monkeypatch.setattr(batch_worker, "assert_urls_public", _noop_async)
    monkeypatch.setattr(batch_worker, "worker_running", lambda: True)
    monkeypatch.setattr(batch_worker, "INLINE_WAIT_BUDGET_S", 0.01)

    urls = [f"https://site{i}.example.com" for i in range(3)]
    client = TestClient(make_app(make_user()))
    response = client.post("/v2/perceive/batch", json={"urls": urls})
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["warnings"]

    # The job is queued, not lost: draining completes it.
    asyncio.run(batch_worker.drain_for_tests())
    status = client.get(f"/v2/perceive/batch/{payload['job_id']}")
    assert status.json()["status"] == "completed"


def test_post_large_batch_returns_202_and_worker_drains(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    quota_ok(monkeypatch)
    run_fake_flow(monkeypatch, fake_ops=fake_ops)
    monkeypatch.setattr(batch_worker, "assert_urls_public", _noop_async)
    urls = [f"https://site{i}.example.com" for i in range(INLINE_THRESHOLD + 5)]
    user = make_user()
    client = TestClient(make_app(user))
    response = client.post("/v2/perceive/batch", json={"urls": urls})
    assert response.status_code == 202
    payload = response.json()
    job_id = payload["job_id"]
    assert payload["status"] == "queued"
    assert payload["total"] == len(urls)

    # All rows pre-created as queued; drain the worker queue directly.
    assert len(fake_ops.rows) == len(urls)
    asyncio.run(batch_worker.drain_for_tests())
    status = client.get(f"/v2/perceive/batch/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["completed"] == len(urls)
    assert len(body["items"]) == len(urls)


async def _noop_async(urls: list[str]) -> None:
    return None


# ─── Handler: GET /v2/perceive/batch/{job_id} ───────────────────────────────


def test_get_batch_status_unknown_job_is_404(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps
) -> None:
    client = TestClient(make_app(make_user()))
    response = client.get("/v2/perceive/batch/batch_missing")
    assert response.status_code == 404


def test_get_batch_status_is_project_scoped(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps
) -> None:
    fake_ops.create_queued(
        batch_id="batch_other",
        project_id=2,  # different tenant
        entries=[("per_1", "https://example.com")],
        outputs_requested=["markdown"],
    )
    client = TestClient(make_app(make_user()))
    response = client.get("/v2/perceive/batch/batch_other")
    assert response.status_code == 404


def test_get_batch_status_aggregates(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps
) -> None:
    fake_ops.create_queued(
        batch_id="batch_agg",
        project_id=1,
        entries=[
            ("per_1", "https://a.example.com"),
            ("per_2", "https://b.example.com"),
            ("per_3", "https://c.example.com"),
        ],
        outputs_requested=["markdown"],
    )
    fake_ops.rows["per_1"].status = "completed"
    fake_ops.rows["per_2"].status = "failed"
    client = TestClient(make_app(make_user()))
    response = client.get("/v2/perceive/batch/batch_agg")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"  # one row still queued
    assert body["total"] == 3
    assert body["completed"] == 1
    assert body["failed"] == 1
    assert body["pending"] == 1


# ─── Startup sweep ──────────────────────────────────────────────────────────


def test_startup_sweep_fails_orphaned_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swept: list[str] = []
    monkeypatch.setattr(
        batch_worker.operations,
        "fail_stale_operations",
        lambda reason: swept.append(reason) or 2,
    )
    # No durable batches to resume (avoid the real list_active_batch_ids DB call).
    monkeypatch.setattr(
        batch_worker.batch_store, "list_active_batch_ids", lambda: []
    )
    asyncio.run(batch_worker.startup())
    try:
        assert len(swept) == 1
        assert "restart" in swept[0]
    finally:
        asyncio.run(batch_worker.shutdown())


def test_startup_resumes_active_batches(
    monkeypatch: pytest.MonkeyPatch, fake_ops: FakeOps, quiet_activity: list
) -> None:
    """A batch left non-terminal by a restart is re-enqueued and its still
    -pending URLs re-rendered (durable resume), NOT failed."""
    quota_ok(monkeypatch)
    run_fake_flow(monkeypatch, fake_ops=fake_ops)
    monkeypatch.setattr(
        batch_worker.operations, "fail_stale_operations", lambda reason: 0
    )
    # _load_job rebuilds the per-request user from the project — stub the DB read.
    monkeypatch.setattr(
        "utils.subscription.get_effective_subscription", lambda pid: {}
    )
    # Simulate a batch persisted before a crash: envelope + 3 queued op rows,
    # one already completed (must NOT be re-rendered).
    store = fake_ops.batch_store
    store.create_batch(
        "batch_resume", 1, output_mode="manifest",
        options={"outputs": ["markdown"]}, total=3,
    )
    fake_ops.create_queued(
        batch_id="batch_resume", project_id=1,
        entries=[("per_a", "https://a.example.com"),
                 ("per_b", "https://b.example.com"),
                 ("per_c", "https://c.example.com")],
        outputs_requested=["markdown"],
    )
    fake_ops.rows["per_a"].status = "completed"  # done before the crash

    rendered: list[str] = []
    orig_run = batch_worker.perceive_flow.run

    async def tracking_run(request, operation_id, user, batch_id=None):
        rendered.append(operation_id)
        return await orig_run(request, operation_id, user, batch_id=batch_id)

    monkeypatch.setattr(batch_worker.perceive_flow, "run", tracking_run)

    # Resume mechanics: _load_job rebuilds ONLY the still-pending URLs from the
    # DB (envelope options + operation rows), and process_batch finalizes.
    job = batch_worker._load_job("batch_resume")
    assert {op_id for op_id, _ in job.requests} == {"per_b", "per_c"}
    assert job.output_mode == "manifest"
    asyncio.run(batch_worker.process_batch(job))

    # Only the two still-pending URLs were re-rendered; the completed one was not.
    assert set(rendered) == {"per_b", "per_c"}
    assert store.get_batch("batch_resume").status == "completed"
