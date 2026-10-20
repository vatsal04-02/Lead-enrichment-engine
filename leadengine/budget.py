"""Per-job budgets and per-company run metrics.

Budgets are enforced in-process by the single worker that holds the job lease, so
accounting is exact for that worker. Spend is flushed to jobs.stats on every item.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

KINDS = ("pages", "ai_calls", "browser_ops")


@dataclass
class Budget:
    pages: int = 0              # 0 = unlimited for every limit
    ai_calls: int = 0
    browser_ops: int = 0
    seconds: int = 0
    est_cost_usd: float = 0.0
    spent_pages: int = 0
    spent_ai_calls: int = 0
    spent_browser_ops: int = 0
    spent_cost_usd: float = 0.0
    spent_seconds_prior: float = 0.0      # time used by earlier worker sessions of this job
    _t0: float = field(default_factory=time.monotonic)
    exhausted_reasons: set = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: dict, spent: Optional[dict] = None) -> "Budget":
        b = cls(pages=int(cfg.get("pages", 0)), ai_calls=int(cfg.get("ai_calls", 0)),
                browser_ops=int(cfg.get("browser_ops", 0)), seconds=int(cfg.get("seconds", 0)),
                est_cost_usd=float(cfg.get("est_cost_usd", 0.0)))
        if spent:
            b.spent_pages = int(spent.get("pages", 0))
            b.spent_ai_calls = int(spent.get("ai_calls", 0))
            b.spent_browser_ops = int(spent.get("browser_ops", 0))
            b.spent_cost_usd = float(spent.get("est_cost_usd", 0.0))
            b.spent_seconds_prior = float(spent.get("seconds", 0.0))
        return b

    @property
    def elapsed(self) -> float:
        return self.spent_seconds_prior + (time.monotonic() - self._t0)

    def _limit_hit(self, kind: str, n: int = 1) -> Optional[str]:
        if kind == "pages" and self.pages and self.spent_pages + n > self.pages:
            return "pages"
        if kind == "ai_calls" and self.ai_calls and self.spent_ai_calls + n > self.ai_calls:
            return "ai_calls"
        if kind == "browser_ops" and self.browser_ops and self.spent_browser_ops + n > self.browser_ops:
            return "browser_ops"
        if self.seconds and self.elapsed >= self.seconds:
            return "seconds"
        if self.est_cost_usd and self.spent_cost_usd >= self.est_cost_usd:
            return "est_cost_usd"
        return None

    def allows(self, kind: str, n: int = 1) -> bool:
        hit = self._limit_hit(kind, n)
        if hit:
            self.exhausted_reasons.add(hit)
            return False
        return True

    def charge(self, kind: str, n: int = 1) -> None:
        if kind == "pages":
            self.spent_pages += n
        elif kind == "ai_calls":
            self.spent_ai_calls += n
        elif kind == "browser_ops":
            self.spent_browser_ops += n

    def add_cost(self, usd: float) -> None:
        self.spent_cost_usd += usd

    @property
    def any_exhausted(self) -> bool:
        return bool(self.exhausted_reasons)

    def snapshot(self) -> dict:
        return {"pages": self.spent_pages, "ai_calls": self.spent_ai_calls,
                "browser_ops": self.spent_browser_ops, "est_cost_usd": round(self.spent_cost_usd, 6),
                "seconds": round(self.elapsed, 1), "exhausted": sorted(self.exhausted_reasons)}


@dataclass
class TierStat:
    attempted: bool = False
    ms: float = 0.0
    requests: int = 0
    fields_found: int = 0
    cost_usd: float = 0.0
    note: str = ""


@dataclass
class RunMetrics:
    """Measurements for one company run; becomes one enrichment_runs row."""
    http_requests: int = 0          # network requests actually sent (incl. retries, robots)
    http_cache_hits: int = 0
    parse_cache_hits: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    ai_calls: int = 0
    ai_tokens_in: int = 0
    ai_tokens_out: int = 0
    browser_ops: int = 0
    browser_seconds: float = 0.0
    pages_fetched: int = 0
    cost_http_usd: float = 0.0
    cost_ai_usd: float = 0.0
    cost_browser_usd: float = 0.0
    tiers: dict = field(default_factory=dict)
    pages_by_host: dict = field(default_factory=dict)

    def tier(self, name: str) -> TierStat:
        return self.tiers.setdefault(name, TierStat())

    @property
    def cost_total(self) -> float:
        return self.cost_http_usd + self.cost_ai_usd + self.cost_browser_usd
