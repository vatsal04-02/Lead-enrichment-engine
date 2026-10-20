import argparse
import asyncio
import csv
import json
import os

import pytest

from benchmarks import pilot
from tests.conftest import DB_URL

GOOD = {"domain": "acme-example.org", "name": "Acme", "permission_basis": "own website operated by the pilot owner", "reviewed_by": "alice"}


def write(tmp_path, rows, header=("domain", "name", "permission_basis", "reviewed_by")):
    p = tmp_path / "s.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return str(p)


def test_sample_validation_accepts_a_reviewed_sample_and_ignores_comment_lines(tmp_path):
    p = write(tmp_path, [GOOD])
    open(p, "a").write("# a comment line\n")
    rows = pilot.load_sample(p)
    assert rows == [{"domain": "acme-example.org", "name": "Acme", "permission_basis": GOOD["permission_basis"], "reviewed_by": "alice"}]


@pytest.mark.parametrize("mutation,needle", [
    ({"permission_basis": "TODO"}, "permission_basis"), ({"permission_basis": "ok"}, "permission_basis"),
    ({"permission_basis": ""}, "permission_basis"), ({"reviewed_by": ""}, "reviewed_by"),
    ({"domain": "https://acme.org/about"}, "bare domain"), ({"domain": "acme.org/path"}, "bare domain"),
    ({"domain": "10.0.0.5"}, "IP"), ({"domain": "localhost"}, "not a valid"), ({"domain": "shop.acme.org"}, "registrable domain"),
    ({"domain": "user@acme.org"}, "bare domain"),
])
def test_sample_validation_rejects_unsafe_or_unjustified_rows(tmp_path, mutation, needle):
    with pytest.raises(SystemExit) as e:
        pilot.load_sample(write(tmp_path, [{**GOOD, **mutation}]))
    assert needle in str(e.value)


def test_sample_validation_rejects_duplicates_missing_columns_empty_and_oversized(tmp_path):
    with pytest.raises(SystemExit, match="duplicates"):
        pilot.load_sample(write(tmp_path, [GOOD, {**GOOD, "domain": "WWW.Acme-Example.org"}]))
    with pytest.raises(SystemExit, match="columns"):
        pilot.load_sample(write(tmp_path, [{"domain": "a.org"}], header=("domain",)))
    with pytest.raises(SystemExit, match="no rows"):
        pilot.load_sample(write(tmp_path, []))
    many = [{**GOOD, "domain": f"site{i}.org"} for i in range(pilot.MAX_SITES + 1)]
    with pytest.raises(SystemExit, match="capped"):
        pilot.load_sample(write(tmp_path, many))


