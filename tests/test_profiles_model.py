"""Profile enrichment through the REAL ScrapeGraphAI worker, with a stub Ollama-compatible server standing in for qwen3:4b (a rule-based
responder, NOT a language model). These prove the wiring and the safety behaviour; they say nothing about qwen3:4b's extraction quality."""
import json
import socket

import pytest

from benchmarks.stub_llm import StubLLMServer, profile_answer
from leadengine.backends import BackendUnavailable
from leadengine.profiles.runner import ProfileConfig, run_profiles
from leadengine.profiles.schema import SCHEMA
from tests.conftest import SG_PYTHON, make_settings, sg_available
from tests.profile_sites import GOOD, handler_for
from tests.rawserver import serve
from tests.test_profiles import read_out, url, write_csv

pytestmark = pytest.mark.skipif(not sg_available(), reason="set LE_TEST_SCRAPEGRAPH_PYTHON to a Python>=3.12 with scrapegraphai installed")


def cfg(tmp_path, src, base_url, **kw) -> ProfileConfig:
    base = dict(input_path=src, output_path=str(tmp_path / "out.csv"), base_url=base_url, sg_python=SG_PYTHON, model_timeout=30.0, retries=1,
                settings=make_settings(http_max_retries=0), allow_synthetic=True)
    base.update(kw)
    return ProfileConfig(**base)


def localhost_url(stub) -> str:
    return stub.base_url.replace("127.0.0.1", "localhost")          # the URL form the user configures: http://localhost:11434


async def test_qwen3_profile_run_through_the_real_worker_sends_the_right_request_and_grounds_the_answer(tmp_path):
    counts = {}
    async with serve(handler_for({"good.fixture.test": GOOD}, counts=counts)) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["Good Co", url(port, "good")]])
        with StubLLMServer(answer=profile_answer) as stub:
            stub.models = ["qwen3:4b"]
            res = await run_profiles(cfg(tmp_path, src, localhost_url(stub)))
            posts = [p["body"] for p in stub.posts]
            paths = {(r["method"], r["path"].split("?")[0]) for r in stub.requests}
    r = read_out(res["output"])[0]
    assert r["status"] == "ok" and r["company_name"] == "Lucknow Flow Works" and r["confidence"] == "high" and r["backend"] == "scrapegraph"
    assert r["products_services"] == "WhatsApp order automation; Workflow design; CRM integration"
    assert json.loads(r["field_support"])["ai_automation_opportunities"] == "inferred" and r["pain_points"]
    assert len(r["evidence_urls"].split(" | ")) >= 3 and r["pages_used"].count("(") == 4
    assert len(posts) == 1, "one model call per company"
    b = posts[0]
    assert b["model"] == "qwen3:4b" and b["think"] is False, "qwen3 must answer, not deliberate"
    assert b["options"]["num_ctx"] == 8192 and b["options"]["num_predict"] == 1400 and b["options"]["temperature"] == 0
    props = b["format"]["properties"]
    assert set(props) == set(SCHEMA) and any("array" in json.dumps(props[k]) for k in ("products_services", "pain_points", "ai_automation_opportunities"))
    assert "string" in json.dumps(props["business_description"]) and "array" not in json.dumps(props["company_name"])
    assert paths <= {("GET", "/api/tags"), ("POST", "/api/chat")}, "list + chat only: the model is never downloaded"
    assert res["model_usage"]["calls"] == 1 and res["model_usage"]["tokens_in"] > 0 and res["model_usage"]["tokens_out"] > 0
    assert "paid API cost: 0" in res["note"]
    assert [p for _, p in counts if p == "/robots.txt"] and ("good.fixture.test", "/about") in counts and ("good.fixture.test", "/services") in counts


async def test_ollama_not_running_stops_the_run_before_any_website_is_contacted(tmp_path):
    counts = {}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead = f"http://localhost:{sock.getsockname()[1]}"
    sock.close()
    async with serve(handler_for({"good.fixture.test": GOOD}, counts=counts)) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["Good Co", url(port, "good")]])
        with pytest.raises(BackendUnavailable, match="cannot reach Ollama"):
            await run_profiles(cfg(tmp_path, src, dead))
    assert counts == {} and not (tmp_path / "out.csv").exists()


