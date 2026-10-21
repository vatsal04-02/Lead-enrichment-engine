"""Discovery -> enrichment through the REAL ScrapeGraphAI worker, with a stub Ollama-compatible server standing in for qwen3:4b.
The stub is a rule-based responder, not a language model: these tests prove the wiring (preflight, thinking-off, provenance, fallback),
not model quality."""
import json
import socket
from pathlib import Path

import pytest

from benchmarks.stub_llm import StubLLMServer
from leadengine.discovery.pipeline import execute
from leadengine.discovery.sources.csv_import import CsvImportSource
from tests.conftest import SG_PYTHON, fresh_engine, sg_available, site_of, url_of
from tests.test_discovery_integration import csv_of, plan_for, read_rows

pytestmark = pytest.mark.skipif(not sg_available(), reason="set LE_TEST_SCRAPEGRAPH_PYTHON to a Python>=3.12 with scrapegraphai installed")


@pytest.fixture
async def clean_db(fixtures):
    e = await fresh_engine(fixtures)
    await e.stop()


def plan_qwen(tmp_path, urls, base_url, mode="auto", **kw):
    kw = {"sg_python": SG_PYTHON, "request_timeout": 30, **kw}
    return plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, urls))], backend_mode=mode, model="qwen3:4b", base_url=base_url, **kw)


async def test_qwen3_through_scrapegraph_records_backend_thinking_off_and_provenance(fixtures, clean_db, tmp_path):
    plain = site_of(fixtures, "plain", 0)
    with StubLLMServer() as stub:
        stub.models = ["qwen3:4b"]
        res = await execute(plan_qwen(tmp_path, [url_of(fixtures, plain)], stub.base_url))
        row = read_rows(res)[plain.host]
        assert "ScrapeGraphAI with local Ollama model `qwen3:4b`" in res["primary_note"]
        for f in ("company_name", "email", "phone"):
            assert (row[f + "_backend"], row[f + "_method"], row[f + "_status"]) == ("scrapegraph", "scrapegraph_llm", "extracted")
            assert row[f + "_source_url"].startswith(f"http://{plain.host}:")
        assert row["email"] == plain.email and row["backend_used"] == "scrapegraph" and row["fallback_used"] == "no"
        posts = [p["body"] for p in stub.posts]
        assert posts and all(b["model"] == "qwen3:4b" and b.get("think") is False for b in posts), "thinking must be off for qwen3"
        assert {r["path"].split("?")[0] for r in stub.requests} <= {"/api/tags", "/api/chat"}, "only list + chat: no pull"
        rep = (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")
        assert "model `qwen3:4b` via ollama (local)" in rep and "paid API cost: 0" in rep and "not priced" in rep


@pytest.mark.parametrize("case", ["model_not_installed", "server_down", "bad_interpreter"])
async def test_auto_mode_falls_back_to_inhouse_when_the_model_path_cannot_start_and_strict_mode_refuses(fixtures, clean_db, tmp_path, case):
    s = site_of(fixtures, "jsonld", 0)
    with StubLLMServer() as stub:
        stub.models = ["something-else:latest"]
        base = stub.base_url
        if case == "server_down":
            sock = socket.socket(); sock.bind(("127.0.0.1", 0)); base = f"http://127.0.0.1:{sock.getsockname()[1]}"; sock.close()
        kw = {"sg_python": "/nonexistent/python"} if case == "bad_interpreter" else {}
        res = await execute(plan_qwen(tmp_path, [url_of(fixtures, s)], base, **kw))
        row = read_rows(res)[s.host]
        assert "was NOT available" in res["primary_note"] and row["backend_used"] == "inhouse" and row["fallback_used"] == "yes"
        assert row["company_name"] == s.name and row["company_name_backend"] == "inhouse"
        errs = [json.loads(l) for l in (Path(res["out_dir"]) / "errors.jsonl").read_text().splitlines()]
        assert any(e["kind"] == "primary_backend_unavailable" for e in errs)
        assert not any(r["method"] == "POST" for r in stub.requests), "nothing was generated"
        if case == "model_not_installed":
            assert "ollama pull qwen3:4b" in res["primary_note"]
        from leadengine.backends import BackendUnavailable
        with pytest.raises(BackendUnavailable):
            await execute(plan_qwen(tmp_path, [url_of(fixtures, s)], base, mode="scrapegraph", **kw))


async def test_model_unavailable_mid_run_falls_back_per_company_and_the_failure_is_in_the_audit_trail(fixtures, clean_db, tmp_path):
    a, b = site_of(fixtures, "jsonld", 0), site_of(fixtures, "jsonld", 1)
    with StubLLMServer() as stub:
        stub.models, stub.http_status = ["qwen3:4b"], 500           # tags fine, every generation call fails
        res = await execute(plan_qwen(tmp_path, [url_of(fixtures, a), url_of(fixtures, b)], stub.base_url))
        rows = read_rows(res)
        for s in (a, b):
            r = rows[s.host]
            assert r["fallback_used"] == "yes" and "model_error" in r["fallback_reason"] and r["backend_used"] == "inhouse"
            assert r["company_name"] == s.name and r["attempts"].startswith("scrapegraph:failed[model_error] -> inhouse:")
        assert res["leads_detail"][0].metrics[0]["cost"]["llm"]["failures"] >= 1


def test_doctor_smoke_runs_one_local_extraction_on_a_builtin_synthetic_page():
    from leadengine.discovery.doctor import run_doctor
    with StubLLMServer() as stub:
        stub.models = ["qwen3:4b"]
        r = run_doctor(model="qwen3:4b", base_url=stub.base_url, python=SG_PYTHON, smoke=True)
        smoke = next(i for i in r["checks"] if i["check"].startswith("smoke"))
        assert r["ready_for_scrapegraph_ollama"] is True and smoke["ok"], smoke
        assert json.loads(smoke["detail"])["states"]["email"] == "extracted"
        assert {(q["method"], q["path"].split("?")[0]) for q in stub.requests} <= {("GET", "/api/tags"), ("POST", "/api/chat")}
        assert all(p["body"].get("think") is False for p in stub.posts)
