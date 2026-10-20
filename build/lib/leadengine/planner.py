"""Cost-aware enrichment planner: decide what is worth doing before any I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .budget import Budget
from .config import Settings
from .fields import FIELDS, PAGE_KINDS


@dataclass
class Plan:
    requested: list[str]
    preserve: list[str] = field(default_factory=list)       # fresh values kept as-is
    skip_not_found: list[str] = field(default_factory=list)  # recently searched, nothing found: do not retry yet
    missing: list[str] = field(default_factory=list)         # never attempted
    stale: list[str] = field(default_factory=list)           # found but past refresh_after
    retry_not_found: list[str] = field(default_factory=list) # not_found past retry window
    page_priority: list[str] = field(default_factory=list)   # page kinds, best first
    ai_eligible_fields: list[str] = field(default_factory=list)
    max_pages: int = 0
    max_retries: int = 0

    @property
    def to_extract(self) -> list[str]:
        return self.missing + self.stale + self.retry_not_found

    @property
    def needs_network(self) -> bool:
        return bool(self.to_extract)

    def describe(self) -> dict:
        return {"preserve": self.preserve, "skip_not_found": self.skip_not_found, "missing": self.missing,
                "stale": self.stale, "retry_not_found": self.retry_not_found,
                "page_priority": self.page_priority, "ai_eligible_fields": self.ai_eligible_fields,
                "max_pages": self.max_pages, "max_retries": self.max_retries, "needs_network": self.needs_network}


def make_plan(rows: dict[str, dict], requested: list[str], settings: Settings,
              budget: Optional[Budget] = None, now: Optional[datetime] = None,
              ai_available: bool = False, force: bool = False) -> Plan:
    now = now or datetime.now(timezone.utc)
    p = Plan(requested=requested, max_pages=settings.max_pages_per_company, max_retries=settings.http_max_retries)
    for f in requested:
        row = rows.get(f)
        if force:
            (p.stale if row else p.missing).append(f)
        elif row is None:
            p.missing.append(f)
        elif row["status"] == "found":
            (p.stale if row["refresh_after"] <= now else p.preserve).append(f)
        else:  # not_found
            (p.retry_not_found if row["refresh_after"] <= now else p.skip_not_found).append(f)
    # page kinds ordered by how many wanted fields they serve (home first on ties: it is already fetched)
    votes: dict[str, float] = {}
    for f in p.to_extract:
        for rank, kind in enumerate(FIELDS[f].pages):
            votes[kind] = votes.get(kind, 0) + 1.0 / (1 + rank)
    p.page_priority = sorted(votes, key=lambda k: (-votes[k], PAGE_KINDS.index(k)))
    if p.to_extract and "home" not in p.page_priority:
        p.page_priority.insert(0, "home")
    p.ai_eligible_fields = [f for f in p.to_extract if FIELDS[f].ai_eligible] if ai_available else []
    return p
