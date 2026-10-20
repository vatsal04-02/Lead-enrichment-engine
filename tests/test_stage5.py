"""Stage-5 regressions: Windows/Excel/PowerShell friendliness of the pilot tooling, DB guard, review sheet + scoring."""
import argparse
import asyncio
import csv
import importlib
import json
import os
import subprocess
import sys

import pytest

from benchmarks import pilot
from tests.conftest import DB_URL

GOOD_ROW = ["acme-example.org", "Müller GmbH", "own website operated by the pilot owner", "alice"]
HEADER = ["domain", "name", "permission_basis", "reviewed_by"]


def text(rows, sep=","):
    return "\r\n".join(sep.join(f'"{c}"' if sep in c else c for c in r) for r in rows) + "\r\n"


# ------------------------------------------------------------------ input files as Windows tools write them
@pytest.mark.parametrize("encoding,sep", [("utf-8", ","), ("utf-8-sig", ","), ("utf-16", ","), ("cp1252", ","), ("utf-8-sig", ";"), ("cp1252", ";")])
def test_sample_loads_from_powershell_and_excel_encodings_and_delimiters(tmp_path, encoding, sep):
    p = tmp_path / "s.csv"
    p.write_bytes(text([HEADER, GOOD_ROW], sep).encode(encoding))
    rows = pilot.load_sample(str(p))
    assert rows[0]["domain"] == "acme-example.org" and rows[0]["name"] == "Müller GmbH"


def test_binary_file_gets_a_friendly_error(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"PK\x03\x04\x00\x00" * 50)                       # e.g. an .xlsx saved with a .csv extension
    with pytest.raises(SystemExit, match="binary"):
        pilot.load_sample(str(p))


def test_shipped_templates_are_consistent_with_the_code():
    with pytest.raises(SystemExit) as e:                            # every placeholder row must be rejected
        pilot.load_sample("benchmarks/templates/pilot_input.csv")
    msg = str(e.value)
    assert msg.count("domain 'REPLACE_ME_") == 20 and msg.count("permission_basis") == 20
    assert "line 8:" in msg and "line 27:" in msg                  # real file lines: 7 header/comment lines precede the first row
    headers, rows = pilot.read_csv_flexible("benchmarks/templates/pilot_input.csv", skip_comments=True)
    assert headers == HEADER and len(rows) == 20
    headers, rows = pilot.read_csv_flexible("benchmarks/templates/pilot_review.csv")
    assert headers == pilot.REVIEW_COLUMNS and rows == []


def test_database_guard_rejects_url_tricks():
    pilot.check_db_name("postgresql://u:p%40ss@localhost:5432/enrich_pilot")
    pilot.check_db_name("postgres://u@h/enrich_pilot?sslmode=require")
    for bad in ["postgresql://u@h/enrich_pilot?dbname=production", "postgresql://u@h/enrich_pilot?options=-csearch_path%3Dprod",
                "postgresql://u@h/enrich_pilot?service=prod", "postgresql://u@h/production", "postgresql://u@h/",
                "host=h dbname=enrich_pilot", "mysql://u@h/enrich_pilot", "enrich_pilot"]:
        with pytest.raises(SystemExit):
            pilot.check_db_name(bad)


# ------------------------------------------------------------------ platform behaviour
def test_windows_gets_the_selector_event_loop(monkeypatch):
    from leadengine import compat
    calls = []
    monkeypatch.setattr(compat.sys, "platform", "win32")
    monkeypatch.setattr(compat.asyncio, "WindowsSelectorEventLoopPolicy", lambda: "SELECTOR", raising=False)
    monkeypatch.setattr(compat.asyncio, "set_event_loop_policy", lambda p: calls.append(p))
    compat.configure_event_loop()
    assert calls == ["SELECTOR"]
    calls.clear()
    monkeypatch.setattr(compat.sys, "platform", "linux")
    compat.configure_event_loop()
    assert calls == []                                              # untouched elsewhere


