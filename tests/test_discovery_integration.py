"""Discovery engine against LOCAL servers only (fixture sites, a raw listing-page server, an Overpass stand-in). Nothing here
contacts a real website; fixture hosts are 'synthetic' and the report says so."""
import csv
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from leadengine import jobs
from leadengine.backends import BackendUnavailable
from leadengine.csv_io import CSVReader
from leadengine.discovery import outputs
from leadengine.discovery.discover import discover
from leadengine.discovery.models import SRC_CSV
from leadengine.discovery.pipeline import RunPlan, SYNTH_LABEL, execute
from leadengine.discovery.sources.csv_import import CsvImportSource
from leadengine.discovery.sources.overpass import OverpassSource
from leadengine.discovery.sources.seed_pages import SeedPagesSource
from leadengine.fetcher import Fetcher
from leadengine.pipeline import Enricher
from tests.conftest import fresh_engine, make_settings, site_of, url_of
from tests.rawserver import serve
from tests.test_scrapegraph_unit import FakeBackend


@pytest.fixture
async def clean_db(fixtures):
    e = await fresh_engine(fixtures)
    await e.stop()


def csv_of(tmp_path: Path, urls: list[str], names: list[str] = None) -> str:
    p = tmp_path / "cands.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Company", "Website"])
        for i, u in enumerate(urls):
            w.writerow([(names or [""] * len(urls))[i], u])
    return str(p)


def plan_for(tmp_path, sources, **kw) -> RunPlan:
    base = dict(command="run", sources=sources, settings=make_settings(), niches=["AI automation agency"], locations=[],
                out_root=str(tmp_path / "out"), authorized=True, mode_label=SYNTH_LABEL, allow_synthetic=True, backend_mode="inhouse", limit=50)
    base.update(kw)
    return RunPlan(**base)


class SiteBackend(FakeBackend):
    """Fake model backend whose answers depend on the site (keyed by host)."""

    def __init__(self, by_host, **kw):
        super().__init__(**kw)
        self.by_host = by_host

    async def extract_page(self, page, fields, max_calls=None):
        self.answers = {"*": self.by_host.get(urlsplit(page.url).hostname, {})}
        return await super().extract_page(page, fields, max_calls)


def primary_with(backend, note="test primary (in-process fake model backend)"):
    async def factory(engine, plan):
        return Enricher(engine.settings, engine.db, engine.fetcher, None, None, None, llm_backend=backend), backend, note
    return factory


def truth(s):
    return {"company_name": s.name, "email": s.email, "phone": s.phone, "description": s.description}


def read_rows(res) -> dict:
    with open(res["leads_csv"], newline="", encoding="utf-8-sig") as f:
        return {r["domain"]: r for r in csv.DictReader(f)}


# ------------------------------------------------------------------ seed pages
async def test_seed_page_source_extracts_external_company_links_with_provenance_and_skips_the_rest(fixtures, tmp_path):
    s1, s2, s3 = site_of(fixtures, "jsonld", 0), site_of(fixtures, "jsonld", 1), site_of(fixtures, "plain", 0)

    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n"
        if path == "/list":
            body = (f"<html><body><h2>AI automation agencies</h2><ul>"
                    f'<li><a href="{url_of(fixtures, s1)}">Flow Labs Pvt</a> - workflow automation</li>'
                    f'<li><a href="{url_of(fixtures, s2)}">Visit website</a></li>'
                    f'<li><a href="{url_of(fixtures, s1)}about?utm=x">Flow Labs again</a></li>'
                    f'<li><a href="{url_of(fixtures, s3)}">{s3.host}</a></li>'
                    '<li><a href="https://linkedin.com/company/foo">LinkedIn</a></li><li><a href="/about">About us</a></li>'
                    '<li><a href="mailto:a@b.c">mail</a></li><li><a href="javascript:void(0)">x</a></li><li><a href="#top">top</a></li>'
                    "</ul></body></html>").encode()
            return "200 OK", {"Content-Type": "text/html"}, body
        return "404 Not Found", {"Content-Type": "text/plain"}, b"no"

    async with serve(handler) as (port, hits):
        seed = f"http://seed.fixture.test:{port}/list"
        res = await execute(plan_for(tmp_path, [SeedPagesSource([seed])], command="discover", locations=["Pune"]))
        d = Path(res["out_dir"])
        cands = {c.domain: c for c in res["discovery"].candidates}
        assert set(cands) == {s1.host, s2.host, s3.host}, "company links only: directories, same-site, mailto, js and anchors are dropped"
        assert cands[s1.host].name == "Flow Labs Pvt" and cands[s2.host].name is None and cands[s3.host].name is None   # never invent a name
        assert all(c.discovery_source == "seed_page" and c.source_url == seed and "AI automation agencies" in c.evidence for c in cands.values())
        assert cands[s1.host].duplicate_sources and cands[s1.host].niche_signal      # duplicate link merged; evidence mentions 'automation'
        assert cands[s1.host].location_query == "Pune" and cands[s1.host].niche_query == "AI automation agency"
        assert any(r.status == "blocked_domain" for r in res["discovery"].rejected)
        assert [p for h, p in hits if p == "/list"] == ["/list"] and ("seed.fixture.test", "/robots.txt") in hits
        log = [json.loads(l) for l in (d / "discovery_fetch_log.jsonl").read_text().splitlines()]
        assert {e["kind"] for e in log} == {"page", "robots"} and any(e["url"] == seed and e["status"] == 200 for e in log)


