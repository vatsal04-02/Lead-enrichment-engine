"""Web-search discovery (leadengine/discovery/websearch): provider parsing/retries/cache, query planning, result -> candidate rules, site validation,
de-duplication, source failures, budgets and the full pipeline. Offline: httpx.MockTransport, a loopback fake search endpoint and *.fixture.test sites."""
import csv
import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from leadengine.discovery.websearch.candidates import from_result, infer_name
from leadengine.discovery.websearch.engine import WebConfig, planned_requests, run_web_discovery
from leadengine.discovery.websearch.providers import (BraveProvider, HttpJsonClient, QueryCache, SearchError, SearchResult, SearxngProvider, StaticProvider,
                                                     make_provider)
from leadengine.discovery.websearch.queries import Query, plan_queries
from leadengine.discovery.websearch.validate import validate_site
from leadengine.discovery.websearch import cli as web_cli
from leadengine.fetcher import Fetcher
from tests.conftest import make_settings
from tests.netguard import clean_le_env, netguard, no_network  # noqa: F401
from tests.profile_sites import NAV, handler_for, page
from tests.rawserver import serve


# ------------------------------------------------------------------ providers (no sockets: MockTransport)
def mock_client(handler, **kw):
    async def nosleep(_): ...
    delays = []

    async def sleep(d):
        delays.append(d)
    c = HttpJsonClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep, min_interval=0.0, **kw)
    return c, delays


async def test_searxng_results_are_parsed_and_malformed_rows_are_ignored():
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"results": [{"title": " Acme  Soft | Pune ", "url": "https://acme.in/", "content": "We build software"},
                                                      {"title": "no url"}, "junk", {"url": 5}]})
    c, _ = mock_client(h)
    res = await SearxngProvider("http://localhost:8080/", http=c).search("it company pune", page=2, count=10)
    assert [(r.title, r.url, r.snippet, r.rank, r.page) for r in res] == [("Acme Soft | Pune", "https://acme.in/", "We build software", 1, 2)]
    assert seen["format"] == "json" and seen["pageno"] == "2" and seen["q"] == "it company pune"


