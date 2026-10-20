"""Per-company bookkeeping around a model-backed extraction backend: page/call caps, job budgets, result cache, accounting."""
from __future__ import annotations

import json
import time
from typing import Optional

from psycopg.types.json import Jsonb

from ..budget import Budget, RunMetrics
from ..config import Settings
from ..extract import Candidate
from .base import BackendUnavailable, FieldOutcome, PageInput, PageOutcome
from .normalize import METHOD, TIER

LOCAL_NOTE = "local inference is not free: hardware, electricity and time are NOT priced here; external_usd_estimated is 0 by definition"


class CompanyLLMSession:
    def __init__(self, backend, settings: Settings, db, run: RunMetrics, budget: Optional[Budget]):
        self.b, self.s, self.db, self.run, self.budget = backend, settings, db, run, budget
        self.pages_sent = 0
        self.calls = 0
        self.errors: list[dict] = []
        self.fatal: Optional[BackendUnavailable] = None
        self.states: dict[str, dict] = {}        # field -> {"state","reason"} for fields that produced no candidate
        self.fields_extracted: set[str] = set()
        self.stat = run.tier(TIER)
        run.llm.update({"backend": backend.name, "provider": backend.provider, "model": backend.model, "scope": backend.scope,
                        "calls": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0.0, "failures": 0, "timeouts": 0, "retries": 0,
                        "pages_sent": 0, "pages_from_cache": 0, "pages_skipped": {}, "errors": [], "rejected": {},
                        "external_usd_estimated": 0.0, "worker_blocked_attempts": [],
                        "scope_note": LOCAL_NOTE if backend.scope == "local" else "external provider: USD = measured tokens x configured prices (estimate)"})

    @property
    def had_errors(self) -> bool:
        return bool(self.errors) or self.fatal is not None

    def _skip(self, reason: str, fields: list[str]) -> list[Candidate]:
        d = self.run.llm["pages_skipped"]
        d[reason] = d.get(reason, 0) + 1
        for f in fields:
            self.states.setdefault(f, {"state": "skipped_budget" if reason.startswith("budget") or reason.endswith("cap") else "missing",
                                       "reason": reason})
        self.stat.note = (self.stat.note + f" skipped:{reason}").strip()
        return []

    async def _cache_get(self, page: PageInput, fields: list[str]):
        key = (f"{page.url}#{page.kind}#sgllm", page.content_hash, f"{self.b.fingerprint}|{','.join(sorted(fields))}")
        async with self.db.connection() as c:
            row = await (await c.execute("SELECT result FROM extraction_cache WHERE url=%s AND content_hash=%s AND extractor_version=%s", key)).fetchone()
        return key, row["result"] if row else None

    async def process_page(self, page: PageInput, wanted: list[str]) -> list[Candidate]:
        if self.fatal or not wanted:
            return []
        if not page.html.strip():
            return self._skip("empty_page", wanted)
        if self.errors:
            return self._skip("after_model_error", wanted)       # the model is failing: do not spend more pages/time on it
        if self.pages_sent >= self.s.scrapegraph_max_pages_per_company:
            return self._skip("page_cap", wanted)
        key, cached = (None, None)
        if page.content_hash:
            key, cached = await self._cache_get(page, wanted)
        if cached is not None:
            self.run.llm["pages_from_cache"] += 1
            outcome = PageOutcome(fields={f: FieldOutcome(**{**v, "alternates": v.get("alternates", [])}) for f, v in cached["fields"].items()},
                                  rejected=cached.get("rejected", {}))
            return self._candidates(page, outcome, wanted)
        if self.calls >= self.s.scrapegraph_max_calls_per_company:
            return self._skip("call_cap", wanted)
        if self.budget is not None and not (self.budget.allows("ai_calls") and self.budget.allows("ai_tokens")):
            return self._skip("budget:" + ",".join(sorted(self.budget.exhausted_reasons & {"ai_calls", "ai_tokens"})) or "budget", wanted)
        self.pages_sent += 1
        self.run.llm["pages_sent"] += 1
        t0 = time.monotonic()
        self.stat.attempted = True
        try:
            outcome = await self.b.extract_page(page, wanted, max_calls=self.s.scrapegraph_max_calls_per_company - self.calls)
        except BackendUnavailable as e:
            self.fatal = e
            self.stat.note = f"unavailable: {e.kind}"
            return []
        finally:
            self.stat.ms += (time.monotonic() - t0) * 1000
        u = outcome.usage
        self.calls += max(u.calls, 1 if u.failures else 0)
        L = self.run.llm
        for k in ("calls", "tokens_in", "tokens_out", "failures", "timeouts", "retries"):
            L[k] += getattr(u, k)
        L["latency_ms"] = round(L["latency_ms"] + u.latency_ms, 1)
        if outcome.worker_blocked:
            L["worker_blocked_attempts"] = sorted(set(L["worker_blocked_attempts"]) | set(outcome.worker_blocked))
        self.run.ai_calls += u.calls
        self.run.ai_tokens_in += u.tokens_in
        self.run.ai_tokens_out += u.tokens_out
        if self.budget is not None:
            self.budget.charge("ai_calls", u.calls)
            self.budget.charge("ai_tokens", u.tokens_in + u.tokens_out)
        if self.b.scope == "external":
            usd = u.tokens_in / 1e6 * self.s.scrapegraph_price_in_per_mtok + u.tokens_out / 1e6 * self.s.scrapegraph_price_out_per_mtok
            L["external_usd_estimated"] = round(L["external_usd_estimated"] + usd, 8)
            self.run.cost_ai_usd += usd
            self.stat.cost_usd = self.run.cost_ai_usd
        if outcome.error_kind:
            err = {"page": page.url, "kind": outcome.error_kind, "message": outcome.error}
            self.errors.append(err)
            L["errors"].append(err)
            self.stat.note = (self.stat.note + f" error:{outcome.error_kind}").strip()
            return []
        L["rejected"].update({f"{page.kind}:{k}": v for k, v in outcome.rejected.items()})
        if key is not None:
            async with self.db.connection() as c:
                await c.execute("INSERT INTO extraction_cache (url, content_hash, extractor_version, result) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (*key, Jsonb({"fields": {f: vars(fo) for f, fo in outcome.fields.items()}, "rejected": outcome.rejected})))
        return self._candidates(page, outcome, wanted)

    def _candidates(self, page: PageInput, outcome: PageOutcome, wanted: list[str]) -> list[Candidate]:
        cands: list[Candidate] = []
        for f in wanted:
            fo = outcome.fields.get(f)
            if fo is None:
                self.states.setdefault(f, {"state": "missing", "reason": "not returned"})
                continue
            if fo.state in ("extracted", "conflict") and fo.value:
                cands.append(Candidate(f, fo.value, METHOD, fo.source_url or page.url, fo.evidence, tier=TIER))
                for alt in fo.alternates:
                    cands.append(Candidate(f, alt["value"], METHOD, alt.get("source_url") or page.url, "", tier=TIER))
                self.fields_extracted.add(f)
                self.states.pop(f, None)
            elif f not in self.fields_extracted:
                prev = self.states.get(f)
                if prev is None or fo.state == "invalid":
                    self.states[f] = {"state": fo.state, "reason": fo.reason}
        self.stat.fields_found = len(self.fields_extracted)
        return cands

    def state_for(self, f: str) -> dict:
        return self.states.get(f, {"state": "missing", "reason": "page(s) did not state the field"})
