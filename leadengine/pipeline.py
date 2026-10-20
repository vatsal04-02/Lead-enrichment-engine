"""The enrichment cascade for a single company.

Tier 1  existing data (PostgreSQL)      -> skip everything that is fresh
Tier 2  homepage over pooled async HTTP -> deterministic parse
Tier 3  targeted extra pages            -> only for still-missing, valuable fields
Tier 4  browser rendering               -> only if the homepage is a JS shell
Tier 5  LLM extraction                  -> only for still-missing AI-eligible fields, grounded
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from . import store
from .ai import AIExtractor
from .browser import BrowserRenderer
from .budget import Budget, RunMetrics
from .config import Settings
from .db import Database
from .domain import Identity, InvalidDomain, normalize
from .extract import (EXTRACTOR_VERSION, METHOD_PRIOR, Candidate, PageExtraction, extract_page, pick_best)
from .fetcher import FetchResult, Fetcher
from .fields import FIELDS
from .planner import Plan, make_plan
from .scoring import score_company
from .validate import format_checks, mx_check, verify

MIN_WEIGHT_FOR_EXTRA_PAGE = 0.5   # don't spend pages chasing only low-value fields
PERMANENT_HTTP = {400, 401, 403, 404, 405, 406, 410, 451}


@dataclass
class EnrichResult:
    domain: str
    outcome: str                      # cached | enriched | partial | no_data | unreachable | failed
    company_id: Optional[int] = None
    transient: bool = False           # job layer should retry
    error_kind: str = ""
    error: str = ""
    score: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    budget_exhausted: list = field(default_factory=list)
    duration_ms: int = 0

    def summary(self) -> dict:
        return {"domain": self.domain, "outcome": self.outcome, "completeness": self.score.get("completeness"),
                "quality": self.score.get("quality"), "error_kind": self.error_kind, "error": self.error,
                "budget_exhausted": self.budget_exhausted, "plan": self.plan, "metrics": self.metrics}


def _parse(html: str, url: str, kind: str, key: str) -> dict:
    """Top-level so it can run in a process pool. Returns JSON-able dict."""
    return extract_page(html, url, kind, key).to_json()


class Enricher:
    def __init__(self, settings: Settings, db: Database, fetcher: Fetcher,
                 ai: Optional[AIExtractor] = None, browser: Optional[BrowserRenderer] = None,
                 parse_executor: Optional[Executor] = None):
        self.s, self.db, self.fetcher, self.ai, self.browser = settings, db, fetcher, ai, browser
        self.parse_executor = parse_executor

    # ---------------------------------------------------------------- parsing
    async def _parse_page(self, res: FetchResult, kind: str, key: str, run: RunMetrics) -> PageExtraction:
        cache_key = f"{res.final_url or res.url}#{kind}"
        async with self.db.connection() as c:
            row = await (await c.execute(
                "SELECT result FROM extraction_cache WHERE url=%s AND content_hash=%s AND extractor_version=%s",
                (cache_key, res.content_hash, EXTRACTOR_VERSION))).fetchone()
        if row:
            run.parse_cache_hits += 1
            return PageExtraction.from_json(row["result"])
        loop = asyncio.get_running_loop()
        d = await loop.run_in_executor(self.parse_executor, _parse, res.text, res.final_url or res.url, kind, key)
        async with self.db.connection() as c:
            await c.execute(
                """INSERT INTO extraction_cache (url, content_hash, extractor_version, result)
                   VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (cache_key, res.content_hash, EXTRACTOR_VERSION, Jsonb(d)))
        return PageExtraction.from_json(d)

    # ---------------------------------------------------------------- main
    async def enrich(self, website: str, *, name: str = None, location: str = None, industry: str = None,
                     requested: list[str], budget: Optional[Budget] = None, force: bool = False,
                     job_id=None, row_index: int = None) -> EnrichResult:
        t_start = time.monotonic()
        ident = normalize(website)           # raises InvalidDomain
        run = RunMetrics(job_id=str(job_id) if job_id else None)
        now = datetime.now(timezone.utc)

        # ---- Tier 1: existing data
        t1 = time.monotonic()
        async with self.db.connection() as c:
            cid = await store.upsert_company(c, ident, name, location, industry)
            rows = await store.load_fields(c, cid)
            comp = await (await c.execute("SELECT last_outcome, last_enriched_at FROM companies WHERE id=%s", (cid,))).fetchone()
        ai_ok = bool(self.ai) and (budget is None or budget.allows("ai_calls"))
        plan = make_plan(rows, requested, self.s, budget, now, ai_available=ai_ok, force=force)
        run.tier("tier1_existing").attempted = True
        run.tier("tier1_existing").ms = (time.monotonic() - t1) * 1000
        run.tier("tier1_existing").fields_found = len(plan.preserve)

        unreachable_recent = (not force and comp["last_outcome"] == "unreachable" and comp["last_enriched_at"]
                              and comp["last_enriched_at"] + timedelta(days=self.s.not_found_retry_days) > now)
        if not plan.needs_network or unreachable_recent:
            if unreachable_recent and plan.needs_network:
                web = rows.get("website") or {}
                return await self._finish(cid, ident, requested, plan, run, "unreachable", t_start, budget, job_id,
                                          row_index, record_outcome=False,
                                          error_kind=(web.get("checks") or {}).get("error_kind") or "unreachable",
                                          error=web.get("note") or "site was unreachable on a recent attempt")
            return await self._finish(cid, ident, requested, plan, run, "cached", t_start, budget, job_id, row_index,
                                      record_outcome=False)

        # ---- Tier 2: homepage
        picks_src: list[Candidate] = []
        pages_text: list[tuple[str, str]] = []
        link_map: dict[str, list[str]] = {}
        fetched: set[str] = set()
        t2 = run.tier("tier2_http")
        t2.attempted = True
        t0 = time.monotonic()
        home = await self.fetcher.fetch(ident.start_url, run, budget)
        if not home.ok and home.error_kind in ("network", "timeout") and ident.scheme_assumed \
                and ident.start_url.startswith("https://") and "DNS" not in home.error:
            home = await self.fetcher.fetch("http://" + ident.start_url[len("https://"):], run, budget)
        t2.ms += (time.monotonic() - t0) * 1000
        t2.requests = run.http_requests
        fetched.add(home.url)
        if not home.ok:
            return await self._handle_home_failure(cid, ident, requested, plan, run, home, t_start, budget, job_id, row_index)

        if not _on_site(home.final_url or home.url, ident):
            # The site redirected to a different registrable domain (rebrand, parking page, wrong input).
            # Its content is NOT evidence about the input company, so nothing is extracted from it.
            home = dataclasses.replace(home, error_kind="redirected_off_domain",
                                       error=f"redirected to {urlsplit(home.final_url).hostname}")
            return await self._handle_home_failure(cid, ident, requested, plan, run, home, t_start, budget, job_id, row_index)

        ex = await self._parse_page(home, "home", ident.key, run)
        picks_src += ex.candidates
        pages_text.append((home.final_url or home.url, ex.visible_text))
        link_map = ex.links
        home_js_hint = ex.js_hint
        homepage_url = home.final_url or home.url
        website_ok = True      # off-domain redirects were rejected above
        t2.fields_found = len(self._found(picks_src, plan, ident))

        # ---- Tier 3: targeted pages for still-missing valuable fields
        t3 = run.tier("tier3_targeted")
        pages_used = 1
        tried_kinds: set[str] = set()
        while pages_used < plan.max_pages:
            remaining = [f for f in plan.to_extract if f not in self._found(picks_src, plan, ident)]
            if not any(FIELDS[f].weight >= MIN_WEIGHT_FOR_EXTRA_PAGE for f in remaining):
                break
            target = None
            for kind in plan.page_priority:
                if kind == "home" or kind in tried_kinds:
                    continue
                if not any(kind in FIELDS[f].pages for f in remaining):
                    continue
                urls = [u for u in link_map.get(kind, []) if u not in fetched]
                if urls:
                    target = (kind, urls[0])
                    break
                tried_kinds.add(kind)
            if not target:
                break
            kind, url = target
            tried_kinds.add(kind)
            fetched.add(url)
            t3.attempted = True
            t0 = time.monotonic()
            before = run.http_requests
            res = await self.fetcher.fetch(url, run, budget)
            t3.ms += (time.monotonic() - t0) * 1000
            t3.requests += run.http_requests - before
            if budget is not None and res.error_kind == "budget":
                break
            if not res.ok or not _on_site(res.final_url or res.url, ident):
                continue
            pages_used += 1
            pex = await self._parse_page(res, kind, ident.key, run)
            picks_src += pex.candidates
            pages_text.append((res.final_url or res.url, pex.visible_text))
        t3.fields_found = len(self._found(picks_src, plan, ident)) - t2.fields_found

        # ---- Tier 4: browser rendering, only for JS shells with valuable fields still missing
        remaining = [f for f in plan.to_extract if f not in self._found(picks_src, plan, ident)]
        if (self.browser and self.s.browser_enabled and home_js_hint
                and any(FIELDS[f].weight >= MIN_WEIGHT_FOR_EXTRA_PAGE for f in remaining)):
            t4 = run.tier("tier4_browser")
            t0 = time.monotonic()
            before = run.http_requests
            rr = await self.browser.render(homepage_url, run, budget)
            t4.attempted = rr.error not in ("browser_unavailable", "budget")   # didn't run => not an attempt
            t4.ms = (time.monotonic() - t0) * 1000
            t4.requests = run.http_requests - before
            if rr.html:
                rex = _extract_inline(rr.html, homepage_url, ident.key)
                picks_src += [_tag(c, "tier4_browser") for c in rex.candidates]
                pages_text.insert(0, (homepage_url, rex.visible_text))
                for kind, urls in rex.links.items():
                    link_map.setdefault(kind, urls)
                t4.fields_found = len(self._found(picks_src, plan, ident)) - t2.fields_found - t3.fields_found
            else:
                t4.note = rr.error
            t4.cost_usd = run.cost_browser_usd

        # ---- Tier 5: grounded LLM extraction for what is still missing
        remaining = [f for f in plan.to_extract if f not in self._found(picks_src, plan, ident)
                     and FIELDS[f].ai_eligible]
        ai_note = ""
        if remaining and self.ai and budget_allows(budget, "ai_calls"):
            t5 = run.tier("tier5_ai")
            t5.attempted = True
            t0 = time.monotonic()
            if budget is not None:
                budget.charge("ai_calls")
            run.ai_calls += 1
            ordered = sorted(pages_text, key=lambda p: 0 if "about" in p[0].lower() else 1)
            ares = await self.ai.extract(remaining, [(u, t) for u, t in ordered if t])
            run.ai_tokens_in += ares.tokens_in
            run.ai_tokens_out += ares.tokens_out
            run.cost_ai_usd += self.ai.cost(ares.tokens_in, ares.tokens_out)
            t5.cost_usd = run.cost_ai_usd
            t5.ms = (time.monotonic() - t0) * 1000
            for f, (value, quote, src) in ares.values.items():
                picks_src.append(Candidate(f, value, "ai_grounded", src, quote, tier="tier5_ai"))
            t5.fields_found = len(ares.values)
            t5.note = ares.error or ("rejected:" + ",".join(f"{k}={v}" for k, v in ares.rejected.items()))
            ai_note = ares.error
        elif remaining and self.ai and not budget_allows(budget, "ai_calls"):
            run.tier("tier5_ai").note = "budget_exhausted"

        # ---- persist atomically
        await self._persist(cid, ident, plan, rows, picks_src, home, website_ok, now, run)
        outcome_fields = await self._reload_score(cid, requested)
        outcome = ("enriched" if outcome_fields["completeness"] >= 0.999 else
                   "partial" if outcome_fields["fields_present"] else "no_data")
        return await self._finish(cid, ident, requested, plan, run, outcome, t_start, budget, job_id, row_index,
                                  score=outcome_fields)

    # ---------------------------------------------------------------- helpers
    def _found(self, cands: list[Candidate], plan: Plan, ident: Identity) -> set[str]:
        return {f for f in pick_best(cands, ident.key) if f in plan.to_extract}

    async def _persist(self, cid, ident, plan, rows, cands, home, website_ok, now, run) -> None:
        picks = pick_best(cands, ident.key)
        all_by_field = cands
        mx_cache: dict[str, Optional[bool]] = {}
        async with self.db.connection() as c:
            for f in plan.to_extract:
                spec = FIELDS[f]
                ttl = self.s.freshness_for(f, spec.freshness_days)
                existing = rows.get(f)
                if f in picks:
                    best, alts = picks[f]
                    checks = format_checks(f, spec.kind, best.value, ident.key)
                    checks["published_on_source"] = True
                    vstatus, vmethod = verify(f, best, [x for x in all_by_field if x.field == f])
                    if f == "email" and self.s.email_dns_check:
                        dom = best.value.rsplit("@", 1)[-1].lower()
                        if dom not in mx_cache:
                            mx_cache[dom] = await mx_check(dom)
                        checks["domain_mx"] = mx_cache[dom]
                        if mx_cache[dom] is False:
                            vstatus, vmethod = "failed", "dns_mx"
                    tier = best.tier or ("tier2_http" if best.source_url == (home.final_url or home.url)
                                                           else "tier3_targeted")
                    await store.save_field(
                        c, cid, f, status="found", value=best.value, source_url=best.source_url, method=best.method,
                        tier=tier, prior=METHOD_PRIOR.get(best.method), verification_status=vstatus,
                        verification_method=vmethod, checks=checks, source_hash=home.content_hash,
                        alternates=[{"value": a.value, "method": a.method, "source_url": a.source_url} for a in alts],
                        refresh_after=now + timedelta(days=ttl), existing=existing)
                elif existing and existing["status"] == "found":
                    await store.defer_refresh(c, cid, f, now + timedelta(days=self.s.not_found_retry_days),
                                              "refresh found nothing; previous value kept")
                else:
                    await store.save_field(
                        c, cid, f, status="not_found", value=None, source_url=None, method=None, tier=None,
                        prior=None, verification_status="unverified", verification_method=None, checks={},
                        source_hash=None, alternates=[],
                        refresh_after=now + timedelta(days=self.s.not_found_retry_days), existing=existing,
                        note="searched; not present on fetched pages")
            # website: recorded from input, verified by reachability + domain match
            await store.save_field(
                c, cid, "website", status="found", value=home.final_url or home.url, source_url=home.url,
                method="input", tier="input", prior=METHOD_PRIOR["input"],
                verification_status="verified" if website_ok else "failed",
                verification_method="http_reachable_domain_match",
                checks={"nonempty": True, "url_syntax": True, "reachable": True, "domain_match": website_ok},
                source_hash=home.content_hash, alternates=[],
                refresh_after=now + timedelta(days=self.s.freshness_for("website", FIELDS["website"].freshness_days)),
                existing=rows.get("website"))

    async def _reload_score(self, cid: int, requested: list[str]) -> dict:
        async with self.db.connection() as c:
            rows = await store.load_fields(c, cid)
        return score_company(rows, requested)

    async def _handle_home_failure(self, cid, ident, requested, plan, run, home, t_start, budget, job_id, row_index):
        permanent = (home.error_kind in ("blocked", "robots", "content_type", "too_large", "bot_challenge",
                                         "redirected_off_domain")
                     or (home.error_kind == "http" and home.status in PERMANENT_HTTP)
                     or (home.error_kind == "network" and "DNS resolution failed" in home.error))
        if home.error_kind == "budget":
            return await self._finish(cid, ident, requested, plan, run, "partial", t_start, budget, job_id,
                                      row_index, error_kind="budget", error=home.error)
        if permanent:
            async with self.db.connection() as c:
                await store.save_field(
                    c, cid, "website", status="found", value=ident.start_url, source_url=ident.start_url,
                    method="input", tier="input", prior=METHOD_PRIOR["input"], verification_status="failed",
                    verification_method="http_reachable_domain_match",
                    checks={"nonempty": True, "reachable": False, "error_kind": home.error_kind},
                    source_hash=None, alternates=[],
                    refresh_after=datetime.now(timezone.utc) + timedelta(days=self.s.not_found_retry_days),
                    existing=None, note=home.error)
            return await self._finish(cid, ident, requested, plan, run, "unreachable", t_start, budget, job_id,
                                      row_index, error_kind=home.error_kind, error=home.error)
        return await self._finish(cid, ident, requested, plan, run, "failed", t_start, budget, job_id, row_index,
                                  error_kind=home.error_kind, error=home.error, transient=True)

    async def _finish(self, cid, ident, requested, plan, run, outcome, t_start, budget, job_id, row_index,
                      score=None, error_kind="", error="", transient=False, record_outcome=True) -> EnrichResult:
        if score is None:
            score = await self._reload_score(cid, requested)
        dur = int((time.monotonic() - t_start) * 1000)
        cost_total = run.cost_total
        if budget is not None:
            budget.add_cost(cost_total)
        metrics = _metrics_json(run, cost_total)
        async with self.db.connection() as c:
            if record_outcome and outcome in ("enriched", "partial", "no_data", "unreachable"):
                await c.execute("UPDATE companies SET last_enriched_at=now(), last_outcome=%s, updated_at=now() WHERE id=%s",
                                (outcome, cid))
            await c.execute(
                """INSERT INTO enrichment_runs (company_id, job_id, row_index, duration_ms, outcome, requested,
                       fields_found, http_requests, http_cache_hits, bytes_downloaded, ai_calls, browser_ops,
                       retries, cost_usd_est, tiers, cost)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (cid, job_id, row_index, dur, outcome if not error_kind or outcome != "failed" else "failed",
                 requested, score["fields_present"], run.http_requests, run.http_cache_hits, run.bytes_downloaded,
                 run.ai_calls, run.browser_ops, run.retries, cost_total,
                 Jsonb(metrics["tiers"]), Jsonb(metrics["cost"])))
        return EnrichResult(domain=ident.key, outcome=outcome, company_id=cid, transient=transient,
                            error_kind=error_kind, error=error, score=score, plan=plan.describe(), metrics=metrics,
                            budget_exhausted=sorted(budget.exhausted_reasons) if budget else [], duration_ms=dur)


def budget_allows(budget: Optional[Budget], kind: str) -> bool:
    return budget is None or budget.allows(kind)


def _on_site(url: str, ident: Identity) -> bool:
    """Same registrable domain, or one host is a subdomain of the other (also covers suffixes the bundled
    public-suffix list does not know)."""
    host = (urlsplit(url).hostname or "").lower()
    return (_registrable_host(url) == ident.key or host == ident.host
            or host.endswith("." + ident.host) or ident.host.endswith("." + host))


def _registrable_host(url: str) -> str:
    from urllib.parse import urlsplit
    from .domain import registrable
    return registrable((urlsplit(url).hostname or "").lower())


def _extract_inline(html: str, url: str, key: str) -> PageExtraction:
    return extract_page(html, url, "home", key)


def _tag(c: Candidate, tier: str) -> Candidate:
    c.tier = tier
    return c


def _metrics_json(run: RunMetrics, cost_total: float) -> dict:
    return {
        "http_requests": run.http_requests, "http_cache_hits": run.http_cache_hits,
        "bytes_downloaded": run.bytes_downloaded, "retries": run.retries, "ai_calls": run.ai_calls,
        "ai_tokens_in": run.ai_tokens_in, "ai_tokens_out": run.ai_tokens_out, "browser_ops": run.browser_ops,
        "browser_seconds": round(run.browser_seconds, 3), "pages_fetched": run.pages_fetched,
        "tiers": {k: {"attempted": v.attempted, "ms": round(v.ms, 1), "requests": v.requests,
                      "fields_found": v.fields_found, "cost_usd": round(v.cost_usd, 8), "note": v.note}
                  for k, v in run.tiers.items()},
        "cost": {"total_usd_estimated": round(cost_total, 8), "http_usd_estimated": round(run.cost_http_usd, 8),
                 "ai_usd_estimated": round(run.cost_ai_usd, 8), "browser_usd_estimated": round(run.cost_browser_usd, 8),
                 "basis": "tokens/seconds/requests are measured; USD uses the configured LE_* price settings, "
                          "so every USD figure is an estimate; infrastructure/compute cost excluded"},
    }