@pytest.mark.parametrize("case", ["robots_disallow", "challenge", "forbidden", "unauthorized", "server_error"])
async def test_seed_page_refusals_are_recorded_as_errors_and_never_bypassed_or_retried(fixtures, tmp_path, case):
    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, (b"User-agent: *\nDisallow: /\n" if case == "robots_disallow" else b"User-agent: *\nAllow: /\n")
        if case == "challenge":
            return "200 OK", {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html>Just a moment...</html>"
        code = {"forbidden": "403 Forbidden", "unauthorized": "401 Unauthorized", "server_error": "500 Error"}.get(case, "200 OK")
        return code, {"Content-Type": "text/html"}, b'<a href="https://some-agency.in">x</a>'

    async with serve(handler) as (port, hits):
        res = await execute(plan_for(tmp_path, [SeedPagesSource([f"http://seed.fixture.test:{port}/list"])], command="discover",
                                     settings=make_settings(http_max_retries=0)))
        assert res["candidates"] == 0
        err = res["discovery"].errors[0]
        assert err.source == "seed_page" and err.kind == {"robots_disallow": "robots", "challenge": "bot_challenge", "forbidden": "http",
                                                          "unauthorized": "http", "server_error": "http"}[case]
        page_hits = [p for h, p in hits if p == "/list"]
        assert len(page_hits) == (0 if case == "robots_disallow" else 1), "a refusal is a stop: no retry, no workaround"
        assert (Path(res["out_dir"]) / "errors.jsonl").exists()
        if case in ("robots_disallow", "challenge"):
            assert "not attempting to bypass" in err.message or case == "robots_disallow"


# ------------------------------------------------------------------ OpenStreetMap / Overpass
async def test_overpass_source_builds_a_bounded_query_and_reads_only_mapped_websites_in_india(fixtures, tmp_path):
    s1, s2, s3 = site_of(fixtures, "jsonld", 0), site_of(fixtures, "plain", 0), site_of(fixtures, "plain", 1)
    seen = []

    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {"Content-Type": "text/plain"}, b""
        seen.append(parse_qs(urlsplit(path).query)["data"][0])
        els = [
            {"type": "node", "id": 11, "lat": 18.52, "lon": 73.85, "tags": {"name": "AI Flow Labs", "office": "it", "website": url_of(fixtures, s1),
                                                                              "addr:city": "Pune", "addr:street": "MG Road"}},
            {"type": "way", "id": 12, "center": {"lat": 18.5, "lon": 73.8}, "tags": {"name": "No Site Co", "office": "company"}},
            {"type": "node", "id": 13, "lat": 51.5, "lon": -0.12, "tags": {"name": "London Same Name", "website": "https://london-firm.co.uk"}},
            {"type": "node", "id": 14, "lat": 18.6, "lon": 73.7, "tags": {"name": "Contact Site Co", "contact:website": url_of(fixtures, s2)}},
            {"type": "node", "id": 15, "lat": 18.6, "lon": 73.7, "tags": {"name": "Social Only", "website": "https://facebook.com/socialonly"}},
            {"type": "node", "id": 16, "lat": 18.6, "lon": 73.7, "tags": {"website": url_of(fixtures, s3)}},          # unnamed: skipped
        ]
        return "200 OK", {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"elements": els}).encode()

    async with serve(handler) as (port, hits):
        src = OverpassSource(f"http://overpass.fixture.test:{port}/api/interpreter")
        res = await execute(plan_for(tmp_path, [src], command="discover", locations=["Pune"], limit=20, extra_keywords=["n8n"]))
        by = {c.domain: c for c in res["discovery"].candidates}
        assert set(by) == {s1.host, s2.host} and res["discovery"].raw_hits == {"osm_overpass": 3}
        c = by[s1.host]
        assert (c.name, c.location, c.industry, c.discovery_source) == ("AI Flow Labs", "MG Road, Pune", "it", "osm_overpass")
        assert c.source_url == "https://www.openstreetmap.org/node/11" and "office=it" in c.evidence and c.location_query == "Pune"
        assert any("OpenStreetMap contributors" in n and "ODbL" in n for n in res["discovery"].notes)
        assert "ODbL" in (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")
        assert len(seen) == 1 and 'area["name"="Pune"]' in seen[0] and "[timeout:60]" in seen[0] and "out tags center 60;" in seen[0] and "n8n" in seen[0]
        assert [r.status for r in res["discovery"].rejected if r.raw.startswith("https://facebook")] == ["blocked_domain"]


@pytest.mark.parametrize("case,kind", [("rate_limited", "http"), ("xml", "content_type"), ("garbage", "bad_response"), ("shape", "bad_response")])
async def test_overpass_failures_are_recorded_not_hidden(fixtures, tmp_path, case, kind):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {"Content-Type": "text/plain"}, b""
        if case == "rate_limited":
            return "429 Too Many Requests", {"Content-Type": "text/plain", "Retry-After": "1"}, b"busy"
        if case == "xml":
            return "200 OK", {"Content-Type": "application/xml"}, b"<busy/>"
        if case == "garbage":
            return "200 OK", {"Content-Type": "application/json"}, b"{not json"
        return "200 OK", {"Content-Type": "application/json"}, b'{"elements": "nope"}'

    async with serve(handler) as (port, hits):
        res = await execute(plan_for(tmp_path, [OverpassSource(f"http://overpass.fixture.test:{port}/api/interpreter")], command="discover",
                                     locations=["Pune"], settings=make_settings(http_max_retries=0)))
        assert res["candidates"] == 0 and res["discovery"].errors[0].kind == kind and res["discovery"].errors[0].where == "Pune"


async def test_json_is_accepted_only_when_a_caller_opts_in_for_that_one_fetch(fixtures):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {"Content-Type": "text/plain"}, b""
        return "200 OK", {"Content-Type": "application/json"}, b'{"a": 1}'

    async with serve(handler) as (port, hits):
        f = Fetcher(make_settings())
        try:
            url = f"http://api.fixture.test:{port}/x"
            assert (await f.fetch(url)).error_kind == "content_type"
            ok = await f.fetch(url + "?1", accept_types=("application/json",))
            assert ok.ok and ok.text == '{"a": 1}'
            assert (await f.fetch(url + "?2")).error_kind == "content_type", "the opt-in must not leak to later fetches"
            blocked = await f.fetch("http://127.0.0.1:1/x", accept_types=("application/json",))
            assert blocked.error_kind == "blocked", "the SSRF guard still applies when accepting extra types"
        finally:
            await f.aclose()


# ------------------------------------------------------------------ enrichment with the model backend and the recorded fallback chain
async def test_full_run_records_backend_source_status_and_sends_failures_to_manual_review(fixtures, clean_db, tmp_path):
    plain, jsonld = site_of(fixtures, "plain", 0), site_of(fixtures, "jsonld", 0)
    robots, gone = site_of(fixtures, "robots_blocked", 0), site_of(fixtures, "not_found", 0)
    sites = [plain, jsonld, robots, gone]
    be = SiteBackend({plain.host: truth(plain), jsonld.host: {"company_name": jsonld.name, "description": jsonld.description}})
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s) for s in sites], [s.name for s in sites]))],
                                 backend_mode="auto", primary_factory=primary_with(be)))
    d, rows = Path(res["out_dir"]), read_rows(res)
    assert res["mode"] == SYNTH_LABEL and set(rows) == {s.host for s in sites}
    p = rows[plain.host]
    for f in ("company_name", "email", "phone", "description"):
        assert (p[f + "_backend"], p[f + "_method"], p[f + "_status"]) == ("scrapegraph", "scrapegraph_llm", "extracted"), f
        assert p[f + "_source_url"].startswith(f"http://{plain.host}:") and p[f + "_extracted_at"]
    assert (p["email"], p["phone"]) == (plain.email, plain.phone) and p["website_status"] == "independently_verified"
    assert p["usable"] == "yes" and p["backend_used"] == "scrapegraph" and p["fallback_used"] == "no" and p["review_status"] == "pending_review"
    assert p["discovery_source"] == SRC_CSV and p["source_ref"].endswith("#row 1") and p["synthetic"] == "true" and p["enriched_at"]
    assert p["email_verification"] in ("", "unverified")                      # never claimed verified
    j = rows[jsonld.host]
    assert j["company_name_backend"] == "scrapegraph" and j["email_status"] == "not_found" and j["email"] == ""      # model did not state it: nothing invented
    assert fixtures.state.count(robots.host, "/") == 0, "robots.txt disallow: the page was never fetched by either engine"
    r = rows[robots.host]
    assert r["review_status"] == "needs_manual_review" and r["usable"] == "no" and "robots" in r["manual_review_reason"] and r["fallback_used"] == "no"
    assert r["attempts"].count("->") == 0, "access refusals are not retried with the other engine"
    g = rows[gone.host]
    assert g["review_status"] == "needs_manual_review" and g["enrichment_status"] in ("unreachable", "failed", "no_data") and g["manual_review_reason"]
    errs = [json.loads(l) for l in (d / "errors.jsonl").read_text().splitlines()]
    assert {e["domain"] for e in errs if e["stage"] == "enrichment"} >= {robots.host, gone.host}
    assert (d / "fetch_log.jsonl").exists() and all(json.loads(l)["url"].startswith("http://") for l in (d / "fetch_log.jsonl").read_text().splitlines())
    rep = (d / "discovery_report.md").read_text(encoding="utf-8")
    assert "SYNTHETIC FIXTURES" in rep and "not real businesses" in rep and "model `fake`" in rep and "paid API cost: 0" in rep
    with open(res["leads_csv"], newline="", encoding="utf-8-sig") as fh:
        assert next(csv.reader(fh)) == outputs.LEAD_COLUMNS