async def test_searxng_json_disabled_bad_json_and_bad_url_are_reported_precisely():
    c, _ = mock_client(lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(SearchError) as e:
        await SearxngProvider("http://localhost:8080", http=c).search("q")
    assert e.value.kind == "format_disabled" and "settings.yml" in str(e.value)
    c, _ = mock_client(lambda req: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(SearchError) as e:
        await SearxngProvider("http://localhost:8080", http=c).search("q")
    assert e.value.kind == "bad_response"
    c, _ = mock_client(lambda req: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(SearchError) as e:
        await SearxngProvider("http://localhost:8080", http=c).search("q")
    assert e.value.kind == "bad_response"
    for bad in ("ftp://x", "http://user:pw@host", "not a url"):
        with pytest.raises(SearchError):
            SearxngProvider(bad)


async def test_brave_needs_a_key_sends_it_as_a_header_and_parses_results():
    with pytest.raises(SearchError) as e:
        BraveProvider("")
    assert e.value.kind == "not_configured" and "LE_BRAVE_API_KEY" in str(e.value)
    captured = {}

    def h(req):
        captured["token"], captured["params"] = req.headers.get("x-subscription-token"), dict(req.url.params)
        return httpx.Response(200, json={"web": {"results": [{"title": "Beta Ltd", "url": "https://beta.co.in/", "description": "Healthcare software"}]}})
    c, _ = mock_client(h)
    res = await BraveProvider("secret-key", http=c).search("healthcare pune", page=3, count=50)
    assert res[0].url == "https://beta.co.in/" and res[0].snippet == "Healthcare software"
    assert captured["token"] == "secret-key" and captured["params"]["country"] == "IN" and captured["params"]["count"] == "20" and captured["params"]["offset"] == "2"
    assert "secret-key" not in json.dumps([r.__dict__ for r in res]) and "secret-key" not in repr(BraveProvider("secret-key", http=c).describe())


async def test_retries_use_backoff_honour_retry_after_and_give_up_with_a_typed_error():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "7"}) if len(calls) == 1 else httpx.Response(200, json={"results": []})
    c, delays = mock_client(h, retries=2, backoff=2.0)
    assert await SearxngProvider("http://localhost:8080", http=c).search("q") == [] and len(calls) == 2 and 7.0 in delays
    calls.clear()
    c, delays = mock_client(lambda req: (calls.append(1), httpx.Response(503))[1], retries=2, backoff=2.0)
    with pytest.raises(SearchError) as e:
        await SearxngProvider("http://localhost:8080", http=c).search("q")
    assert e.value.kind == "http" and len(calls) == 3 and len(delays) == 2 and delays[1] > delays[0] * 1.2, "exponential backoff"
    c, _ = mock_client(lambda req: httpx.Response(402), retries=2)
    with pytest.raises(SearchError) as e:
        await BraveProvider("k", http=c).search("q")
    assert e.value.kind == "quota" and c.requests == 1, "a quota error is not retried"

    def boom(req):
        raise httpx.ConnectTimeout("slow")
    c, _ = mock_client(boom, retries=1)
    with pytest.raises(SearchError) as e:
        await SearxngProvider("http://localhost:8080", http=c).search("q")
    assert e.value.kind == "timeout" and c.requests == 2


def test_static_provider_replays_saved_results_and_the_cache_roundtrips(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"results": {"IT company Pune": [{"title": "A", "url": "https://a.in/", "snippet": "s"}], "x": {"2": [{"url": "https://b.in/"}]}}}))
    sp = StaticProvider(str(f))
    import asyncio
    assert [r.url for r in asyncio.run(sp.search("it company pune"))] == ["https://a.in/"] and asyncio.run(sp.search("x", page=2))[0].url == "https://b.in/"
    assert asyncio.run(sp.search("unknown")) == [] and sp.needs_network is False
    cache = QueryCache(tmp_path / "cache", ttl_seconds=60)
    rows = [SearchResult("T", "https://a.in/", "s", "p", "q", 1, 1)]
    assert cache.get("p", "q", 1, 10) is None
    cache.put("p", "q", 1, 10, rows)
    assert cache.get("p", "q", 1, 10) == rows and cache.get("p", "q", 2, 10) is None
    assert QueryCache(tmp_path / "cache", ttl_seconds=-1).get("p", "q", 1, 10) is None, "expired entries are ignored"


def test_make_provider_reads_environment_and_refuses_unknown_or_unconfigured():
    assert make_provider("searxng", {"LE_SEARXNG_URL": "http://127.0.0.1:9999"}).base == "http://127.0.0.1:9999"
    with pytest.raises(SearchError) as e:
        make_provider("brave", {})
    assert e.value.kind == "not_configured"
    with pytest.raises(SearchError):
        make_provider("google", {})
    with pytest.raises(SearchError):
        make_provider("static", {})


# ------------------------------------------------------------------ query planning
def test_every_industry_city_pair_is_tried_before_any_is_repeated_and_the_cap_holds():
    inds = {"A": ["a one", "a two"], "B": ["b one"]}
    qs = plan_queries(inds, ["X", "Y", "Z"], max_queries=100, max_pages=2)
    first = qs[:6]
    assert {(q.industry, q.city) for q in first} == {(i, c) for i in inds for c in "XYZ"} and all(q.page == 1 for q in first)
    assert len({q.text for q in qs if q.page == 1}) == len([q for q in qs if q.page == 1]), "no duplicate query text within a page"
    assert plan_queries(inds, ["X"], 3) == plan_queries(inds, ["X"], 3) and len(plan_queries(inds, ["X", "Y"], 5)) == 5
    assert max(q.page for q in qs) == 2 and plan_queries({}, ["X"], 5) == []


# ------------------------------------------------------------------ result -> candidate
def res(url, title="", snippet="", rank=1):
    return SearchResult(title, url, snippet, "searxng", "q", rank)


Q = Query("q", "IT services", "Pune", "IT services company")