def args(**kw):
    base = dict(database_url="postgresql://x@h/enrich_pilot", concurrency=99, min_delay=0.0, max_pages=10, retries=9,
                allow_browser=False, browser_executable="", ai=False, ai_model="m", ai_price_in=None, ai_price_out=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_live_settings_force_conservative_limits_and_require_identification(monkeypatch):
    monkeypatch.delenv("LE_HOST_OVERRIDES", raising=False)
    for ua in ("", "Bot/1.0", "LeadEnrichmentBot/0.1 (+https://example.invalid/bot; set LE_USER_AGENT)"):
        monkeypatch.setenv("LE_USER_AGENT", ua)
        with pytest.raises(SystemExit, match="LE_USER_AGENT"):
            pilot.live_settings(args())
    monkeypatch.setenv("LE_USER_AGENT", "PilotBot/1.0 (+https://acme.example/bot; ops@acme.example)")
    s = pilot.live_settings(args())
    assert (s.global_concurrency, s.per_domain_concurrency, s.max_pages_per_company, s.http_max_retries) == (4, 1, 3, 1)
    assert s.per_domain_min_delay >= 2.0 and s.respect_robots and s.host_overrides == "" and not s.browser_enabled
    assert s.ai_provider == "" and s.total_timeout <= 20 and s.max_response_bytes <= 1_000_000
    monkeypatch.setenv("LE_RESPECT_ROBOTS", "false")
    assert pilot.live_settings(args()).respect_robots is True            # cannot be switched off for the pilot
    monkeypatch.setenv("LE_HOST_OVERRIDES", "x=127.0.0.1")
    with pytest.raises(SystemExit, match="HOST_OVERRIDES"):
        pilot.live_settings(args())


def test_live_run_refuses_without_explicit_opt_in_and_never_starts_the_engine(tmp_path, monkeypatch):
    p = write(tmp_path, [GOOD])
    started = []
    monkeypatch.setattr(pilot, "Engine", lambda *a, **k: started.append(1))
    base = dict(sample=p, fixtures=False, confirm_permitted=False, confirm_sites=-1, out_dir=str(tmp_path), reset=False,
                database_url="postgresql://x@h/enrich_pilot", **{k: v for k, v in vars(args()).items() if k != "database_url"})
    for kw in (dict(), dict(confirm_permitted=True), dict(confirm_permitted=True, confirm_sites=2), dict(confirm_sites=1)):
        with pytest.raises(SystemExit, match="Refusing"):
            asyncio.run(pilot.run(argparse.Namespace(**{**base, **kw})))
    assert not started


def test_database_name_guard():
    pilot.check_db_name("postgresql://u@h:5432/enrich_pilot")
    pilot.check_db_name("postgresql://u@h:5432/enrich_pilot?sslmode=require")
    for bad in ("postgresql://u@h/production", "postgresql://u@h/enrich", "postgresql://u@h/pilot_enrich"):
        with pytest.raises(SystemExit, match="_pilot"):
            pilot.check_db_name(bad)


def test_wilson_interval_and_audit_scoring(tmp_path):
    lo, hi = pilot.wilson(8, 10)
    assert 0.48 < lo < 0.50 and 0.93 < hi < 0.95
    assert pilot.wilson(0, 0) is None and pilot.wilson(10, 10)[1] == 1.0
    p = tmp_path / "a.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "field", "value", "source_url", "method", "verdict (correct|incorrect|unsure)", "notes"])
        for i, v in enumerate(["correct"] * 8 + ["incorrect"] * 2 + ["unsure"] * 3 + [""] * 2):
            w.writerow([f"d{i}", "email", "x", "u", "m", v, ""])
    r = pilot.score_sheet(str(p))
    e = r["per_field"]["email"]
    assert (e["correct"], e["incorrect"], e["unsure"]) == (8, 2, 3) and e["precision"]["value"] == 0.8
    assert r["rows_without_verdict"] == 2 and r["recall"]["label"] == "UNKNOWN" and r["overall_precision"]["label"] == "MEASURED"


def test_fixture_dry_run_produces_labelled_report_and_artifacts(tmp_path):
    """Exercises the whole pilot pipeline on SYNTHETIC sites (never real ones)."""
    pilot_db = DB_URL.rsplit("/", 1)[0] + "/enrich_pilot"
    try:
        import psycopg
        psycopg.connect(pilot_db, connect_timeout=3).close()
    except Exception:
        pytest.skip("database enrich_pilot not available (scripts/dev_postgres.sh creates it)")
    a = argparse.Namespace(sample=None, fixtures=True, fixture_sites=15, confirm_permitted=False, confirm_sites=-1,
                           database_url=pilot_db, reset=True, concurrency=4, min_delay=0.0, max_pages=3, retries=1, max_seconds=300,
                           allow_browser=False, browser_executable="", ai=False, ai_model="m", ai_price_in=None, ai_price_out=None,
                           compute_usd_per_hour=0.0, audit_per_field=30, out_dir=str(tmp_path))
    asyncio.run(pilot.run(a))
    rep = json.loads(next(tmp_path.glob("pilot-*.json")).read_text())
    assert rep["mode"].startswith("FIXTURE") and rep["results"]["volume"]["unique_companies_attempted"]["value"] == 15
    r = rep["results"]
    assert r["accuracy"]["label"] == "UNKNOWN" and r["usable_record_rate"]["audited"]["label"] == "UNKNOWN"
    assert r["coverage_presence_only"]["email_mailbox_verified"]["value"] == 0
    assert r["ai_cost"]["infrastructure"]["label"] == "UNKNOWN"
    assert r["ai_cost"]["per_1000_attempted_usd"]["note"] == "no AI calls were made"
    assert {"MEASURED", "UNKNOWN"} <= {v.get("label") for sec in r.values() if isinstance(sec, dict) for v in sec.values() if isinstance(v, dict)}
    for suffix in ("provenance.csv", "fetch-log.jsonl", "audit.csv"):
        assert next(tmp_path.glob(f"pilot-*-{suffix}")).stat().st_size > 0
    assert rep["fixture_auto_audit"]["overall_precision"]["value"] >= 0.99
    lines = [json.loads(l) for l in next(tmp_path.glob("pilot-*-fetch-log.jsonl")).read_text().splitlines()]
    assert any(l["kind"] == "robots" for l in lines) and all(l["job_id"] == rep["job_id"] for l in lines)
