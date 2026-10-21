"""Automatic discovery sources: Wikidata (SPARQL, guarded fetcher), local OpenStreetMap extracts (no network), place aliases, the
`check-sources` robots preflight and `--only-niche-signal`. Everything runs against local servers/files; hosts are synthetic
(*.fixture.test) or IANA-reserved. No real endpoint is contacted and no database other than the test database is used."""
import asyncio
import bz2
import csv
import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import leadengine.discovery.cli as cli
from leadengine.discovery.models import SRC_OSM_FILE, SRC_WIKIDATA
from leadengine.discovery.pipeline import execute
from leadengine.discovery.places import bbox_for, expand_locations, in_box, place_ok
from leadengine.discovery.sources import SourceContext
from leadengine.discovery.sources.osm_extract import OsmExtractSource, osm_url
from leadengine.discovery.sources.wikidata import WikidataSource, build_query
from tests.conftest import make_settings, site_of, url_of
from tests.netguard import clean_le_env, netguard, no_network  # noqa: F401
from tests.rawserver import serve
from tests.test_discovery_gates import Harness, LIVE_OK, UA_OK, h  # noqa: F401
from tests.test_discovery_integration import clean_db, plan_for, read_rows  # noqa: F401


def sparql(*rows) -> bytes:
    return json.dumps({"head": {"vars": []}, "results": {"bindings": list(rows)}}).encode()


def row(qid, website, label=None, industry=None, desc=None):
    r = {"item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"}, "website": {"type": "literal", "value": website}}
    if label is not None:
        r["itemLabel"] = {"value": label}
    if industry:
        r["industryLabel"] = {"value": industry}
    if desc:
        r["desc"] = {"value": desc}
    return r


JSON_HDR = {"Content-Type": "application/sparql-results+json; charset=utf-8"}


def wd_handler(body, hdr=JSON_HDR, status="200 OK", robots=b"User-agent: *\nAllow: /\n", queries=None):
    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, robots
        if queries is not None:
            queries.append(parse_qs(urlsplit(path).query))
        return status, hdr, body
    return handler


async def run_wikidata(tmp_path, port, locations=("Lucknow",), **kw):
    src = WikidataSource(f"http://wikidata.fixture.test:{port}/sparql")
    return await execute(plan_for(tmp_path, [src], command="discover", locations=list(locations), settings=make_settings(http_max_retries=0), **kw))


# ------------------------------------------------------------------ places
def test_place_aliases_expand_delhi_ncr_and_keep_what_the_user_asked_for():
    got = expand_locations(["Delhi NCR", "Lucknow", "delhi ncr", "Bengaluru"])
    assert [p for p, _ in got] == ["Delhi", "New Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad", "Lucknow", "Bengaluru"]
    assert {r for p, r in got if p in ("Noida", "Delhi")} == {"Delhi NCR"} and ("Lucknow", "Lucknow") in got
    assert place_ok("Bengaluru") and place_ok("St. Mary's (East)") and not place_ok('Pune"; DROP') and not place_ok("")
    assert in_box(12.97, 77.59, bbox_for("Bangalore")) and not in_box(28.6, 77.2, bbox_for("Bengaluru")) and bbox_for("Nowhereville") is None