@pytest.mark.parametrize("failure", ["model_error", "model_unavailable"])
async def test_a_model_failure_falls_back_to_the_inhouse_engine_per_company_and_says_so(fixtures, clean_db, tmp_path, failure):
    plain = site_of(fixtures, "jsonld", 0)
    be = FakeBackend(error="model_error") if failure == "model_error" else FakeBackend(raise_unavailable=BackendUnavailable("model gone", "model_unavailable"))
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, plain)]))], backend_mode="auto",
                                 primary_factory=primary_with(be)))
    row = read_rows(res)[plain.host]
    assert be.calls >= 1 and row["fallback_used"] == "yes" and failure in row["fallback_reason"]
    assert row["attempts"].startswith(f"scrapegraph:failed[{failure}] -> inhouse:")
    assert row["backend_used"] == "inhouse" and row["company_name"] == plain.name and row["company_name_backend"] == "inhouse"
    assert row["company_name_method"] in ("json_ld", "meta", "title", "microdata") and row["company_name_source_url"].startswith("http://")
    assert row["review_status"] == "pending_review"
    assert res["leads_detail"][0].fallback_used is True


async def test_when_the_model_finds_nothing_on_a_reachable_site_the_inhouse_engine_runs_with_force(fixtures, clean_db, tmp_path):
    s = site_of(fixtures, "plain", 0)
    be = SiteBackend({})                                                # model says "missing" for everything
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s)]))], backend_mode="auto",
                                 primary_factory=primary_with(be)))
    row = read_rows(res)[s.host]
    assert row["attempts"].startswith("scrapegraph:no_data -> inhouse:") and row["fallback_used"] == "yes" and "no data" in row["fallback_reason"]
    assert row["email"] == s.email and row["email_backend"] == "inhouse" and row["backend_used"] == "inhouse"