def test_company_sites_become_candidates_and_everything_else_is_rejected_with_a_reason():
    ok, rej = from_result(res("https://www.acmesoft.in/contact-us", "Contact | Acme Soft Pvt Ltd - Pune", "snip"), Q)
    assert rej is None and ok["website"].startswith("https://") and ok["domain"] == "acmesoft.in" and ok["name"] == "Acme Soft Pvt Ltd" and ok["industry_query"] == "IT services"
    cases = {"https://www.linkedin.com/company/acme": "blocked_domain", "https://clutch.co/profile/acme": "blocked_domain", "https://www.economictimes.com/x": "media_or_reference_site",
             "https://acme.in/blog/10-best-it-firms": "article_or_deep_page", "https://some.in/x/y/z/w": "article_or_deep_page", "ftp://acme.in/": "invalid_url",
             "https://acme.blogspot.com/": "blocked_domain", "https://listicle.in/it-firms": "listicle_or_article_title"}
    titles = {"https://listicle.in/it-firms": "Top 10 IT companies in Pune 2026"}
    for url, reason in cases.items():
        c, r = from_result(res(url, titles.get(url, "")), Q)
        assert c is None and r.reason == reason, (url, r and r.reason)
    c, r = from_result(res("https://acme.in/", "Top 10 reasons to love Acme"), Q)
    assert c is not None, "a company homepage is kept even when its title reads like an article"


def test_name_inference_prefers_the_segment_matching_the_domain_and_never_guesses_a_generic_label():
    assert infer_name("Home | Lucknow Flow Works", "lucknowflow.in") == "Lucknow Flow Works"
    assert infer_name("Best IT company in Pune - Acme Soft | Welcome", "acmesoft.in") == "Acme Soft"
    assert infer_name("Welcome | Home", "x.in") is None and infer_name("", "x.in") is None
    assert infer_name("Acme", "acme.in") == "Acme"


# ------------------------------------------------------------------ site validation + full pipeline over fixture sites
def company(name, city="Pune", *, phone="+91 20 5550 0100", email=True, desc=True, india=True, about=True):
    d = f'<meta name="description" content="{name} builds enterprise software for customers.">' if desc else ""
    home = (f"<!doctype html><html><head><title>Home | {name}</title>{d}</head><body>{NAV}<h1>{name}</h1>"
            f"<p>{name} is a software company with an office in {city if india else 'Toronto'} building enterprise software, workflow automation and IT services "
            f"for mid-size customers across several industries. Our team designs, builds and supports business applications.</p></body></html>").encode()
    contact_body = (f"<h1>Contact</h1><p>{'<a href=\"mailto:hello@' + name.lower().replace(' ', '') + '.example\">hello@' + name.lower().replace(' ', '') + '.example</a>' if email else ''} "
                    f"<a href=\"tel:{phone}\">{phone}</a></p><address>12 Main Road, {city if india else 'Toronto, Canada'}</address>")
    return {"/": home, "/about": page(f"About | {name}", "", "<h1>About</h1><p>" + name + " was founded to build dependable business software. " * 3 + "</p>"),
            "/contact": page(f"Contact | {name}", "", contact_body)}


SITES = {
    "acmesoft.fixture.test": company("Acme Soft"), "betacare.fixture.test": company("Beta Care", "Mumbai"), "gammaedu.fixture.test": company("Gamma Edu", "Delhi"),
    "deltarealty.fixture.test": company("Delta Realty", "Hyderabad"), "foreign.fixture.test": company("Foreign Inc", india=False, phone="+1 416 555 0100"),
    "parked.fixture.test": {"/": page("Parked", "", "<p>This domain is for sale. Buy this domain today.</p>")},
    "parked2.fixture.test": {"/": page("Home", "", "<h1>Welcome</h1><p>" + "Industry leading solutions for every need. " * 8 + "This domain is for sale. "
                                      '<a href="mailto:owner@parked2.example">owner@parked2.example</a></p>')},
    "emptyco.fixture.test": {"/": page("Home", "", "<p>Hi</p>")},
    "blocked.fixture.test": company("Blocked Co"), "challenge.fixture.test": company("Challenge Co"),
    "down.fixture.test": company("Down Co"), "dupname.fixture.test": company("Acme Soft"),
}


