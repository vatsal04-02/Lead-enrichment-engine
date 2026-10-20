"""Controlled live-web pilot + repeatable measurement report.

    python -m benchmarks.pilot validate  --sample my_sites.csv                 # no network
    python -m benchmarks.pilot run       --sample my_sites.csv --confirm-permitted --confirm-sites N [...]
    python -m benchmarks.pilot score     --audit benchmarks/results/pilot-<ts>-audit.csv [--run-json ...]
    python -m benchmarks.pilot run --fixtures                                   # same machinery on SYNTHETIC local sites

Guard rails (live mode; none can be relaxed from the command line):
  * explicit opt-in: --confirm-permitted AND --confirm-sites <exact row count>;
  * the sample file is manually curated: every row needs a `permission_basis` and `reviewed_by`; max 50 rows;
    only bare registrable domains (no URLs/paths/IPs) -> this tool cannot be pointed at "the web";
  * LE_USER_AGENT must identify you and contain a contact (URL or e-mail);
  * robots.txt is enforced (401/403 on robots.txt = disallow; Crawl-delay honoured; >30s Crawl-delay = skip);
    anti-bot challenges / CAPTCHAs / 401 / 403 are recorded as failures and never retried, rendered or bypassed;
  * conservative caps are forced: >=2 s between requests to a host, 1 connection per host, <=4 companies at once,
    <=3 pages/company, 1 retry, 1 MB responses, 20 s hard deadline per request, browser OFF unless --allow-browser,
    AI OFF unless --ai;
  * the SSRF guard is fully on (LE_HOST_OVERRIDES must be empty); every request lands in fetch_log and in a
    JSONL file next to the report; every extracted value is exported with source URL and method;
  * the database name must end in _pilot (it is truncated with --reset), never your production database.

Honesty rules for the report: each number is labelled MEASURED, ESTIMATED or UNKNOWN. Correctness is only claimed
from a human audit (`score`). E-mail is never "verified": no mailbox-level verification exists in this project.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadengine import jobs, metrics  # noqa: E402
from leadengine.config import Settings  # noqa: E402
from leadengine.domain import InvalidDomain, normalize  # noqa: E402
from leadengine.engine import Engine  # noqa: E402
from leadengine.worker import Worker  # noqa: E402

MAX_SITES = 50
FIELDS_REPORTED = ["company_name", "phone", "email", "address", "description", "linkedin_url"]
REQUIRED_COLS = ["domain", "permission_basis", "reviewed_by"]
PLACEHOLDER = re.compile(r"^(todo|tbd|n/?a|none|yes|ok|\.+|-+)$", re.I)


class PilotError(SystemExit):
    pass


# ------------------------------------------------------------------ sample validation (no network)
def load_sample(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLS if c not in (rd.fieldnames or [])]
        if missing:
            raise PilotError(f"sample file must have columns {REQUIRED_COLS}; missing {missing}")
        rows = list(rd)
    problems, seen, out = [], {}, []
    if not rows:
        raise PilotError("sample file has no rows")
    if len(rows) > MAX_SITES:
        raise PilotError(f"pilot is capped at {MAX_SITES} manually reviewed sites (got {len(rows)})")
    for i, r in enumerate(rows, 2):
        dom = (r.get("domain") or "").strip()
        if re.search(r"[/?#@:\s]", dom) or "://" in dom:
            problems.append(f"line {i}: 'domain' must be a bare domain such as example.com, not {dom!r}")
            continue
        try:
            ident = normalize(dom)
        except InvalidDomain as e:
            problems.append(f"line {i}: {dom!r}: {e}")
            continue
        if ident.host != ident.key and ident.host != "www." + ident.key:
            problems.append(f"line {i}: use the registrable domain ({ident.key}), not the subdomain {ident.host}")
        basis = (r.get("permission_basis") or "").strip()
        if len(basis) < 15 or PLACEHOLDER.match(basis):
            problems.append(f"line {i}: {dom}: permission_basis must say WHY fetching is permitted "
                            "(e.g. 'own site', 'written permission from X on 2026-01-05', 'public marketing site; ToS reviewed 2026-01-05')")
        if not (r.get("reviewed_by") or "").strip():
            problems.append(f"line {i}: {dom}: reviewed_by is empty")
        if ident.key in seen:
            problems.append(f"line {i}: {dom} duplicates line {seen[ident.key]}")
        seen[ident.key] = i
        out.append({"domain": ident.key, "name": (r.get("name") or "").strip() or None,
                    "permission_basis": basis, "reviewed_by": r["reviewed_by"].strip()})
    if problems:
        raise PilotError("sample file rejected:\n  " + "\n  ".join(problems))
    return out


# ------------------------------------------------------------------ guarded settings
def live_settings(args) -> Settings:
    ua = os.environ.get("LE_USER_AGENT", "")
    if not ua or "example.invalid" in ua or not re.search(r"(\+https?://|@\w)", ua):
        raise PilotError('LE_USER_AGENT must identify you and include a contact, e.g. '
                         'LE_USER_AGENT="AcmePilotBot/1.0 (+https://acme.example/bot; ops@acme.example)"')
    if os.environ.get("LE_HOST_OVERRIDES"):
        raise PilotError("LE_HOST_OVERRIDES must be empty for a live pilot (it relaxes the SSRF guard).")
    for var in ("LE_RESPECT_ROBOTS", "LE_ALLOWED_PORTS", "LE_MAX_REDIRECTS", "LE_EGRESS_PROXY"):
        if os.environ.get(var):
            print(f"note: {var} is set in the environment and will be ignored/overridden by the pilot", file=sys.stderr)
    return Settings(
        database_url=args.database_url, user_agent=ua, respect_robots=True, host_overrides="", allowed_ports="80,443",
        egress_proxy=None, global_concurrency=min(max(args.concurrency, 1), 4), per_domain_concurrency=1,
        per_domain_min_delay=max(2.0, args.min_delay), max_pages_per_company=min(args.max_pages, 3),
        max_pages_per_domain=min(args.max_pages, 3) + 1, http_max_retries=min(args.retries, 1),
        connect_timeout=5.0, read_timeout=10.0, total_timeout=20.0, max_response_bytes=1_000_000, max_redirects=3,
        browser_enabled=bool(args.allow_browser), browser_concurrency=1,
        browser_executable=args.browser_executable or None,
        ai_provider="anthropic" if args.ai else "", ai_model=args.ai_model,
        ai_price_in_per_mtok=args.ai_price_in or 0.0, ai_price_out_per_mtok=args.ai_price_out or 0.0,
        fetch_audit_log=True, item_max_attempts=2, job_heartbeat_seconds=5, job_lease_seconds=30)


def check_db_name(url: str) -> None:
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name.endswith("_pilot"):
        raise PilotError(f"database name must end in _pilot (got {name!r}); the pilot TRUNCATES its tables with --reset. "
                         "Create one: scripts/dev_postgres.sh   or   createdb enrich_pilot")


# ------------------------------------------------------------------ statistics
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 3), round((c + m) / d, 3)


def label(value, kind: str, note: str = "") -> dict:
    return {"value": value, "label": kind, **({"note": note} if note else {})}


# ------------------------------------------------------------------ the run
async def collect(eng: Engine, job_id: str, n_input: int, wall: float, s: Settings, compute_rate: float) -> dict:
    st = await jobs.job_status(eng.db, job_id)
    m = await metrics.collect(eng.db, job_id)
    c = st["counts"]
    async with eng.db.connection() as conn:
        found = await (await conn.execute(
            "SELECT c.domain, f.field, f.value, f.method, f.verification_status, f.checks FROM company_fields f "
            "JOIN companies c ON c.id=f.company_id WHERE f.status='found'")).fetchall()
        runs = await (await conn.execute(
            "SELECT outcome, http_requests, retries, duration_ms, ai_calls, browser_ops FROM enrichment_runs WHERE job_id=%s", (job_id,))).fetchall()
        fl = await (await conn.execute("SELECT error_kind, count(*) n FROM fetch_log WHERE job_id=%s GROUP BY 1", (job_id,))).fetchall()
        usage = (await (await conn.execute("SELECT coalesce(sum(ai_calls),0) calls FROM enrichment_runs WHERE job_id=%s", (job_id,))).fetchone())
    by: dict[str, dict] = {}
    for r in found:
        by.setdefault(r["domain"], {})[r["field"]] = r
    attempted = c["total"] - c["duplicate"] - c["invalid"]          # unique, valid companies actually attempted
    reachable = sum(1 for d in by.values() if d.get("website", {}).get("verification_status") == "verified")
    cov = lambda f: sum(1 for d in by.values() if f in d)
    usable = sum(1 for d in by.values() if ("email" in d or "phone" in d) and "company_name" in d)
    tokens = await token_usage(eng, job_id)
    ai_cost = tokens["in"] / 1e6 * s.ai_price_in_per_mtok + tokens["out"] / 1e6 * s.ai_price_out_per_mtok
    prices_set = bool(s.ai_price_in_per_mtok or s.ai_price_out_per_mtok)
    rep = {
        "volume": {"input_rows": label(c["total"], "MEASURED"), "unique_companies_attempted": label(attempted, "MEASURED"),
                   "succeeded": label(c["succeeded"], "MEASURED"), "failed": label(c["failed"], "MEASURED"),
                   "skipped": label(c["skipped"], "MEASURED"), "invalid_rows": label(c["invalid"], "MEASURED"),
                   "duplicate_rows": label(c["duplicate"], "MEASURED"),
                   "duplicate_rate": label(round(c["duplicate"] / max(1, c["total"]), 4), "MEASURED"),
                   "failure_reasons": label(st["error_summary"], "MEASURED")},
        "coverage_presence_only": {
            "note": "presence of a value, NOT correctness; denominator = unique companies attempted",
            "domain": label(1.0 if attempted else None, "MEASURED", "input field: every attempted company has one by construction"),
            "website_reachable_and_domain_verified": label(round(reachable / attempted, 3) if attempted else None, "MEASURED",
                                                           "procedure: http_reachable_domain_match"),
            **{f: label(round(cov(f) / attempted, 3) if attempted else None, "MEASURED") for f in FIELDS_REPORTED},
            "email_mailbox_verified": label(0, "MEASURED", "no mailbox-level verification procedure exists; e-mail is never marked verified"),
            "email_syntax_valid_share_of_emails": label(
                round(sum(1 for d in by.values() if "email" in d and d["email"]["checks"].get("syntax")) / max(1, cov("email")), 3), "MEASURED")},
        "usable_record_rate": {
            "presence_only": label(round(usable / attempted, 3) if attempted else None, "MEASURED",
                                   "definition: company_name AND (email OR phone) present; correctness NOT checked"),
            "audited": label(None, "UNKNOWN", "run `score` on a human-completed audit sheet")},
        "accuracy": label(None, "UNKNOWN", "requires the manual audit sheet (`score`)"),
        "performance": {
            "wall_seconds": label(round(wall, 1), "MEASURED"),
            "throughput_companies_per_minute": label(round(attempted / wall * 60, 2) if wall else None, "MEASURED"),
            "latency_ms_p50": label(m["latency_ms"]["p50"], "MEASURED"), "latency_ms_p95": label(m["latency_ms"]["p95"], "MEASURED"),
            "http_requests_per_company": label(m["per_company"]["http_requests"], "MEASURED", "includes robots.txt and retries"),
            "retry_rate": label(m["retry_rate"], "MEASURED", "retries / requests sent"),
            "browser_fallback_rate": label(m["browser_rendering_frequency"], "MEASURED"),
            "ai_calls_per_company": label(round(usage["calls"] / max(1, attempted), 4), "MEASURED")},
        "ai_cost": {
            "tokens_in": label(tokens["in"], "MEASURED"), "tokens_out": label(tokens["out"], "MEASURED"),
            "per_1000_attempted_usd": (label(round(ai_cost / max(1, attempted) * 1000, 4), "ESTIMATED", "measured tokens x configured prices")
                                       if prices_set and tokens["in"] + tokens["out"] > 0 else
                                       label(0.0, "MEASURED", "no AI calls were made") if usage["calls"] == 0 else
                                       label(None, "UNKNOWN", "AI calls made but LE_AI_PRICE_* not configured")),
            "per_1000_usable_usd": (label(round(ai_cost / usable * 1000, 4), "ESTIMATED", "measured tokens x configured prices; usable = presence_only")
                                    if prices_set and usable and tokens["in"] + tokens["out"] > 0 else
                                    label(0.0, "MEASURED", "no AI calls were made") if usage["calls"] == 0 else
                                    label(None, "UNKNOWN", "needs prices and usable>0")),
            "infrastructure": (label(round(compute_rate * wall / 3600 / max(1, attempted) * 1000, 4), "ESTIMATED", "--compute-usd-per-hour x wall time, per 1000 attempted")
                               if compute_rate else label(None, "UNKNOWN", "not provided; excluded from all cost figures"))},
        "fetch_log_breakdown": label({(r["error_kind"] or "ok"): r["n"] for r in fl}, "MEASURED"),
        "tiers": m["tiers"],
    }
    return rep


async def token_usage(eng: Engine, job_id: str) -> dict:
    """Measured LLM tokens, summed from the per-run cost record."""
    async with eng.db.connection() as c:
        r = await (await c.execute(
            "SELECT coalesce(sum((cost->>'ai_tokens_in')::bigint),0) tin, coalesce(sum((cost->>'ai_tokens_out')::bigint),0) tout "
            "FROM enrichment_runs WHERE job_id=%s", (job_id,))).fetchone()
    return {"in": int(r["tin"]), "out": int(r["tout"])}


async def export_artifacts(eng: Engine, job_id: str, domains: list[str], out_dir: Path, stamp: str, sample_per_field: int,
                           rng_seed: int, truth_fn=None) -> dict:
    """Writes: provenance CSV (every value + source), fetch-log JSONL (every request), manual audit sheet."""
    async with eng.db.connection() as c:
        prov = await (await c.execute(
            """SELECT c.domain, f.field, f.value, f.status, f.source_url, f.method, f.tier, f.extracted_at, f.refresh_after,
                      f.verification_status, f.verification_method, f.checks
               FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE c.domain = ANY(%s) ORDER BY c.domain, f.field""", (domains,))).fetchall()
        log = await (await c.execute("SELECT * FROM fetch_log WHERE job_id=%s ORDER BY id", (job_id,))).fetchall()
    pp = out_dir / f"pilot-{stamp}-provenance.csv"
    with open(pp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "field", "value", "status", "source_url", "method", "tier", "extracted_at", "refresh_after",
                    "verification_status", "verification_method", "checks_json"])
        for r in prov:
            w.writerow([r["domain"], r["field"], r["value"], r["status"], r["source_url"], r["method"], r["tier"],
                        r["extracted_at"].isoformat(), r["refresh_after"].isoformat(), r["verification_status"],
                        r["verification_method"], json.dumps(r["checks"])])
    lp = out_dir / f"pilot-{stamp}-fetch-log.jsonl"
    with open(lp, "w") as f:
        for r in log:
            f.write(json.dumps({**{k: v for k, v in r.items() if k not in ("ts", "job_id")}, "ts": r["ts"].isoformat(),
                                "job_id": str(r["job_id"])}) + "\n")
    # audit sheet: stratified by field, seeded => reproducible
    rng = random.Random(rng_seed)
    audit = out_dir / f"pilot-{stamp}-audit.csv"
    byf: dict[str, list] = {}
    for r in prov:
        if r["status"] == "found" and r["field"] in FIELDS_REPORTED:
            byf.setdefault(r["field"], []).append(r)
    n_rows = 0
    with open(audit, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "field", "value", "source_url", "method", "verdict (correct|incorrect|unsure)", "notes"])
        for fld in sorted(byf):
            pick = byf[fld] if len(byf[fld]) <= sample_per_field else rng.sample(byf[fld], sample_per_field)
            for r in sorted(pick, key=lambda x: x["domain"]):
                verdict = truth_fn(r["domain"], fld, r["value"]) if truth_fn else ""
                w.writerow([r["domain"], fld, r["value"], r["source_url"], r["method"], verdict, "auto-judged against fixture ground truth" if truth_fn else ""])
                n_rows += 1
    return {"provenance_csv": str(pp), "fetch_log_jsonl": str(lp), "audit_sheet": str(audit), "audit_rows": n_rows}


def score_sheet(path: str) -> dict:
    per: dict[str, dict] = {}
    pending = 0
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            v = (r.get("verdict (correct|incorrect|unsure)") or "").strip().lower()
            if v not in ("correct", "incorrect", "unsure"):
                pending += 1
                continue
            d = per.setdefault(r["field"], {"correct": 0, "incorrect": 0, "unsure": 0})
            d[v] += 1
    out = {}
    for fld, d in sorted(per.items()):
        n = d["correct"] + d["incorrect"]
        out[fld] = {**d, "precision": label(round(d["correct"] / n, 3) if n else None, "MEASURED",
                                            "share of judged returned values that are correct (unsure excluded)"),
                    "wilson95": wilson(d["correct"], n)}
    tot_c = sum(d["correct"] for d in per.values()); tot_n = tot_c + sum(d["incorrect"] for d in per.values())
    return {"per_field": out, "overall_precision": label(round(tot_c / tot_n, 3) if tot_n else None, "MEASURED"),
            "overall_wilson95": wilson(tot_c, tot_n), "rows_without_verdict": pending,
            "recall": label(None, "UNKNOWN", "needs a ground-truth value for fields the engine did NOT return"),
            "caveat": "sample size is small; read the interval, not the point estimate. Reviewer judgement is subjective for 'description'."}


def markdown(rep: dict) -> str:
    def row(k, v):
        if isinstance(v, dict) and "label" in v:
            return f"| {k} | {v['value']} | **{v['label']}** | {v.get('note', '')} |"
        return None
    L = [f"# {rep['mode']} report", "", f"Run: {rep['timestamp']}  ", f"Hardware: {rep['hardware']}  ",
         f"Sample: `{rep['sample']}` sha256 `{rep['sample_sha256'][:16]}…` ({rep['sample_size']} rows)  ", f"Code: `{rep['git_commit']}`", ""]
    if rep["mode"].startswith("FIXTURE"):
        L += ["> **Synthetic fixture sites on localhost. Not real-world evidence.**", ""]
    L += ["## Effective settings (forced by the pilot)", "```", json.dumps(rep["settings"], indent=1), "```", ""]
    for sec, body in rep["results"].items():
        if sec in ("tiers",):
            continue
        L += [f"## {sec}", "", "| metric | value | status | note |", "|---|---|---|---|"]
        if isinstance(body, dict) and "label" in body:
            L.append(row(sec, body))
        else:
            for k, v in body.items():
                r = row(k, v)
                if r:
                    L.append(r)
                elif k == "note":
                    L.append(f"| _{v}_ | | | |")
        L.append("")
    if "fixture_auto_audit" in rep:
        a = rep["fixture_auto_audit"]
        L += ["## fixture_auto_audit (judged against fixture ground truth by code, not by a human)", "",
              "| field | correct | incorrect | precision (95% Wilson) |", "|---|---|---|---|"]
        for f, d in a["per_field"].items():
            L.append(f"| {f} | {d['correct']} | {d['incorrect']} | {d['precision']['value']} {d['wilson95']} |")
        L.append(f"| **overall** | | | {a['overall_precision']['value']} {a['overall_wilson95']} |")
        L.append("")
    return "\n".join(L)


def hardware() -> dict:
    sys.path.insert(0, str(ROOT))
    from benchmarks.run_local import hardware as hw
    return hw()


async def run(args) -> None:
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    fixtures = args.fixtures
    fx = None
    truth_fn = None
    if fixtures:
        from leadengine.fixture_sites import DEFAULT_MIX, make_site, pick_kind
        port = _free_port()
        fx = subprocess.Popen([sys.executable, "-m", "leadengine.fixture_sites", "--sites", str(args.fixture_sites), "--port", str(port),
                               "--latency-ms", "40"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.1)
        rows = [{"domain": f"c{i}.fixture.test:{port}", "name": None, "permission_basis": "synthetic fixture", "reviewed_by": "n/a"}
                for i in range(args.fixture_sites)]
        sample_path = f"<{args.fixture_sites} synthetic fixture sites>"
        sample_sha = hashlib.sha256(sample_path.encode()).hexdigest()
        s = Settings(database_url=args.database_url, user_agent="LeadEnrichmentPilotFixture/1.0", host_overrides="*.fixture.test=127.0.0.1",
                     allowed_ports="80,443", global_concurrency=args.concurrency, per_domain_min_delay=args.min_delay, per_domain_concurrency=1,
                     max_pages_per_company=min(args.max_pages, 3), http_max_retries=1, browser_enabled=bool(args.allow_browser),
                     browser_executable=args.browser_executable or None, ai_provider="", job_heartbeat_seconds=5, job_lease_seconds=30)
        attr = {"company_name": "name", "description": "description", "email": "email", "phone": "phone", "address": "address",
                "linkedin_url": "linkedin"}

        def truth_fn(domain, fld, value):
            i = int(domain.split(".")[0][1:])
            site = make_site(i, pick_kind(i, DEFAULT_MIX))
            return "correct" if str(getattr(site, attr[fld])) == value else "incorrect"
        mode = "FIXTURE pilot (synthetic)"
    else:
        rows = load_sample(args.sample)
        if not args.confirm_permitted or args.confirm_sites != len(rows):
            raise PilotError("Refusing to contact live websites. Review every row of the sample, then pass "
                             f"--confirm-permitted --confirm-sites {len(rows)}  (the number of rows you reviewed).")
        s = live_settings(args)
        sample_path = args.sample
        sample_sha = hashlib.sha256(Path(args.sample).read_bytes()).hexdigest()
        mode = "LIVE pilot (real websites)"
    check_db_name(args.database_url)
    eng = Engine(s)
    await eng.start()
    try:
        if args.reset:
            async with eng.db.connection() as c:
                await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs, fetch_log RESTART IDENTITY CASCADE")
        cos = [{"website": r["domain"], "name": r["name"]} for r in rows]
        budget = {"pages": len(rows) * (s.max_pages_per_company + 1), "seconds": args.max_seconds, "browser_ops": len(rows) if args.allow_browser else 0,
                  "ai_calls": len(rows) if args.ai else 0}
        info = await jobs.create_job_from_companies(eng.db, s, cos, requested=["company_name", "description", "email", "phone", "address", "linkedin_url"],
                                                    budget=budget)
        print(f"job {info['job_id']}: {len(rows)} companies, {s.global_concurrency} at a time, >= {s.per_domain_min_delay}s per host", file=sys.stderr)
        w = Worker(eng)
        t0 = time.perf_counter()
        await w.run(once=True)
        wall = time.perf_counter() - t0
        await eng.audit.flush()
        res = await collect(eng, info["job_id"], len(rows), wall, s, args.compute_usd_per_hour)
        domains = [normalize(r["domain"]).key for r in rows]
        arts = await export_artifacts(eng, info["job_id"], domains, out_dir, stamp, args.audit_per_field, 7, truth_fn)
        rep = {"mode": mode, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "hardware": hardware(),
               "git_commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
               "sample": sample_path, "sample_sha256": sample_sha, "sample_size": len(rows), "job_id": info["job_id"],
               "settings": {k: getattr(s, k) for k in ("global_concurrency", "per_domain_concurrency", "per_domain_min_delay", "max_pages_per_company",
                                                       "http_max_retries", "total_timeout", "max_response_bytes", "max_redirects", "respect_robots",
                                                       "browser_enabled", "ai_provider", "ai_model", "user_agent")},
               "results": res, "artifacts": arts, "limitations": [
                   "Coverage is presence, not correctness; accuracy comes only from the human audit sheet.",
                   f"Sample of {len(rows)} hand-picked sites: not statistically representative of any larger population.",
                   "One pass; results depend on time of day, network path and the sites' current content.",
                   "Politeness delay (>=2 s per host) dominates throughput; do not extrapolate to larger runs without re-measuring."]}
        if truth_fn:
            rep["fixture_auto_audit"] = score_sheet(arts["audit_sheet"])
        (out_dir / f"pilot-{stamp}.json").write_text(json.dumps(rep, indent=2, default=str))
        (out_dir / f"pilot-{stamp}.md").write_text(markdown(rep))
        print(json.dumps({"report": str(out_dir / f"pilot-{stamp}.md"), **arts}, indent=2))
    finally:
        await eng.stop()
        if fx:
            fx.terminate()


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("--sample", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sample"); r.add_argument("--fixtures", action="store_true"); r.add_argument("--fixture-sites", type=int, default=40)
    r.add_argument("--confirm-permitted", action="store_true"); r.add_argument("--confirm-sites", type=int, default=-1)
    r.add_argument("--database-url", default=os.environ.get("LE_DATABASE_URL", "postgresql://enrich@127.0.0.1:55432/enrich_pilot"))
    r.add_argument("--reset", action="store_true", help="TRUNCATE the (_pilot) database first so every site is fetched fresh")
    r.add_argument("--concurrency", type=int, default=2); r.add_argument("--min-delay", type=float, default=3.0)
    r.add_argument("--max-pages", type=int, default=3); r.add_argument("--retries", type=int, default=1)
    r.add_argument("--max-seconds", type=int, default=1800)
    r.add_argument("--allow-browser", action="store_true"); r.add_argument("--browser-executable", default="")
    r.add_argument("--ai", action="store_true", help="enable the LLM tier (needs ANTHROPIC_API_KEY)")
    r.add_argument("--ai-model", default="claude-haiku-5-5"); r.add_argument("--ai-price-in", type=float); r.add_argument("--ai-price-out", type=float)
    r.add_argument("--compute-usd-per-hour", type=float, default=0.0)
    r.add_argument("--audit-per-field", type=int, default=30); r.add_argument("--out-dir", default=str(ROOT / "benchmarks" / "results"))
    sc = sub.add_parser("score"); sc.add_argument("--audit", required=True)
    a = ap.parse_args()
    if a.cmd == "validate":
        rows = load_sample(a.sample)
        print(f"OK: {len(rows)} sites validated (no network access performed)")
    elif a.cmd == "score":
        print(json.dumps(score_sheet(a.audit), indent=2))
    else:
        if not a.fixtures and not a.sample:
            raise PilotError("--sample is required for a live run (or use --fixtures for the synthetic dry run)")
        asyncio.run(run(a))


if __name__ == "__main__":
    main()
