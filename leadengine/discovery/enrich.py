"""Enrichment orchestration with an explicit, recorded fallback chain:

    1. ScrapeGraphAI on a local Ollama model (the existing backend; page content only, no network for the library),
    2. the in-house HTTP/HTML engine (same guarded fetcher),
    3. a manual-review record when both cannot produce anything.

Both engines share ONE Fetcher, so per-host rate limits, robots.txt, the page budget and the audit trail are common. A fallback is
used only for model-side failures (model error/timeout/unavailable, worker crash) or when the model run found nothing on a
reachable site. Access refusals (robots, 401/403, bot challenge, off-domain redirect, unreachable) are NEVER retried by the other
engine: that would just repeat the refused request.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from ..budget import Budget
from ..pipeline import EnrichResult, Enricher
from ..security import sanitize_for_log
from ..store import company_view
from .models import Candidate

REQUESTED = ["company_name", "description", "email", "phone", "address", "industry", "linkedin_url"]
MODEL_FAILURES = {"model_error", "model_timeout", "model_unavailable", "worker_crash", "dependency_unavailable"}
NEVER_RETRY = {"robots", "bot_challenge", "blocked", "redirected_off_domain", "budget"}
BACKEND_OF_METHOD = {"scrapegraph_llm": "scrapegraph", "input": "input"}


def backend_of(method: Optional[str]) -> str:
    return "" if not method else BACKEND_OF_METHOD.get(method, "inhouse")


@dataclass
class Attempt:
    backend: str                   # scrapegraph | inhouse
    outcome: str
    error_kind: str = ""
    error: str = ""
    ms: int = 0
    forced: bool = False


@dataclass
class Lead:
    candidate: Candidate
    status: str = "pending"        # enriched | partial | no_data | unreachable | failed | cached | skipped_budget | skipped_time
    attempts: list = field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
    primary_note: str = ""
    error_kind: str = ""
    error: str = ""
    view: Optional[dict] = None
    metrics: list = field(default_factory=list)       # EnrichResult.metrics of every attempt
    enriched_at: str = ""
    manual_review_reason: str = ""


class EnrichRunner:
    def __init__(self, engine, primary: Optional[Enricher], *, primary_note: str = "", requested: list[str] = None,
                 concurrency: int = 2, max_seconds: int = 3600, page_budget: int = 0, log=None, primary_requested: Optional[bool] = None):
        self.engine, self.primary, self.primary_note = engine, primary, primary_note
        self.fallback: Enricher = engine.enricher
        self.primary_requested = (primary is not None) if primary_requested is None else primary_requested
        self.requested = requested or list(REQUESTED)
        self.sem = asyncio.Semaphore(max(1, min(concurrency, 4)))
        self.budget = Budget(pages=page_budget, seconds=max_seconds)
        self.log = log or (lambda m: None)

    async def run(self, candidates: list[Candidate]) -> list[Lead]:
        leads = [Lead(c, primary_note=self.primary_note) for c in candidates]
        await asyncio.gather(*(self._guarded(l, i, len(leads)) for i, l in enumerate(leads, 1)))
        return leads

    async def _guarded(self, lead: Lead, i: int, n: int) -> None:
        async with self.sem:
            if not self.budget.allows("pages"):
                reason = "seconds" if "seconds" in self.budget.exhausted_reasons else "pages"
                lead.status = "skipped_time" if reason == "seconds" else "skipped_budget"
                lead.manual_review_reason = f"not attempted: run {reason} budget exhausted"
                return
            try:
                await self._one(lead)
            except Exception as e:                      # never lose a company silently
                lead.status, lead.error_kind = "failed", "exception"
                lead.error = sanitize_for_log(f"{type(e).__name__}: {e}", 300)
                lead.manual_review_reason = f"unexpected error: {lead.error}"
            self.log(f"[{i}/{n}] {lead.candidate.domain}: {lead.status} via {backend_summary(lead)}")

    async def _call(self, enricher: Enricher, backend: str, lead: Lead, *, force: bool = False) -> EnrichResult:
        c = lead.candidate
        t0 = time.monotonic()
        res = await enricher.enrich(c.website, name=c.name, location=c.location, industry=c.industry, requested=self.requested,
                                    budget=self.budget, force=force)
        lead.attempts.append(Attempt(backend, res.outcome, res.error_kind, res.error, int((time.monotonic() - t0) * 1000), force))
        lead.metrics.append(res.metrics)
        return res

    async def _one(self, lead: Lead) -> None:
        res: Optional[EnrichResult] = None
        trigger = ""
        if self.primary is not None:
            res = await self._call(self.primary, "scrapegraph", lead)
            if res.outcome == "failed" and res.error_kind in MODEL_FAILURES:
                trigger = f"{res.error_kind}: {res.error}"[:200]
            elif res.outcome == "no_data":
                trigger = "model run found no data on a reachable site"
        if self.primary is None or trigger:
            res = await self._call(self.fallback, "inhouse", lead, force=bool(trigger and res is not None and res.outcome == "no_data"))
            lead.fallback_used = self.primary_requested
            lead.fallback_reason = trigger or (self.primary_note[:200] if self.primary_requested and self.primary is None else "")
        lead.status, lead.error_kind, lead.error = res.outcome, res.error_kind, res.error
        async with self.engine.db.connection() as conn:
            lead.view = await company_view(conn, lead.candidate.domain, self.requested)
        lead.enriched_at = (lead.view or {}).get("last_enriched_at") or ""
        if res.outcome in ("failed", "unreachable", "no_data"):
            why = f"{res.outcome}" + (f" ({res.error_kind}: {res.error})" if res.error_kind else "")
            tried = " -> ".join(f"{a.backend}:{a.outcome}" for a in lead.attempts)
            lead.manual_review_reason = f"{why}; attempts: {tried}"


def backend_summary(lead: Lead) -> str:
    return ",".join(dict.fromkeys(a.backend for a in lead.attempts)) or "none"


def field_backends(lead: Lead) -> set:
    """Backends that produced the stored field values (from each field's recorded extraction method)."""
    out = set()
    for f, v in ((lead.view or {}).get("fields") or {}).items():
        if v.get("value") and v.get("method"):
            out.add(backend_of(v["method"]))
    return out
