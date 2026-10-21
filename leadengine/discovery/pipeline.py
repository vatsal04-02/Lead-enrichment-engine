"""End-to-end orchestration: discover -> validate/dedupe -> enrich (with fallback) -> write the timestamped output directory.

`execute()` is the single entry point used by the CLI and by tests. It contacts a network host only when `plan.authorized` is True
(the CLI sets it from the explicit --live confirmations); otherwise it runs the offline parts, writes a dry-run report, and says
exactly what it would have contacted."""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..config import Settings
from ..engine import Engine
from ..fetcher import Fetcher
from .discover import DiscoveryResult, discover, plan as plan_requests
from .doctor import DEFAULT_MODEL, DEFAULT_URL
from .enrich import EnrichRunner, Lead
from .models import now_iso
from .niche import expand_keywords
from .outputs import (make_output_dir, write_candidates, write_errors, write_json, write_leads, write_rejected, write_report)
from .runner import build_primary, export_fetch_log
from .safety import DiscoveryError, JsonlAudit, database_target

LIVE_LABEL = "LIVE (real websites contacted)"
SYNTH_LABEL = "SYNTHETIC FIXTURES (local test servers; not real businesses)"


@dataclass
class RunPlan:
    command: str                                   # discover | run
    sources: list
    settings: Settings
    niches: list = field(default_factory=lambda: ["AI automation agency"])
    locations: list = field(default_factory=list)
    extra_keywords: list = field(default_factory=list)
    limit: int = 100
    out_root: str = "discovery_out"
    label: str = ""
    authorized: bool = False                        # True only after the explicit live confirmations (or for local test servers)
    mode_label: str = LIVE_LABEL
    allow_synthetic: bool = False                   # tests only: accept *.fixture.test hosts
    backend_mode: str = "auto"                      # auto | scrapegraph | inhouse
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_URL
    sg_python: str = ""
    request_timeout: float = 120.0
    max_seconds: int = 3600
    max_company_targets: Optional[int] = None       # the number the person confirmed: execute() refuses to enrich more than this
    primary_factory: Optional[Callable[..., Awaitable]] = None      # tests: inject a primary enricher
    next_commands: list = field(default_factory=list)
    argv: list = field(default_factory=list)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).resolve().parent).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _config_record(p: RunPlan, mode: str, started: str) -> dict:
    s = p.settings
    return {"started_at": started, "mode": mode, "command": p.command, "git_commit": _git_commit(), "python": sys.version.split()[0],
            "niches": p.niches, "locations": p.locations, "limit": p.limit, "sources": [x.name for x in p.sources],
            "backend_mode": p.backend_mode, "model": p.model if p.backend_mode != "inhouse" else None,
            "ollama_url": p.base_url if p.backend_mode != "inhouse" else None,
            "crawl": {"respect_robots": s.respect_robots, "concurrency": s.global_concurrency, "per_host_delay_s": s.per_domain_min_delay,
                      "max_pages_per_company": s.max_pages_per_company, "http_retries": s.http_max_retries, "total_timeout_s": s.total_timeout,
                      "max_response_bytes": s.max_response_bytes, "browser": s.browser_enabled, "user_agent": s.user_agent},
            "max_seconds": p.max_seconds, "paid_apis": "none", "database": database_target(s.database_url) + " (credentials not recorded)"}


