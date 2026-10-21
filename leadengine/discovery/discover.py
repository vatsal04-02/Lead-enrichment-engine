"""Run the chosen sources, then validate, deduplicate and cap. Returns candidates + everything that was rejected + errors."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate, Rejected, SourceError
from .niche import expand_keywords
from .sources import Source, SourceContext
from .urlcheck import build_candidates


@dataclass
class DiscoveryResult:
    candidates: list[Candidate]
    rejected: list[Rejected]
    errors: list[SourceError]
    raw_hits: dict = field(default_factory=dict)      # source -> number of raw hits
    notes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    capped_from: int = 0


def plan(sources: list[Source], niches: list[str], locations: list[str], limit: int, keywords: list[str]) -> list[str]:
    ctx = SourceContext(fetcher=None, niches=niches, locations=locations, keywords=keywords, limit=limit)
    return [r for s in sources for r in s.planned_requests(ctx)]


async def discover(sources: list[Source], fetcher, *, niches: list[str], locations: list[str], limit: int,
                   extra_keywords: list[str] = (), allow_synthetic: bool = False, only_niche_signal: bool = False) -> DiscoveryResult:
    keywords = expand_keywords(niches, extra_keywords)
    ctx = SourceContext(fetcher=fetcher, niches=niches, locations=locations, keywords=keywords, limit=limit)
    hits, per_source = [], {}
    for src in sources:
        try:
            got = await src.discover(ctx)
        except Exception as e:                       # a failing source must not abort the others, and must not be silent
            ctx.error(src.name, "-", "source_failed", f"{type(e).__name__}: {e}")
            got = []
        per_source[src.name] = per_source.get(src.name, 0) + len(got)
        hits += got
    cands, rejected = build_candidates(hits, keywords=keywords, allow_synthetic=allow_synthetic)
    # sources' own signal first (stable), then the rest; the cap never drops a candidate silently - it is reported
    dropped = 0
    if only_niche_signal:       # broad sources (Wikidata/OSM) list every organisation in a city: keep only those whose OWN source text mentions a niche keyword
        before = len(cands)
        cands = [c for c in cands if c.niche_signal]
        dropped = before - len(cands)
        ctx.notes.append(f"--only-niche-signal: {dropped} of {before} valid candidates were dropped because the source's text for them "
                         "mentions none of the niche keywords (a weak, name/industry-level hint; they are not necessarily irrelevant).")
    for c in cands:             # the run's own query, so every row states what was being looked for (not a claim about the company)
        c.niche_query = c.niche_query or "; ".join(niches)
        c.location_query = c.location_query or "; ".join(locations)
    cands.sort(key=lambda c: 0 if c.niche_signal else 1)
    capped_from = len(cands)
    cands = cands[:limit]
    return DiscoveryResult(cands, rejected, ctx.errors, per_source, ctx.notes, keywords, capped_from)
