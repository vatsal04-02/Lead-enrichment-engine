"""Batch runner: CSV in -> guarded page fetch -> local model (ScrapeGraphAI + Ollama) -> grounded profile -> NEW CSV out.

Failure isolation: every company is processed inside its own try/except with a hard deadline, so a bad URL, a robots refusal, a timeout or a
model error is recorded on THAT row and the batch continues. The one exception is the model itself going away (Ollama stopped): fetching
more websites would be pointless, so the remaining rows are written as `not_processed` and the run ends non-zero.

No database is opened or needed. The input file is only ever read.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..backends import BackendUnavailable, make_llm_backend
from ..config import Settings
from ..csv_io import CSVError, CSVReader, spreadsheet_safe
from ..discovery.runner import scrapegraph_settings
from ..discovery.safety import live_settings
from ..discovery.urlcheck import check_url
from ..fetcher import Fetcher
from ..security import sanitize_for_log
from .ground import HYPOTHESIS_PREFIX, Profile, confidence, confidence_notes, ground_profile
from .pages import build_document, collect_pages
from .schema import PROMPT_VERSION, SCHEMA, build_prompt

log = logging.getLogger("leadengine.profiles")

DEFAULT_MODEL = "qwen3:4b"
DEFAULT_URL = "http://localhost:11434"
ENRICH_COLUMNS = ["company_name", "website", "business_description", "products_services", "industry_niche", "target_customers",
                  "pain_points", "ai_automation_opportunities", "outreach_angle", "evidence_urls", "confidence", "model_confidence",
                  "field_support", "support_notes", "evidence", "pages_used", "pages_failed", "warnings", "status", "error", "backend", "model", "enriched_at"]
OUTPUT_TOKENS = 1400
MAX_INPUT_CHARS = 12000


class ProfileError(SystemExit):
    """User-facing refusal (the message is the exit text)."""


@dataclass
class ProfileConfig:
    input_path: str
    output_path: Optional[str] = None
    mapping: Optional[dict] = None
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_URL
    sg_python: str = ""
    model_timeout: float = 300.0
    retries: int = 1
    max_companies: int = 25
    min_delay: float = 3.0
    max_pages: int = 4
    company_timeout: Optional[float] = None
    dry_run: bool = False
    # test seams (never set by the CLI): crawl settings with host overrides, an injected backend, acceptance of *.fixture.test hosts
    settings: Optional[Settings] = None
    backend: object = None
    allow_synthetic: bool = False
    fetcher: object = None             # an already-configured (e.g. memoising) page fetcher shared with web discovery; the runner does not close it


def default_output(input_path: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path("profile_out") / f"{input_path.stem}.enriched-{stamp}.csv"          # created only when something is written


def output_columns(input_headers: list[str]) -> list[str]:
    """Enrichment columns appended after the input's own; a name that collides (case-insensitively) with an input column gets '_enriched'."""
    taken = {h.lower() for h in input_headers}
    return [c if c.lower() not in taken else f"{c}_enriched" for c in ENRICH_COLUMNS]


def _blank_row(status: str, error: str, website: str = "") -> dict:
    return {"website": website, "status": status, "error": error, "confidence": "", "backend": "", "model": ""}


