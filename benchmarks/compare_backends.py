"""Side-by-side benchmark: in-house engine vs the OPTIONAL ScrapeGraphAI backend, on SYNTHETIC fixture sites.

    python -m benchmarks.compare_backends --database-url postgresql://u@h:5432/NAME_pilot --sites 60 --model stub-heuristic \
        --sg-python /path/to/py312/python
    python -m benchmarks.compare_backends ... --model ollama --sg-model llama3.2 --sg-base-url http://127.0.0.1:11434

What is and is not evidence (the report repeats this on its first page):
  * SYNTHETIC  - generated local sites with known ground truth. Measures plumbing, grounding, cost accounting and the
                 behaviour of each engine on KNOWN page shapes. It says NOTHING about accuracy on real websites.
  * SIMULATED  - `--model stub-*` uses a rule-based responder (benchmarks/stub_llm.py). It is NOT a language model, so the
                 scrapegraph column then measures the integration, not model quality. Use `--model ollama` for a real local model
                 (still on synthetic pages).
  * HUMAN-REVIEWED LIVE - only filled from `--live-scored` files produced by `benchmarks.pilot score` after a person audited a
                 permitted live sample. Without them the section says UNKNOWN. Nothing here contacts a real website.
E-mail is never reported as mailbox-verified: no mailbox verification exists in this project.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.pilot import check_db_name, wilson  # noqa: E402
from benchmarks.stub_llm import StubLLMServer  # noqa: E402
from benchmarks.sysinfo import git_commit, hardware  # noqa: E402
from leadengine.backends import BackendConfigError, BackendUnavailable  # noqa: E402
from leadengine.compat import configure_event_loop  # noqa: E402
from leadengine.config import Settings  # noqa: E402
from leadengine.engine import Engine  # noqa: E402
from leadengine.fixture_sites import FixtureServer  # noqa: E402
from leadengine.metrics import llm_usage  # noqa: E402

FIELDS = ["company_name", "description", "email", "phone", "address", "linkedin_url"]
OUT_DIR = ROOT / "benchmarks" / "results"


def lab(value, label: str, note: str = "") -> dict:
    return {"value": value, "label": label, "note": note}


def pct(xs: list[float], q: float):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def norm(field: str, v) -> str:
    s = str(v or "").strip()
    if field == "email":
        return s.lower()
    if field == "phone":
        return re.sub(r"\D", "", s)
    if field == "linkedin_url":
        return s.lower().rstrip("/")
    return re.sub(r"\s+", " ", s)


def on_page(field: str, value: str, page: str) -> bool:
    """Same presence test for both engines: is the stored value literally on the page it is attributed to?"""
    if not page:
        return False
    if field == "phone":
        d = re.sub(r"\D", "", value)
        return bool(d) and d in re.sub(r"\D", "", page)
    flat = re.sub(r"\s+", " ", page).lower()
    if field == "address":                          # the engine joins street/city/region parts: each part must be on the page
        parts = [x.strip() for x in value.split(",") if x.strip()]
        return bool(parts) and all(x.lower() in flat for x in parts)
    return re.sub(r"\s+", " ", value).lower().rstrip("/") in flat


def settings_for(backend: str, db: str, args, stub) -> Settings:
    base = dict(database_url=db, host_overrides="*.fixture.test=127.0.0.1", per_domain_min_delay=0.0, per_domain_concurrency=4,
                http_max_retries=1, backoff_base=0.05, backoff_max=0.2, browser_enabled=False, allowed_ports="80,443",
                user_agent="LeadEnrichmentCompare/1.0 (synthetic fixtures only)", ai_provider="", max_pages_per_company=3,
                fetch_audit_log=True)
    if backend == "scrapegraph":
        if stub:
            base.update(scrapegraph_provider="ollama", scrapegraph_model="stubmodel", scrapegraph_base_url=stub.base_url)
        else:
            base.update(scrapegraph_provider="ollama", scrapegraph_model=args.sg_model, scrapegraph_base_url=args.sg_base_url)
        base.update(extraction_backend="scrapegraph", scrapegraph_enabled=True, scrapegraph_python=args.sg_python,
                    scrapegraph_workers=args.sg_workers, scrapegraph_request_timeout=args.sg_timeout)
    return Settings(**base)


async def run_backend(backend: str, fx: FixtureServer, args, stub) -> dict:
    sites = list(fx.state.sites.values())
    s = settings_for(backend, args.database_url, args, stub)
    eng = Engine(s)
    out = {"backend": backend, "started": True, "start_error": None}
    try:
        await eng.start()
    except (BackendUnavailable, BackendConfigError) as e:                 # reported, never replaced by the other engine
        out.update(started=False, start_error=str(e))
        return out
    try:
        async with eng.db.connection() as c:
            await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs, fetch_log RESTART IDENTITY CASCADE")
        fx.state.reset()
        sem = asyncio.Semaphore(args.concurrency)
        results: dict[str, object] = {}

        async def one(site):
            async with sem:
                t0 = time.perf_counter()
                r = await eng.enricher.enrich(fx.url(site.host), requested=FIELDS)
                results[site.host] = (r, (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*(one(x) for x in sites))
        wall = time.perf_counter() - t0
        await eng.audit.flush()
        rows: dict[str, dict] = {}
        async with eng.db.connection() as c:
            for x in await (await c.execute(
                    "SELECT c.domain, f.field, f.value, f.method, f.source_url, f.extracted_at, f.checks, f.verification_status "
                    "FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE f.status='found'")).fetchall():
                rows.setdefault(x["domain"], {})[x["field"]] = x
        out.update(sites=sites, results=results, rows=rows, wall=wall, fixture_requests=fx.state.total(),
                   llm=llm_usage([r.metrics.get("cost", {}).get("llm") for r, _ in results.values() if r.metrics.get("cost", {}).get("llm")]))
        async with httpx.AsyncClient(timeout=5) as h:                       # source pages, fetched from the LOCAL synthetic server only
            pages: dict[str, str] = {}
            for dom, fl in rows.items():
                for f in fl.values():
                    u = f["source_url"]
                    if u and u not in pages:
                        try:
                            pages[u] = (await h.get(f"http://127.0.0.1:{fx.port}" + re.sub(r"^https?://[^/]+", "", u),
                                                    headers={"Host": dom + f":{fx.port}"})).text
                        except Exception:
                            pages[u] = ""
            out["pages"] = pages
        return out
    finally:
        await eng.stop()


def analyse(res: dict) -> dict:
    if not res["started"]:
        return {"backend": res["backend"], "started": False, "start_error": lab(res["start_error"], "MEASURED", "backend refused to start; no fallback was used")}
    sites, results, rows, pages = res["sites"], res["results"], res["rows"], res["pages"]
    N = len(sites)
    per_field = {}
    tot_ok = tot_bad = tot_stored = 0
    attributed = unsupported = 0
    for f in FIELDS:
        n_truth = sum(1 for s in sites if f in s.truth)
        stored = [(s, rows.get(s.host, {}).get(f)) for s in sites if rows.get(s.host, {}).get(f)]
        ok = sum(1 for s, r in stored if f in s.truth and norm(f, r["value"]) == norm(f, s.truth[f]))
        wrong = sum(1 for s, r in stored if f in s.truth and norm(f, r["value"]) != norm(f, s.truth[f]))
        spurious = sum(1 for s, r in stored if f not in s.truth)
        unsup = sum(1 for s, r in stored if not on_page(f, str(r["value"]), pages.get(r["source_url"], "")))
        attr = sum(1 for s, r in stored if r["source_url"] and r["method"] and r["extracted_at"]
                   and on_page(f, str(r["value"]), pages.get(r["source_url"], "")))
        tot_ok += ok; tot_bad += wrong + spurious; tot_stored += len(stored); attributed += attr; unsupported += unsup
        n = len(stored)
        per_field[f] = {
            "truth_published": n_truth, "stored": n, "correct": ok, "incorrect": wrong, "spurious_not_published": spurious,
            "missing": n_truth - ok - wrong if n_truth else 0,
            "coverage": lab(round(n / N, 3), "SYNTHETIC"),
            "recall": lab(round(ok / n_truth, 3) if n_truth else None, "SYNTHETIC", "correct / fields the generator published"),
            "precision": lab(round(ok / n, 3) if n else None, "SYNTHETIC", f"wilson95={tuple(round(x, 3) for x in wilson(ok, n))}" if n else "nothing stored"),
            "false_positive_rate": lab(round((wrong + spurious) / n, 3) if n else None, "SYNTHETIC", "stored value wrong or field not published"),
            "unsupported_claims": lab(unsup, "SYNTHETIC", "stored value not literally on its attributed source page"),
        }
    usable = sum(1 for s in sites
                 if (r := rows.get(s.host, {})).get("company_name") and norm("company_name", r["company_name"]["value"]) == norm("company_name", s.truth.get("company_name"))
                 and any(f in r and f in s.truth and norm(f, r[f]["value"]) == norm(f, s.truth[f]) for f in ("email", "phone")))
    outcomes = Counter(r.outcome for r, _ in results.values())
    errors = Counter(r.error_kind for r, _ in results.values() if r.error_kind)
    lat = [ms for _, ms in results.values()]
    req = [r.metrics.get("http_requests", 0) for r, _ in results.values()]
    pgs = [r.metrics.get("pages_fetched", 0) for r, _ in results.values()]
    retries = sum(r.metrics.get("retries", 0) for r, _ in results.values())
    llm = res["llm"]
    comp = {
        "companies": N,
        "usable_correct_rate": lab(round(usable / N, 3), "SYNTHETIC", "company_name correct AND (email or phone) correct vs generator truth"),
        "usable_correct_count": usable,
        "fields_stored": tot_stored, "fields_correct": tot_ok, "fields_wrong_or_spurious": tot_bad,
        "overall_precision": lab(round(tot_ok / tot_stored, 3) if tot_stored else None, "SYNTHETIC"),
        "unsupported_claims_total": lab(unsupported, "SYNTHETIC"),
        "source_attribution_complete": lab(round(attributed / tot_stored, 3) if tot_stored else None, "SYNTHETIC",
                                           "share of stored values with source_url+method+timestamp AND the value present on that page"),
        "email_mailbox_verified": lab(0, "MEASURED", "no mailbox verification is performed; e-mail is never marked verified"),
        "failure_rate": lab(round((outcomes.get("failed", 0) + outcomes.get("unreachable", 0)) / N, 3), "SYNTHETIC"),
        "outcomes": dict(outcomes), "failure_reasons": dict(errors),
        "latency_ms_p50": lab(pct(lat, 0.5), "MEASURED", "wall time per company inside this run (concurrency applies)"),
        "latency_ms_p95": lab(pct(lat, 0.95), "MEASURED"),
        "http_requests_per_company": lab(round(sum(req) / N, 2), "MEASURED"),
        "pages_per_company": lab(round(sum(pgs) / N, 2), "MEASURED"),
        "http_retry_rate": lab(round(retries / max(1, sum(req)), 4), "MEASURED"),
        "wall_seconds": lab(round(res["wall"], 2), "MEASURED"),
    }
    if llm:
        comp.update({
            "model_calls_per_company": lab(round(llm["calls"] / N, 3), "MEASURED"),
            "tokens_in_per_company": lab(round(llm["tokens_in"] / N), "MEASURED"), "tokens_out_per_company": lab(round(llm["tokens_out"] / N), "MEASURED"),
            "model_retry_rate": lab(round(llm["retries"] / max(1, llm["calls"]), 4), "MEASURED"),
            "model_timeout_rate": lab(round(llm["timeouts"] / max(1, llm["calls"]), 4), "MEASURED"),
            "model_failures": lab(llm["failures"], "MEASURED"),
            "unsupported_values_dropped_before_storage": lab(llm["rejected_total"], "MEASURED", f"by reason {llm['rejected']}"),
            "worker_blocked_network_attempts": lab(llm["worker_blocked_attempts"], "MEASURED", "must be 0"),
            "cost_usd_per_usable": (lab(round(llm["external_usd_estimated"] / usable, 6), "ESTIMATED", "measured tokens x configured prices") if llm["scope"] == "external" and usable
                                    else lab(None, "UNKNOWN", "local inference: no per-token price; hardware/electricity not priced - NOT zero cost")),
            "tokens_per_usable": lab(round((llm["tokens_in"] + llm["tokens_out"]) / usable) if usable else None, "MEASURED"),
            "model_scope": llm["scope"], "model": llm["model"], "provider": llm["provider"]})
    else:
        comp["model_calls_per_company"] = lab(0, "MEASURED", "no model is used by the in-house engine")
        comp["cost_usd_per_usable"] = lab(0.0, "MEASURED", "no model; HTTP/compute cost not priced here")
    return {"backend": res["backend"], "started": True, "summary": comp, "per_field": per_field}


def md(rep: dict) -> str:
    L = ["# Backend comparison report", "",
         f"Run: {rep['timestamp']} · code `{rep['git_commit']}` · {rep['hardware']['platform']}, {rep['hardware']['cpu']} x{rep['hardware']['logical_cpus']}, "
         f"{rep['hardware']['memory']}, Python {rep['hardware']['python']}", "",
         "> **Everything below the SYNTHETIC heading is measured on generated local sites with known ground truth. It is NOT evidence of accuracy on real websites.**", ""]
    if rep["model_kind"].startswith("SIMULATED"):
        L += [f"> **Model: {rep['model_kind']}.** The scrapegraph column therefore measures the integration (grounding, budgets, provenance, cost accounting), not language-model quality.", ""]
    L += ["## 1. SYNTHETIC (fixture sites, generator ground truth)", "", f"Sites: {rep['sites']} · mix: `{json.dumps(rep['mix'])}` · concurrency {rep['concurrency']}", ""]
    bs = [b for b in rep["synthetic"] if b["started"]]
    for b in rep["synthetic"]:
        if not b["started"]:
            L.append(f"**{b['backend']}: did not start** - {b['start_error']['value']}")
    keys = list(dict.fromkeys(k for b in bs for k in b["summary"]))                  # union: model-only metrics show "-" for the in-house column
    if bs:
        L += ["| metric | " + " | ".join(b["backend"] for b in bs) + " |", "|---|" + "---|" * len(bs)]
        for k in keys:
            if k in ("outcomes", "failure_reasons"):
                L.append(f"| {k} | " + " | ".join(json.dumps(b["summary"].get(k)) for b in bs) + " |")
                continue
            cells = []
            for b in bs:
                v = b["summary"].get(k)
                cells.append("-" if v is None else f"{v['value']} _{v['label']}_" if isinstance(v, dict) and "label" in v else str(v))
            L.append(f"| {k} | " + " | ".join(cells) + " |")
        L += ["", "### Per field (precision / recall / unsupported claims)", "", "| field | " + " | ".join(b["backend"] for b in bs) + " |", "|---|" + "---|" * len(bs)]
        for f in FIELDS:
            L.append(f"| {f} | " + " | ".join(
                f"P={b['per_field'][f]['precision']['value']} R={b['per_field'][f]['recall']['value']} cov={b['per_field'][f]['coverage']['value']} "
                f"unsup={b['per_field'][f]['unsupported_claims']['value']}" for b in bs) + " |")
    L += ["", "## 2. HUMAN-REVIEWED LIVE", ""]
    if rep["human_reviewed_live"]:
        for name, d in rep["human_reviewed_live"].items():
            L += [f"- `{name}`: {json.dumps(d)[:600]}"]
    else:
        L += ["**UNKNOWN - none supplied.** No live website was contacted and no human-audited live sample exists for either engine. "
              "Run a separately reviewed, permitted pilot (docs/STAGE5_PILOT_GUIDE.md) per backend, audit it, then pass the `.scored.json` files with `--live-scored`."]
    L += ["", "## 3. UNKNOWN (not measured here)", ""] + [f"- {x}" for x in rep["unknown"]] + [""]
    return "\n".join(L)


def write_csv(path: Path, rep: dict) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "backend", "field", "metric", "value", "label", "note"])
        for b in rep["synthetic"]:
            if not b["started"]:
                w.writerow(["SYNTHETIC", b["backend"], "", "start_error", b["start_error"]["value"], "MEASURED", ""])
                continue
            for k, v in b["summary"].items():
                if isinstance(v, dict) and "label" in v:
                    w.writerow(["SYNTHETIC", b["backend"], "", k, v["value"], v["label"], v["note"]])
            for f, d in b["per_field"].items():
                for k, v in d.items():
                    if isinstance(v, dict) and "label" in v:
                        w.writerow(["SYNTHETIC", b["backend"], f, k, v["value"], v["label"], v["note"]])
                    else:
                        w.writerow(["SYNTHETIC", b["backend"], f, k, v, "SYNTHETIC", ""])
        if not rep["human_reviewed_live"]:
            w.writerow(["HUMAN_REVIEWED_LIVE", "", "", "accuracy", "", "UNKNOWN", "no audited live sample supplied"])


async def main_async(args) -> dict:
    check_db_name(args.database_url)
    stub = None
    if args.model.startswith("stub"):
        stub = StubLLMServer(mode="noisy" if args.model == "stub-noisy" else "heuristic")
        stub.__enter__()
        model_kind = f"SIMULATED rule-based responder ({args.model}); NOT a language model"
    else:
        if not (args.sg_model and args.sg_base_url):
            raise SystemExit("--model ollama needs --sg-model and --sg-base-url (a local Ollama, e.g. http://127.0.0.1:11434)")
        model_kind = f"real local model via Ollama: {args.sg_model} (on synthetic pages)"
    try:
        with FixtureServer(n_sites=args.sites, latency_ms=args.latency_ms) as fx:
            runs = []
            for b in args.backends:
                print(f"running {b} on {args.sites} synthetic sites ...", file=sys.stderr)
                runs.append(analyse(await run_backend(b, fx, args, stub)))
            mix = dict(Counter(s.kind for s in fx.state.sites.values()))
    finally:
        if stub:
            stub.__exit__(None, None, None)
    live = {}
    for p in args.live_scored or []:
        live[Path(p).name] = json.loads(Path(p).read_text(encoding="utf-8"))
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "git_commit": git_commit(), "hardware": hardware("n/a"),
        "model_kind": model_kind, "sites": args.sites, "mix": mix, "concurrency": args.concurrency, "synthetic": runs, "human_reviewed_live": live,
        "unknown": [
            "Accuracy of either engine on REAL websites (no live site was contacted).",
            "Quality of any real language model unless `--model ollama` was used - and even then only on synthetic pages.",
            "Behaviour on JavaScript-heavy, anti-bot, multilingual or very large real pages.",
            "Cost of local inference (hardware, electricity, GPU time): reported as tokens/latency, deliberately not converted to money.",
            "Mailbox existence of any e-mail address: never verified by this project.",
            "Latency under real network conditions and politeness delays: fixtures answer in milliseconds on localhost.",
        ]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True, help="postgresql://.../NAME_pilot (TRUNCATED between backends)")
    ap.add_argument("--sites", type=int, default=40); ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--latency-ms", type=float, default=0.0)
    ap.add_argument("--backends", nargs="+", choices=["inhouse", "scrapegraph"], default=["inhouse", "scrapegraph"])
    ap.add_argument("--model", choices=["stub-heuristic", "stub-noisy", "ollama"], default="stub-heuristic")
    ap.add_argument("--sg-python", default="", help="Python>=3.12 with scrapegraphai installed")
    ap.add_argument("--sg-model"); ap.add_argument("--sg-base-url"); ap.add_argument("--sg-workers", type=int, default=2)
    ap.add_argument("--sg-timeout", type=int, default=60)
    ap.add_argument("--live-scored", nargs="*", help="`pilot score` JSON outputs from HUMAN-audited live pilots (optional)")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()
    configure_event_loop()
    rep = asyncio.run(main_async(args))
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"compare-{stamp}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    (out / f"compare-{stamp}.md").write_text(md(rep), encoding="utf-8")
    write_csv(out / f"compare-{stamp}.csv", rep)
    print(json.dumps({"json": str(out / f"compare-{stamp}.json"), "csv": str(out / f"compare-{stamp}.csv"), "markdown": str(out / f"compare-{stamp}.md")}, indent=2))


if __name__ == "__main__":
    main()
