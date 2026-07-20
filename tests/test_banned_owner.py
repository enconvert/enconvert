"""Banned-owner enforcement (403 "Account suspended") at the gateway.

Hermetic pytest (no live DB, no live network). What this file proves:
  (a) utils.subscription.is_project_owner_active: active owner -> True,
      suspended owner -> False, missing owner row -> True (orphan projects
      are never locked out), owner row without a user row -> True.
  (b) api.deps._attach_subscription: every authenticated request (Bearer
      JWT branch, no DB) is rejected with 403 {"detail": "Account
      suspended"} when the project owner is suspended, and proceeds
      normally when active.
  (c) POST /v1/auth/refresh: the cookie-only refresh mill is closed for
      suspended owners and still works for active ones.
  (d) POST /v1/widget/{id}/token and /v1/widget/{id}/refresh: widget token
      mint/refresh are closed for suspended owners and still work for
      active ones.

Usage (from the gateway root):
    .venv/Scripts/python.exe -m pytest tests/test_banned_owner.py -v
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# api.deps transitively imports utils.postgres, which calls create_engine at
# import time. Engine creation is lazy (no connection), but it requires a
# non-None URL — provide a dummy when the test env has none set.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_dummy")
# auth.jwt_handler reads JWT_SECRET at import time; both mint and validate use
# the same module constant, so any value keeps the tests self-consistent.
os.environ.setdefault("JWT_SECRET", "test-banned-owner-secret")

import pytest  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api.deps as deps  # noqa: E402
import api.v1.auth as auth_module  # noqa: E402
import api.v1.widget as widget_module  # noqa: E402
import utils.postgres as postgres_module  # noqa: E402
from auth.jwt_handler import generate_jwt_token, generate_refresh_token  # noqa: E402
from utils.postgres import get_session  # noqa: E402
from utils.subscription import is_project_owner_active  # noqa: E402


# ─── Shared fakes / helpers ──────────────────────────────────────────────────

class ScriptedResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class ScriptedSession:
    """Stands in for a sqlmodel Session: each .exec() pops the next queued
    row, mirroring how the code under test only ever calls .exec(...).first()."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.closed = False

    def exec(self, statement):
        return ScriptedResult(self._rows.pop(0))

    def close(self):
        self.closed = True


class PosthogStub:
    def distinct_id_for_project(self, project_id):
        return f"project_{project_id}"

    def group_of(self, project_id):
        return {}

    def source_from(self, user, request):
        return "test"

    def identify_context(self, distinct_id):
        return None

    def capture(self, *args, **kwargs):
        return None


OWNER_ROW = SimpleNamespace(user_id=7)


def make_api_key_row():
    return SimpleNamespace(
        project_id=42,
        key_type="public",
        active=True,
        allowed_domains=[],
        allowed_endpoints=[],
    )


def make_widget_row():
    widget = SimpleNamespace(id="wid-1", endpoint="html-to-pdf", api_key_id=1)
    return (widget, make_api_key_row())


def session_override(session):
    def _dep():
        yield session
    return _dep


# ─── Apps under test ─────────────────────────────────────────────────────────

probe_app = FastAPI()


@probe_app.get("/probe")
async def probe(user: dict = Depends(deps.get_current_user)):
    return {"project_id": user["id"]}


auth_app = FastAPI()
auth_app.include_router(auth_module.router, prefix="/v1/auth")

widget_app = FastAPI()
widget_app.include_router(widget_module.router, prefix="/v1/widget")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    widget_app.dependency_overrides.clear()


# ─── (a) Unit: is_project_owner_active ───────────────────────────────────────

class TestIsProjectOwnerActive:
    # Single JOIN query: .first() yields the owner's User.active scalar,
    # or None when there is no owner/user row.
    def test_active_owner_returns_true(self):
        db = ScriptedSession([True])
        assert is_project_owner_active(db, 42) is True

    def test_suspended_owner_returns_false(self):
        db = ScriptedSession([False])
        assert is_project_owner_active(db, 42) is False

    def test_no_owner_row_returns_true(self):
        db = ScriptedSession([None])
        assert is_project_owner_active(db, 42) is True


# ─── (b) Route: _attach_subscription via get_current_user (JWT branch) ──────

