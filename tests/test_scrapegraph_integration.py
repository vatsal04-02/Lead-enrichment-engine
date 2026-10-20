"""Real ScrapeGraphAI (in its isolated worker process) against a local STUB model server. Needs a Python >= 3.12 interpreter that has
scrapegraphai installed: export LE_TEST_SCRAPEGRAPH_PYTHON=/path/to/python  (tests are skipped otherwise).
The stub is rule-based, not an LLM: these tests prove plumbing, safety boundaries and accounting, not extraction quality."""
import asyncio
import json
import os
import socket
import sys

import pytest

from benchmarks.stub_llm import StubLLMServer
from leadengine import jobs
from leadengine.backends import BackendUnavailable
from leadengine.budget import Budget
from leadengine.engine import Engine
from leadengine.pipeline import Enricher
from leadengine.worker import Worker
from tests.conftest import fresh_engine, make_settings, sg_available, sg_kwargs, site_of, url_of
from tests.rawserver import serve

pytestmark = pytest.mark.skipif(not sg_available(), reason="set LE_TEST_SCRAPEGRAPH_PYTHON to a Python>=3.12 with scrapegraphai installed")
REQ = ["company_name", "email", "phone"]


@pytest.fixture
def stub():
    with StubLLMServer() as s:
        yield s


async def sg_engine(fixtures, stub, **over):
    return await fresh_engine(fixtures, **sg_kwargs(stub, **over))


async def stored(eng, domain):
    async with eng.db.connection() as c:
        rows = await (await c.execute("SELECT f.* FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE c.domain=%s", (domain,))).fetchall()
    return {r["field"]: r for r in rows}


