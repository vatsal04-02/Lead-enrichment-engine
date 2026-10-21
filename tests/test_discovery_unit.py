"""Discovery engine - tests that need no database and no real network: URL validation, dedupe, CSV import, niche/India evidence,
live gates, dry-run isolation, exports, review, doctor."""
import argparse
import csv
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.stub_llm import StubLLMServer
from leadengine.csv_io import CSVReader
from leadengine.discovery import doctor as doctor_mod
from leadengine.discovery import outputs
from leadengine.discovery.cli import main as cli_main
from leadengine.discovery.discover import discover
from leadengine.discovery.enrich import Attempt, Lead, REQUESTED, backend_of
from leadengine.discovery.models import (BLOCKED, DUPLICATE, INVALID_URL, RESERVED, VALID, RawHit, SRC_CSV)
from leadengine.discovery.niche import expand_keywords, india_evidence, niche_match
from leadengine.discovery.pipeline import RunPlan, SYNTH_LABEL, execute
from leadengine.discovery.review import ReviewError, apply_review
from leadengine.discovery.safety import DiscoveryError, forced_settings, live_gate, live_settings
from leadengine.discovery.sources import SourceContext
from leadengine.discovery.sources.csv_import import CsvImportSource
from leadengine.discovery.sources.overpass import OverpassSource, build_query, location_ok
from leadengine.discovery.sources.seed_pages import SeedPagesSource
from leadengine.discovery.urlcheck import build_candidates, check_url
from tests.conftest import SG_PYTHON, sg_available
from tests.netguard import clean_le_env, netguard, no_network  # noqa: F401  (fixtures: loopback allowed, outbound refused and recorded; no ambient LE_*)


def write_csv(path: Path, rows: list[list[str]]) -> str:
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return str(path)


def hit(site: str, name=None, source=SRC_CSV, url="", ref="r"):
    return RawHit(website=site, name=name, source=source, source_url=url, source_ref=ref, discovered_at="2026-01-01T00:00:00+00:00")


# ------------------------------------------------------------------ URL validation
@pytest.mark.parametrize("raw,status", [
    ("", INVALID_URL), ("   ", INVALID_URL), ("not a url", INVALID_URL), ("foo@bar.com", INVALID_URL), ("ftp://acme.in", INVALID_URL),
    ("javascript:alert(1)", INVALID_URL), ("http://user:pw@acme.in", INVALID_URL), ("http://127.0.0.1", INVALID_URL),
    ("http://[::1]/", INVALID_URL), ("localhost", INVALID_URL), ("http://169.254.169.254/latest", INVALID_URL),
    ("acme.in:8080", INVALID_URL), ("https://acme", INVALID_URL), ("http://a b.com", INVALID_URL),
    ("https://acme.example", RESERVED), ("https://x.test", RESERVED), ("https://printer.local", RESERVED), ("https://example.com", RESERVED),
    ("http://c1.fixture.test/", RESERVED),
    ("https://linkedin.com/company/x", BLOCKED), ("https://in.linkedin.com/company/x", BLOCKED), ("https://clutch.co/profile/x", BLOCKED),
    ("https://agency.blogspot.com", BLOCKED), ("https://x.github.io", BLOCKED), ("https://wa.me/9199999", BLOCKED),
    ("acme-automation.in", VALID), ("https://www.Acme-Automation.IN/about?utm=1#x", VALID), ("https://xn--mnchen-3ya.de", VALID),
    ("https://acme.co.in", VALID),
])
def test_url_validation_classifies_every_case_without_network(raw, status):
    st, reason, ident, synthetic = check_url(raw)
    assert st == status, (raw, st, reason)
    assert (ident is None) == (status != VALID)
    assert not synthetic or status != VALID


def test_valid_urls_are_normalised_to_the_homepage_and_registrable_domain():
    _, _, ident, _ = check_url("https://www.Acme-Automation.IN/about/team?utm=1#x")
    assert ident.start_url == "https://www.acme-automation.in/" and ident.key == "acme-automation.in"
    _, _, ident2, _ = check_url("ACME-automation.in")
    assert ident2.key == ident.key


