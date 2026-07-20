"""Sprint H.3 — /v2/lookup endpoint + Serper adapter + lookup_flow.

Hermetic pytest (no live network, no live DB, no live browser). The live
smoke test (operator supplies a real SERPER_API_KEY; "AI agents" returns
5 results) is run manually per the H.3 verification, not here.

What this file proves:
  (a) Schemas: LookupRequest defaults + bounds (category enum, num/page/
      perceive_top bounds, empty-query rejection, time_filter enum).
  (b) Quota gate: deps.check_v2_quota("lookup_queries") -> 402 on a
      disabled plan AND on an exhausted monthly quota (H.3 verification
      d), passes when unlimited, bypasses for admin.
  (c) Serper adapter: category -> endpoint routing + gl/hl/tbs/num
      passthrough (H.3 verification a), per-category normalization,
      missing-key -> SearchConfigError, circuit-open -> Unavailable,
      retry on 5xx then success, 401 -> ConfigError, 400 -> Upstream.
  (d) lookup_flow: request params reach the adapter; perceive_top=N runs
      N perceive_flow.run calls and attaches each PerceiveResponse (H.3
      verification c); usage bumped once + one audit row; auto-perceive
      degrades gracefully when the perceive quota runs out; provider
      faults map to 502/503.
  (e) Handler contract: 402 on the lookup gate, 200 + section-4 shape on
      success.

Usage (from the gateway root):
    .venv/bin/pytest tests/v2/test_lookup.py -v
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GATEWAY_ROOT))

from dotenv import load_dotenv

load_dotenv(GATEWAY_ROOT / ".env")

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


# ─── Shared test data / helpers ──────────────────────────────────────────────

SUB_ENABLED = {
    "plan_slug": "starter",
    "lookup_enabled": True,
    "lookup_queries_month": 0,  # 0 + enabled = unlimited (migration 011)
    "perceive_enabled": True,
    "perceive_operations_month": 0,
}
SUB_DISABLED = {"plan_slug": "free-v1", "lookup_enabled": False}


def make_user(sub: dict, user_id: str = "42") -> dict:
    return {"id": user_id, "key_type": "private", "subscription": dict(sub)}


class _FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def install_fake_httpx(monkeypatch, responses: list, calls: list) -> None:
    """Patch httpx.AsyncClient so each .post() returns/raises the next
    queued item. ``responses`` items are either (status, body) tuples or
    Exception instances to raise (transport faults)."""
    state = {"i": 0}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            item = responses[state["i"]]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            status, body = item
            return _FakeResponse(status, body)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


class _FakeBreaker:
    def __init__(self, open_: bool = False):
        self.open = open_
        self.success = 0
        self.failure = 0

    def is_open(self, name):
        return self.open

    def record_success(self, name):
        self.success += 1

    def record_failure(self, name):
        self.failure += 1


def _no_sleep(monkeypatch):
    import services.v2_search.serper as serper

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(serper.asyncio, "sleep", _sleep)


# ─── (a) Schemas ─────────────────────────────────────────────────────────────


class TestLookupSchemas:
    def test_request_defaults(self):
        from api.v2.schemas.lookup import LookupRequest

        req = LookupRequest(query="ai agents")
        assert req.category == "web"
        assert req.num_results == 10
        assert req.page == 1
        assert req.autocorrect is True
        assert req.perceive_top == 0
        assert req.country is None
        assert req.time_filter is None

    def test_category_enum_enforced(self):
        from api.v2.schemas.lookup import LookupRequest

        for category in ("web", "news", "images", "scholar", "patents", "maps"):
            assert LookupRequest(query="x", category=category).category == category
        with pytest.raises(ValidationError):
            LookupRequest(query="x", category="videos")

    def test_empty_query_rejected(self):
        from api.v2.schemas.lookup import LookupRequest

        with pytest.raises(ValidationError):
            LookupRequest(query="   ")
        with pytest.raises(ValidationError):
            LookupRequest(query="")

    def test_query_is_stripped(self):
        from api.v2.schemas.lookup import LookupRequest

        assert LookupRequest(query="  hi  ").query == "hi"

    def test_num_results_and_page_bounds(self):
        from api.v2.schemas.lookup import LookupRequest

        with pytest.raises(ValidationError):
            LookupRequest(query="x", num_results=0)
        with pytest.raises(ValidationError):
            LookupRequest(query="x", num_results=101)
        with pytest.raises(ValidationError):
            LookupRequest(query="x", page=0)

    def test_perceive_top_bounds(self):
        from api.v2.schemas.lookup import LookupRequest

        assert LookupRequest(query="x", perceive_top=10).perceive_top == 10
        with pytest.raises(ValidationError):
            LookupRequest(query="x", perceive_top=-1)
        with pytest.raises(ValidationError):
            LookupRequest(query="x", perceive_top=11)

    def test_time_filter_enum(self):
        from api.v2.schemas.lookup import LookupRequest

        assert LookupRequest(query="x", time_filter="week").time_filter == "week"
        with pytest.raises(ValidationError):
            LookupRequest(query="x", time_filter="decade")

    def test_response_shape(self):
        from api.v2.schemas.lookup import LookupResponse

        fields = set(LookupResponse.model_fields)
        for required in (
            "lookup_id",
            "query",
            "category",
            "total",
            "results",
            "perceive_top",
            "perceive_operation_ids",
            "cost_cents",
            "warnings",
        ):
            assert required in fields, f"LookupResponse missing {required}"


# ─── (b) Quota gate (402, H.3 verification d) ───────────────────────────────


class TestLookupQuotaGate:
    def test_disabled_plan_raises_402(self):
        import api.deps as deps

        with pytest.raises(HTTPException) as exc:
            deps.check_v2_quota(make_user(SUB_DISABLED), "lookup_queries")
        assert exc.value.status_code == 402

    def test_unlimited_plan_passes(self):
        import api.deps as deps

        deps.check_v2_quota(make_user(SUB_ENABLED), "lookup_queries")

    def test_admin_bypasses(self):
        import api.deps as deps

        deps.check_v2_quota(make_user({"plan_slug": "admin"}), "lookup_queries")

    def test_over_quota_raises_402(self, monkeypatch):
        import api.deps as deps

        sub = {
            "plan_slug": "free",
            "lookup_enabled": True,
            "lookup_queries_month": 25,
        }
        monkeypatch.setattr(
            deps,
            "get_current_usage_period",
            lambda project_id: SimpleNamespace(lookup_queries=25),
        )
        with pytest.raises(HTTPException) as exc:
            deps.check_v2_quota(make_user(sub), "lookup_queries")
        assert exc.value.status_code == 402

    def test_lookup_registered_in_quota_registry(self):
        import api.deps as deps

        assert "lookup_queries" in deps.V2_QUOTAS
        spec = deps.V2_QUOTAS["lookup_queries"]
        assert spec["flag"] == "lookup_enabled"
        assert spec["limit_key"] == "lookup_queries_month"


# ─── (c) Serper adapter ─────────────────────────────────────────────────────


class TestSerperAdapter:
    def test_missing_key_raises_config_error(self, monkeypatch):
        from services.v2_search.adapter import SearchConfigError
        from services.v2_search.serper import SerperAdapter

        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        with pytest.raises(SearchConfigError):
            asyncio.run(SerperAdapter().search(query="x"))

    def test_circuit_open_raises_unavailable(self, monkeypatch):
        import services.v2_search.serper as serper
        from services.v2_search.adapter import SearchUnavailableError

        monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker(open_=True))
        with pytest.raises(SearchUnavailableError):
            asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))

    def test_category_routing_and_payload_passthrough(self, monkeypatch):
        """H.3 verification a: each category hits the right endpoint and
        gl/hl/tbs/num/page travel through unchanged."""
        import services.v2_search.serper as serper

        endpoints = {
            "web": "/search",
            "news": "/news",
            "images": "/images",
            "scholar": "/scholar",
            "patents": "/patents",
            "maps": "/maps",
        }
        for category, path in endpoints.items():
            calls: list = []
            monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker())
            install_fake_httpx(monkeypatch, [(200, {})], calls)
            asyncio.run(
                serper.SerperAdapter(api_key="k").search(
                    query="ai agents",
                    category=category,
                    country="us",
                    locale="en",
                    time_filter="week",
                    num=7,
                    page=2,
                )
            )
            assert calls[0]["url"] == f"https://google.serper.dev{path}"
            body = calls[0]["json"]
            assert body["q"] == "ai agents"
            assert body["gl"] == "us"
            assert body["hl"] == "en"
            assert body["tbs"] == "qdr:w"
            assert body["num"] == 7
            assert body["page"] == 2
            assert calls[0]["headers"]["X-API-KEY"] == "k"

    def test_optional_params_omitted_when_absent(self, monkeypatch):
        import services.v2_search.serper as serper

        calls: list = []
        monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker())
        install_fake_httpx(monkeypatch, [(200, {})], calls)
        asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        body = calls[0]["json"]
        for absent in ("gl", "hl", "tbs", "location"):
            assert absent not in body

    def test_normalize_web_organic(self, monkeypatch):
        import services.v2_search.serper as serper

        monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker())
        body = {
            "organic": [
                {
                    "title": "T1",
                    "link": "https://a.com",
                    "snippet": "S1",
                    "position": 1,
                    "sitelinks": [{"title": "x", "link": "y"}],
                },
                {"title": "T2", "link": "https://b.com", "position": 2},
            ],
            "credits": 1,
            "knowledgeGraph": {"title": "KG"},
        }
        install_fake_httpx(monkeypatch, [(200, body)], [])
        out = asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        assert out.total == 2
        assert out.results[0].url == "https://a.com"
        assert out.results[0].title == "T1"
        assert out.results[0].snippet == "S1"
        # Unmapped fields are preserved in extra (nothing silently dropped).
        assert out.results[0].extra.get("sitelinks")
        assert out.credits == 1
        assert out.knowledge_graph == {"title": "KG"}
        assert out.cost_cents == serper.SERPER_COST_CENTS

    def test_normalize_images_and_news_and_maps(self, monkeypatch):
        import services.v2_search.serper as serper

        monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker())

        install_fake_httpx(
            monkeypatch,
            [
                (
                    200,
                    {
                        "images": [
                            {
                                "title": "img",
                                "imageUrl": "https://i/x.png",
                                "thumbnailUrl": "https://i/t.png",
                                "link": "https://src.com",
                                "position": 1,
                            }
                        ]
                    },
                )
            ],
            [],
        )
        images = asyncio.run(
            serper.SerperAdapter(api_key="k").search(query="x", category="images")
        )
        assert images.results[0].image_url == "https://i/x.png"
        assert images.results[0].thumbnail_url == "https://i/t.png"
        assert images.results[0].url == "https://src.com"

        install_fake_httpx(
            monkeypatch,
            [
                (
                    200,
                    {
                        "news": [
                            {
                                "title": "n",
                                "link": "https://news.com/a",
                                "snippet": "ns",
                                "date": "1d ago",
                                "source": "NN",
                            }
                        ]
                    },
                )
            ],
            [],
        )
        news = asyncio.run(
            serper.SerperAdapter(api_key="k").search(query="x", category="news")
        )
        assert news.results[0].url == "https://news.com/a"
        assert news.results[0].source == "NN"
        assert news.results[0].date == "1d ago"

        install_fake_httpx(
            monkeypatch,
            [
                (
                    200,
                    {
                        "places": [
                            {
                                "title": "Cafe",
                                "address": "1 St",
                                "website": "https://cafe.com",
                                "rating": 4.5,
                            }
                        ]
                    },
                )
            ],
            [],
        )
        maps = asyncio.run(
            serper.SerperAdapter(api_key="k").search(query="x", category="maps")
        )
        assert maps.results[0].url == "https://cafe.com"
        assert maps.results[0].snippet == "1 St"
        assert maps.results[0].extra.get("rating") == 4.5

    def test_retry_on_5xx_then_success(self, monkeypatch):
        import services.v2_search.serper as serper

        breaker = _FakeBreaker()
        monkeypatch.setattr(serper, "circuit_breaker", breaker)
        _no_sleep(monkeypatch)
        install_fake_httpx(
            monkeypatch, [(503, {}), (200, {"organic": []})], []
        )
        out = asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        assert out.total == 0
        assert breaker.success == 1
        assert breaker.failure == 0

    def test_exhausted_retries_raises_unavailable(self, monkeypatch):
        import services.v2_search.serper as serper
        from services.v2_search.adapter import SearchUnavailableError

        breaker = _FakeBreaker()
        monkeypatch.setattr(serper, "circuit_breaker", breaker)
        _no_sleep(monkeypatch)
        install_fake_httpx(monkeypatch, [(500, {}), (500, {}), (500, {})], [])
        with pytest.raises(SearchUnavailableError):
            asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        assert breaker.failure == 1

    def test_transport_error_then_success(self, monkeypatch):
        import services.v2_search.serper as serper

        monkeypatch.setattr(serper, "circuit_breaker", _FakeBreaker())
        _no_sleep(monkeypatch)
        install_fake_httpx(
            monkeypatch,
            [httpx.ConnectError("boom"), (200, {"organic": []})],
            [],
        )
        out = asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        assert out.total == 0

    def test_auth_status_raises_config_error_no_retry(self, monkeypatch):
        import services.v2_search.serper as serper
        from services.v2_search.adapter import SearchConfigError

        breaker = _FakeBreaker()
        monkeypatch.setattr(serper, "circuit_breaker", breaker)
        calls: list = []
        install_fake_httpx(monkeypatch, [(403, {}), (200, {})], calls)
        with pytest.raises(SearchConfigError):
            asyncio.run(serper.SerperAdapter(api_key="bad").search(query="x"))
        assert len(calls) == 1  # not retried
        assert breaker.failure == 0  # credential fault does not trip breaker

    def test_non_retryable_status_raises_upstream(self, monkeypatch):
        import services.v2_search.serper as serper
        from services.v2_search.adapter import SearchUpstreamError

        breaker = _FakeBreaker()
        monkeypatch.setattr(serper, "circuit_breaker", breaker)
        calls: list = []
        install_fake_httpx(monkeypatch, [(400, {}), (200, {})], calls)
        with pytest.raises(SearchUpstreamError):
            asyncio.run(serper.SerperAdapter(api_key="k").search(query="x"))
        assert len(calls) == 1
        assert breaker.failure == 1


# ─── (d) lookup_flow orchestration ──────────────────────────────────────────


def _fake_results(urls: list[str]):
    from services.v2_search.adapter import SearchResult, SearchResults

    return SearchResults(
        provider="serper",
        category="web",
        query="ai agents",
        results=[
            SearchResult(title=f"T{i}", url=u, snippet="s", position=i + 1)
            for i, u in enumerate(urls)
        ],
        total=len(urls),
        credits=1,
        cost_cents=__import__("decimal").Decimal("0.06"),
    )


class _FakeAdapter:
    def __init__(self, results):
        self.results = results
        self.captured = None

    async def search(self, **kwargs):
        self.captured = kwargs
        return self.results


def _patch_flow_io(monkeypatch, adapter, perceive_calls):
    """Patch lookup_flow's side-effecting collaborators (adapter, usage,
    persistence, perceive_flow, quota) for hermetic flow tests."""
    import services.v2_engine.lookup_flow as flow
    from api.v2.schemas.perceive import PerceiveResponse

    monkeypatch.setattr(flow, "_adapter", lambda: adapter)

    bumped = {"lookup": 0}
    monkeypatch.setattr(
        flow.usage,
        "increment_lookup_usage",
        lambda project_id, count=1: bumped.__setitem__("lookup", bumped["lookup"] + count),
    )

    persisted = {"count": 0, "last": None}

    def _fake_persist(**kwargs):
        persisted["count"] += 1
        persisted["last"] = kwargs
        return 999

    monkeypatch.setattr(flow, "_persist_lookup_query", _fake_persist)

    async def _fake_perceive(request, operation_id, user, batch_id=None):
        perceive_calls.append({"url": request.url, "operation_id": operation_id})
        return PerceiveResponse(
            operation_id=operation_id, status="completed", url=request.url
        )

    monkeypatch.setattr(flow.perceive_flow, "run", _fake_perceive)
    monkeypatch.setattr(flow, "check_v2_quota", lambda *a, **k: None)
    return bumped, persisted


class TestLookupFlow:
    def test_request_params_reach_adapter(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest

        adapter = _FakeAdapter(_fake_results([]))
        _patch_flow_io(monkeypatch, adapter, [])
        req = LookupRequest(
            query="ai agents", category="news", country="in", locale="en",
            time_filter="day", num_results=5, page=1,
        )
        resp = asyncio.run(flow.run(req, make_user(SUB_ENABLED)))
        assert adapter.captured["category"] == "news"
        assert adapter.captured["country"] == "in"
        assert adapter.captured["time_filter"] == "day"
        assert adapter.captured["num"] == 5
        assert resp.total == 0
        assert resp.cost_cents == 0.06

    def test_results_mapped_without_perceive(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest

        adapter = _FakeAdapter(_fake_results(["https://a.com", "https://b.com"]))
        bumped, persisted = _patch_flow_io(monkeypatch, adapter, [])
        resp = asyncio.run(
            flow.run(LookupRequest(query="x"), make_user(SUB_ENABLED))
        )
        assert resp.total == 2
        assert [r.url for r in resp.results] == ["https://a.com", "https://b.com"]
        assert all(r.perceive is None for r in resp.results)
        assert resp.perceive_top == 0
        assert bumped["lookup"] == 1  # one query counted
        assert persisted["count"] == 1  # one audit row

    def test_perceive_top_runs_n_perceives(self, monkeypatch):
        """H.3 verification c: perceive_top=3 -> 3 perceive_flow.run calls,
        each result perceived, one lookup row."""
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest

        urls = ["https://a.com", "https://b.com", "https://c.com", "https://d.com"]
        adapter = _FakeAdapter(_fake_results(urls))
        perceive_calls: list = []
        bumped, persisted = _patch_flow_io(monkeypatch, adapter, perceive_calls)

        resp = asyncio.run(
            flow.run(
                LookupRequest(query="x", perceive_top=3), make_user(SUB_ENABLED)
            )
        )
        assert len(perceive_calls) == 3
        assert [c["url"] for c in perceive_calls] == urls[:3]
        assert resp.perceive_top == 3
        assert len(resp.perceive_operation_ids) == 3
        assert all(resp.results[i].perceive is not None for i in range(3))
        assert resp.results[3].perceive is None
        assert bumped["lookup"] == 1
        assert persisted["count"] == 1
        assert len(persisted["last"]["perceive_ids"]) == 3

    def test_auto_perceive_stops_when_quota_exhausted(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest

        adapter = _FakeAdapter(_fake_results(["https://a.com", "https://b.com"]))
        perceive_calls: list = []
        _patch_flow_io(monkeypatch, adapter, perceive_calls)

        # Allow the 1st perceive, deny the 2nd (perceive quota exhausted).
        state = {"n": 0}

        def _quota(user, counter, units=1):
            state["n"] += 1
            if counter == "perceive_operations" and state["n"] > 1:
                raise HTTPException(status_code=402, detail="quota")

        monkeypatch.setattr(flow, "check_v2_quota", _quota)
        resp = asyncio.run(
            flow.run(
                LookupRequest(query="x", perceive_top=2), make_user(SUB_ENABLED)
            )
        )
        assert len(perceive_calls) == 1
        assert resp.perceive_top == 1
        assert any("auto-perceive stopped" in w for w in resp.warnings)

    def test_perceive_skipped_when_no_navigable_url(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest
        from services.v2_search.adapter import SearchResult, SearchResults

        results = SearchResults(
            provider="serper", category="maps", query="x",
            results=[SearchResult(title="no-link", url=None)], total=1,
        )
        adapter = _FakeAdapter(results)
        perceive_calls: list = []
        _patch_flow_io(monkeypatch, adapter, perceive_calls)
        resp = asyncio.run(
            flow.run(
                LookupRequest(query="x", category="maps", perceive_top=2),
                make_user(SUB_ENABLED),
            )
        )
        assert perceive_calls == []
        assert resp.perceive_top == 0
        assert any("no result had a navigable URL" in w for w in resp.warnings)

    def test_provider_config_error_maps_to_503(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest
        from services.v2_search.adapter import SearchConfigError

        class _Boom:
            async def search(self, **kwargs):
                raise SearchConfigError("no key")

        monkeypatch.setattr(flow, "_adapter", lambda: _Boom())
        with pytest.raises(HTTPException) as exc:
            asyncio.run(flow.run(LookupRequest(query="x"), make_user(SUB_ENABLED)))
        assert exc.value.status_code == 503

    def test_provider_upstream_error_maps_to_502(self, monkeypatch):
        import services.v2_engine.lookup_flow as flow
        from api.v2.schemas.lookup import LookupRequest
        from services.v2_search.adapter import SearchUpstreamError

        class _Boom:
            async def search(self, **kwargs):
                raise SearchUpstreamError("bad gateway")

        monkeypatch.setattr(flow, "_adapter", lambda: _Boom())
        with pytest.raises(HTTPException) as exc:
            asyncio.run(flow.run(LookupRequest(query="x"), make_user(SUB_ENABLED)))
        assert exc.value.status_code == 502


# ─── (e) Handler contract ───────────────────────────────────────────────────


def _build_app(user: dict):
    from api.deps import get_current_user
    from api.v2.handlers.lookup import router as lookup_router

    app = FastAPI()
    app.include_router(lookup_router, prefix="/v2")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


class TestLookupHandler:
    def test_disabled_plan_returns_402(self):
        app = _build_app(make_user(SUB_DISABLED))
        client = TestClient(app)
        resp = client.post("/v2/lookup", json={"query": "ai agents"})
        assert resp.status_code == 402

    def test_empty_query_returns_422(self):
        app = _build_app(make_user(SUB_ENABLED))
        client = TestClient(app)
        resp = client.post("/v2/lookup", json={"query": "  "})
        assert resp.status_code == 422

    def test_success_returns_section_4_shape(self, monkeypatch):
        import api.v2.handlers.lookup as handler
        from api.v2.schemas.lookup import LookupResponse, LookupResult

        async def fake_run(body, user):
            return LookupResponse(
                lookup_id=5,
                query=body.query,
                category=body.category,
                total=1,
                results=[LookupResult(title="T", url="https://a.com")],
                perceive_top=0,
                cost_cents=0.06,
            )

        async def fake_log_start(**kwargs):
            return 777

        async def fake_update(*args, **kwargs):
            return None

        monkeypatch.setattr(handler.lookup_flow, "run", fake_run)
        monkeypatch.setattr(handler, "log_activity_start", fake_log_start)
        monkeypatch.setattr(handler, "update_activity_status", fake_update)

        app = _build_app(make_user(SUB_ENABLED))
        client = TestClient(app)
        resp = client.post(
            "/v2/lookup", json={"query": "ai agents", "category": "web"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["url"] == "https://a.com"
        assert body["category"] == "web"
        assert body["cost_cents"] == 0.06