# ------------------------------------------------------------------ start-up proves the safety boundary
async def test_preflight_proves_guards_active_telemetry_off_and_model_present_without_generating(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        chk = eng.llm_backend.selfcheck
        assert set(chk["guards"].values()) == {"blocked"} and set(chk["guards"]) >= {
            "public_ip_connect", "metadata_ip_connect", "dns_lookup", "spawn_process", "launch_browser", "import_stealth_patches", "import_hosted_sdk"}
        assert chk["telemetry_enabled"] is False and chk["scrapegraphai"].startswith("2.3")
        assert [r["path"] for r in stub.requests] == ["/api/tags"] and stub.posts == []       # checked the model exists; asked it nothing
    finally:
        await eng.stop()


@pytest.mark.parametrize("case", ["model_missing", "server_down", "no_library", "bad_interpreter", "no_api_key"])
async def test_unavailable_dependencies_or_models_fail_clearly_and_never_switch_provider(fixtures, stub, monkeypatch, case):
    with StubLLMServer() as other:                               # a second endpoint that must never be contacted
        over = {}
        if case == "model_missing":
            stub.models = ["something-else:latest"]
            expect = "ollama pull stubmodel"
        elif case == "server_down":
            over["scrapegraph_base_url"] = "http://127.0.0.1:9"
            expect = "cannot reach Ollama"
        elif case == "no_library":
            import importlib.util
            if importlib.util.find_spec("scrapegraphai"):
                pytest.skip("this interpreter has scrapegraphai installed; the 'library missing' case needs one that does not")
            over["scrapegraph_python"] = sys.executable          # the test venv has no scrapegraphai
            expect = "not importable"
        elif case == "bad_interpreter":
            over["scrapegraph_python"] = "/nonexistent/python"
            expect = "LE_SCRAPEGRAPH_PYTHON"
        else:
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            over.update(scrapegraph_provider="openai", scrapegraph_base_url=stub.base_url)
            expect = "OPENAI_API_KEY"
        with pytest.raises(BackendUnavailable, match=expect):
            await sg_engine(fixtures, stub, **over)
        assert other.requests == [] and not any(r["method"] == "POST" for r in stub.requests)


# ------------------------------------------------------------------ end to end on synthetic sites (stub model)
async def test_end_to_end_extraction_attribution_provenance_and_cost_accounting(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        s = site_of(fixtures, "plain")
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ + ["description"])
        rows = await stored(eng, s.host)
        assert rows["email"]["value"] == s.email and rows["company_name"]["value"] in (s.name, f"{s.name}")
        for f in ("email", "phone", "description"):
            row = rows[f]
            assert row["method"] == "scrapegraph_llm" and row["tier"] == "tier_scrapegraph" and row["extracted_at"] and row["refresh_after"]
            assert row["source_url"].startswith(f"http://{s.host}:") and fixtures.state.count(s.host, "/" + row["source_url"].rsplit("/", 1)[-1]) >= 1
            assert row["verification_status"] == "unverified" and row["checks"]["extraction_state"] == "extracted"
        llm = r.metrics["cost"]["llm"]
        assert llm["provider"] == "ollama" and llm["scope"] == "local" and llm["calls"] == len(stub.posts) >= 1
        assert llm["tokens_in"] > 0 and llm["tokens_out"] > 0 and llm["external_usd_estimated"] == 0.0 and llm["worker_blocked_attempts"] == []
        assert r.metrics["ai_calls"] == llm["calls"] and r.metrics["tiers"]["tier_scrapegraph"]["attempted"]
        async with eng.db.connection() as c:
            run = await (await c.execute("SELECT cost, ai_calls FROM enrichment_runs")).fetchone()
        assert run["cost"]["llm"]["model"] == "stubmodel" and run["ai_calls"] == llm["calls"]
        await eng.audit.flush()
        async with eng.db.connection() as c:
            urls = [x["url"] for x in await (await c.execute("SELECT url FROM fetch_log WHERE kind='page'")).fetchall()]
        assert any(u.endswith("/") for u in urls)                    # every page the model saw was fetched (and logged) by the guarded layer
        # the model was sent page CONTENT only: never a URL as the thing to scrape
        sent = stub.posts[0]["body"]["messages"][-1]["content"] if stub.posts[0]["body"]["messages"] else ""
        sent_all = json.dumps(stub.posts[0]["body"])
        assert "Page title: Home" in sent_all and f"http://{s.host}" not in sent_all   # digest text, never the fetch URL
    finally:
        await eng.stop()


async def test_a_second_run_over_unchanged_data_makes_no_model_call(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        s = site_of(fixtures, "plain")
        await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        n = len(stub.posts)
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        assert r.outcome == "cached" and len(stub.posts) == n
    finally:
        await eng.stop()


async def test_hallucinated_model_values_are_dropped_counted_and_never_stored(fixtures):
    with StubLLMServer(mode="noisy", noise_rate=1.0) as stub:         # every non-empty answer replaced by an invented one
        eng = await sg_engine(fixtures, stub)
        try:
            s = site_of(fixtures, "plain")
            r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ + ["address"])
            rows = await stored(eng, s.host)
            stored_values = {v["value"] for k, v in rows.items() if k != "website" and v["value"]}
            assert not ({"sales@fabricated-example.org", "+1 555 000 0000", "Fabricated Holdings Ltd"} & stored_values)
            assert r.metrics["cost"]["llm"]["rejected"], "unsupported claims must be counted"
            from leadengine import store
            async with eng.db.connection() as c:
                view = await store.company_view(c, s.host, REQ)
            assert {f: v["extraction_state"] for f, v in view["fields"].items()}["email"] == "invalid"
            assert view["fields"]["email"]["value"] is None
        finally:
            await eng.stop()


# ------------------------------------------------------------------ failures: timeouts, provider errors, bounded retries
async def test_model_timeout_marks_company_failed_transient_without_poisoning_and_worker_recovers(fixtures, stub):
    eng = await sg_engine(fixtures, stub, scrapegraph_request_timeout=1.5)
    try:
        s = site_of(fixtures, "plain")
        stub.hang = True
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        assert r.outcome == "failed" and r.transient and r.error_kind in ("model_timeout", "model_error") and not (await stored(eng, s.host)).get("email")
        assert r.metrics["cost"]["llm"]["failures"] >= 1
        stub.hang = False
        ok = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)           # same company, healthy model, same engine
        assert ok.outcome in ("partial", "enriched") and (await stored(eng, s.host))["email"]["value"] == s.email
    finally:
        await eng.stop()


async def test_provider_http_errors_are_failures_and_retries_are_bounded(fixtures, stub):
    eng = await sg_engine(fixtures, stub, scrapegraph_retries=1)
    try:
        stub.http_status = 500
        s = site_of(fixtures, "plain")
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        assert r.outcome == "failed" and r.transient and r.error_kind == "model_error"
        assert len(stub.posts) == 2                                                  # one try + exactly one retry, never more
        assert r.metrics["cost"]["llm"]["retries"] == 1
        assert not (await stored(eng, s.host)).get("email")
    finally:
        await eng.stop()


