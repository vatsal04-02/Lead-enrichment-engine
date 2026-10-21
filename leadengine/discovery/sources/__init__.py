"""Discovery sources. Each source returns RawHit rows with real provenance and never invents a business.

Network-using sources (seed pages, OpenStreetMap) fetch ONLY through the engine's guarded Fetcher: SSRF checks, robots.txt,
challenge/401/403 = stop (never bypass), size caps, per-host rate limits and an audit record of every request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import RawHit, SourceError


@dataclass
class SourceContext:
    fetcher: object                       # leadengine.fetcher.Fetcher (guarded) - None for sources that need no network
    niches: list[str]
    locations: list[str]
    keywords: list[str]
    limit: int                            # soft cap on hits requested from one source
    errors: list[SourceError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # human-readable facts for the report (e.g. licence attribution)

    def error(self, source: str, where: str, kind: str, message: str) -> None:
        self.errors.append(SourceError(source, where, kind, str(message)[:400]))


class Source(Protocol):
    name: str
    needs_network: bool

    def planned_requests(self, ctx: SourceContext) -> list[str]: ...
    async def discover(self, ctx: SourceContext) -> list[RawHit]: ...