def profile_row(p: Profile, pages: list, failures: list, website: str, model: str, usage, warnings: list = ()) -> dict:
    v = p.values
    urls = list(dict.fromkeys(e.url for e in p.evidence))
    status = "ok" if p.supported_extractive else "no_evidence"
    conf = confidence(p)
    hyp = lambda x: (HYPOTHESIS_PREFIX + x) if x else ""           # inferred cells are visibly labelled: a hypothesis is not a fact
    return {
        "company_name": v.get("company_name", ""), "website": website, "business_description": v.get("business_description", ""),
        "products_services": "; ".join(v.get("products_services", [])), "industry_niche": v.get("industry_niche", ""),
        "target_customers": v.get("target_customers", ""), "pain_points": hyp("; ".join(v.get("pain_points", []))),
        "ai_automation_opportunities": hyp("; ".join(v.get("ai_automation_opportunities", []))), "outreach_angle": hyp(v.get("outreach_angle", "")),
        "evidence_urls": " | ".join(urls), "confidence": conf, "model_confidence": p.model_confidence,
        "field_support": json.dumps(p.support, separators=(",", ":")),
        "support_notes": json.dumps(p.reasons, ensure_ascii=False) if p.reasons else "",
        "evidence": json.dumps([{"field": e.field, "url": e.url, "quote": e.quote, "overlap": e.overlap} for e in p.evidence][:40], ensure_ascii=False),
        "pages_used": " | ".join(f"{pg.url} ({pg.kind})" for pg in pages),
        "pages_failed": " | ".join(f"{f['url']} [{f['error_kind']}]" for f in failures),
        "warnings": " | ".join([*warnings, *confidence_notes(p, conf)]), "status": status,
        "error": "" if status == "ok" else "the model's statements could not be tied to text on the pages",
        "backend": "scrapegraph", "model": model, "enriched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def crawl_settings(cfg: ProfileConfig) -> Settings:
    s = cfg.settings
    if s is None:
        s = live_settings(database_url="postgresql://unused@localhost/unused",     # never connected: no database is used by this tool
                          concurrency=1, min_delay=cfg.min_delay, max_pages=3)
    return s.model_copy(update={"max_pages_per_company": min(max(cfg.max_pages, 1), 4), "max_pages_per_domain": min(max(cfg.max_pages, 1), 4) + 1})


def model_settings(cfg: ProfileConfig, base: Settings) -> Settings:
    s = scrapegraph_settings(base, model=cfg.model, base_url=cfg.base_url, python=cfg.sg_python, request_timeout=cfg.model_timeout)
    s = s.model_copy(update={"scrapegraph_retries": min(max(cfg.retries, 0), 2), "scrapegraph_max_calls_per_company": 3,
                             "scrapegraph_max_input_chars": MAX_INPUT_CHARS, "scrapegraph_max_output_tokens": OUTPUT_TOKENS})
    s._check()
    return s


@dataclass
class Job:
    index: int
    original: dict
    website_raw: str
    ident: object = None
    skip: Optional[tuple] = None          # (status, reason)


def plan_jobs(rd: CSVReader, cfg: ProfileConfig) -> list[Job]:
    jobs, seen = [], {}
    for row in rd.rows():
        j = Job(row.row_index, row.original, row.website or row.domain or "")
        if row.error and not j.website_raw:
            j.skip = ("skipped", row.error)
        else:
            st, reason, ident, _syn = check_url(j.website_raw, allow_synthetic=cfg.allow_synthetic)
            if ident is None:
                j.skip = ("skipped", f"{st}: {reason}")
            elif ident.key in seen:
                j.skip = ("skipped", f"duplicate of row {seen[ident.key]}")
            else:
                j.ident = ident
                seen[ident.key] = row.row_index
        jobs.append(j)
    todo = 0
    for j in jobs:
        if j.skip is None:
            todo += 1
            if todo > cfg.max_companies:
                j.skip, j.ident = ("skipped", f"beyond --max-companies {cfg.max_companies}"), None
    return jobs


async def run_profiles(cfg: ProfileConfig) -> dict:
    t_start = time.monotonic()
    inp = Path(cfg.input_path)
    if not inp.is_file():
        raise ProfileError(f"input file not found: {inp}")
    try:
        rd = CSVReader(str(inp), cfg.mapping)
    except CSVError as e:
        raise ProfileError(f"cannot read {inp.name}: {e}")
    jobs = plan_jobs(rd, cfg)
    todo = [j for j in jobs if j.skip is None]
    skipped = [j for j in jobs if j.skip is not None]
    out = Path(cfg.output_path) if cfg.output_path else default_output(inp)
    if out.resolve() == inp.resolve():
        raise ProfileError("the output path is the input file; the input is never modified. Choose a different -o path.")
    if out.exists():
        raise ProfileError(f"{out} already exists; existing files are never overwritten. Choose another -o path.")
    log.info("input %s: %d rows, %d to process, %d skipped; model %s via %s", inp.name, len(jobs), len(todo), len(skipped), cfg.model, cfg.base_url)
    for j in skipped:
        log.info("row %d skipped: %s", j.index, j.skip[1])
    if cfg.dry_run:
        return {"dry_run": True, "rows": len(jobs), "to_process": len(todo), "skipped": len(skipped), "output": str(out),
                "targets": [j.ident.start_url for j in todo]}

    base = crawl_settings(cfg)
    ms = model_settings(cfg, base)
    backend = cfg.backend or make_llm_backend(ms)
    if todo:
        log.info("checking the local model (no website is contacted before this passes)")
        await backend.preflight()                         # BackendUnavailable / BackendConfigError -> caller, before any fetch and before the output exists
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = output_columns(rd.headers)
    fetcher = cfg.fetcher or Fetcher(base, db=None)
    company_timeout = cfg.company_timeout or (cfg.model_timeout * (cfg.retries + 1) + 180)
    counts: dict[str, int] = {}
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0.0, "retries": 0, "timeouts": 0}
    fatal: Optional[BackendUnavailable] = None
    try:
        with out.open("x", newline="", encoding="utf-8-sig") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rd.headers) + cols, extrasaction="ignore")
            wr.writeheader()

            def emit(job: Job, vals: dict) -> None:
                row = dict(job.original)
                for src, dst in zip(ENRICH_COLUMNS, cols):
                    v = vals.get(src, "")
                    row[dst] = spreadsheet_safe(v) if isinstance(v, str) else v
                wr.writerow(row)
                fh.flush()
                counts[vals["status"]] = counts.get(vals["status"], 0) + 1

            n_done = 0
            for job in jobs:
                if job.skip:
                    emit(job, _blank_row(job.skip[0], job.skip[1], job.website_raw))
                    continue
                if fatal is not None:
                    emit(job, _blank_row("not_processed", f"model unavailable: {fatal}"[:300], job.ident.start_url))
                    continue
                n_done += 1
                t0 = time.monotonic()
                log.info("[%d/%d] %s: fetching pages", n_done, len(todo), job.ident.key)
                try:
                    async with asyncio.timeout(company_timeout):
                        vals = await _process(job, fetcher, backend, base, cfg, totals)
                except BackendUnavailable as e:
                    fatal = e
                    log.error("[%d/%d] %s: the local model is no longer available (%s); stopping further website requests", n_done, len(todo), job.ident.key, e)
                    vals = _blank_row("failed", f"model unavailable: {e}"[:300], job.ident.start_url)
                except TimeoutError:
                    vals = _blank_row("failed", f"company_timeout: exceeded {company_timeout:.0f}s", job.ident.start_url)
                except Exception as e:                       # one bad website must never stop the batch
                    log.exception("[%d/%d] %s: unexpected error", n_done, len(todo), job.ident.key)
                    vals = _blank_row("failed", sanitize_for_log(f"{type(e).__name__}: {e}", 250), job.ident.start_url)
                log.info("[%d/%d] %s: %s (confidence %s) in %.1fs%s", n_done, len(todo), job.ident.key, vals["status"], vals.get("confidence") or "-",
                         time.monotonic() - t0, f" - {vals['error']}" if vals.get("error") else "")
                emit(job, vals)
    finally:
        if cfg.fetcher is None:
            await fetcher.aclose()
        if cfg.backend is None:
            await backend.aclose()
    summary = {"input": str(inp), "output": str(out), "rows": len(jobs), "status_counts": counts, "model": cfg.model, "endpoint": cfg.base_url,
               "prompt_version": PROMPT_VERSION, "model_usage": totals, "seconds": round(time.monotonic() - t_start, 1),
               "model_unavailable_mid_run": str(fatal) if fatal else None,
               "note": "paid API cost: 0 (local model). Presence of a value is not correctness; inferred fields are hypotheses for human review."}
    sp = out.with_suffix(".summary.json")
    with sp.open("x", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    summary["summary_file"] = str(sp)
    log.info("done: %s in %.1fs -> %s", counts, summary["seconds"], out)
    return summary


async def _process(job: Job, fetcher, backend, base: Settings, cfg: ProfileConfig, totals: dict) -> dict:
    ps = await collect_pages(fetcher, job.ident, max_pages=cfg.max_pages)
    for f in ps.failures:
        log.info("%s: page skipped (%s): %s", job.ident.key, f["error_kind"], f["url"])
    if not ps.pages:
        return _blank_row("failed", f"{ps.error_kind}: {ps.error}"[:300], job.ident.start_url)
    html, texts, warnings = build_document(ps.pages, MAX_INPUT_CHARS)
    for w in warnings:
        log.warning("%s: %s", job.ident.key, w)
    log.info("%s: %d page(s) -> model (%d chars)", job.ident.key, len(ps.pages), len(html))
    values, out = await backend.extract_structured(html, build_prompt(), SCHEMA, max_output_tokens=OUTPUT_TOKENS)
    for k, src in (("calls", out.usage.calls), ("tokens_in", out.usage.tokens_in), ("tokens_out", out.usage.tokens_out),
                   ("latency_ms", out.usage.latency_ms), ("retries", out.usage.retries), ("timeouts", out.usage.timeouts)):
        totals[k] += src
    if not isinstance(values, dict):
        return _blank_row("failed", f"{out.error_kind or 'model_error'}: {out.error or 'no usable answer from the model'}"[:300], job.ident.start_url) | {
            "pages_used": " | ".join(f"{p.url} ({p.kind})" for p in ps.pages), "warnings": " | ".join(warnings)}
    prof = ground_profile(values, texts, {pg.url: pg.kind for pg in ps.pages})
    return profile_row(prof, ps.pages, ps.failures, job.ident.start_url, cfg.model, out.usage, warnings)
