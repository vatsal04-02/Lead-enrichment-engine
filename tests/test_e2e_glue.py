"""leadengine.discovery.e2e: city-balanced selection, de-duplication, no-overwrite, and summary counting. Pure local files; synthetic rows only."""
import csv
import json

import pytest

from leadengine.discovery import e2e

CAND_COLS = ["website", "name", "location", "industry", "domain", "discovery_source", "source_url", "source_ref", "validation_status", "validation_notes", "synthetic"]


def cand(i, city, industry="", name=None, domain=None, status="valid", synthetic="false"):
    d = domain or f"company{i}.example.invalid"
    return {"website": f"https://{d}/", "name": name if name is not None else f"Company {i} Pvt Ltd", "location": city, "industry": industry, "domain": d,
            "discovery_source": "wikidata", "source_url": f"https://src.invalid/Q{i}", "source_ref": f"q{i}", "validation_status": status,
            "validation_notes": "", "synthetic": synthetic}


def write(path, cols, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_selection_is_balanced_across_cities_and_industries_not_first_come(tmp_path):
    rows = [cand(i, "Bengaluru", "software") for i in range(40)] + [cand(100 + i, "Pune", "banking" if i % 2 else "") for i in range(10)] + \
           [cand(200 + i, "Lucknow") for i in range(5)]
    src = tmp_path / "discovery_candidates.csv"
    write(src, CAND_COLS, rows)
    res = e2e.prepare(str(src), str(tmp_path / "out"), target=15)
    assert res["selected_for_enrichment"] == 15 and res["reached_target"]
    assert res["cities_selected"] == {"Bengaluru": 5, "Pune": 5, "Lucknow": 5}, "a big first city must not fill the target"
    pune = [r for r in read(res["profile_input_csv"]) if r["City"] == "Pune"]
    assert {r["Industry/Niche"] for r in pune[:2]} == {"banking", ""}, "industries alternate inside a city"


def test_duplicates_invalid_and_synthetic_rows_are_never_selected_and_everything_is_recorded(tmp_path):
    rows = [cand(1, "Pune"), cand(2, "Pune", domain="company1.example.invalid"), cand(3, "Pune", name="Company 1 Limited", domain="other.example.invalid"),
            cand(4, "Pune", synthetic="true"), cand(5, "Delhi"), cand(6, "Delhi", name="")]
    src = tmp_path / "discovery_candidates.csv"
    write(src, CAND_COLS, rows)
    write(tmp_path / "rejected_candidates.csv", ["raw_website", "name", "validation_status", "reason", "discovery_source", "source_url", "source_ref"],
          [{"raw_website": "https://www.facebook.com/x", "name": "Social Co", "validation_status": "blocked_domain", "reason": "social network",
            "discovery_source": "wikidata", "source_url": "https://src.invalid/Q9", "source_ref": "q9"}])
    res = e2e.prepare(str(src), str(tmp_path / "out"), target=100)
    disc = read(res["discovery_csv"])
    assert [r["DiscoveryStatus"] for r in disc[:4]] == ["selected", "duplicate_domain", "duplicate_identity", "synthetic_excluded"], "in file order, nothing dropped"
    assert [r["DiscoveryStatus"] for r in disc if r["Website"].startswith("https://www.facebook")] == ["blocked_domain"]
    assert res["valid_unique_companies"] == 3 and res["selected_for_enrichment"] == 3
    assert len(read(res["profile_input_csv"])) == 3 and "company4.example.invalid" not in open(res["profile_input_csv"]).read()
    assert set(read(res["discovery_csv"])[0]) >= {"Company", "Website", "City", "Industry/Niche", "SourceURL", "DiscoveryStatus"}
    assert res["reached_target"] is False, "fewer than the target: the real count is reported, nothing is padded"


def test_unselected_valid_companies_stay_in_the_discovery_csv_and_outputs_are_never_overwritten(tmp_path):
    src = tmp_path / "discovery_candidates.csv"
    write(src, CAND_COLS, [cand(i, "Pune") for i in range(5)])
    res = e2e.prepare(str(src), str(tmp_path / "out"), target=3)
    counts = res["status_counts"]
    assert counts == {"selected": 3, "discovered_not_selected": 2}
    with pytest.raises(FileExistsError):
        e2e._write(__import__("pathlib").Path(res["discovery_csv"]), e2e.DISCOVERY_COLUMNS, [])
    with pytest.raises(SystemExit):
        e2e.prepare(str(tmp_path / "missing.csv"), str(tmp_path / "out"))


def test_web_derived_names_are_spreadsheet_safe(tmp_path):
    src = tmp_path / "discovery_candidates.csv"
    write(src, CAND_COLS, [cand(1, "Pune", name="=HYPERLINK(1) Co")])
    res = e2e.prepare(str(src), str(tmp_path / "out"))
    assert read(res["discovery_csv"])[0]["Company"].startswith("'=") and read(res["profile_input_csv"])[0]["Company"].startswith("'=")


def test_summary_counts_come_from_the_files_and_anti_bot_is_not_a_plain_failure(tmp_path):
    disc = [{"Company": f"C{i}", "Website": f"https://c{i}.invalid/", "City": c, "Industry/Niche": "", "SourceURL": "", "DiscoveryStatus": s}
            for i, (c, s) in enumerate([("Pune", "selected")] * 6 + [("Delhi", "selected"), ("Delhi", "discovered_not_selected"), ("Delhi", "duplicate_domain")])]
    dcols = list(disc[0])
    write(tmp_path / "d.csv", dcols, disc)
    enr = [("ok", "high", ""), ("ok", "medium", ""), ("ok", "low", ""), ("no_evidence", "none", "the model's statements could not be tied to text on the pages"),
           ("failed", "", "bot_challenge: site served an anti-bot challenge; not attempting to bypass"), ("failed", "", "robots: disallowed by robots.txt"),
           ("failed", "", "http: HTTP 500")]
    rows = [{"Company": f"C{i}", "City": "Pune", "status": s, "confidence": c, "error": e, "industry_niche": "Retail" if c == "high" else ""} for i, (s, c, e) in enumerate(enr)]
    write(tmp_path / "e.csv", list(rows[0]), rows)
    (tmp_path / "e.summary.json").write_text(json.dumps({"model": "qwen3:4b", "seconds": 12.5, "model_usage": {"calls": 3, "tokens_in": 10, "tokens_out": 5}}))
    res = e2e.summarize(str(tmp_path / "d.csv"), str(tmp_path / "e.csv"), str(tmp_path / "e.summary.json"), str(tmp_path / "s"))
    assert (res["total_discovered"], res["unique_companies"], res["selected_for_enrichment"]) == (9, 8, 7)
    assert (res["successfully_enriched"], res["partially_enriched"], res["failed"], res["anti_bot_blocked"], res["robots_disallowed"]) == (2, 2, 1, 1, 1)
    assert res["model"] == "qwen3:4b" and res["elapsed_seconds_enrichment"] == 12.5 and res["model_usage"]["calls"] == 3 and res["paid_api_cost_usd"] == 0.0
    assert res["industry_distribution_from_websites"] == {"Retail": 1, "(not stated)": 3} and json.loads(open(res["summary_file"]).read())["anti_bot_blocked"] == 1
