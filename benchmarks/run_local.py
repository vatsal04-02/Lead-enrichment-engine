"""Local-fixture benchmark: repeatable, deterministic content, ground-truth accuracy.

    python -m benchmarks.run_local --sites 300 --concurrency 1,2,4,8,16,32,64 --min-delay 0.5,0

What it measures (all measured, nothing assumed): wall time, companies/minute, per-company latency
p50/p95, usable-record rate, required-field coverage, wrong/spurious value rate, duplicate rate,
HTTP requests/company, AI calls/company, browser frequency, retry rate, bytes, process CPU use, peak RSS.

What it is NOT: a statement about the real web. Fixture sites are synthetic, served from localhost with a
fixed simulated latency. Use benchmarks/pilot.py for real-site numbers.

Each configuration runs in its own subprocess against a freshly emptied database and a separate fixture-server
process (so the fixture does not share the engine's GIL).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
try:
    import resource          # Unix only
except ImportError:
    resource = None
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadengine import jobs, metrics  # noqa: E402
from leadengine.config import Settings  # noqa: E402
from leadengine.engine import Engine  # noqa: E402
from leadengine.fixture_sites import DEFAULT_MIX, make_site, pick_kind  # noqa: E402
from leadengine.worker import Worker  # noqa: E402

REQUIRED = ["company_name", "email", "phone"]
CONTACT = ["email", "phone", "address"]
IDENTITY = ["company_name", "description"]


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def build_input(n_sites: int, port: int, dup_rate: float = 0.05) -> list[dict]:
    rows = [{"website": f"http://c{i}.fixture.test:{port}/", "name": f"Co {i}"} for i in range(n_sites)]
    for i in range(0, n_sites, int(1 / dup_rate) if dup_rate else n_sites + 1):    # deterministic duplicates
        rows.append({"website": f"HTTP://C{i}.FIXTURE.TEST:{port}/about?utm=x", "name": f"Co {i} (dup)"})
    return rows


async def accuracy(engine: Engine, n_sites: int) -> dict:
    async with engine.db.connection() as c:
        rows = await (await c.execute(
            """SELECT c.domain, f.field, f.value, f.method FROM company_fields f JOIN companies c ON c.id=f.company_id
               WHERE f.status='found' AND f.field <> 'website'""")).fetchall()
    found: dict[str, dict] = {}
    for r in rows:
        found.setdefault(r["domain"], {})[r["field"]] = r["value"]
    tot = dict(truth=0, correct=0, wrong=0, missed=0, spurious=0, returned=0, usable=0, reachable=0, req_truth=0, req_correct=0)
    per_field: dict[str, list[int]] = {}
    by_kind: dict[str, dict] = {}
    for i in range(n_sites):
        site = make_site(i, pick_kind(i, DEFAULT_MIX))
        got = found.get(site.host, {})
        facts = {site.name, site.description, site.email, site.phone, site.address, site.linkedin, str(site.founded)}
        correct_fields = set()
        k = by_kind.setdefault(site.kind, dict(sites=0, usable=0, correct=0, truth=0, spurious=0))
        k["sites"] += 1
        for f, v in site.truth.items():
            tot["truth"] += 1; k["truth"] += 1
            pf = per_field.setdefault(f, [0, 0])
            pf[1] += 1
            if f in REQUIRED:
                tot["req_truth"] += 1
            if f in got and got[f] == v:
                tot["correct"] += 1; k["correct"] += 1; pf[0] += 1
                correct_fields.add(f)
                if f in REQUIRED:
                    tot["req_correct"] += 1
            elif f in got:
                tot["wrong"] += 1
            else:
                tot["missed"] += 1
        for f, v in got.items():
            tot["returned"] += 1
            if v not in facts and not (f in ("logo_url",) or f.endswith("_url") and f != "linkedin_url"):
                tot["spurious"] += 1; k["spurious"] += 1
        if correct_fields & set(CONTACT) and correct_fields & set(IDENTITY):
            tot["usable"] += 1; k["usable"] += 1
    return {**tot, "per_field_recall": {f: round(a / b, 3) for f, (a, b) in sorted(per_field.items())},
            "by_kind": by_kind}


async def run_single(a) -> dict:
    s = Settings(database_url=a.database_url, global_concurrency=a.concurrency, per_domain_min_delay=a.min_delay,
                 per_domain_concurrency=2, host_overrides="*.fixture.test=127.0.0.1", allowed_ports="80,443",
                 user_agent="LeadEnrichmentBench/1.0", http_max_connections=max(50, a.concurrency * 2),
                 http_max_keepalive=max(20, a.concurrency), queue_size=max(32, a.concurrency * 2),
                 claim_batch_size=max(8, a.concurrency // 2), browser_enabled=not a.no_browser,
                 browser_executable=a.browser_executable or None, parse_workers=a.parse_workers,
                 db_pool_max=max(10, min(40, a.concurrency)), job_heartbeat_seconds=5, ai_provider="",
                 budget_pages=0, budget_seconds=0, budget_est_cost_usd=0, budget_browser_ops=0, budget_ai_calls=0)
    eng = Engine(s)
    await eng.start()
    async with eng.db.connection() as c:
        await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs, fetch_log RESTART IDENTITY CASCADE")
    rows = build_input(a.sites, a.port)
    info = await jobs.create_job_from_companies(eng.db, s, rows, requested=None)
    w = Worker(eng)
    cpu0, t0 = time.process_time(), time.perf_counter()
    await w.run(once=True)
    wall, cpu = time.perf_counter() - t0, time.process_time() - cpu0
    st = await jobs.job_status(eng.db, info["job_id"])
    m = await metrics.collect(eng.db, info["job_id"])
    acc = await accuracy(eng, a.sites)
    n_in = len(rows)
    n_unique = a.sites
    out = {
        "config": {"sites": a.sites, "input_rows": n_in, "concurrency": a.concurrency, "per_domain_min_delay_s": a.min_delay,
                   "simulated_latency_ms": a.latency_ms, "browser": not a.no_browser, "parse_workers": a.parse_workers},
        "wall_seconds": round(wall, 2), "companies_per_minute": round(n_unique / wall * 60, 1),
        "input_rows_per_minute": round(n_in / wall * 60, 1),
        "process_cpu_utilisation": round(cpu / wall, 2), "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1) if resource else None,
        "job_status": st["status"], "counts": st["counts"], "error_summary": st["error_summary"],
        "duplicate_rate": round(st["counts"]["duplicate"] / n_in, 4),
        "latency_ms": m["latency_ms"], "per_company": m["per_company"], "outcomes": m["outcomes"],
        "browser_rendering_frequency": m["browser_rendering_frequency"], "ai_call_frequency": m["ai_call_frequency"],
        "retry_rate": m["retry_rate"], "tiers": m["tiers"],
        "usable_record_rate": round(acc["usable"] / a.sites, 3),
        "required_field_coverage": round(acc["req_correct"] / acc["req_truth"], 3) if acc["req_truth"] else None,
        "precision_of_returned_values": round((acc["returned"] - acc["wrong"] - acc["spurious"]) / acc["returned"], 4) if acc["returned"] else None,
        "accuracy": acc,
        "measured_usage": {"http_requests": st["stats"]["measured"]["http_requests"], "bytes": st["stats"]["measured"]["bytes"],
                           "browser_ops": st["stats"]["measured"]["browser_ops"], "ai_calls": st["stats"]["measured"]["ai_calls"]},
    }
    await eng.stop()
    return out


def hardware() -> dict:
    pg = "unknown"
    try:
        pg = subprocess.run(["psql", "-h", "127.0.0.1", "-p", "55432", "-U", "enrich", "-d", "enrich", "-tAc", "show server_version"],
                            capture_output=True, text=True, timeout=10).stdout.strip() + " (local, same machine, fsync=off)"
    except (OSError, subprocess.SubprocessError):
        pass
    from benchmarks.sysinfo import hardware as hw
    return hw(pg)


def choose_operating_point(results: list[dict]) -> dict:
    """Smallest concurrency reaching >=90% of the best throughput while the engine is not CPU-saturated
    (<=0.85 of one core) and failure rate is no worse than the lowest-concurrency run + 1 percentage point."""
    base = min(results, key=lambda r: r["config"]["concurrency"])
    base_fail = base["counts"]["failed"] / max(1, base["counts"]["total"])
    ok = [r for r in results if r["process_cpu_utilisation"] <= 0.85
          and r["counts"]["failed"] / max(1, r["counts"]["total"]) <= base_fail + 0.01]
    pool = ok or results
    best = max(r["companies_per_minute"] for r in pool)
    pick = min((r for r in pool if r["companies_per_minute"] >= 0.9 * best), key=lambda r: r["config"]["concurrency"])
    note = ("" if ok else "NO configuration met the CPU/failure criteria; picked among all runs. ")
    return {"concurrency": pick["config"]["concurrency"], "companies_per_minute": pick["companies_per_minute"], "note": note +
            f"rule: smallest concurrency with >=90% of best throughput ({best}/min) among runs with CPU<=0.85 core and failure rate <= baseline+1pp"}


def markdown(report: dict) -> str:
    L = ["# Local fixture benchmark", "", f"Run: {report['timestamp']}  ", f"Hardware: {report['hardware']}", "",
         "> **Synthetic fixtures served from localhost with simulated latency. These numbers say nothing about the real web.**", ""]
    for md, rs in report["by_min_delay"].items():
        L += [f"## per-domain minimum delay = {md}s", "",
              "| concurrency | wall s | companies/min | p50 ms | p95 ms | CPU (cores) | peak RSS MB | usable rate | req. coverage | precision | req/company | retry rate | failed |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rs["runs"]:
            L.append(f"| {r['config']['concurrency']} | {r['wall_seconds']} | {r['companies_per_minute']} | {r['latency_ms']['p50']} | {r['latency_ms']['p95']} | "
                     f"{r['process_cpu_utilisation']} | {r['peak_rss_mb']} | {r['usable_record_rate']} | {r['required_field_coverage']} | "
                     f"{r['precision_of_returned_values']} | {r['per_company']['http_requests']} | {r['retry_rate']} | {r['counts']['failed']} |")
        L += ["", f"**Best sustainable operating point:** {rs['operating_point']}", ""]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", type=int, default=300)
    ap.add_argument("--concurrency", default="1,2,4,8,16,32,64")
    ap.add_argument("--min-delay", default="0.5,0")
    ap.add_argument("--latency-ms", type=float, default=40.0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--browser-executable", default=os.environ.get("LE_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else ""))
    ap.add_argument("--parse-workers", type=int, default=0)
    ap.add_argument("--database-url", default=os.environ.get("LE_DATABASE_URL", "postgresql://enrich@127.0.0.1:55432/enrich"))
    ap.add_argument("--out-dir", default=str(ROOT / "benchmarks" / "results"))
    ap.add_argument("--single", action="store_true", help="internal: run one configuration and print JSON")
    ap.add_argument("--concurrency-one", type=int, dest="concurrency_one")
    ap.add_argument("--min-delay-one", type=float, dest="min_delay_one")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    from leadengine.compat import configure_event_loop
    configure_event_loop()

    if a.single:
        a.concurrency, a.min_delay = a.concurrency_one, a.min_delay_one
        print("RESULT " + json.dumps(asyncio.run(run_single(a))))
        return

    port = free_port()
    fx = subprocess.Popen([sys.executable, "-m", "leadengine.fixture_sites", "--sites", str(a.sites), "--port", str(port),
                           "--latency-ms", str(a.latency_ms)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.1)
        report = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "hardware": hardware(),
                  "args": {k: v for k, v in vars(a).items() if k not in ("single",)}, "by_min_delay": {}}
        for md in [float(x) for x in a.min_delay.split(",")]:
            runs = []
            for c in [int(x) for x in a.concurrency.split(",")]:
                cmd = [sys.executable, "-m", "benchmarks.run_local", "--single", "--sites", str(a.sites), "--port", str(port),
                       "--concurrency-one", str(c), "--min-delay-one", str(md), "--latency-ms", str(a.latency_ms),
                       "--database-url", a.database_url, "--parse-workers", str(a.parse_workers),
                       "--browser-executable", a.browser_executable] + (["--no-browser"] if a.no_browser else [])
                p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
                line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
                if not line:
                    print(p.stdout[-2000:], p.stderr[-4000:]); raise SystemExit(f"benchmark run failed (c={c}, delay={md})")
                r = json.loads(line[7:])
                print(f"delay={md} c={c:>3}: {r['companies_per_minute']:>8} companies/min  p95={r['latency_ms']['p95']:>7}ms  "
                      f"cpu={r['process_cpu_utilisation']}  usable={r['usable_record_rate']}  failed={r['counts']['failed']}", flush=True)
                runs.append(r)
            report["by_min_delay"][str(md)] = {"runs": runs, "operating_point": choose_operating_point(runs)}
        out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        (out / f"local-{stamp}.json").write_text(json.dumps(report, indent=2))
        (out / f"local-{stamp}.md").write_text(markdown(report))
        print(f"wrote {out}/local-{stamp}.json|.md")
    finally:
        fx.terminate()


if __name__ == "__main__":
    main()