async def test_unavailable_primary_backend_is_reported_once_per_run_and_every_company_uses_the_inhouse_engine(fixtures, clean_db, tmp_path):
    s = site_of(fixtures, "plain", 0)
    note = "ScrapeGraphAI/Ollama (qwen3:4b) was NOT available: cannot reach Ollama at http://127.0.0.1:11434. The whole run used the in-house engine instead (recorded fallback)."

    async def none_factory(engine, plan):
        return None, None, note
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s)]))], backend_mode="auto", primary_factory=none_factory))
    row = read_rows(res)[s.host]
    assert row["backend_used"] == "inhouse" and row["fallback_used"] == "yes" and "NOT available" in row["fallback_reason"]
    assert row["attempts"] == "inhouse:enriched" or row["attempts"].startswith("inhouse:")
    assert "NOT available" in (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")
    errs = [json.loads(l) for l in (Path(res["out_dir"]) / "errors.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "primary_backend_unavailable" for e in errs)


async def test_explicit_inhouse_mode_has_no_primary_and_is_not_called_a_fallback(fixtures, clean_db, tmp_path):
    s = site_of(fixtures, "plain", 0)
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s)]))], backend_mode="inhouse"))
    row = read_rows(res)[s.host]
    assert row["fallback_used"] == "no" and row["backend_used"] == "inhouse" and "in-house engine only" in res["primary_note"]


