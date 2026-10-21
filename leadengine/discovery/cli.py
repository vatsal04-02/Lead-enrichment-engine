"""python -m leadengine.discovery <doctor|plan|discover|run|enrich|review> ...

Nothing contacts a real host unless --live, --confirm-terms-reviewed and --confirm-count <exact planned number> are all given.
Without them the command is a DRY RUN: offline parts (CSV import, validation, dedupe) still run and write an output directory;
everything that would touch the network is listed instead. No command ever resets, truncates or overwrites existing data."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from ..compat import configure_event_loop
from .discover import discover, plan as plan_requests
from .doctor import DEFAULT_MODEL, DEFAULT_URL, run_doctor
from .niche import expand_keywords
from .pipeline import LIVE_LABEL, RunPlan, execute
from .review import ReviewError, apply_review
from .safety import DiscoveryError, forced_settings, live_gate, live_settings
from .sources import SourceContext
from .sources.csv_import import CsvImportSource
from .sources.overpass import DEFAULT_ENDPOINT, OverpassSource
from .sources.seed_pages import SeedPagesSource


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--niche", action="append", help='niche to look for (repeatable). default: "AI automation agency"')
    p.add_argument("--location", action="append", help="city/area name (repeatable); required for --osm")
    p.add_argument("--keyword", action="append", help="extra niche keyword for matching hints (repeatable)")
    p.add_argument("--limit", type=int, default=100, help="max candidates to keep / enrich (default 100)")
    p.add_argument("--out-root", default="discovery_out", help="parent of the new timestamped output directory (default ./discovery_out)")
    p.add_argument("--label", default="", help="optional suffix for the output directory name")
    p.add_argument("--backend", choices=["auto", "scrapegraph", "inhouse"], default="auto",
                   help="auto = ScrapeGraphAI/Ollama if available else in-house (recorded); scrapegraph = refuse to run without it")
    p.add_argument("--model", default=DEFAULT_MODEL); p.add_argument("--ollama-url", default=DEFAULT_URL)
    p.add_argument("--sg-python", default=os.environ.get("LE_SCRAPEGRAPH_PYTHON", ""), help="Python>=3.12 with scrapegraphai installed")
    p.add_argument("--model-timeout", type=float, default=120.0, help="seconds per model call (small local models on CPU are slow)")
    p.add_argument("--database-url", default=os.environ.get("LE_DATABASE_URL"))
    p.add_argument("--concurrency", type=int, default=2); p.add_argument("--min-delay", type=float, default=3.0)
    p.add_argument("--max-pages", type=int, default=3); p.add_argument("--max-seconds", type=int, default=3600)
    p.add_argument("--live", action="store_true", help="actually contact the listed sources / company sites")
    p.add_argument("--confirm-terms-reviewed", action="store_true")
    p.add_argument("--confirm-count", type=int, help="the exact number printed by `plan` / the dry run")


def add_sources(p: argparse.ArgumentParser) -> None:
    p.add_argument("--csv", action="append", default=[], help="manual candidates CSV (repeatable); needs a website/domain column")
    p.add_argument("--mapping", help='JSON column mapping for --csv, e.g. {"website":"Company URL"}')
    p.add_argument("--seed-url", action="append", default=[], help="a public listing page YOU chose and reviewed (repeatable)")
    p.add_argument("--seed-file", help="text file: one seed URL per line, # comments allowed")
    p.add_argument("--osm", action="store_true", help="OpenStreetMap/Overpass open data for each --location")
    p.add_argument("--overpass-endpoint", default=DEFAULT_ENDPOINT)


def build_sources(a) -> list:
    out = []
    mapping = json.loads(a.mapping) if getattr(a, "mapping", None) else None
    for path in getattr(a, "csv", []) or []:
        out.append(CsvImportSource(path, mapping))
    seeds = list(getattr(a, "seed_url", []) or [])
    if getattr(a, "seed_file", None):
        seeds += [ln.strip() for ln in Path(a.seed_file).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if seeds:
        out.append(SeedPagesSource(list(dict.fromkeys(seeds))))
    if getattr(a, "osm", False):
        out.append(OverpassSource(a.overpass_endpoint))
    if getattr(a, "candidates", None):
        out.append(CsvImportSource(a.candidates, None))
    return out


async def _plan(a, command: str) -> tuple[RunPlan, int, bool]:
    """-> (plan, number of targets a live run may contact, whether any source needs the network). Never touches the network."""
    sources = build_sources(a)
    if not sources:
        raise DiscoveryError("no discovery source given: use --csv FILE, --seed-url URL / --seed-file FILE, and/or --osm --location CITY")
    niches = a.niche or ["AI automation agency"]
    locations = a.location or []
    ctx = SourceContext(fetcher=None, niches=niches, locations=locations, keywords=expand_keywords(niches, a.keyword or []), limit=a.limit)
    n_disc = sum(len(s.planned_requests(ctx)) for s in sources if s.needs_network)
    if command == "discover":
        planned = n_disc
    elif n_disc:
        planned = n_disc + a.limit                         # candidates are not known yet: the limit is the upper bound
    else:                                                   # offline sources only: the exact number of company sites is known now
        dres = await discover([s for s in sources if not s.needs_network], None, niches=niches, locations=locations, limit=a.limit,
                              extra_keywords=a.keyword or [])
        planned = len(dres.candidates)
    authorized = True if planned == 0 else live_gate(a.live, a.confirm_terms_reviewed, a.confirm_count, planned)
    if authorized and planned > 0:
        if not a.database_url and command == "run":
            raise DiscoveryError("no database: set LE_DATABASE_URL (or --database-url). Use a dedicated database, e.g. enrich_discovery; "
                                 "discovery never resets or truncates anything.")
        settings = live_settings(database_url=a.database_url or "postgresql://unused@localhost/unused", concurrency=a.concurrency,
                                 min_delay=a.min_delay, max_pages=a.max_pages)
    else:                                                   # dry run: settings only feed the report's configuration block
        settings = forced_settings(database_url=a.database_url or "postgresql://unused@localhost/unused",
                                   user_agent=os.environ.get("LE_USER_AGENT", "(set LE_USER_AGENT before a live run)"),
                                   concurrency=a.concurrency, min_delay=a.min_delay, max_pages=a.max_pages)
    base = [sys.executable, "-m", "leadengine.discovery", a.cmd] + [x for x in sys.argv[2:] if not x.startswith("--confirm") and x != "--live"]
    live_cmd = " ".join(shlex.quote(x) for x in base) + f" --live --confirm-terms-reviewed --confirm-count {planned}"
    nxt = ["# review discovery_candidates.csv, enriched_leads.csv and errors.jsonl in this directory, then record a person's field checks",
           "# (writes a NEW file):  python -m leadengine.discovery review --leads <this dir>/enriched_leads.csv --review reviewed.csv"]
    if planned > 0 and not a.live:
        nxt = ["# This was a dry run. To contact the targets above (after reviewing each source's terms and robots.txt):", live_cmd] + nxt
    return RunPlan(command=command, sources=sources, settings=settings, niches=niches, locations=locations, extra_keywords=a.keyword or [],
                   limit=a.limit, out_root=a.out_root, label=a.label, authorized=authorized, mode_label=LIVE_LABEL,
                   backend_mode=a.backend, model=a.model, base_url=a.ollama_url, sg_python=a.sg_python, request_timeout=a.model_timeout,
                   max_seconds=a.max_seconds, next_commands=nxt, argv=sys.argv[1:]), planned, n_disc > 0


def cmd_doctor(a) -> int:
    r = run_doctor(model=a.model, base_url=a.ollama_url, python=a.sg_python, smoke=a.smoke)
    print(json.dumps(r, indent=2))
    return 0 if r["ready_for_scrapegraph_ollama"] else 1


async def cmd_plan(a) -> int:
    dry = argparse.Namespace(**{**vars(a), "live": False, "confirm_terms_reviewed": False, "confirm_count": None})
    for cmd in ("discover", "run"):
        try:
            _, planned, _ = await _plan(dry, cmd)
        except DiscoveryError as e:
            print(f"{cmd}: {e}")
            continue
        print(f"`{cmd}` would contact {planned} target(s) -> to run for real add: --live --confirm-terms-reviewed --confirm-count {planned}")
    niches = a.niche or ["AI automation agency"]
    kws = expand_keywords(niches, a.keyword or [])
    lines = plan_requests(build_sources(a), niches, a.location or [], a.limit, kws)
    print("\nDiscovery requests:" + ("" if lines else " (none: offline sources only)"))
    for r in lines:
        print("  ", r)
    print(f"\nEnrichment: up to {a.limit} company websites, <= {a.concurrency} at a time, >= {max(2.0, a.min_delay)}s between requests per host, "
          f"<= {min(a.max_pages, 3)} pages each, robots.txt enforced, browser off, no paid APIs. Nothing was contacted.")
    return 0


async def _run(a, command: str) -> int:
    plan, planned, network = await _plan(a, command)
    res = await execute(plan)
    print(json.dumps({k: v for k, v in res.items() if k not in ("discovery", "leads_detail", "rows")}, indent=2))
    if not plan.authorized:
        print(f"\nDRY RUN complete - nothing was contacted. Targets a live run would contact: {planned}.", file=sys.stderr)
    return 0


def cmd_review(a) -> int:
    try:
        print(apply_review(a.leads, a.review, a.out))
    except (ReviewError, FileExistsError) as e:
        raise DiscoveryError(str(e))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m leadengine.discovery", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check Ollama / qwen3:4b / the ScrapeGraphAI interpreter (read-only; never downloads a model)")
    d.add_argument("--model", default=DEFAULT_MODEL); d.add_argument("--ollama-url", default=DEFAULT_URL)
    d.add_argument("--sg-python", default=os.environ.get("LE_SCRAPEGRAPH_PYTHON", ""))
    d.add_argument("--smoke", action="store_true", help="also run one extraction on a built-in synthetic page with the installed model")
    for name, hlp in (("plan", "print exactly what a run would contact (no network)"), ("discover", "find + validate + dedupe candidates only"),
                      ("run", "discover, then enrich (ScrapeGraphAI -> in-house -> manual review)")):
        s = sub.add_parser(name, help=hlp); add_common(s); add_sources(s)
    e = sub.add_parser("enrich", help="enrich an existing discovery_candidates.csv (or any CSV with a website column)")
    add_common(e); e.add_argument("--candidates", required=True)
    r = sub.add_parser("review", help="apply a human review sheet to enriched_leads.csv (writes a new file)")
    r.add_argument("--leads", required=True); r.add_argument("--review", required=True); r.add_argument("--out")
    a = p.parse_args(argv)
    configure_event_loop()
    try:
        if a.cmd == "doctor":
            return cmd_doctor(a)
        if a.cmd == "plan":
            return asyncio.run(cmd_plan(a))
        if a.cmd == "review":
            return cmd_review(a)
        return asyncio.run(_run(a, {"discover": "discover", "run": "run", "enrich": "run"}[a.cmd]))
    except DiscoveryError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