def site_handler(port_box, counts=None):
    base = handler_for({h: p for h, p in SITES.items() if h != "challenge.fixture.test"}, robots_blocked=("blocked.fixture.test",), broken=("down.fixture.test",), counts=counts)

    def handler(host, path):
        if host == "challenge.fixture.test" and path != "/robots.txt":
            return "403 Forbidden", {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html><title>Just a moment...</title></html>"
        return base(host, path)
    return handler


async def validate(host, port, **kw):
    f = Fetcher(make_settings(http_max_retries=0), db=None)
    try:
        return await validate_site(f, {"website": f"http://{host}:{port}/", "name": kw.get("name", ""), "city_query": kw.get("city", "Pune")}, kw.get("kw", ["software", "automation"]))
    finally:
        await f.aclose()


async def test_validation_extracts_public_contact_details_and_gives_each_failure_its_own_status():
    counts = {}
    async with serve(site_handler(None, counts)) as (port, hits):
        ok = await validate("acmesoft.fixture.test", port, name="Acme Soft")
        assert ok.status == "valid" and ok.name == "Acme Soft" and ok.phone.replace(" ", "") == "+912055500100" and ok.email == "hello@acmesoft.example"
        assert "Pune" in ok.address and ok.india_signal == "yes" and "software" in ok.industry_evidence and ok.description.startswith("Acme Soft builds")
        assert ok.confidence >= 0.8 and ok.missing_fields == ["linkedin_url"], (ok.confidence, ok.missing_fields)
        assert {k for _, k in ok.pages} >= {"home", "about", "contact"}
        assert (await validate("foreign.fixture.test", port, city="Pune")).status == "india_unconfirmed"
        assert (await validate("parked.fixture.test", port)).status == "parked_or_empty" and (await validate("emptyco.fixture.test", port)).status == "parked_or_empty"
        assert (await validate("parked2.fixture.test", port)).status == "parked_or_empty", "a long page that says the domain is for sale is still parked"
        assert (await validate("blocked.fixture.test", port)).status == "robots_disallowed" and counts.get(("blocked.fixture.test", "/")) is None
        assert (await validate("challenge.fixture.test", port)).status == "anti_bot_blocked"
        assert (await validate("down.fixture.test", port)).status == "unreachable"
        assert (await validate("nosuch.fixture.test", port)).status == "unreachable"


def search_handler(port_box, answers, fail=(), counts=None):
    """Fake SearXNG on loopback. answers: {query text: [(title, host_or_url, snippet)]}; fail: query texts that answer HTTP 500."""
    def handler(host, path):
        if not path.startswith("/search"):
            return site_handler(None)(host, path)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        query, pageno = q["q"][0], q.get("pageno", ["1"])[0]
        if counts is not None:
            counts[(query, pageno)] = counts.get((query, pageno), 0) + 1
        if query in fail:
            return "500 Internal Server Error", {"Content-Type": "text/plain"}, b"boom"
        rows = answers.get(query, []) if pageno == "1" else []
        out = [{"title": t, "url": (u if "://" in u else f"http://{u}:{port_box[0]}/"), "content": s} for t, u, s in rows]
        return "200 OK", {"Content-Type": "application/json"}, json.dumps({"results": out}).encode()
    return handler


def cfg_for(tmp_path, port, **kw):
    from leadengine.discovery.websearch.providers import HttpJsonClient as H
    prov = SearxngProvider(f"http://127.0.0.1:{port}", http=H(min_interval=0.0, retries=0, timeout=5))
    base = dict(provider=prov, industries={"IT services": ["it company"], "Healthcare": ["clinic"]}, cities=["Pune", "Mumbai"], target=4, max_queries=8,
                max_pages_per_query=1, results_per_query=10, max_sites=50, out_root=str(tmp_path / "out"), batch_queries=2, concurrency=2,
                settings=make_settings(http_max_retries=0), allow_synthetic=True, max_provider_failures=3)
    base.update(kw)
    return WebConfig(**base)


def read_csv(p):
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


ANSWERS = {
    "it company in Pune": [("Home | Acme Soft", "acmesoft.fixture.test", "software"), ("Top 10 IT firms", "https://www.clutch.co/it", ""), ("Blocked Co", "blocked.fixture.test", "")],
    "it company in Mumbai": [("Beta Care - Mumbai", "betacare.fixture.test", ""), ("Acme Soft again", "acmesoft.fixture.test", "dup domain")],
    "clinic in Pune": [("Parked", "parked.fixture.test", ""), ("Gamma Edu", "gammaedu.fixture.test", "")],
    "clinic in Mumbai": [("Delta Realty", "deltarealty.fixture.test", ""), ("Acme Soft (other domain, same name)", "dupname.fixture.test", ""),
                         ("Challenge Co", "challenge.fixture.test", ""), ("Down Co", "down.fixture.test", "")],
}


async def test_full_pipeline_discovers_validates_deduplicates_and_exports(tmp_path):
    box, scounts = [0], {}
    async with serve(search_handler(box, ANSWERS, counts=scounts)) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=4, max_queries=4))
    out = Path(s["run_directory"])
    rows = read_csv(out / "discovery.csv")
    by = {r["Domain"]: r for r in rows}
    assert s["valid_companies"] == 4 and s["reached_target"] and s["stop_reason"] == "target_reached" and s["selected_for_enrichment"] == 4
    assert {d for d, r in by.items() if r["DiscoveryStatus"] == "selected"} == {"acmesoft.fixture.test", "betacare.fixture.test", "gammaedu.fixture.test", "deltarealty.fixture.test"}
    assert by["blocked.fixture.test"]["DiscoveryStatus"] == "robots_disallowed" and by["parked.fixture.test"]["DiscoveryStatus"] == "parked_or_empty"
    assert by["challenge.fixture.test"]["DiscoveryStatus"] == "anti_bot_blocked" and by["down.fixture.test"]["DiscoveryStatus"] == "unreachable"
    assert by["dupname.fixture.test"]["DiscoveryStatus"] == "duplicate_identity" and "acmesoft" in by["dupname.fixture.test"]["Detail"]
    assert "www.clutch.co" not in json.dumps(rows) and s["results_rejected"] == {"blocked_domain": 1}
    a = by["acmesoft.fixture.test"]
    assert a["Company"] == "Acme Soft" and a["City"] == "Pune" and a["Industry/Niche"] == "IT services" and a["Email"] == "hello@acmesoft.example" and a["Phone"]
    assert a["SourceURL"].startswith("http://acmesoft.fixture.test:") and float(a["Confidence"]) >= 0.8 and "AlsoFoundVia" in a and "dup domain" not in a["AlsoFoundVia"]
    assert a["AlsoFoundVia"].startswith("it company in Mumbai"), "the same domain found by a second query is merged, not duplicated"
    assert float(by["betacare.fixture.test"]["Confidence"]) > 0 and by["parked.fixture.test"]["Confidence"] == "0.0"
    assert json.loads((out / "discovery.json").read_text())[0]["Website"] and len(read_csv(out / "profile_input.csv")) == 4
    assert set(read_csv(out / "profile_input.csv")[0]) == {"Company", "Website", "City", "Industry/Niche", "SourceURL"}
    assert (out / "run.log").read_text() and (out / "search_log.jsonl").read_text().count("\n") == 4 and (out / "summary.json").exists() and (out / "fetch_log.jsonl").exists(), "every request to a company site is audited"
    assert s["search_requests_made"] == 4 and s["paid_api"] is False and s["fields_populated_among_selected"]["Email"] == 4 and set(s["city_distribution"]) == {"Pune", "Mumbai"}