async def test_run_page_budget_skips_remaining_companies_without_contacting_them(fixtures, clean_db, tmp_path, monkeypatch):
    import leadengine.discovery.pipeline as pl
    from leadengine.discovery.enrich import EnrichRunner
    orig = EnrichRunner.__init__

    def tiny(self, *a, **kw):
        kw.update(page_budget=3, concurrency=1)
        orig(self, *a, **kw)
    monkeypatch.setattr(pl.EnrichRunner, "__init__", tiny)
    sites = [site_of(fixtures, "plain", i) for i in range(4)]
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s) for s in sites]))], backend_mode="inhouse"))
    rows = read_rows(res)
    skipped = [r for r in rows.values() if r["enrichment_status"] == "skipped_budget"]
    assert skipped and all(r["review_status"] == "needs_manual_review" and "budget exhausted" in r["manual_review_reason"] for r in skipped)
    for r in skipped:
        assert fixtures.state.count(r["domain"], "/") == 0, "a skipped company must not be contacted"
    assert len(skipped) < len(sites)


# ------------------------------------------------------------------ safety of what counts as a real business, data preservation, compat
async def test_synthetic_hosts_are_rejected_and_never_contacted_unless_a_test_allows_them(fixtures, clean_db, tmp_path):
    s = site_of(fixtures, "plain", 0)
    fixtures.state.reset()
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s)]))], allow_synthetic=False))
    assert res["candidates"] == 0 and res["leads"] == 0 and res["discovery"].rejected[0].status == "reserved_domain"
    assert fixtures.state.total() == 0