def test_reporting_helpers_never_crash_without_git_psql_or_unix_modules(monkeypatch):
    from benchmarks import sysinfo
    monkeypatch.setattr(sysinfo.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert "unknown" in sysinfo.git_commit()
    assert set(sysinfo.hardware()) >= {"platform", "cpu", "memory", "python", "postgres", "git_commit"}
    monkeypatch.undo()
    import benchmarks.run_local as rl
    monkeypatch.setitem(sys.modules, "resource", None)             # `import resource` -> ImportError, as on Windows
    try:
        rl = importlib.reload(rl)
        assert rl.resource is None
    finally:
        monkeypatch.undo()
        importlib.reload(rl)


def test_pilot_writes_all_outputs_under_a_non_utf8_default_encoding(tmp_path):
    """Report text contains characters outside ASCII; with the old code a legacy-locale machine lost the report AFTER the run."""
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LE_DATABASE_URL": DB_URL.rsplit("/", 1)[0] + "/enrich_pilot"}
    p = subprocess.run([sys.executable, "-m", "benchmarks.pilot", "run", "--fixtures", "--fixture-sites", "8", "--reset", "--concurrency", "4",
                        "--min-delay", "0", "--out-dir", str(tmp_path)], env=env, capture_output=True, text=True, timeout=180)
    if "does not exist" in p.stderr or "connection" in p.stderr.lower():
        pytest.skip("database enrich_pilot not available")
    assert p.returncode == 0, p.stderr[-800:]
    for suffix in (".json", ".md", "-review.csv", "-provenance.csv", "-fetch-log.jsonl"):
        assert next(tmp_path.glob(f"pilot-*{suffix}")).stat().st_size > 0
    assert "…" in next(tmp_path.glob("pilot-*.md")).read_text(encoding="utf-8")


# ------------------------------------------------------------------ review sheet
def make_prov(domain, name=None, email=None, phone=None, web_ok=True):
    def r(field, value, url=None):
        return dict(domain=domain, field=field, value=value, status="found" if value else "not_found",
                    source_url=url or f"https://{domain}/", method="json_ld", verification_status="unverified",
                    verification_method=None, checks={})
    out = [dict(r("website", f"https://{domain}/"), verification_status="verified" if web_ok else "failed",
                verification_method="http_reachable_domain_match" if web_ok else None, checks={} if web_ok else {"error_kind": "robots"})]
    for f, v in (("company_name", name), ("email", email), ("phone", phone)):
        if v:
            out.append(r(f, v, f"https://{domain}/contact" if f != "company_name" else None))
    return out


def test_review_sheet_is_prefilled_flags_shared_contacts_and_failures(tmp_path):
    prov = make_prov("a.com", "A Co", "info@shared.com", "+1 512 555 0100") + make_prov("b.com", "B Co", "INFO@shared.com", "+1 (512) 555-0100") \
        + make_prov("c.com", "C Co", "c@c.com") + make_prov("d.com", web_ok=False)
    items = [dict(row_index=i, input={"website": d, "name": n}, domain=d, status=s, error_kind=ek, error=er)
             for i, (d, n, s, ek, er) in enumerate([("a.com", "A", "succeeded", None, None), ("b.com", "B", "succeeded", None, None),
                                                    ("c.com", "C", "succeeded", None, None),
                                                    ("d.com", "D", "failed", "unreachable_robots", "disallowed by robots.txt"),
                                                    ("e.com", "E", "failed", "unreachable_http", "HTTP 404")], 1)]
    path = pilot.write_review_sheet(tmp_path / "r.csv", prov, items)
    _, rows = pilot.read_csv_flexible(str(path))
    assert [r["input_domain"] for r in rows] == ["a.com", "b.com", "c.com", "d.com", "e.com"]
    a, b, c, d, e = rows
    assert a["extracted_company_name"] == "A Co" and a["email_source_url"] == "https://a.com/contact" and a["website_status"].startswith("verified")
    assert "shared_email_with:b.com" in a["duplicate_status"] and "shared_phone_with:b.com" in a["duplicate_status"]
    assert "shared_email_with:a.com" in b["duplicate_status"]                         # case-insensitive e-mail, formatting-insensitive phone
    assert c["duplicate_status"] == "unique"
    assert d["website_status"] == "failed:robots" and d["failure_reason"].startswith("unreachable_robots")
    assert e["website_status"] == "not_fetched" and e["failure_reason"].startswith("unreachable_http")
    for r in rows:
        assert r["company_name_verdict"] == r["email_verdict"] == r["phone_verdict"] == r["usable_record"] == ""    # humans fill these


def test_review_scoring_precision_recall_usable_and_cost(tmp_path):
    headers = pilot.REVIEW_COLUMNS
    rows = []

    def row(name, email, phone, nv, ev, pv, usable="", dup="unique", fail=""):
        d = dict.fromkeys(headers, "")
        d.update(input_domain="x", extracted_company_name=name, extracted_email=email, extracted_phone=phone, company_name_verdict=nv,
                 email_verdict=ev, phone_verdict=pv, usable_record=usable, duplicate_status=dup, failure_reason=fail)
        rows.append(d)
    row("A", "a@a.com", "1", "correct", "correct", "correct", "yes")
    row("B", "b@b.com", "", "correct", "misattributed", "absent", "no")
    row("C", "", "3", "incorrect", "missed", "correct", "yes")                    # reviewer says usable but verdicts say not
    row("", "", "", "missed", "absent", "unchecked", "no", fail="unreachable_robots: disallowed")
    row("E", "e@e.com", "5", "unsure", "correct", "correct", "", dup="shared_email_with:a.com")
    path = tmp_path / "rv.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    run_json = tmp_path / "run.json"
    run_json.write_text(json.dumps({"results": {"volume": {"unique_companies_attempted": {"value": 5}},
                                                "ai_cost": {"per_1000_attempted_usd": {"value": 2.0, "label": "ESTIMATED", "note": "x"}}}}))
    r = pilot.score_review(str(path), str(run_json))
    nm, em, ph = r["per_field"]["company_name"], r["per_field"]["email"], r["per_field"]["phone"]
    assert (nm["correct"], nm["incorrect"], nm["missed"]) == (2, 1, 1) and nm["precision"]["value"] == round(2 / 3, 3)
    assert (em["correct"], em["misattributed"], em["missed"], em["absent"]) == (2, 1, 1, 1)
    assert em["precision"]["value"] == round(2 / 3, 3) and em["recall_reviewed"]["value"] == round(2 / 3, 3)
    assert ph["unchecked"] == 1 and ph["correct"] == 3
    assert r["misattributed_values"]["value"] == 1 and r["incorrect_values"]["value"] == 1
    assert r["usable_audited"]["value"] == 0.2                                        # only row A has correct name AND correct contact
    assert r["usable_disagreements_with_verdicts"] == 1 and r["usable_unreviewed"] == 1
    assert r["duplicate_flags"]["value"] == 1 and r["failure_reasons"]["value"] == {"unreachable_robots": 1}
    assert r["not_in_review_sheet"]["label"] == "UNKNOWN"
    c = r["ai_cost_per_1000_usable_audited_usd"]
    assert c["label"] == "ESTIMATED" and c["value"] == 10.0                            # run total $0.01 over 1 audited-usable record x 1000


def test_review_scoring_flags_invalid_verdict_combinations(tmp_path):
    headers = pilot.REVIEW_COLUMNS
    d = dict.fromkeys(headers, "")
    d.update(extracted_email="", email_verdict="correct", extracted_company_name="X", company_name_verdict="absent")
    path = tmp_path / "bad.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerow(d)
    r = pilot.score_review(str(path))
    assert len(r["problems"]) == 2 and any("email_verdict" in p for p in r["problems"])
    with pytest.raises(SystemExit, match="missing columns"):
        p2 = tmp_path / "short.csv"
        p2.write_text("a,b\n1,2\n")
        pilot.score_review(str(p2))
