"""Live-source benchmark against REAL company websites. Never run by tests or CI.

    export LE_USER_AGENT="YourBot/1.0 (+https://your.site/bot; ops@your.site)"
    python -m benchmarks.run_live --confirm-permitted --sample benchmarks/live_sample.csv --concurrency 4

Rules this script enforces:
  * it refuses to run without --confirm-permitted (you assert you reviewed the sample against each site's terms);
  * it refuses to run with the placeholder user agent (sites must be able to identify and contact you);
  * robots.txt is always respected (LE_RESPECT_ROBOTS cannot be turned off here), per-domain delay >= 1s;
  * the production SSRF guard is fully active (no host overrides).

There is no ground truth for real sites, so this reports PRESENCE of fields (coverage), not correctness.
Use --audit N to export N random records for a human to check; precision is only claimed from that audit.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadengine import jobs, metrics  # noqa: E402
from leadengine.config import Settings  # noqa: E402
from leadengine.engine import Engine  # noqa: E402
from leadengine.worker import Worker  # noqa: E402
from benchmarks.run_local import hardware  # noqa: E402

REQUIRED = ["company_name", "email", "phone"]


async def one_pass(args, concurrency: int, rows: list[dict]) -> dict:
    s = Settings(global_concurrency=concurrency, per_domain_min_delay=max(1.0, float(os.environ.get("LE_PER_DOMAIN_MIN_DELAY", 1.0))),
                 respect_robots=True, host_overrides="", job_heartbeat_seconds=5)
    if "invalid" in s.user_agent or "example.invalid" in s.user_agent:
        raise SystemExit("Set LE_USER_AGENT to a real, contactable identifier before crawling live sites.")
    eng = Engine(s)
    await eng.start()
    async with eng.db.connection() as c:
        await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs RESTART IDENTITY CASCADE")
    info = await jobs.create_job_from_companies(eng.db, s, rows, requested=None)
    t0 = time.perf_counter()
    await Worker(eng).run(once=True)
    wall = time.perf_counter() - t0
    st = await jobs.job_status(eng.db, info["job_id"])
    m = await metrics.collect(eng.db, info["job_id"])
    async with eng.db.connection() as c:
        fr = await (await c.execute("SELECT c.domain, f.field, f.value, f.source_url, f.method FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE f.status='found' AND f.field<>'website'")).fetchall()
    by: dict[str, dict] = {}
    for r in fr:
        by.setdefault(r["domain"], {})[r["field"]] = r
    n = len(rows)
    present = {f: sum(1 for d in by.values() if f in d) / n for f in ["company_name", "description", "email", "phone", "address", "linkedin_url"]}
    usable_presence = sum(1 for d in by.values() if {"email", "phone", "address"} & d.keys() and {"company_name", "description"} & d.keys()) / n
    out = {"concurrency": concurrency, "sample_size": n, "wall_seconds": round(wall, 1), "companies_per_minute": round(n / wall * 60, 1),
           "job_status": st["status"], "counts": st["counts"], "error_summary": st["error_summary"], "latency_ms": m["latency_ms"],
           "per_company": m["per_company"], "browser_rendering_frequency": m["browser_rendering_frequency"],
           "ai_call_frequency": m["ai_call_frequency"], "retry_rate": m["retry_rate"], "tiers": m["tiers"],
           "field_presence": {k: round(v, 3) for k, v in present.items()},
           "required_field_presence": round(sum(1 for d in by.values() for f in REQUIRED if f in d) / (n * len(REQUIRED)), 3),
           "usable_record_rate_UNVERIFIED_presence_only": round(usable_presence, 3),
           "measured_usage": st["stats"]["measured"]}
    if args.audit:
        random.seed(7)
        pick = random.sample(sorted(by), min(args.audit, len(by)))
        path = Path(args.out_dir) / f"live-audit-c{concurrency}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["domain", "field", "value", "source_url", "method", "human_verdict(correct/wrong)"])
            for d in pick:
                for fld, r in by[d].items():
                    w.writerow([d, fld, r["value"], r["source_url"], r["method"], ""])
        out["audit_file"] = str(path)
    await eng.stop()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-permitted", action="store_true", help="I reviewed the sample and I am permitted to fetch these sites")
    ap.add_argument("--sample", default=str(ROOT / "benchmarks" / "live_sample.csv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", default="4")
    ap.add_argument("--audit", type=int, default=0)
    ap.add_argument("--out-dir", default=str(ROOT / "benchmarks" / "results"))
    a = ap.parse_args()
    if not a.confirm_permitted:
        raise SystemExit("Refusing to crawl live sites without --confirm-permitted (see docstring).")
    rows = [{"website": r["domain"], "name": r.get("name")} for r in csv.DictReader(open(a.sample))]
    if a.limit:
        rows = rows[: a.limit]
    results = [asyncio.run(one_pass(a, int(c), rows)) for c in a.concurrency.split(",")]
    rep = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "hardware": hardware(),
           "kind": "LIVE (real websites)", "sample_file": a.sample, "results": results,
           "limitations": ["No ground truth: coverage is presence, not correctness (see --audit).",
                           "Each pass re-fetches every site (cache emptied); keep passes few.",
                           "Results depend on the time of day, network path, and the sites' current content."]}
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    p = out / f"live-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    p.write_text(json.dumps(rep, indent=2))
    print(json.dumps(results, indent=2)); print("wrote", p)


if __name__ == "__main__":
    main()