def test_synthetic_fixture_hosts_are_valid_only_when_a_test_allows_them_and_are_flagged():
    assert check_url("http://c1.fixture.test:5000/")[0] == RESERVED
    st, _, ident, synthetic = check_url("http://c1.fixture.test:5000/", allow_synthetic=True)
    assert st == VALID and synthetic and ident.host == "c1.fixture.test"
    cands, _ = build_candidates([hit("http://c1.fixture.test:5000/")], allow_synthetic=True)
    assert cands[0].synthetic is True
    cands, rej = build_candidates([hit("http://c1.fixture.test:5000/")])
    assert not cands and rej[0].status == RESERVED


# ------------------------------------------------------------------ dedupe
def test_dedupe_merges_same_business_keeps_first_source_and_records_the_others():
    hits = [hit("https://www.acme.in/", "Acme", "seed_page", "https://list.example.org/a", "a"),
            hit("acme.in", None, "osm_overpass", "https://www.openstreetmap.org/node/1", "b"),
            hit("https://acme.in/contact", "Acme Ltd", "manual_csv", "", "c"),
            hit("https://other-firm.in", "Other Firm")]
    cands, rej = build_candidates(hits)
    assert [c.domain for c in cands] == ["acme.in", "other-firm.in"]
    a = cands[0]
    assert a.discovery_source == "seed_page" and a.source_url == "https://list.example.org/a" and a.name == "Acme"
    assert a.duplicate_sources == [("osm_overpass", "https://www.openstreetmap.org/node/1"), ("manual_csv", "c")]
    assert [r.status for r in rej] == [DUPLICATE, DUPLICATE] and "acme.in" in rej[0].reason


def test_dedupe_fills_missing_name_from_a_duplicate_and_flags_same_name_on_different_domains():
    cands, _ = build_candidates([hit("acme.in"), hit("www.acme.in", "Acme Automation Pvt"),
                                 hit("acme-automation.com", "Acme Automation Pvt")])
    assert cands[0].name == "Acme Automation Pvt"
    assert len(cands) == 2 and "possible duplicate" in cands[1].validation_notes and cands[1].validation_status == VALID


# ------------------------------------------------------------------ CSV import
async def test_csv_import_maps_aliases_keeps_row_provenance_and_reports_bad_rows(tmp_path):
    p = write_csv(tmp_path / "mine.csv", [["Company", "Website URL", "City", "Notes", "Source URL"],
                                          ["Alpha Works", "https://alpha-works.in", "Pune", "met at event", "https://dir.example.org/alpha"],
                                          ["", "", "", "", ""], ["Beta", "", "Delhi", "", ""], ["Gamma", "gamma-labs.com", "", "", ""]])
    res = await discover([CsvImportSource(p)], None, niches=["AI automation agency"], locations=["Pune"], limit=10)
    assert [c.domain for c in res.candidates] == ["alpha-works.in", "gamma-labs.com"]
    a = res.candidates[0]
    assert (a.name, a.location, a.discovery_source, a.source_url, a.source_ref, a.evidence) == (
        "Alpha Works", "Pune", "manual_csv", "https://dir.example.org/alpha", "mine.csv#row 1", "met at event")
    assert a.niche_query == "AI automation agency" and a.location_query == "Pune"      # the run's query, not a claim about the company
    assert a.discovered_at.endswith("+00:00")
    kinds = sorted(e.kind for e in res.errors)
    assert kinds == ["bad_row", "bad_row"] and {e.where for e in res.errors} == {"mine.csv#row 2", "mine.csv#row 3"}
    assert res.raw_hits == {"manual_csv": 2}


async def test_csv_import_without_a_website_column_is_a_clear_error_not_a_crash(tmp_path):
    p = write_csv(tmp_path / "names.csv", [["Company"], ["Alpha"]])
    res = await discover([CsvImportSource(p)], None, niches=["x"], locations=[], limit=5)
    assert res.candidates == [] and res.errors[0].kind == "csv_unreadable" and "website" in res.errors[0].message