async def test_the_search_keeps_going_across_queries_until_the_target_and_stops_there(tmp_path):
    box, hits_by_host = [0], {}
    async with serve(search_handler(box, ANSWERS)) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=2, max_queries=8, batch_queries=1))
        visited = {h for h, _ in hits if h.endswith(".fixture.test")}
    assert s["valid_companies"] >= 2 and s["stop_reason"] == "target_reached" and s["queries_run"] < 8, "it must not exhaust the queries once the target is met"
    assert "deltarealty.fixture.test" not in visited, "later candidates are never visited once the target is reached"


async def test_when_more_valid_companies_are_found_than_wanted_the_selection_is_balanced_across_cities(tmp_path):
    box = [0]
    answers = {"it company in Pune": [("Acme Soft", "acmesoft.fixture.test", ""), ("Gamma Edu", "gammaedu.fixture.test", "")],
               "it company in Mumbai": [("Beta Care", "betacare.fixture.test", "")]}
    async with serve(search_handler(box, answers)) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=2, max_queries=2, batch_queries=8, industries={"IT services": ["it company"]}))
    rows = read_csv(Path(s["run_directory"]) / "discovery.csv")
    assert s["valid_companies"] == 3 and s["selected_for_enrichment"] == 2
    assert sorted(r["City"] for r in rows if r["DiscoveryStatus"] == "selected") == ["Mumbai", "Pune"], "not both from the first city"
    assert [r["DiscoveryStatus"] for r in rows if r["DiscoveryStatus"] == "discovered_not_selected"] == ["discovered_not_selected"]