async def test_model_not_installed_names_the_pull_command_and_nothing_is_downloaded_or_fetched(tmp_path):
    counts = {}
    async with serve(handler_for({"good.fixture.test": GOOD}, counts=counts)) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["Good Co", url(port, "good")]])
        with StubLLMServer() as stub:
            stub.models = ["llama3.2:latest"]
            with pytest.raises(BackendUnavailable, match="ollama pull qwen3:4b"):
                await run_profiles(cfg(tmp_path, src, localhost_url(stub)))
            assert not any(r["method"] == "POST" for r in stub.requests)
    assert counts == {}


async def test_model_http_errors_fail_each_row_but_the_batch_completes(tmp_path):
    async with serve(handler_for({"a.fixture.test": GOOD, "b.fixture.test": GOOD})) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["A", url(port, "a")], ["B", url(port, "b")]])
        with StubLLMServer(answer=profile_answer) as stub:
            stub.models = ["qwen3:4b"]
            stub.http_status = 500
            res = await run_profiles(cfg(tmp_path, src, localhost_url(stub)))
            n_posts = len(stub.posts)
    rows = read_out(res["output"])
    assert [r["status"] for r in rows] == ["failed", "failed"] and all("model_error" in r["error"] for r in rows)
    assert res["model_usage"]["retries"] == 2 and n_posts == 4, "exactly one bounded retry per company: 2 attempts x 2 companies"
    assert res["status_counts"] == {"failed": 2} and res["model_unavailable_mid_run"] is None


async def test_a_model_that_hangs_times_out_that_company_without_hanging_the_run(tmp_path):
    async with serve(handler_for({"a.fixture.test": GOOD})) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["A", url(port, "a")]])
        with StubLLMServer(answer=profile_answer) as stub:
            stub.models = ["qwen3:4b"]
            stub.hang = True
            res = await run_profiles(cfg(tmp_path, src, localhost_url(stub), model_timeout=3.0, retries=0))
    r = read_out(res["output"])[0]
    assert r["status"] == "failed" and "model_timeout" in r["error"] and res["model_usage"]["timeouts"] == 1 and res["seconds"] < 60


async def test_a_hallucinating_model_cannot_put_invented_facts_in_the_csv(tmp_path):
    invented = {k: ("Quantum Cloud Ltd" if SCHEMA[k] == "str" else ["Blockchain farming"]) for k in SCHEMA}
    invented.update({"description_quote": "We are the world leader in quantum cloud farming", "model_confidence": "high"})
    async with serve(handler_for({"a.fixture.test": GOOD})) as (port, hits):
        src = write_csv(tmp_path / "in.csv", ["Company", "Website"], [["A", url(port, "a")]])
        with StubLLMServer(answer=lambda fields, digest, text: invented) as stub:
            stub.models = ["qwen3:4b"]
            res = await run_profiles(cfg(tmp_path, src, localhost_url(stub)))
    r = read_out(res["output"])[0]
    assert r["status"] == "no_evidence" and r["confidence"] == "none" and r["model_confidence"] == "high"
    assert not any(bad in json.dumps(r) for bad in ("Quantum", "Blockchain")), "nothing the model invented may reach the output"
    assert set(json.loads(r["field_support"]).values()) <= {"unsupported", "missing"}


def test_cli_check_reports_connectivity_and_model_availability_and_never_pulls(capsys):
    from leadengine.profiles import cli
    with StubLLMServer(answer=profile_answer) as stub:
        stub.models = ["qwen3:4b"]
        assert cli.main(["--check", "--ollama-url", localhost_url(stub), "--sg-python", SG_PYTHON]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ready_for_scrapegraph_ollama"] and out["model"] == "qwen3:4b"
        stub.models = []
        assert cli.main(["--check", "--ollama-url", localhost_url(stub), "--sg-python", SG_PYTHON]) == 1
        assert "ollama pull qwen3:4b" in capsys.readouterr().out
        assert {(r["method"], r["path"].split("?")[0]) for r in stub.requests} == {("GET", "/api/tags")}