# ------------------------------------------------------------------ exports and compatibility with the existing workflow
async def test_candidate_csv_is_formula_safe_never_overwrites_and_is_readable_by_the_existing_workflow(tmp_path):
    p = write_csv(tmp_path / "in.csv", [["Company", "Website"], ["=HYPERLINK(\"http://x\")", "https://safe-agency.in"],
                                        ["Normal Co", "normal-co.in"]])
    res = await discover([CsvImportSource(p)], None, niches=["AI"], locations=[], limit=10)
    d = outputs.make_output_dir(tmp_path / "out")
    cp = outputs.write_candidates(d, res.candidates)
    with pytest.raises(FileExistsError):
        outputs.write_candidates(d, res.candidates)                                   # exclusive create: never overwrite
    assert outputs.make_output_dir(tmp_path / "out") != d and d.exists()
    text = cp.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text
    rd = CSVReader(str(cp))                                                           # the existing ingest understands our file
    rows = list(rd.rows())
    assert rd.mapping["website"] == "website" and rd.mapping["name"] == "name" and [r.domain for r in rows] == ["safe-agency.in", "normal-co.in"]
    assert all(r.error is None for r in rows)


def test_candidates_csv_has_the_required_provenance_columns():
    for col in ("discovery_source", "source_url", "niche_query", "location_query", "discovered_at", "validation_status", "review_status"):
        assert col in outputs.CANDIDATE_COLUMNS
    for col in ("source_url", "backend", "status", "method", "extracted_at"):
        assert all(f + "_" + col in outputs.LEAD_COLUMNS for f in REQUESTED)
    for col in ("enrichment_status", "backend_used", "fallback_used", "review_status", "validation_status", "discovered_at", "enriched_at",
                "discovery_source", "source_url", "niche_query", "location_query", "manual_review_reason"):
        assert col in outputs.LEAD_COLUMNS


# ------------------------------------------------------------------ niche and India evidence
def test_keywords_expand_and_niche_match_is_evidence_based_with_unknown_when_no_text():
    kw = expand_keywords(["AI automation agency"], ["whatsapp bot"])
    assert {"ai", "automation", "workflow", "n8n", "whatsapp bot"} <= set(kw) and "agency" not in kw
    assert niche_match(["We build workflow automation and AI agents for SMEs"], kw)[0] == "strong"
    assert niche_match(["Best accounting services"], kw) == ("none", "")
    assert niche_match(["We make paint"], kw)[0] == "none"
    assert niche_match([None, ""], kw) == ("unknown", "")
    assert niche_match(["said and trained"], kw)[0] == "none"                       # 'ai' must be a whole word, not 'said'


def test_india_evidence_needs_a_signal_from_the_company_and_absence_is_unconfirmed_not_no():
    assert india_evidence("acme.in", None, None) == ("yes", "tld_in")
    assert india_evidence("acme.com", "+91 98765 43210", None)[0] == "yes"
    assert india_evidence("acme.com", None, "4th Floor, Koramangala, Bengaluru, Karnataka 560034")[0] == "yes"
    assert india_evidence("acme.com", "+1 555-1234", "12 Main St, Austin, TX") == ("unconfirmed", "")
    assert india_evidence("acme.com", None, None, "Pune")[0] == "unconfirmed"       # a source's claim alone is not company evidence


# ------------------------------------------------------------------ OpenStreetMap query safety
def test_overpass_query_escapes_and_rejects_unsafe_locations():
    assert location_ok("Bengaluru") and location_ok("St. Mary's (East)") and not location_ok('Pune"]; out;') and not location_ok("")
    q = build_query("Pune", ["ai", "n8n", 'x"y', "make.com"], 50)
    assert 'area["name"="Pune"]' in q and "out tags center 50;" in q and 'x"y' not in q and "make\\.com" in q
    q2 = build_query('a"b', [], 10)
    assert 'a\\"b' in q2