async def test_existing_database_rows_and_previous_outputs_are_preserved(fixtures, clean_db, tmp_path):
    s1, s2 = site_of(fixtures, "plain", 0), site_of(fixtures, "plain", 1)
    first = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s1)]))], backend_mode="inhouse"))
    before = Path(first["leads_csv"]).read_bytes()
    from leadengine.engine import Engine
    e = Engine(make_settings()); await e.start()
    try:
        async with e.db.connection() as c:
            n1 = (await (await c.execute("SELECT count(*) n FROM companies")).fetchone())["n"]
    finally:
        await e.stop()
    second = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s2)]))], backend_mode="inhouse"))
    e = Engine(make_settings()); await e.start()
    try:
        async with e.db.connection() as c:
            rows = {r["domain"] for r in await (await c.execute("SELECT domain FROM companies")).fetchall()}
            n2 = (await (await c.execute("SELECT count(*) n FROM companies")).fetchone())["n"]
    finally:
        await e.stop()
    assert n2 >= n1 and {s1.host, s2.host} <= rows, "the second run adds; it never resets or truncates"
    assert first["out_dir"] != second["out_dir"] and Path(first["leads_csv"]).read_bytes() == before
    assert set(read_rows(second)) == {s2.host}, "a run's output contains only that run's companies, not rows left in a shared database"


async def test_outputs_feed_the_existing_enrichment_workflow(fixtures, clean_db, tmp_path):
    sites = [site_of(fixtures, "plain", 0), site_of(fixtures, "jsonld", 0)]
    res = await execute(plan_for(tmp_path, [CsvImportSource(csv_of(tmp_path, [url_of(fixtures, s) for s in sites], [s.name for s in sites]))],
                                 command="discover"))
    rd = CSVReader(res["candidates_csv"])
    parsed = list(rd.rows())
    assert [r.domain for r in parsed] == [s.host for s in sites] and not any(r.error for r in parsed) and rd.mapping["website"] == "website"
    from leadengine.engine import Engine
    e = Engine(make_settings()); await e.start()
    try:
        info = await jobs.create_job_from_companies(e.db, e.settings, [{"website": r.website, "name": r.name} for r in parsed], requested=None)
        assert info["created"] and info["ingest"]
    finally:
        await e.stop()
    again = await discover([CsvImportSource(res["candidates_csv"])], None, niches=["x"], locations=[], limit=10, allow_synthetic=True)
    assert [c.domain for c in again.candidates] == [s.host for s in sites]
    assert again.candidates[0].discovery_source == SRC_CSV and again.candidates[0].source_ref.endswith("#row 1")


def test_cli_authorized_path_runs_end_to_end_with_the_same_gates_against_local_fixtures(fixtures, tmp_path, monkeypatch, capsys):
    """Drives python -m leadengine.discovery run --live ... through argparse, the gate, the planner and execute(). Only two things are
    swapped for the test: the live settings (a real live run forbids fixture host overrides) and acceptance of *.fixture.test hosts."""
    import psycopg
    import leadengine.discovery.cli as cli
    import leadengine.discovery.pipeline as pl
    from tests.conftest import DB_URL
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs, fetch_log RESTART IDENTITY CASCADE")
    fixtures.state.reset()
    sites = [site_of(fixtures, "plain", 0), site_of(fixtures, "jsonld", 0)]
    p = csv_of(tmp_path, [url_of(fixtures, s) for s in sites])
    real = cli.discover
    both = lambda *a, **kw: real(*a, **{**kw, "allow_synthetic": True})
    monkeypatch.setattr(cli, "discover", both)
    monkeypatch.setattr(pl, "discover", both)
    monkeypatch.setattr(cli, "live_settings", lambda **kw: make_settings(global_concurrency=2))
    args = ["run", "--csv", p, "--backend", "inhouse", "--database-url", DB_URL, "--out-root", str(tmp_path / "out")]
    assert cli.main(args) == 0 and "DRY RUN complete" in capsys.readouterr().err and fixtures.state.total() == 0
    capsys.readouterr()
    assert cli.main(args + ["--live", "--confirm-terms-reviewed", "--confirm-count", "2"]) == 0
    done = Path(json.loads(capsys.readouterr().out)["out_dir"])
    assert len(list((tmp_path / "out").iterdir())) == 2, "the dry run and the live run each got their own directory"
    with open(done / "enriched_leads.csv", newline="", encoding="utf-8-sig") as f:
        rows = {r["domain"]: r for r in csv.DictReader(f)}
    assert set(rows) == {s.host for s in sites} and all(r["enrichment_status"] in ("enriched", "partial") for r in rows.values())
    assert "LIVE (real websites contacted)" in (done / "discovery_report.md").read_text(encoding="utf-8")
    assert fixtures.state.total() > 0