# ------------------------------------------------------------------ Wikidata: results, provenance, query
async def test_wikidata_hits_keep_entity_provenance_merge_industries_and_never_invent_names(fixtures, tmp_path):
    a, b, c = site_of(fixtures, "jsonld", 0), site_of(fixtures, "jsonld", 1), site_of(fixtures, "plain", 0)
    body = sparql(row("Q111", url_of(fixtures, a), "Alpha Software Ltd", "software industry", "Indian IT services company"),
                  row("Q111", url_of(fixtures, a), "Alpha Software Ltd", "information technology"),            # second industry row for the same item
                  row("Q222", url_of(fixtures, b), "Q222"),                                                       # only a Q-id label: not a name
                  row("Q333", url_of(fixtures, c), "Gamma Bank", "banking"))
    async with serve(wd_handler(body)) as (port, hits):
        res = await run_wikidata(tmp_path, port, extra_keywords=["software"])
    by = {x.domain: x for x in res["discovery"].candidates}
    assert set(by) == {a.host, b.host, c.host} and res["discovery"].raw_hits == {SRC_WIKIDATA: 3}
    x = by[a.host]
    assert (x.name, x.industry, x.discovery_source, x.source_url) == ("Alpha Software Ltd", "software industry, information technology", SRC_WIKIDATA, "https://www.wikidata.org/wiki/Q111")
    assert "Wikidata Q111" in x.evidence and "Indian IT services company" in x.evidence and x.location == "Lucknow" and x.location_query == "Lucknow"
    assert "software" in x.niche_signal and "sparql place 'Lucknow'" in x.source_ref and x.discovered_at.endswith("+00:00")
    assert by[b.host].name is None, "a bare Q-id is not a company name"
    assert by[c.host].niche_signal == "", "a bank is not flagged as niche-relevant"
    assert any("CC0" in n for n in res["discovery"].notes)
    assert "CC0" in (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")


async def test_wikidata_query_is_bounded_escaped_india_only_and_sent_with_the_identifying_user_agent(fixtures, tmp_path):
    seen_headers, qs = [], []

    async def server(r, w):
        raw = (await r.readuntil(b"\r\n\r\n")).decode("latin-1")
        seen_headers.append(raw)
        path = raw.split()[1]
        if path == "/robots.txt":
            body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
        else:
            qs.append(parse_qs(urlsplit(path).query))
            body, ctype = sparql(), "application/sparql-results+json"
        w.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await w.drain()
        w.close()
    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        s = make_settings(user_agent=UA_OK, http_max_retries=0)
        res = await execute(plan_for(tmp_path, [WikidataSource(f"http://wikidata.fixture.test:{port}/sparql")], command="discover",
                                     locations=["Lucknow"], settings=s, limit=40))
    finally:
        srv.close()
    assert res["candidates"] == 0 and not res["discovery"].errors, "an empty result set is not an error"
    q = qs[0]["query"][0]
    assert qs[0]["format"] == ["json"] and '"Lucknow"@en' in q and "wd:Q668" in q and "wdt:P856" in q and "wdt:P159" in q and "LIMIT 120" in q
    assert q.lstrip().upper().startswith("SELECT") and "DELETE" not in q.upper() and "INSERT" not in q.upper()
    assert all(f"User-Agent: {UA_OK}" in req for req in seen_headers), "every request must identify the operator"


def test_wikidata_query_escapes_quotes_and_slashes():
    q = build_query('A"B\\C', 10)
    assert 'rdfs:label|skos:altLabel "A\\"B\\\\C"@en' in q


async def test_wikidata_delhi_ncr_alias_runs_one_bounded_query_per_member_city_and_records_what_was_asked(fixtures, tmp_path):
    qs = []
    async with serve(wd_handler(sparql(), queries=qs)) as (port, hits):
        src = WikidataSource(f"http://wikidata.fixture.test:{port}/sparql")
        ctx = SourceContext(fetcher=None, niches=["x"], locations=["Delhi NCR", "Lucknow"], keywords=[], limit=10)
        assert len(src.planned_requests(ctx)) == 7 and all("wikidata.fixture.test" in r for r in src.planned_requests(ctx))
        await run_wikidata(tmp_path, port, locations=("Delhi NCR", "Lucknow"))
    labels = [q["query"][0].split('rdfs:label|skos:altLabel "')[1].split('"')[0] for q in qs]
    assert labels == ["Delhi", "New Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad", "Lucknow"]


# ------------------------------------------------------------------ Wikidata: empty, invalid, duplicate
async def test_wikidata_empty_results_produce_an_honest_empty_report(fixtures, tmp_path):
    async with serve(wd_handler(sparql())) as (port, hits):
        res = await run_wikidata(tmp_path, port)
    assert res["candidates"] == 0 and res["discovery"].errors == [] and res["discovery"].raw_hits == {SRC_WIKIDATA: 0}
    rep = (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")
    assert "valid unique candidates | 0" in rep


async def test_wikidata_invalid_blocked_reserved_and_duplicate_domains_are_rejected_or_merged(fixtures, tmp_path):
    a = site_of(fixtures, "jsonld", 0)
    body = sparql(row("Q1", url_of(fixtures, a), "Real Co"),
                  row("Q2", url_of(fixtures, a) + "about", "Real Co again"),                     # same business, different path
                  row("Q3", "http://" + a.host + ":" + str(urlsplit(url_of(fixtures, a)).port) + "/x?y=1", "Real Co third"),
                  row("Q4", "not a url", "Broken"), row("Q5", "mailto:a@b.c", "Email not site"), row("Q6", "javascript:alert(1)", "Script"),
                  row("Q7", "https://www.linkedin.com/company/foo", "Social page"), row("Q8", "https://acme.example/", "Reserved"),
                  row("Q9", "http://user:pw@host.fixture.test/", "Credentials"),
                  {"item": {"value": "http://www.wikidata.org/entity/Q10"}},                       # no website statement: nothing usable
                  {"website": {"value": "http://x.fixture.test/"}})                                  # no entity: no provenance
    async with serve(wd_handler(body)) as (port, hits):
        res = await run_wikidata(tmp_path, port)
    d = res["discovery"]
    assert [c.domain for c in d.candidates] == [a.host] and d.candidates[0].source_url.endswith("/Q1")
    assert len(d.candidates[0].duplicate_sources) == 2
    st = sorted((r.status for r in d.rejected))
    assert st.count("duplicate") == 2 and st.count("invalid_url") >= 3 and st.count("blocked_domain") == 1 and st.count("reserved_domain") == 1
    rej = (Path(res["out_dir"]) / "rejected_candidates.csv")
    assert rej.exists() and "Broken" in rej.read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------ Wikidata: source errors and compliance refusals
@pytest.mark.parametrize("case,kind", [("rate_limited", "http"), ("server_error", "http"), ("html", "bad_response"), ("garbage", "bad_response"),
                                       ("shape", "bad_response"), ("xml", "content_type")])
async def test_wikidata_source_errors_are_recorded_not_hidden_and_not_retried_into_a_flood(fixtures, tmp_path, case, kind):
    cfg = {"rate_limited": ("429 Too Many Requests", {"Content-Type": "text/plain", "Retry-After": "1"}, b"slow down"),
           "server_error": ("500 Error", {"Content-Type": "text/plain"}, b"x"),
           "html": ("200 OK", {"Content-Type": "text/html"}, b"<html>busy</html>"),
           "garbage": ("200 OK", JSON_HDR, b"{nope"), "shape": ("200 OK", JSON_HDR, b'{"results": {"bindings": "x"}}'),
           "xml": ("200 OK", {"Content-Type": "application/xml"}, b"<x/>")}[case]
    n = []
    handler = wd_handler(cfg[2], cfg[1], cfg[0], queries=n)
    async with serve(handler) as (port, hits):
        res = await run_wikidata(tmp_path, port)
    e = res["discovery"].errors
    assert res["candidates"] == 0 and len(e) == 1 and e[0].source == SRC_WIKIDATA and e[0].kind == kind and e[0].where == "Lucknow"
    assert len(n) == 1, "one attempt: failures are not hammered"
    assert (Path(res["out_dir"]) / "errors.jsonl").exists()


@pytest.mark.parametrize("robots,verdict", [(b"User-agent: *\nDisallow: /\n", "disallowed"), (b"User-agent: *\nDisallow: /sparql\n", "disallowed")])
async def test_robots_disallow_stops_the_source_and_names_the_lawful_alternatives(fixtures, tmp_path, robots, verdict):
    qs = []
    async with serve(wd_handler(sparql(row("Q1", "http://c0.fixture.test/", "X")), robots=robots, queries=qs)) as (port, hits):
        res = await run_wikidata(tmp_path, port)
    e = res["discovery"].errors[0]
    assert res["candidates"] == 0 and e.kind == "robots" and "does not bypass robots.txt" in e.message
    assert "--osm-file" in e.message and "--csv" in e.message and "--wikidata-endpoint" in e.message
    assert qs == [] and not any(p.startswith("/sparql") and "query=" in p for _, p in hits), "the SPARQL endpoint must never be requested"


async def test_robots_403_is_treated_as_disallow_and_nothing_is_requested(fixtures, tmp_path):
    def handler(host, path):
        return ("403 Forbidden", {"Content-Type": "text/plain"}, b"no") if path == "/robots.txt" else ("200 OK", JSON_HDR, sparql())
    async with serve(handler) as (port, hits):
        res = await run_wikidata(tmp_path, port)
    assert res["discovery"].errors[0].kind == "robots" and not any("query=" in p for _, p in hits)


async def test_wikidata_needs_a_location_and_rejects_unsafe_ones_without_any_request(fixtures, tmp_path):
    async with serve(wd_handler(sparql())) as (port, hits):
        none = await run_wikidata(tmp_path, port, locations=())
        bad = await run_wikidata(tmp_path, port, locations=('Pune"; out;',))
    assert none["discovery"].errors[0].kind == "no_location" and bad["discovery"].errors[0].kind == "invalid_location"
    assert not any("query=" in p for _, p in hits)


# ------------------------------------------------------------------ OpenStreetMap extract (local file, no network)
def geojson(*features) -> str:
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


def pt(lat, lon, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


OSM_XML = """<?xml version="1.0"?><osm version="0.6">
 <node id="1" lat="26.85" lon="80.95"><tag k="name" v="Lucknow Flow Labs"/><tag k="office" v="it"/><tag k="website" v="http://lucknow-flow.fixture.test/"/></node>
 <way id="2"><center lat="12.97" lon="77.59"/><tag k="name" v="Bengaluru Bots"/><tag k="office" v="company"/><tag k="contact:website" v="http://bots.fixture.test/"/></way>
 <node id="3" lat="51.5" lon="-0.12"><tag k="name" v="London Co"/><tag k="office" v="it"/><tag k="website" v="http://london.fixture.test/"/></node>
 <node id="4" lat="26.86" lon="80.96"><tag k="name" v="No Site"/><tag k="office" v="it"/></node>
</osm>"""


async def run_osm(tmp_path, files, locations, **kw):
    return await execute(plan_for(tmp_path, [OsmExtractSource([str(f) for f in files])], command="discover", locations=list(locations), **kw))


async def test_osm_extract_geojson_flat_and_nested_tags_polygons_and_city_filtering_under_the_network_guard(tmp_path, no_network):
    f = tmp_path / "offices.geojson"
    f.write_text(geojson(
        pt(26.85, 80.95, **{"@id": "node/10", "name": "Lucknow Automation", "office": "it", "website": "http://lko-auto.fixture.test/"}),
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[77.58, 12.96], [77.60, 12.96], [77.60, 12.98], [77.58, 12.98], [77.58, 12.96]]]},
         "properties": {"@id": "way/20", "tags": {"name": "Bengaluru Workflow", "office": "consulting", "website": "http://blr-flow.fixture.test/",
                                                  "description": "workflow automation"}}},
        pt(28.57, 77.32, **{"@id": "node/30", "name": "Noida Systems", "office": "software", "website": "http://noida-sys.fixture.test/"}),
        pt(19.07, 72.87, **{"@id": "node/40", "name": "Mumbai Co", "office": "it", "website": "http://mumbai.fixture.test/"}),     # outside the asked cities
        pt(26.85, 80.95, **{"@id": "node/50", "name": "Shop", "shop": "bakery", "website": "http://bakery.fixture.test/"}),          # not an office
        pt(26.85, 80.95, **{"@id": "node/60", "office": "it", "website": "http://unnamed.fixture.test/"}),                          # no name
        pt(26.85, 80.95, **{"@id": "node/70", "name": "No site", "office": "it"}),                                                 # no website
    ))
    res = await run_osm(tmp_path, [f], ["Lucknow", "Bengaluru", "Delhi NCR"])
    by = {c.domain: c for c in res["discovery"].candidates}
    assert set(by) == {"lko-auto.fixture.test", "blr-flow.fixture.test", "noida-sys.fixture.test"}
    x = by["lko-auto.fixture.test"]
    assert (x.name, x.discovery_source, x.source_url, x.location_query, x.industry) == ("Lucknow Automation", SRC_OSM_FILE, "https://www.openstreetmap.org/node/10", "Lucknow", "it")
    assert x.source_ref == "offices.geojson (node/10)" and by["blr-flow.fixture.test"].source_url.endswith("/way/20")
    assert by["noida-sys.fixture.test"].location_query == "Delhi NCR" and "workflow" in by["blr-flow.fixture.test"].niche_signal
    assert res["discovery"].raw_hits == {SRC_OSM_FILE: 3}
    assert res["discovery"].rejected == [], "objects without a website/name/office tag must never become hits, not even rejected ones"
    assert any("ODbL" in n for n in res["discovery"].notes) and res["discovery"].errors == []
    assert "ODbL" in (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("opener,suffix", [(open, ".osm"), (gzip.open, ".osm.gz"), (bz2.open, ".osm.bz2")])
async def test_osm_extract_reads_overpass_style_xml_plain_and_compressed(tmp_path, no_network, opener, suffix):
    f = tmp_path / f"extract{suffix}"
    with opener(f, "wt", encoding="utf-8") as fh:
        fh.write(OSM_XML)
    res = await run_osm(tmp_path, [f], ["Lucknow", "Bengaluru"])
    by = {c.name: c for c in res["discovery"].candidates}
    assert set(by) == {"Lucknow Flow Labs", "Bengaluru Bots"}, "London is outside the asked cities; the object without a website states nothing"
    assert by["Bengaluru Bots"].source_url == "https://www.openstreetmap.org/way/2" and by["Bengaluru Bots"].domain == "bots.fixture.test"


async def test_osm_extract_without_locations_keeps_only_what_is_inside_india_and_addr_city_is_a_fallback(tmp_path, no_network):
    f = tmp_path / "a.geojson"
    f.write_text(geojson(pt(26.85, 80.95, name="In India", office="it", website="http://in.fixture.test/"),
                         pt(51.5, -0.12, name="In London", office="it", website="http://uk.fixture.test/"),
                         {"type": "Feature", "geometry": None, "properties": {"name": "No Coords", "office": "it", "website": "http://nc.fixture.test/",
                                                                              "addr:city": "Lucknow"}}))
    all_india = await run_osm(tmp_path, [f], [])
    assert {c.domain for c in all_india["discovery"].candidates} == {"in.fixture.test", "nc.fixture.test"}
    lko = await run_osm(tmp_path, [f], ["Lucknow"])
    assert {c.domain for c in lko["discovery"].candidates} == {"in.fixture.test", "nc.fixture.test"}, "addr:city matches when there are no coordinates"
    blr = await run_osm(tmp_path, [f], ["Bengaluru"])
    assert blr["candidates"] == 0


async def test_osm_extract_invalid_blocked_and_duplicate_websites_are_rejected_or_merged(tmp_path, no_network):
    f1, f2 = tmp_path / "one.geojson", tmp_path / "two.geojson"
    f1.write_text(geojson(pt(26.85, 80.95, name="Dup Co", office="it", website="http://dup.fixture.test/"),
                          pt(26.85, 80.95, name="Bad", office="it", website="ht!tp://??"),
                          pt(26.85, 80.95, name="Social", office="it", website="https://facebook.com/x"),
                          pt(26.85, 80.95, name="Mailish", office="it", website="info@thing.in")))
    f2.write_text(geojson(pt(26.86, 80.96, name="Dup Co Ltd", office="company", website="http://dup.fixture.test/contact")))
    res = await run_osm(tmp_path, [f1, f2], ["Lucknow"])
    d = res["discovery"]
    assert [c.domain for c in d.candidates] == ["dup.fixture.test"] and d.candidates[0].duplicate_sources
    assert sorted(r.status for r in d.rejected).count("invalid_url") == 2 and any(r.status == "blocked_domain" for r in d.rejected)


@pytest.mark.parametrize("name,content,kind,hint", [
    ("raw.osm.pbf", b"\x00\x00", "unreadable_file", "osmium"), ("broken.geojson", b"{not json", "unreadable_file", ""),
    ("notgeo.geojson", b'{"a": 1}', "unreadable_file", "FeatureCollection"), ("empty.osm", b"<osm></osm>", "empty_file", ""),
    ("weird.txt", b"x", "unreadable_file", "unsupported")])
async def test_osm_extract_bad_files_are_reported_with_a_reason_and_never_crash_the_run(tmp_path, no_network, name, content, kind, hint):
    f = tmp_path / name
    f.write_bytes(content)
    res = await run_osm(tmp_path, [f, tmp_path / "missing.geojson"], ["Lucknow"])
    kinds = [(e.where, e.kind) for e in res["discovery"].errors]
    assert (name, kind) in kinds and ("missing.geojson" in kinds[0][0] or any(w.endswith("missing.geojson") and k == "file_missing" for w, k in kinds))
    if hint:
        assert any(hint in e.message for e in res["discovery"].errors if e.where == name)
    assert res["candidates"] == 0


def test_osm_extract_is_offline_by_declaration_and_urls_are_built_from_both_id_styles():
    s = OsmExtractSource(["x.geojson"])
    assert s.needs_network is False and s.planned_requests(SourceContext(None, [], [], [], 10)) == []
    assert osm_url("node/12") == "https://www.openstreetmap.org/node/12" and osm_url("w5").endswith("/way/5") and osm_url("junk") == ""


# ------------------------------------------------------------------ only-niche-signal
async def test_only_niche_signal_keeps_candidates_whose_own_source_text_mentions_a_keyword_and_says_how_many_were_dropped(fixtures, tmp_path):
    a, b = site_of(fixtures, "jsonld", 0), site_of(fixtures, "plain", 0)
    body = sparql(row("Q1", url_of(fixtures, a), "Flow IT", "software industry"), row("Q2", url_of(fixtures, b), "Plain Bank", "banking"))
    async with serve(wd_handler(body)) as (port, hits):
        keep_all = await run_wikidata(tmp_path, port, niches=["IT services"])
        filtered = await run_wikidata(tmp_path, port, niches=["IT services"], only_niche_signal=True)
    assert keep_all["candidates"] == 2 and [c.domain for c in filtered["discovery"].candidates] == [a.host]
    assert any("--only-niche-signal: 1 of 2" in n for n in filtered["discovery"].notes)


# ------------------------------------------------------------------ gates for the new flags (no network, refused before execute)
def test_new_sources_are_gated_exactly_like_the_old_ones(h):
    base = ["discover", "--wikidata", "--location", "Delhi NCR", "--out-root", str(h.out)]
    rc, out, err = h.run(base)                                         # dry run: six cities -> six targets
    assert rc == 0 and h.calls[0].authorized is False and "Targets a live run would contact: 6" in err
    h.calls.clear()
    for extra, msg in ((["--live"], "confirm-terms-reviewed"), (["--live", "--confirm-terms-reviewed", "--confirm-count", "1"], "must equal"),
                       (["--live", "--confirm-terms-reviewed", "--confirm-count", "7"], "must equal")):
        rc, out, err = h.run(base + extra)
        assert rc == 2 and msg in err
    assert h.calls == [] and h.constructed == [] and h.guard.attempts == []


def test_a_local_osm_file_needs_no_confirmation_and_contacts_nothing(h, tmp_path):
    f = tmp_path / "x.geojson"
    f.write_text(geojson(pt(26.85, 80.95, name="Lko", office="it", website="http://lko.fixture.test/")))
    rc, out, err = h.run(["discover", "--osm-file", str(f), "--location", "Lucknow", "--out-root", str(h.out)])
    assert rc == 0 and h.calls[0].authorized is True and not any(s.needs_network for s in h.calls[0].sources)
    assert h.constructed == [] and h.guard.attempts == []
    assert h.calls[0].allow_synthetic is False                                 # the CLI itself never accepts synthetic hosts


# ------------------------------------------------------------------ check-sources preflight
def test_check_sources_dry_run_lists_hosts_and_contacts_nothing_and_requires_the_live_confirmations(h):
    base = ["check-sources", "--wikidata", "--osm", "--seed-url", "http://listing.example.org/x", "--out-root", str(h.out)]
    rc, out, err = h.run(base)
    assert rc == 0 and "DRY RUN" in out and "query.wikidata.org/robots.txt" in out and "overpass-api.de/robots.txt" in out and "listing.example.org/robots.txt" in out
    assert "--confirm-count 3" in out
    for extra, msg in ((["--live"], "confirm-terms-reviewed"), (["--live", "--confirm-terms-reviewed", "--confirm-count", "2"], "must equal")):
        rc, out, err = h.run(base + extra)
        assert rc == 2 and msg in err
    rc, out, err = h.run(["check-sources", "--csv", str(h.csv)])
    assert rc == 2 and "nothing to check" in err
    assert h.calls == [] and h.constructed == [] and h.guard.attempts == [] and not h.out.exists()


@pytest.mark.parametrize("robots,exit_code,allowed", [(b"User-agent: *\nAllow: /\n", 0, True), (b"User-agent: *\nDisallow: /\n", 1, False)])
async def test_check_sources_reads_only_robots_txt_and_reports_the_verdict(fixtures, monkeypatch, capsys, robots, exit_code, allowed):
    monkeypatch.setenv("LE_USER_AGENT", UA_OK)
    monkeypatch.setattr(cli, "live_settings", lambda **kw: make_settings(global_concurrency=1))
    async with serve(wd_handler(sparql(), robots=robots)) as (port, hits):
        args = cli.build_parser().parse_args(["check-sources", "--wikidata", "--wikidata-endpoint", f"http://wikidata.fixture.test:{port}/sparql",
                                              "--live", "--confirm-terms-reviewed", "--confirm-count", "1"])
        rc = await cli.cmd_check_sources(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == exit_code and out["results"][0]["allowed_by_robots"] is allowed and out["results"][0]["source"] == SRC_WIKIDATA
    assert {p for _, p in hits} == {"/robots.txt"}, "only robots.txt may be fetched, never the SPARQL endpoint"
    assert "never bypassed" in out["note"]
    if not allowed:
        assert out["results"][0]["kind"] == "robots"


# ------------------------------------------------------------------ end to end: discovery -> in-house enrichment on synthetic sites
async def test_wikidata_to_enrichment_end_to_end_keeps_discovery_provenance_in_the_leads(fixtures, clean_db, tmp_path):
    s1, s2 = site_of(fixtures, "plain", 0), site_of(fixtures, "jsonld", 0)
    body = sparql(row("Q11", url_of(fixtures, s1), s1.name, "software industry"), row("Q12", url_of(fixtures, s2), s2.name, "information technology"),
                  row("Q13", url_of(fixtures, s1), s1.name))
    async with serve(wd_handler(body)) as (port, hits):
        src = WikidataSource(f"http://wikidata.fixture.test:{port}/sparql")
        res = await execute(plan_for(tmp_path, [src], locations=["Lucknow"], backend_mode="inhouse", settings=make_settings(http_max_retries=0)))
    rows = read_rows(res)
    assert set(rows) == {s1.host, s2.host} and res["mode"].startswith("SYNTHETIC")
    r = rows[s1.host]
    assert r["discovery_source"] == SRC_WIKIDATA and r["source_url"] == "https://www.wikidata.org/wiki/Q11" and "Q13" in r["duplicate_sources"]
    assert r["location_query"] == "Lucknow" and r["backend_used"] == "inhouse" and r["synthetic"] == "true"
    assert r["email"] == s1.email and r["email_backend"] == "inhouse" and r["email_source_url"].startswith(f"http://{s1.host}:")
    assert r["email_verification"] in ("", "unverified"), "e-mail is never mailbox-verified"
    rep = (Path(res["out_dir"]) / "discovery_report.md").read_text(encoding="utf-8")
    assert "wikidata" in rep and "CC0" in rep and "SYNTHETIC FIXTURES" in rep
    with open(res["candidates_csv"], newline="", encoding="utf-8-sig") as f:
        assert {x["discovery_source"] for x in csv.DictReader(f)} == {SRC_WIKIDATA}