async def test_osm_source_needs_a_location_and_never_contacts_anything_when_planning(tmp_path):
    src = OverpassSource("https://overpass.example.org/api/interpreter")
    ctx = SourceContext(fetcher=None, niches=["AI"], locations=["Pune", "Mumbai"], keywords=["ai"], limit=10)
    assert len(src.planned_requests(ctx)) == 2 and all("overpass.example.org" in r for r in src.planned_requests(ctx))
    ctx0 = SourceContext(fetcher=object(), niches=["AI"], locations=[], keywords=[], limit=10)
    assert await src.discover(ctx0) == [] and ctx0.errors[0].kind == "no_location"
    ctx1 = SourceContext(fetcher=object(), niches=["AI"], locations=['x"; out'], keywords=[], limit=10)
    assert await src.discover(ctx1) == [] and ctx1.errors[0].kind == "invalid_location"


# ------------------------------------------------------------------ live gate and forced limits
def test_live_gate_requires_explicit_matching_confirmation():
    assert live_gate(False, False, None, 5) is False
    with pytest.raises(DiscoveryError, match="only apply together"):
        live_gate(False, True, 5, 5)
    with pytest.raises(DiscoveryError, match="confirm-terms-reviewed"):
        live_gate(True, False, 5, 5)
    with pytest.raises(DiscoveryError, match="must equal"):
        live_gate(True, True, 4, 5)
    with pytest.raises(DiscoveryError, match="must equal"):
        live_gate(True, True, None, 5)
    assert live_gate(True, True, 5, 5) is True


def test_forced_limits_equal_the_stage5_pilot_limits_and_cannot_be_relaxed(monkeypatch):
    from benchmarks import pilot
    monkeypatch.delenv("LE_HOST_OVERRIDES", raising=False)
    monkeypatch.setenv("LE_USER_AGENT", "FlowHQBot/1.0 (+https://flowhq.example/bot; ops@flowhq.example)")
    ns = argparse.Namespace(database_url="postgresql://x@h/db", concurrency=99, min_delay=0.0, max_pages=10, retries=9, allow_browser=True,
                            browser_executable="", ai=True, ai_model="m", ai_price_in=None, ai_price_out=None)
    a, b = live_settings(database_url="postgresql://x@h/db", concurrency=99, min_delay=0.0, max_pages=10, retries=9), pilot.live_settings(ns)
    for k in ("respect_robots", "host_overrides", "allowed_ports", "egress_proxy", "global_concurrency", "per_domain_concurrency",
              "per_domain_min_delay", "max_pages_per_company", "max_pages_per_domain", "http_max_retries", "connect_timeout", "read_timeout",
              "total_timeout", "max_response_bytes", "max_redirects", "fetch_audit_log"):
        assert getattr(a, k) == getattr(b, k), k
    assert a.browser_enabled is False and a.ai_provider == "" and a.global_concurrency == 4 and a.per_domain_min_delay == 2.0
    monkeypatch.setenv("LE_HOST_OVERRIDES", "*.x=127.0.0.1")
    with pytest.raises(DiscoveryError, match="LE_HOST_OVERRIDES"):
        live_settings(database_url="postgresql://x@h/db")
    monkeypatch.delenv("LE_HOST_OVERRIDES")
    for ua in ("", "Bot/1.0", "LeadEnrichmentBot/0.1 (+https://example.invalid/bot; set LE_USER_AGENT)"):
        monkeypatch.setenv("LE_USER_AGENT", ua)
        with pytest.raises(DiscoveryError, match="LE_USER_AGENT"):
            live_settings(database_url="postgresql://x@h/db")


# ------------------------------------------------------------------ dry run and CLI gates contact nothing
async def test_dry_run_runs_offline_parts_lists_network_targets_and_contacts_nothing(tmp_path, no_network, clean_le_env):
    p = write_csv(tmp_path / "in.csv", [["Company", "Website"], ["Alpha", "http://alpha.fixture.test/"], ["Bad", "nope nope"]])
    plan = RunPlan(command="run", sources=[CsvImportSource(p), SeedPagesSource(["https://list.example.org/ai"]), OverpassSource()],
                   settings=forced_settings(database_url="postgresql://u@h/db", user_agent="x/1 (+https://a.b; a@b.c)"),
                   locations=["Pune"], out_root=str(tmp_path / "out"), authorized=False, allow_synthetic=True,
                   next_commands=["python -m leadengine.discovery run ... --live --confirm-terms-reviewed --confirm-count 101"])
    res = await execute(plan)
    d = Path(res["out_dir"])
    assert res["mode"].startswith("DRY RUN") and res["candidates"] == 1 and res["leads"] == 0
    rep = (d / "discovery_report.md").read_text(encoding="utf-8")
    assert "Dry run: no website was contacted" in rep and "NOT RUN (dry run)" in rep and "list.example.org/ai" in rep
    assert "overpass-api.de" in rep and "--confirm-count 101" in rep
    assert not (d / "discovery_fetch_log.jsonl").exists() and not (d / "fetch_log.jsonl").exists()
    assert {f.name for f in d.iterdir()} >= {"discovery_candidates.csv", "enriched_leads.csv", "discovery_report.md", "rejected_candidates.csv", "run_config.json"}