async def test_budgets_stop_the_run_and_the_real_count_is_reported_not_padded(tmp_path):
    box = [0]
    async with serve(search_handler(box, ANSWERS)) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=50, max_queries=8))
        assert s["valid_companies"] == 4 and not s["reached_target"] and s["stop_reason"] == "queries_exhausted" and s["selected_for_enrichment"] == 4
        s2 = await run_web_discovery(cfg_for(tmp_path, port, target=50, max_queries=8, max_sites=2, label="sites"))
        assert s2["stop_reason"] == "site_budget_exhausted" and s2["sites_visited"] == 2
        statuses = {r["DiscoveryStatus"] for r in read_csv(Path(s2["run_directory"]) / "discovery.csv")}
        assert "not_visited" in statuses, "candidates beyond the site budget are listed, not silently dropped"
        s3 = await run_web_discovery(cfg_for(tmp_path, port, target=50, max_queries=8, max_seconds=0.0, label="time"))
        assert s3["stop_reason"] == "time_budget_exhausted" and s3["valid_companies"] == 0


async def test_a_failing_query_is_logged_and_skipped_but_a_failing_provider_stops_the_run(tmp_path):
    box = [0]
    async with serve(search_handler(box, ANSWERS, fail=("it company in Pune",))) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=50, max_queries=8, cache_dir=str(tmp_path / "cache-fail")))
    assert s["search_errors"] == {"http": 1} and s["valid_companies"] >= 3 and "failed" not in s["stop_reason"]
    assert '"error": "http' in (Path(s["run_directory"]) / "search_log.jsonl").read_text()
    all_fail = tuple(q for q in ("it company in Pune", "it company in Mumbai", "clinic in Pune", "clinic in Mumbai"))
    box2 = [0]
    async with serve(search_handler(box2, ANSWERS, fail=all_fail)) as (port, hits):
        box2[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=5, max_queries=8, label="dead", cache_dir=str(tmp_path / "cache-dead")))
    assert s["stop_reason"] == "search_provider_failing" and s["valid_companies"] == 0 and s["queries_run"] == 3 and (Path(s["run_directory"]) / "summary.json").exists()


async def test_cached_queries_cost_no_search_request_and_outputs_never_overwrite(tmp_path):
    box, scounts = [0], {}
    async with serve(search_handler(box, ANSWERS, counts=scounts)) as (port, hits):
        box[0] = port
        first = await run_web_discovery(cfg_for(tmp_path, port, target=4, max_queries=4, cache_dir=str(tmp_path / "cache")))
        n = sum(scounts.values())
        second = await run_web_discovery(cfg_for(tmp_path, port, target=4, max_queries=4, cache_dir=str(tmp_path / "cache"), label="again"))
    assert n == 4 and sum(scounts.values()) == 4 and second["queries_served_from_cache"] == 4 and second["search_requests_made"] == 0
    assert first["run_directory"] != second["run_directory"] and (Path(first["run_directory"]) / "discovery.csv").exists()
    with pytest.raises(FileExistsError):
        import leadengine.discovery.websearch.engine as eng
        eng._rows_csv(Path(first["run_directory"]) / "discovery.csv", ["a"], [])


