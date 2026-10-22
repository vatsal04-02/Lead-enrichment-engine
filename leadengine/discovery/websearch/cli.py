"""python -m leadengine.discovery.websearch <plan|probe|run> ...

DRY RUN by default: `plan` (and `run`/`probe` without the live flags) print exactly what would be requested and contact nothing. A live run needs
--live --confirm-terms-reviewed --confirm-count N (N = search requests + company-site visits the plan allows, printed by the dry run).
Search provider: searxng (default, free, keyless, YOU run it) | brave (optional key, metered) | static (offline replay file). See providers.py."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from ...compat import configure_event_loop
from ..doctor import DEFAULT_MODEL, DEFAULT_URL, run_doctor
from ..safety import DiscoveryError, live_gate
from .engine import WebConfig, planned_requests, run_web_discovery
from .providers import SearchError, make_provider
from .queries import DEFAULT_CITIES, DEFAULT_INDUSTRIES


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m leadengine.discovery.websearch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["plan", "probe", "run"])
    p.add_argument("--provider", choices=["searxng", "brave", "static"], default="searxng")
    p.add_argument("--static-results", default="", help="JSON file for --provider static")
    p.add_argument("--industry", action="append", help='"Label=phrase one;phrase two" (repeatable). default: IT services, SaaS, Healthcare, Education, Real estate, Marketing, Business services')
    p.add_argument("--city", action="append", help="Indian city (repeatable). default: 15 cities")
    p.add_argument("--target", type=int, default=100, help="valid companies wanted")
    p.add_argument("--max-queries", type=int, default=150); p.add_argument("--max-pages-per-query", type=int, default=2)
    p.add_argument("--max-sites", type=int, default=400, help="company websites that may be visited (budget)")
    p.add_argument("--max-seconds", type=float, default=7200)
    p.add_argument("--concurrency", type=int, default=2); p.add_argument("--min-delay", type=float, default=3.0, help="seconds between requests to one site (min 2)")
    p.add_argument("--search-min-interval", type=float, default=2.0, help="seconds between search requests")
    p.add_argument("--out-root", default="websearch_out"); p.add_argument("--label", default="")
    p.add_argument("--cache-dir", help="search cache directory (default <out-root>/_search_cache)")
    p.add_argument("--enrich", action="store_true", help="after discovery, run the ScrapeGraphAI + Ollama profile enrichment on the valid companies")
    p.add_argument("--model", default=DEFAULT_MODEL); p.add_argument("--ollama-url", default=DEFAULT_URL)
    p.add_argument("--sg-python", default=os.environ.get("LE_SCRAPEGRAPH_PYTHON", "")); p.add_argument("--model-timeout", type=float, default=300.0)
    p.add_argument("--live", action="store_true"); p.add_argument("--confirm-terms-reviewed", action="store_true")
    p.add_argument("--confirm-count", type=int)
    return p


def parse_industries(specs) -> dict:
    if not specs:
        return dict(DEFAULT_INDUSTRIES)
    out = {}
    for s in specs:
        label, _, phrases = s.partition("=")
        out[label.strip()] = [x.strip() for x in phrases.split(";") if x.strip()] or [label.strip()]
    return out


async def _main(a) -> int:
    ua = os.environ.get("LE_USER_AGENT", "")
    try:
        provider = make_provider(a.provider, min_interval=a.search_min_interval, user_agent=ua, static_path=a.static_results)
    except SearchError as e:
        raise DiscoveryError(f"{e.kind}: {e}")
    cfg = WebConfig(provider=provider, industries=parse_industries(a.industry), cities=a.city or list(DEFAULT_CITIES), target=a.target,
                    max_queries=a.max_queries, max_pages_per_query=a.max_pages_per_query, max_sites=a.max_sites, max_seconds=a.max_seconds,
                    concurrency=a.concurrency, min_delay=a.min_delay, out_root=a.out_root, label=a.label, cache_dir=a.cache_dir, enrich=a.enrich,
                    enrich_kwargs={"model": a.model, "base_url": a.ollama_url, "sg_python": a.sg_python, "model_timeout": a.model_timeout})
    plan = planned_requests(cfg)
    if a.cmd == "probe":
        plan = {**plan, "confirm_count": 1 if provider.needs_network else 0, "site_visits_max": 0, "page_requests_max": 0, "queries": 1}
    live = live_gate(a.live, a.confirm_terms_reviewed, a.confirm_count, plan["confirm_count"]) if plan["confirm_count"] > 0 or a.live else False
    if not a.live or a.cmd == "plan":
        print(json.dumps({"DRY RUN": "nothing was contacted", **plan, "to_run_for_real": (
            f"add: --live --confirm-terms-reviewed --confirm-count {plan['confirm_count']}  (after reading the terms of the search provider and checking "
            "each source; set LE_USER_AGENT to identify yourself)")}, indent=2))
        await provider.aclose()
        return 0
    if a.cmd == "probe":
        from .queries import plan_queries
        q = plan_queries(cfg.industries, cfg.cities, 1)[0]
        res = await provider.search(q.text, page=1, count=10)
        await provider.aclose()
        print(json.dumps({"query": q.text, "results": [{"rank": r.rank, "title": r.title, "url": r.url} for r in res]}, indent=2))
        return 0 if res else 1
    from ..safety import require_identifying_user_agent
    require_identifying_user_agent()
    if a.enrich:
        r = run_doctor(model=a.model, base_url=a.ollama_url, python=a.sg_python, smoke=False)
        if not r["ready_for_scrapegraph_ollama"]:
            raise DiscoveryError("--enrich needs a ready local model; `python -m leadengine.discovery doctor` says: " + json.dumps(r)[:500])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    summary = await run_web_discovery(cfg)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    configure_event_loop()
    try:
        return asyncio.run(_main(a))
    except DiscoveryError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