def test_cli_plan_prints_exact_targets_and_contacts_nothing(tmp_path, capsys, no_network, clean_le_env):
    p = write_csv(tmp_path / "in.csv", [["Website"], ["http://alpha.fixture.test/"]])
    assert cli_main(["plan", "--csv", p, "--seed-url", "https://list.example.org/a", "--osm", "--location", "Pune", "--location", "Mumbai",
                     "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert "would contact 3 target(s)" in out and "--confirm-count 3" in out and "--confirm-count 53" in out
    assert "list.example.org/a" in out and "Nothing was contacted" in out


def test_discovery_output_goes_to_a_new_directory_each_run_and_previous_outputs_are_kept(tmp_path, no_network, clean_le_env):
    p = write_csv(tmp_path / "in.csv", [["Website"], ["http://alpha.fixture.test/"]])
    out = tmp_path / "o"
    assert cli_main(["discover", "--csv", p, "--out-root", str(out)]) == 0
    assert cli_main(["discover", "--csv", p, "--out-root", str(out)]) == 0
    dirs = sorted(out.iterdir())
    assert len(dirs) == 2 and all((d / "discovery_candidates.csv").exists() for d in dirs)


# ------------------------------------------------------------------ lead rows: field status, backend and provenance
def view(**fields):
    base = {f: {"value": None, "extraction_state": "missing"} for f in REQUESTED}
    base.update(fields)
    return {"fields": base, "completeness": 0.5, "quality": 0.4, "last_enriched_at": "2026-01-02T00:00:00+00:00",
            "website": {"verification_status": "verified", "verification_method": "http_reachable_domain_match", "checks": {}}}


def fld(value, method="json_ld", state="extracted", ver="unverified", vm=None, url="https://acme.in/"):
    return {"value": value, "extraction_state": state, "method": method, "source_url": url, "extracted_at": "2026-01-02T00:00:00+00:00",
            "verification_status": ver, "verification_method": vm}


def lead_with(v, status="enriched", **kw):
    c, _ = build_candidates([hit("acme.in", "Acme", url="https://list.example.org/", ref="x")])
    l = Lead(c[0], status=status, view=v, enriched_at="2026-01-02T00:00:00+00:00", **kw)
    l.attempts = [Attempt("scrapegraph", "enriched")]
    return l


def test_lead_row_distinguishes_extracted_conflict_verified_and_missing_and_records_backend_and_source():
    v = view(company_name=fld("Acme Automation", "json_ld", ver="verified", vm="cross_source_agreement"),
             email=fld("=evil@x.in", "scrapegraph_llm", url="https://acme.in/contact"),
             phone=fld("+91 98765 43210", "tel", state="conflict"),
             address=fld(None, state="invalid"), description=fld("We build AI workflow automation for SMEs", "scrapegraph_llm"))
    row = outputs.lead_row(lead_with(v), expand_keywords(["AI automation agency"]))
    assert row["company_name_status"] == "extracted" and row["company_name_verification"] == "cross_source_agreement"   # same-site agreement is NOT 'independent'
    assert row["website_status"] == "independently_verified" and row["website_verification_method"] == "http_reachable_domain_match"
    assert (row["email_backend"], row["email_method"], row["email_source_url"], row["email_status"]) == ("scrapegraph", "scrapegraph_llm", "https://acme.in/contact", "extracted")
    assert (row["company_name_backend"], row["phone_status"], row["phone_backend"]) == ("inhouse", "conflict", "inhouse")
    assert row["address_status"] == "invalid" and row["address"] == "" and row["address_source_url"] == "" and row["industry_status"] == "not_found"
    assert row["email_extracted_at"] and row["backend_used"] == "mixed" and row["usable"] == "yes"
    assert row["niche_match"] == "strong" and row["india_signal"] == "yes" and "tld_in" in row["india_evidence"] and "phone_+91" in row["india_evidence"]
    assert row["discovery_source"] == "manual_csv" and row["source_url"] == "https://list.example.org/" and row["review_status"] == "pending_review"


def test_lead_row_flags_failures_for_manual_review_without_inventing_values():
    l = lead_with(view(), status="failed", error_kind="robots", error="disallowed by robots.txt")
    l.manual_review_reason = "failed (robots: disallowed by robots.txt); attempts: scrapegraph:failed"
    row = outputs.lead_row(l, ["ai"])
    assert row["review_status"] == "needs_manual_review" and row["usable"] == "no" and row["backend_used"] == "none"
    assert all(row[f] == "" and row[f + "_status"] == "not_found" and row[f + "_backend"] == "" for f in REQUESTED)
    assert row["niche_match"] == "unknown" and row["india_signal"] == "yes"            # .in domain is a company-level signal
    assert backend_of("scrapegraph_llm") == "scrapegraph" and backend_of("regex") == "inhouse" and backend_of(None) == ""


def test_errors_log_is_only_written_when_there_is_something_to_log(tmp_path):
    d = outputs.make_output_dir(tmp_path)
    assert outputs.write_errors(d, [], [lead_with(view())]) is None
    p = outputs.write_errors(d, [], [lead_with(view(), "failed", error_kind="robots", error="x")])
    rec = json.loads(p.read_text().splitlines()[0])
    assert rec["stage"] == "enrichment" and rec["kind"] == "robots" and rec["domain"] == "acme.in"


def test_report_never_counts_synthetic_runs_as_real_and_never_claims_the_target(tmp_path):
    d = outputs.make_output_dir(tmp_path)
    rows = [outputs.lead_row(lead_with(view(company_name=fld("A"), email=fld("a@acme.in"))), ["ai"])]
    cands, rej = build_candidates([hit("acme.in", "Acme")])
    dres = SimpleNamespace(candidates=cands, rejected=rej, errors=[], raw_hits={"manual_csv": 1}, notes=[], keywords=["ai"], capped_from=1)
    p = outputs.write_report(d, mode=SYNTH_LABEL, config={"started_at": "t"}, discovery=dres, leads=[lead_with(view())], rows=rows,
                             primary_note="n", files={"x": "y"}, timings={}, next_commands=["cmd"])
    txt = p.read_text(encoding="utf-8")
    assert "not real businesses and must not be counted toward the 100-lead target" in txt
    assert "production-ready" not in txt.lower() and "target met" not in txt.lower() and "target reached" not in txt.lower()
    assert "Presence is not correctness" in txt and "never mailbox-verified" in txt


# ------------------------------------------------------------------ manual review
def test_review_marks_fields_manually_reviewed_writes_a_new_file_and_validates_input(tmp_path):
    l = lead_with(view(company_name=fld("Acme"), email=fld("info@acme.in"), phone=fld("+91 98765 43210")))
    d = outputs.make_output_dir(tmp_path)
    lp, _ = outputs.write_leads(d, [l], ["ai"])
    original = lp.read_bytes()
    rv = write_csv(tmp_path / "rv.csv", [["domain", "field", "verdict", "corrected_value", "reviewed_by", "notes"],
                                         ["acme.in", "company_name", "correct", "", "Vatsal", ""],
                                         ["acme.in", "email", "incorrect", "", "Vatsal", "that is a webmaster alias"],
                                         ["acme.in", "phone", "corrected", "+91 99999 00000", "Vatsal", "from contact page"]])
    out = apply_review(lp, rv)
    assert out != lp and lp.read_bytes() == original                                   # original untouched
    row = next(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert row["company_name_status"] == "manually_reviewed" and row["company_name_backend"] == "inhouse"
    assert row["email_status"] == "rejected_by_review" and row["email"] == "info@acme.in"
    assert (row["phone"], row["phone_status"], row["phone_backend"], row["phone_method"], row["phone_source_url"]) == (
        "+91 99999 00000", "manually_reviewed", "manual", "manual_correction", "")
    assert row["reviewed_by"] == "Vatsal" and row["reviewed_at"] and row["review_status"] == "reviewed" and "webmaster" in row["review_notes"]
    assert row["usable"] == "yes"                                                       # name + corrected phone remain
    with pytest.raises(FileExistsError):
        apply_review(lp, rv, out)
    for bad_rows, pat in [([["nope.in", "email", "correct", "", "V", ""]], "not in the leads file"),
                          ([["acme.in", "bogus", "correct", "", "V", ""]], "unknown field"),
                          ([["acme.in", "email", "maybe", "", "V", ""]], "verdict"),
                          ([["acme.in", "email", "correct", "", "", ""]], "reviewed_by"),
                          ([["acme.in", "address", "correct", "", "V", ""]], "no extracted value"),
                          ([["acme.in", "email", "corrected", "", "V", ""]], "corrected_value")]:
        bad = write_csv(tmp_path / "bad.csv", [["domain", "field", "verdict", "corrected_value", "reviewed_by", "notes"], *bad_rows])
        with pytest.raises(ReviewError, match=pat):
            apply_review(lp, bad, tmp_path / "never.csv")
        assert not (tmp_path / "never.csv").exists()


# ------------------------------------------------------------------ doctor: read-only, loopback only, never downloads
def test_doctor_reports_model_presence_without_pulling_anything():
    with StubLLMServer() as stub:
        stub.models = ["qwen3:4b", "llama3.2:latest"]
        items = {i["check"]: i for i in doctor_mod.check_ollama(stub.base_url, "qwen3:4b")}
        assert items["model qwen3:4b installed"]["ok"] and items["ollama server reachable"]["ok"]
        stub.models = ["llama3.2:latest"]
        m = {i["check"]: i for i in doctor_mod.check_ollama(stub.base_url, "qwen3:4b")}["model qwen3:4b installed"]
        assert not m["ok"] and "ollama pull qwen3:4b" in m["fix"] and "not run automatically" in m["fix"]
        assert {(r["method"], r["path"].split("?")[0]) for r in stub.requests} == {("GET", "/api/tags")}      # list only: no /api/pull, no /api/chat


def test_doctor_refuses_non_loopback_hosts_and_handles_a_stopped_server():
    r = doctor_mod.check_ollama("http://192.0.2.10:11434", "qwen3:4b")
    assert not r[0]["ok"] and "not a loopback" in r[0]["detail"]
    assert doctor_mod.is_loopback("http://localhost:11434") and doctor_mod.is_loopback("http://[::1]:11434") and not doctor_mod.is_loopback("http://10.0.0.5")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    down = doctor_mod.check_ollama(f"http://127.0.0.1:{port}", "qwen3:4b")
    assert [i["ok"] for i in down if i["check"] == "ollama server reachable"] == [False]


def test_doctor_checks_the_library_interpreter(tmp_path):
    bad = doctor_mod.check_interpreter(str(tmp_path / "no-such-python"))
    assert not bad[0]["ok"] and "LE_SCRAPEGRAPH_PYTHON" in bad[0]["fix"]
    import importlib.util
    if not importlib.util.find_spec("scrapegraphai"):
        no_lib = doctor_mod.check_interpreter(__import__("sys").executable)
        assert not no_lib[0]["ok"] and "pip install" in no_lib[0]["fix"]
    if sg_available():
        ok = doctor_mod.check_interpreter(SG_PYTHON)
        assert all(i["ok"] for i in ok) and ok[1]["detail"].startswith("2.3.")


def test_doctor_overall_verdict_names_the_fallback_when_not_ready():
    r = doctor_mod.run_doctor(model="qwen3:4b", base_url="http://127.0.0.1:9", python="/nonexistent/python")
    assert r["ready_for_scrapegraph_ollama"] is False and "in-house" in r["if_not_ready"]
