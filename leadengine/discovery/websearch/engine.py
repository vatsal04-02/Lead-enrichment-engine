"""Orchestrates: search queries -> result collection -> candidate extraction -> website validation -> de-duplication -> (optional) enrichment -> export.

Keeps going, batch by batch, until the requested number of VALID companies is reached or a budget is exhausted (queries, site visits, time, or the
search provider failing repeatedly). The stop reason is always reported. Nothing is invented: every row is a search result whose website was visited."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ...csv_io import spreadsheet_safe
from ...fetcher import Fetcher
from ..e2e import S_NOT_SELECTED, S_SELECTED, balanced_order, identity_key
from ..niche import expand_keywords
from ..safety import JsonlAudit
from .candidates import Rejection, from_result, priority
from .providers import QueryCache, SearchError, SearchProvider
from .queries import DEFAULT_CITIES, DEFAULT_INDUSTRIES, Query, plan_queries
from .validate import SiteRecord, score, validate_site

log = logging.getLogger("leadengine.websearch")
CSV_COLUMNS = ["Company", "Website", "City", "Industry/Niche", "SourceURL", "DiscoveryStatus", "Confidence", "MissingFields", "Description", "Email",
               "Phone", "Address", "LinkedIn", "ContactSourceURL", "IndiaEvidence", "IndustryEvidence", "NameSource", "Domain", "SearchProvider",
               "SearchQuery", "SearchRank", "AlsoFoundVia", "Detail"]
PROFILE_INPUT_COLUMNS = ["Company", "Website", "City", "Industry/Niche", "SourceURL"]


@dataclass
class WebConfig:
    provider: SearchProvider
    industries: dict = field(default_factory=lambda: dict(DEFAULT_INDUSTRIES))
    cities: list = field(default_factory=lambda: list(DEFAULT_CITIES))
    target: int = 100
    max_queries: int = 150
    max_pages_per_query: int = 2
    results_per_query: int = 10
    max_sites: int = 400                    # company websites that may be visited (each visit = up to 4 page requests)
    max_seconds: float = 7200.0
    batch_queries: int = 6                  # queries searched before the new candidates are validated
    concurrency: int = 2
    min_delay: float = 3.0
    out_root: str = "websearch_out"
    label: str = ""
    cache_dir: Optional[str] = None
    cache_ttl: float = 7 * 86400
    max_provider_failures: int = 6          # consecutive failed queries before the search side is declared broken
    enrich: bool = False
    enrich_kwargs: dict = field(default_factory=dict)      # model, ollama url, sg python, timeouts ... for run_profiles
    # test seams (never set by the CLI)
    fetcher: object = None
    settings: object = None
    allow_synthetic: bool = False
    backend: object = None


class MemoFetcher:
    """Remembers every page fetched in this run so enrichment does not request the same page again. Same interface the page collector uses."""

    def __init__(self, inner):
        self.inner, self.memo, self.network_fetches = inner, {}, 0

    async def fetch(self, url, *a, **k):
        if url not in self.memo:
            self.network_fetches += 1
            self.memo[url] = await self.inner.fetch(url, *a, **k)
        return self.memo[url]

    async def aclose(self) -> None:
        await self.inner.aclose()


def planned_requests(cfg: WebConfig) -> dict:
    qs = plan_queries(cfg.industries, cfg.cities, cfg.max_queries, cfg.max_pages_per_query)
    search = len(qs) if cfg.provider.needs_network else 0
    return {"queries": len(qs), "search_requests_max": search, "site_visits_max": cfg.max_sites, "page_requests_max": cfg.max_sites * 4,
            "confirm_count": search + cfg.max_sites, "provider": cfg.provider.describe(), "first_queries": [q.text for q in qs[:8]]}


def _rows_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("x", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: spreadsheet_safe(v) if isinstance(v, str) else v for k, v in r.items()})


def _setup_logging(path: Path) -> logging.Handler:
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(h)
    return h


async def run_web_discovery(cfg: WebConfig) -> dict:
    t0 = time.monotonic()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(cfg.out_root) / f"{stamp}-websearch{('-' + cfg.label) if cfg.label else ''}"
    out.mkdir(parents=True, exist_ok=False)
    handler = _setup_logging(out / "run.log")
    cache = QueryCache(cfg.cache_dir or (Path(cfg.out_root) / "_search_cache"), cfg.cache_ttl)
    queries = plan_queries(cfg.industries, cfg.cities, cfg.max_queries, cfg.max_pages_per_query)
    log.info("run %s: provider=%s target=%d queries<=%d sites<=%d", out.name, cfg.provider.name, cfg.target, len(queries), cfg.max_sites)
    own_fetcher = cfg.fetcher is None
    if own_fetcher:
        from ..safety import live_settings
        settings = cfg.settings or live_settings(database_url="postgresql://unused@localhost/unused", concurrency=cfg.concurrency, min_delay=cfg.min_delay, max_pages=3)
        fetcher = Fetcher(settings, db=None, audit=JsonlAudit(out / "fetch_log.jsonl"))
    else:
        fetcher = cfg.fetcher
    memo = MemoFetcher(fetcher)

    cands: dict[str, dict] = {}            # domain -> candidate (first sighting wins; later sightings are recorded as extra hits)
    records: dict[str, SiteRecord] = {}
    rejections: list[Rejection] = []
    rej_counts: Counter = Counter()
    search = {"queries_run": 0, "cached": 0, "results_seen": 0, "errors": Counter(), "requests": 0}
    stop_reason, fail_streak, sites_visited = "queries_exhausted", 0, 0
    sem = asyncio.Semaphore(max(1, min(cfg.concurrency, 4)))
    search_log = (out / "search_log.jsonl").open("x", encoding="utf-8")
    seen_names: dict[str, str] = {}

    def valid_unique() -> int:
        return sum(1 for d, r in records.items() if r.status == "valid" and cands[d].get("dup_of") is None)

    async def validate(c: dict) -> None:
        nonlocal sites_visited
        async with sem:
            if time.monotonic() - t0 > cfg.max_seconds:
                return
            sites_visited += 1
            kws = expand_keywords([c["industry_query"]], cfg.industries.get(c["industry_query"], []))
            try:
                rec = await validate_site(memo, c, kws)
            except Exception as e:                                  # one bad site never stops the run
                log.exception("validation crashed for %s", c["domain"])
                rec = SiteRecord(status="unreachable", detail=f"validation_error: {type(e).__name__}")
            records[c["domain"]] = rec
            log.info("%s -> %s%s", c["domain"], rec.status, f" ({rec.detail})" if rec.detail else "")

    try:
        for start in range(0, len(queries), cfg.batch_queries):
            if valid_unique() >= cfg.target:
                stop_reason = "target_reached"
                break
            if time.monotonic() - t0 > cfg.max_seconds:
                stop_reason = "time_budget_exhausted"
                break
            if sites_visited >= cfg.max_sites:
                stop_reason = "site_budget_exhausted"
                break
            new: list[dict] = []
            for q in queries[start:start + cfg.batch_queries]:
                cached = cache.get(cfg.provider.name, q.text, q.page, cfg.results_per_query)
                err = ""
                if cached is not None:
                    results, search["cached"] = cached, search["cached"] + 1
                else:
                    try:
                        results = await cfg.provider.search(q.text, page=q.page, count=cfg.results_per_query)
                        cache.put(cfg.provider.name, q.text, q.page, cfg.results_per_query, results)
                        search["requests"] += 1
                    except SearchError as e:
                        results, err = [], f"{e.kind}: {e}"[:200]
                        search["errors"][e.kind] += 1
                        log.warning("search failed [%s] %r: %s", cfg.provider.name, q.text, err)
                        if e.kind in ("not_configured", "quota"):
                            fail_streak = cfg.max_provider_failures            # retrying cannot help
                search["queries_run"] += 1
                fail_streak = 0 if (results and not err) else fail_streak + (1 if err else 0)
                search["results_seen"] += len(results)
                search_log.write(json.dumps({"query": q.text, "page": q.page, "industry": q.industry, "city": q.city, "provider": cfg.provider.name,
                                             "results": len(results), "cached": cached is not None, "error": err}) + "\n")
                search_log.flush()
                for r in results:
                    c, rej = from_result(r, q, allow_synthetic=cfg.allow_synthetic)
                    if rej:
                        rejections.append(rej)
                        rej_counts[rej.reason] += 1
                        continue
                    old = cands.get(c["domain"])
                    if old:
                        old["hits"] += 1
                        old["also_found_via"].append(f"{q.text} #{r.rank}")
                        continue
                    c["hits"], c["also_found_via"], c["dup_of"] = 1, [], None
                    cands[c["domain"]] = c
                    new.append(c)
                if fail_streak >= cfg.max_provider_failures:
                    break
            if fail_streak >= cfg.max_provider_failures:
                stop_reason = "search_provider_failing"
            new.sort(key=priority)                                  # official-looking domains are visited first (matters when the site budget is tight)
            room = max(0, cfg.max_sites - sites_visited)
            if len(new) > room:
                stop_reason = "site_budget_exhausted"
            await asyncio.gather(*(validate(c) for c in new[:room]))
            for c in new[:room]:                                    # identity de-duplication AFTER the site told us its real name
                r = records.get(c["domain"])
                key = identity_key((r.name if r else "") or c.get("name") or "")
                if r and r.status == "valid" and key:
                    if key in seen_names:
                        c["dup_of"] = seen_names[key]
                    else:
                        seen_names[key] = c["domain"]
            if stop_reason in ("search_provider_failing", "site_budget_exhausted"):
                break
        else:
            stop_reason = "target_reached" if valid_unique() >= cfg.target else "queries_exhausted"
        if stop_reason == "queries_exhausted" and valid_unique() >= cfg.target:
            stop_reason = "target_reached"
    finally:
        search_log.close()

    # ---- rows
    valid = [c for d, c in cands.items() if records.get(d) and records[d].status == "valid" and c["dup_of"] is None]
    ordered = balanced_order([{"location": c["city_query"], "industry": c["industry_query"], "_c": c} for c in valid])
    chosen = {id(x["_c"]) for x in ordered[:cfg.target]}
    rows = []
    for d, c in cands.items():
        r = records.get(d)
        if r is None:
            status, detail = "not_visited", "site budget or time budget reached before this candidate was visited"
        elif c["dup_of"]:
            status, detail = "duplicate_identity", f"same company name as {c['dup_of']}"
        elif r.status == "valid":
            status, detail = (S_SELECTED if id(c) in chosen else S_NOT_SELECTED), ("" if id(c) in chosen else "valid but beyond the target (city-balanced selection)")
        else:
            status, detail = r.status, r.detail
        r = r or SiteRecord()
        rows.append({"Company": r.name or c.get("name") or "", "Website": c["website"], "City": c["city_query"], "Industry/Niche": c["industry_query"],
                     "SourceURL": c["source_url"], "DiscoveryStatus": status, "Confidence": score(r, r.name_confirmed, c["hits"] - 1) if r.status in ("valid", "india_unconfirmed") else 0.0, "MissingFields": "; ".join(r.missing_fields),
                     "Description": r.description, "Email": r.email, "Phone": r.phone, "Address": r.address, "LinkedIn": r.linkedin_url,
                     "ContactSourceURL": r.contact_source_url, "IndiaEvidence": r.india_evidence, "IndustryEvidence": r.industry_evidence,
                     "NameSource": r.name_source, "Domain": d, "SearchProvider": c["provider"], "SearchQuery": c["query"], "SearchRank": c["rank"],
                     "AlsoFoundVia": " | ".join(c["also_found_via"][:5]), "Detail": detail})
    _rows_csv(out / "discovery.csv", CSV_COLUMNS, rows)
    (out / "discovery.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    _rows_csv(out / "rejected_results.csv", ["url", "reason", "detail", "query", "provider"], [r.__dict__ for r in rejections])
    sel = [r for r in rows if r["DiscoveryStatus"] == S_SELECTED]
    _rows_csv(out / "profile_input.csv", PROFILE_INPUT_COLUMNS, sel)

    status_counts = Counter(r["DiscoveryStatus"] for r in rows)
    summary = {
        "run_directory": str(out), "provider": cfg.provider.describe(), "target": cfg.target, "valid_companies": len(valid),
        "selected_for_enrichment": len(sel), "reached_target": len(valid) >= cfg.target, "stop_reason": stop_reason,
        "queries_planned": len(queries), "queries_run": search["queries_run"], "queries_served_from_cache": search["cached"],
        "search_requests_made": search["requests"], "search_errors": dict(search["errors"]), "search_results_seen": search["results_seen"],
        "results_rejected": dict(rej_counts), "candidate_websites": len(cands), "sites_visited": sites_visited,
        "page_requests_to_company_sites": memo.network_fetches, "discovery_status_counts": dict(status_counts),
        "fields_populated_among_selected": {k: sum(1 for r in sel if r[k]) for k in ("Company", "Description", "Email", "Phone", "Address", "LinkedIn", "IndustryEvidence")},
        "city_distribution": dict(Counter(r["City"] for r in sel).most_common()), "industry_distribution": dict(Counter(r["Industry/Niche"] for r in sel).most_common()),
        "mean_confidence_selected": round(sum(float(r["Confidence"] or 0) for r in sel) / len(sel), 2) if sel else None,
        "elapsed_seconds_discovery": round(time.monotonic() - t0, 1), "paid_api": cfg.provider.name == "brave",
        "notes": ["City and Industry/Niche are what was SEARCHED for; IndiaEvidence says whether the site itself confirms India/the city.",
                  "Emails/phones are publicly listed on the company's own pages; emails are not mailbox-verified.",
                  "Confidence is a heuristic over evidence found (see validate.score), not a probability."]}
    if cfg.enrich and sel:
        summary["enrichment"] = await _enrich(cfg, out, memo, summary)
    elif cfg.enrich:
        summary["enrichment"] = {"skipped": "no valid companies were discovered"}
    if own_fetcher:
        await memo.aclose()
    await cfg.provider.aclose()
    summary["elapsed_seconds_total"] = round(time.monotonic() - t0, 1)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("done: %d valid of target %d (%s) in %.0fs", len(valid), cfg.target, stop_reason, summary["elapsed_seconds_total"])
    log.removeHandler(handler)
    handler.close()
    return summary


async def _enrich(cfg: WebConfig, out: Path, memo: MemoFetcher, summary: dict) -> dict:
    """Run the existing profile enrichment (ScrapeGraphAI + Ollama) on profile_input.csv, reusing the pages already fetched."""
    from ...backends import BackendConfigError, BackendUnavailable
    from ...profiles.runner import ProfileConfig, ProfileError, run_profiles
    kw = dict(cfg.enrich_kwargs)
    pc = ProfileConfig(input_path=str(out / "profile_input.csv"), output_path=str(out / "enriched.csv"), max_companies=cfg.target, fetcher=memo,
                       backend=cfg.backend, allow_synthetic=cfg.allow_synthetic, settings=cfg.settings, **kw)
    try:
        res = await run_profiles(pc)
    except (BackendUnavailable, BackendConfigError) as e:
        log.error("enrichment not run: %s", e)
        return {"ran": False, "error": f"model unavailable: {e}"[:300], "note": "discovery outputs are complete; re-run enrichment with `python -m leadengine.profiles`"}
    except ProfileError as e:
        return {"ran": False, "error": str(e)[:300]}
    from ..e2e import summarize
    full = summarize(str(out / "discovery.csv"), res["output"], res.get("summary_file"), str(out))
    return {"ran": True, "enriched_csv": res["output"], "profiles_summary": res.get("summary_file"), "combined_summary": full.get("summary_file"),
            "status_counts": res["status_counts"], "outcomes": {k: full[k] for k in ("successfully_enriched", "partially_enriched", "failed", "anti_bot_blocked",
                                                                                      "robots_disallowed", "skipped", "not_processed")},
            "page_requests_saved_by_reuse": memo.network_fetches}