class TestAttachSubscriptionBanCheck:
    def _bearer(self):
        token = generate_jwt_token(
            {"id": "42", "key_type": "private", "plan_slug": "free"}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_banned_owner_gets_403(self, monkeypatch):
        monkeypatch.setattr(deps, "posthog_client", PosthogStub())
        monkeypatch.setattr(deps, "get_db", lambda: ScriptedSession([]))
        monkeypatch.setattr(
            deps, "is_project_owner_active", lambda db, project_id: False
        )
        client = TestClient(probe_app)
        resp = client.get("/probe", headers=self._bearer())
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Account suspended"}

    def test_active_owner_passes_through(self, monkeypatch):
        monkeypatch.setattr(deps, "posthog_client", PosthogStub())
        monkeypatch.setattr(deps, "get_db", lambda: ScriptedSession([]))
        monkeypatch.setattr(
            deps, "is_project_owner_active", lambda db, project_id: True
        )
        monkeypatch.setattr(
            deps, "is_admin_default_project", lambda db, project_id: False
        )
        monkeypatch.setattr(deps, "get_subscription", lambda project_id: None)
        monkeypatch.setattr(deps, "check_rate_limits", lambda request, user: None)
        client = TestClient(probe_app)
        resp = client.get("/probe", headers=self._bearer())
        assert resp.status_code == 200
        assert resp.json() == {"project_id": "42"}

    def test_check_receives_parsed_project_id(self, monkeypatch):
        seen = []

        def _record(db, project_id):
            seen.append(project_id)
            return False

        monkeypatch.setattr(deps, "posthog_client", PosthogStub())
        monkeypatch.setattr(deps, "get_db", lambda: ScriptedSession([]))
        monkeypatch.setattr(deps, "is_project_owner_active", _record)
        client = TestClient(probe_app)
        resp = client.get("/probe", headers=self._bearer())
        assert resp.status_code == 403
        assert seen == [42]

    def test_api_key_branch_banned_owner_gets_403(self, monkeypatch):
        # The X-API-Key branch of get_current_user invokes _attach_subscription
        # independently of the JWT branch — prove the ban gate covers it too.
        monkeypatch.setattr(deps, "posthog_client", PosthogStub())
        monkeypatch.setattr(deps, "get_db", lambda: ScriptedSession([]))
        monkeypatch.setattr(
            deps, "is_project_owner_active", lambda db, project_id: False
        )
        monkeypatch.setattr(
            deps,
            "validate_api_key",
            lambda api_key, request: {"id": "42", "key_type": "private", "plan_slug": "free"},
        )
        client = TestClient(probe_app)
        resp = client.get("/probe", headers={"X-API-Key": "sk_test"})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Account suspended"}


# ─── (c) Route: POST /v1/auth/refresh ────────────────────────────────────────

class TestAuthRefreshBanCheck:
    def test_banned_owner_gets_403(self, monkeypatch):
        monkeypatch.setattr(auth_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            auth_module, "is_project_owner_active", lambda db, project_id: False
        )
        monkeypatch.setattr(
            postgres_module, "get_db", lambda: ScriptedSession([make_api_key_row()])
        )
        client = TestClient(auth_app)
        client.cookies.set("refresh_token", generate_refresh_token("42"))
        resp = client.post("/v1/auth/refresh")
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Account suspended"}

    def test_active_owner_still_refreshes(self, monkeypatch):
        monkeypatch.setattr(auth_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            auth_module, "is_project_owner_active", lambda db, project_id: True
        )
        monkeypatch.setattr(
            postgres_module,
            "get_db",
            lambda: ScriptedSession([make_api_key_row(), None]),
        )
        client = TestClient(auth_app)
        client.cookies.set("refresh_token", generate_refresh_token("42"))
        resp = client.post("/v1/auth/refresh")
        assert resp.status_code == 200
        assert resp.json()["token"]


# ─── (d) Route: widget token mint + refresh ──────────────────────────────────

class TestWidgetBanCheck:
    def test_token_mint_banned_owner_gets_403(self, monkeypatch):
        monkeypatch.setattr(widget_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            widget_module, "is_project_owner_active", lambda db, project_id: False
        )
        widget_app.dependency_overrides[get_session] = session_override(
            ScriptedSession([make_widget_row()])
        )
        client = TestClient(widget_app)
        resp = client.post("/v1/widget/wid-1/token", json={"turnstile_token": "tok"})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Account suspended"}

    def test_token_mint_active_owner_succeeds(self, monkeypatch):
        async def _turnstile_ok(token, ip):
            return None

        monkeypatch.setattr(widget_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            widget_module, "is_project_owner_active", lambda db, project_id: True
        )
        monkeypatch.setattr(widget_module, "verify_turnstile", _turnstile_ok)
        widget_app.dependency_overrides[get_session] = session_override(
            ScriptedSession([make_widget_row(), None])
        )
        client = TestClient(widget_app)
        resp = client.post("/v1/widget/wid-1/token", json={"turnstile_token": "tok"})
        assert resp.status_code == 200
        assert resp.json()["token"]

    def test_refresh_banned_owner_gets_403(self, monkeypatch):
        monkeypatch.setattr(widget_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            widget_module, "is_project_owner_active", lambda db, project_id: False
        )
        widget_app.dependency_overrides[get_session] = session_override(
            ScriptedSession([make_widget_row()])
        )
        client = TestClient(widget_app)
        client.cookies.set("refresh_token", generate_refresh_token("42"))
        resp = client.post("/v1/widget/wid-1/refresh")
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Account suspended"}

    def test_refresh_active_owner_succeeds(self, monkeypatch):
        monkeypatch.setattr(widget_module, "enforce_ip", lambda request, scope: None)
        monkeypatch.setattr(
            widget_module, "is_project_owner_active", lambda db, project_id: True
        )
        widget_app.dependency_overrides[get_session] = session_override(
            ScriptedSession([make_widget_row(), None])
        )
        client = TestClient(widget_app)
        client.cookies.set("refresh_token", generate_refresh_token("42"))
        resp = client.post("/v1/widget/wid-1/refresh")
        assert resp.status_code == 200
        assert resp.json()["token"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