async def test_enrichment_reuses_the_pages_discovery_already_fetched(tmp_path):
    from tests.test_profiles import FakeBackend
    box, counts = [0], {}
    answer = lambda html: {"company_name": "Acme Soft", "business_description": "software company with an office in Pune building enterprise software",
                           "description_quote": "software company with an office in Pune building enterprise software, workflow automation and IT services",
                           "model_confidence": "medium"}
    async with serve(search_handler(box, {"it company in Pune": [("Home | Acme Soft", "acmesoft.fixture.test", "")]}, counts=counts)) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=1, max_queries=1, industries={"IT services": ["it company"]}, cities=["Pune"], enrich=True,
                                            backend=FakeBackend(answer=answer), enrich_kwargs={"max_pages": 4, "model_timeout": 5.0}))
        page_hits = [(h, p) for h, p in hits if h == "acmesoft.fixture.test" and p != "/robots.txt"]
    en = s["enrichment"]
    assert en["ran"] and en["status_counts"] == {"ok": 1} and Path(en["enriched_csv"]).exists() and en["outcomes"]["successfully_enriched"] + en["outcomes"]["partially_enriched"] == 1
    assert len(page_hits) == len(set(page_hits)), f"every page was requested once, not twice: {page_hits}"
    assert read_csv(en["enriched_csv"])[0]["Company"] == "Acme Soft" and Path(en["combined_summary"]).exists()


async def test_enrichment_that_cannot_start_leaves_complete_discovery_outputs(tmp_path):
    from leadengine.backends import BackendUnavailable
    from tests.test_profiles import FakeBackend
    box = [0]
    async with serve(search_handler(box, {"it company in Pune": [("Acme Soft", "acmesoft.fixture.test", "")]})) as (port, hits):
        box[0] = port
        s = await run_web_discovery(cfg_for(tmp_path, port, target=1, max_queries=1, industries={"IT services": ["it company"]}, cities=["Pune"], enrich=True,
                                            backend=FakeBackend(fail_preflight="Ollama is not running")))
    assert s["enrichment"]["ran"] is False and "Ollama" in s["enrichment"]["error"] and s["valid_companies"] == 1
    assert (Path(s["run_directory"]) / "discovery.csv").exists() and (Path(s["run_directory"]) / "profile_input.csv").exists()


# ------------------------------------------------------------------ CLI: dry run by default, explicit confirmation for anything live
def test_cli_plan_and_default_run_contact_nothing(capsys, no_network):
    assert web_cli.main(["plan", "--target", "10", "--max-queries", "6", "--max-sites", "20"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["DRY RUN"] == "nothing was contacted" and out["search_requests_max"] == 6 and out["confirm_count"] == 26 and "--confirm-count 26" in out["to_run_for_real"]
    assert web_cli.main(["run", "--max-queries", "6", "--max-sites", "20"]) == 0 and "DRY RUN" in capsys.readouterr().out
    assert web_cli.main(["run", "--max-queries", "6", "--max-sites", "20", "--live"]) == 2, "--live alone is refused"
    assert "--confirm-terms-reviewed" in capsys.readouterr().err
    assert web_cli.main(["run", "--max-queries", "6", "--max-sites", "20", "--live", "--confirm-terms-reviewed", "--confirm-count", "5"]) == 2
    assert "must equal" in capsys.readouterr().err
    assert web_cli.main(["plan", "--provider", "brave"]) == 2 and "LE_BRAVE_API_KEY" in capsys.readouterr().err


def test_cli_industries_and_a_static_provider_plan_offline(tmp_path, capsys, no_network):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"results": {}}))
    assert web_cli.main(["plan", "--provider", "static", "--static-results", str(f), "--industry", "Legal=law firm;corporate lawyers", "--city", "Pune", "--max-queries", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["search_requests_max"] == 0 and out["first_queries"][0] == "law firm in Pune" and out["confirm_count"] == 400