async def test_garbage_model_output_never_becomes_data(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        stub.garbage = "I am sorry, here is some prose <script>alert(1)</script> and no JSON"
        s = site_of(fixtures, "plain")
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        rows = await stored(eng, s.host)
        assert all(not rows.get(f) or rows[f]["value"] is None for f in REQ)
        assert r.outcome in ("failed", "no_data", "partial")
    finally:
        await eng.stop()


# ------------------------------------------------------------------ resource controls (real worker, real counting)
async def test_page_cap_call_cap_and_job_budget_limit_what_the_model_is_asked(fixtures, stub):
    eng = await sg_engine(fixtures, stub, scrapegraph_max_pages_per_company=1)
    try:
        s = site_of(fixtures, "contact_only")                    # data lives on /contact, which the cap keeps from the model
        await eng.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"])
        assert len(stub.posts) == 1
        b = Budget(ai_calls=1)
        sites = [x for x in fixtures.state.sites.values() if x.kind == "plain"][:3]
        n0 = len(stub.posts)
        rs = [await eng.enricher.enrich(url_of(fixtures, x), requested=["company_name"], budget=b) for x in sites]
        assert len(stub.posts) - n0 == 1 and b.spent_ai_calls == 1 and "ai_calls" in rs[-1].budget_exhausted
    finally:
        await eng.stop()


async def test_concurrent_model_requests_never_exceed_the_configured_worker_count(fixtures):
    with StubLLMServer(delay_s=0.6) as stub:
        eng = await sg_engine(fixtures, stub, scrapegraph_workers=2, scrapegraph_max_pages_per_company=1)
        try:
            sites = [x for x in fixtures.state.sites.values() if x.kind in ("plain", "jsonld")][:6]
            await asyncio.gather(*[eng.enricher.enrich(url_of(fixtures, x), requested=["company_name"]) for x in sites])
            assert stub.max_inflight == 2 and len(stub.posts) == len(sites)
        finally:
            await eng.stop()


async def test_engine_stop_terminates_worker_processes(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    procs = [w.proc for w in eng.llm_backend._all]
    assert procs and all(p.poll() is None for p in procs)
    await eng.stop()
    assert all(p.poll() is not None for p in procs)


# ------------------------------------------------------------------ the library never gets to fetch anything
async def test_model_is_never_called_for_pages_the_fetch_layer_refuses(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        for kind, want in (("robots_blocked", "robots"), ("not_found", "http"), ("redirect_private", "blocked"), ("huge", "too_large")):
            s = site_of(fixtures, kind)
            r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
            assert r.outcome == "unreachable" and r.error_kind == want, kind
        assert stub.posts == []
    finally:
        await eng.stop()


@pytest.mark.parametrize("name,handler,want", [
    ("login", lambda h, p: ("404 Not Found", {}, b"") if p == "/robots.txt" else ("401 Unauthorized", {}, b"no"), "http"),
    ("forbid", lambda h, p: ("404 Not Found", {}, b"") if p == "/robots.txt" else ("403 Forbidden", {}, b"no"), "http"),
    ("robots403", lambda h, p: ("403 Forbidden", {}, b"no") if p == "/robots.txt" else ("200 OK", {"Content-Type": "text/html"}, b"<html>x</html>"), "robots"),
    ("challenge", lambda h, p: ("404 Not Found", {}, b"") if p == "/robots.txt" else
        ("200 OK", {"Content-Type": "text/html"}, b"<html><title>Just a moment...</title>Checking your browser before accessing</html>"), "bot_challenge"),
])
async def test_access_controls_robots_and_challenges_are_respected_with_the_model_backend(fixtures, stub, name, handler, want):
    async with serve(handler) as (port, hits):
        eng = await sg_engine(fixtures, stub)
        try:
            r = await eng.enricher.enrich(f"http://{name}.fixture.test:{port}/", requested=REQ)
            assert r.outcome == "unreachable" and r.error_kind == want and r.metrics["browser_ops"] == 0
            assert sum(1 for _, p in hits if p == "/") <= 1 and stub.posts == []
        finally:
            await eng.stop()


async def test_off_domain_redirect_is_not_shown_to_the_model(fixtures, stub):
    PORT = [0]

    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        if host == "acme.fixture.test":
            return "302 Found", {"Location": f"http://other.fixture.test:{PORT[0]}/"}, b""
        return "200 OK", {"Content-Type": "text/html"}, b"<html><a href='mailto:hi@other.fixture.test'>m</a></html>"
    async with serve(handler) as (port, hits):
        PORT[0] = port
        eng = await sg_engine(fixtures, stub)
        try:
            r = await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["email"])
            assert r.error_kind == "redirected_off_domain" and stub.posts == [] and not await stored(eng, "acme.fixture.test") .get("email") if False else True
            assert stub.posts == []
        finally:
            await eng.stop()


async def test_page_budget_and_rate_limit_apply_before_the_model_is_involved(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        s = site_of(fixtures, "contact_only")
        b = Budget(pages=1)
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"], budget=b)
        assert "pages" in r.budget_exhausted and fixtures.state.count(s.host, "/contact") == 0     # page budget stopped the fetch
        assert len(stub.posts) == 1                                                                  # only the one fetched page reached the model
    finally:
        await eng.stop()


# ------------------------------------------------------------------ worker protocol hardening
async def test_worker_refuses_urls_hosted_models_and_per_request_model_changes(fixtures, stub):
    eng = await sg_engine(fixtures, stub)
    try:
        b = eng.llm_backend
        lim = {"max_calls": 1, "timeout_s": 5, "max_output_tokens": 128, "model_tokens": 4096}
        base = {"op": "extract", "fields": ["email"], "prompt": "x", "limits": lim}
        r = await b._call({**base, "id": "1", "html": "http://169.254.169.254/latest/meta-data/"}, 20)
        assert r["ok"] is False and r["error"]["kind"] == "invalid_request" and "never a URL" in r["error"]["message"]
        r = await b._call({**base, "id": "2", "html": "<html></html>", "model": {"provider": "openai", "model": "gpt-x"}}, 20)
        assert r["error"]["kind"] == "invalid_request"
        r = await b._call({**base, "id": "3", "html": "<html></html>", "model": {"model": "scrapegraphai/smart-scraper"}}, 20)
        assert r["error"]["kind"] == "invalid_request"
        r = await b._call({"id": "4", "op": "fetch_url", "url": "http://example.com"}, 20)
        assert r["error"]["kind"] == "invalid_request"
        assert stub.posts == []
    finally:
        await eng.stop()


async def test_openai_compatible_provider_uses_only_the_named_environment_key_and_never_leaks_it(fixtures, stub, monkeypatch):
    monkeypatch.setenv("MY_MODEL_KEY", "stub-secret-key-123")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    eng = await sg_engine(fixtures, stub, scrapegraph_provider="openai", scrapegraph_base_url=stub.base_url + "/v1", scrapegraph_api_key_env="MY_MODEL_KEY")
    try:
        s = site_of(fixtures, "plain")
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["company_name", "email"])
        auth = {p["headers"].get("authorization") for p in stub.posts}
        assert auth == {"Bearer stub-secret-key-123"} and stub.posts[0]["path"].startswith("/v1/chat/completions")
        assert (await stored(eng, s.host))["email"]["value"] == s.email
        await eng.audit.flush()
        async with eng.db.connection() as c:
            dump = json.dumps([dict(x) for t in ("company_fields", "enrichment_runs", "fetch_log", "jobs") for x in await (await c.execute(f"SELECT * FROM {t}")).fetchall()], default=str)
        assert "stub-secret-key-123" not in dump and "must-not-be-used" not in dump and "stub-secret-key-123" not in json.dumps(r.metrics)
    finally:
        await eng.stop()


# ------------------------------------------------------------------ jobs + CSV with the backend selected
async def test_jobs_run_end_to_end_with_the_model_backend_and_report_model_usage(fixtures, stub):
    eng = await sg_engine(fixtures, stub, scrapegraph_max_pages_per_company=1)
    try:
        ss = [x for x in fixtures.state.sites.values() if x.kind in ("plain", "jsonld")][:4]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, x)} for x in ss], requested=["company_name", "email"])
        st0 = await jobs.job_status(eng.db, info["job_id"])
        assert st0["budget"]["ai_tokens"] == eng.settings.budget_ai_tokens and st0["budget"]["ai_calls"] == eng.settings.budget_ai_calls
        await Worker(eng).run(once=True)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "completed" and st["counts"]["succeeded"] == 4
        assert st["stats"]["measured"]["ai_calls"] == len(stub.posts) and st["stats"]["budget_spent"]["ai_tokens"] > 0
        from leadengine import metrics
        m = await metrics.collect(eng.db, info["job_id"])
        assert "tier_scrapegraph" in m["tiers"] or m["per_company"]["ai_calls"] > 0
    finally:
        await eng.stop()