async def execute(plan: RunPlan) -> dict:
    t_start = time.monotonic()
    out = make_output_dir(plan.out_root, plan.label)
    started = now_iso()
    mode = plan.mode_label if plan.authorized else "DRY RUN (nothing contacted)"
    keywords = expand_keywords(plan.niches, plan.extra_keywords)
    net_sources = [s for s in plan.sources if s.needs_network]
    run_sources = plan.sources if plan.authorized else [s for s in plan.sources if not s.needs_network]

    # ---------------- discovery
    fetcher = None
    audit_path = out / "discovery_fetch_log.jsonl"
    if any(s.needs_network for s in run_sources):
        fetcher = Fetcher(plan.settings, db=None, audit=JsonlAudit(audit_path))
    t0 = time.monotonic()
    try:
        dres: DiscoveryResult = await discover(run_sources, fetcher, niches=plan.niches, locations=plan.locations, limit=plan.limit,
                                               extra_keywords=plan.extra_keywords, allow_synthetic=plan.allow_synthetic)
    finally:
        if fetcher is not None:
            await fetcher.aclose()
    discover_seconds = time.monotonic() - t0
    if plan.authorized and plan.max_company_targets is not None and len(dres.candidates) > plan.max_company_targets:
        # The confirmation named an exact number. The candidate list changed after it was counted (e.g. the CSV was edited): refuse
        # before anything is written or any company site is contacted.
        try:
            out.rmdir()
        except OSError:
            pass
        raise DiscoveryError(f"refused: this run would contact {len(dres.candidates)} company site(s) but the confirmation covered "
                             f"{plan.max_company_targets}. The input changed after it was counted; re-run `plan` and confirm again.")
    if not plan.authorized:
        for s in net_sources:
            lines = plan_requests([s], plan.niches, plan.locations, plan.limit, keywords)
            for line in lines:
                dres.notes.append(f"NOT RUN (dry run) - `{s.name}` would make: {line}")
            if not lines:
                dres.notes.append(f"NOT RUN - `{s.name}` has nothing to contact as configured (check --location / --seed-url)")
    files = {}
    files["discovery_candidates.csv"] = f"{len(dres.candidates)} validated, de-duplicated candidates with source, niche/location query, timestamps"
    cand_path = write_candidates(out, dres.candidates)
    if write_rejected(out, dres.rejected):
        files["rejected_candidates.csv"] = f"{len(dres.rejected)} rejected/duplicate rows with reasons"

    # ---------------- enrichment
    leads: list[Lead] = []
    rows: list[dict] = []
    primary_note = ""
    extra_errors: list[dict] = []
    enrich_seconds = 0.0
    do_enrich = plan.command == "run" and plan.authorized and bool(dres.candidates)
    if do_enrich:
        t1 = time.monotonic()
        engine = Engine(plan.settings)
        await engine.start()
        backend = None
        try:
            since = (await _db_now(engine))
            factory = plan.primary_factory or _default_primary
            primary, backend, primary_note = await factory(engine, plan)
            if plan.backend_mode != "inhouse" and primary is None:
                extra_errors.append({"at": now_iso(), "stage": "enrichment", "kind": "primary_backend_unavailable", "message": primary_note})
            runner = EnrichRunner(engine, primary, primary_note=primary_note, concurrency=plan.settings.global_concurrency,
                                  max_seconds=plan.max_seconds, primary_requested=plan.backend_mode != "inhouse",
                                  page_budget=len(dres.candidates) * (plan.settings.max_pages_per_company + 1) + 10,
                                  log=lambda m: print(m, file=sys.stderr, flush=True))
            leads = await runner.run(dres.candidates)
            fl = await export_fetch_log(engine, {c.domain for c in dres.candidates}, since, out)
            if fl:
                files["fetch_log.jsonl"] = "every HTTP request made for the candidate sites (URL, status, error kind) - the audit trail"
        finally:
            if backend is not None:
                await backend.aclose()
            await engine.stop()
        enrich_seconds = time.monotonic() - t1
    elif plan.command == "run" and not plan.authorized:
        primary_note = "not run (dry run)"
    lp, rows = write_leads(out, leads, keywords)
    files["enriched_leads.csv"] = (f"{len(rows)} companies: every field with source URL, backend, method, status, timestamp; "
                                   f"failures are rows with review_status=needs_manual_review") if leads else "header only (no enrichment was run)"
    if audit_path.exists():
        files["discovery_fetch_log.jsonl"] = "every request the discovery sources made"
    ep = write_errors(out, dres.errors, leads, extra_errors)
    if ep:
        files["errors.jsonl"] = "source errors, refused/failed fetches and per-company failures (one JSON object per line)"
    cfg = _config_record(plan, mode, started)
    write_json(out, "run_config.json", cfg)
    files["run_config.json"] = "exact settings of this run (no secrets)"
    report = out / "discovery_report.md"
    files = {"discovery_report.md": "this file", **files}
    write_report(out, mode=mode, config=cfg, discovery=dres, leads=leads, rows=rows, primary_note=primary_note, files=files,
                 timings={"discover_seconds": discover_seconds, "enrich_seconds": enrich_seconds}, next_commands=plan.next_commands)
    return {"out_dir": str(out), "candidates_csv": str(cand_path), "leads_csv": str(lp), "report": str(report), "errors": str(ep) if ep else None,
            "candidates": len(dres.candidates), "leads": len(rows), "usable": sum(1 for r in rows if r["usable"] == "yes"),
            "needs_manual_review": sum(1 for r in rows if r["review_status"] == "needs_manual_review"), "mode": mode,
            "primary_note": primary_note, "seconds": round(time.monotonic() - t_start, 1), "discovery": dres, "leads_detail": leads, "rows": rows}


async def _db_now(engine):
    async with engine.db.connection() as c:
        return (await (await c.execute("SELECT now() AS t")).fetchone())["t"]


async def _default_primary(engine, plan: RunPlan):
    return await build_primary(engine, plan.settings, mode=plan.backend_mode, model=plan.model, base_url=plan.base_url,
                               python=plan.sg_python, request_timeout=plan.request_timeout)
