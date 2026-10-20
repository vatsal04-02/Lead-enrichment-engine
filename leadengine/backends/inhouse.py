"""The in-house deterministic extractor behind the common backend interface (for tests and fair benchmarks only; the runtime
pipeline keeps calling its own code path directly so the default behaviour cannot change)."""
from __future__ import annotations

from ..domain import normalize
from ..extract import extract_page, pick_best
from .base import FieldOutcome, PageInput, PageOutcome


class InHouseBackend:
    name = "inhouse"
    provider = "none"
    model = "deterministic"
    scope = "none"

    def __init__(self, site_key: str):
        self.site_key = site_key

    async def preflight(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def extract_page(self, page: PageInput, fields: list[str], max_calls=None) -> PageOutcome:
        ex = extract_page(page.html, page.url, page.kind, self.site_key)
        picks = pick_best(ex.candidates, self.site_key)
        out = PageOutcome()
        for f in fields:
            if f in picks:
                best, alts = picks[f]
                fo = FieldOutcome(f, "conflict" if alts else "extracted", best.value, best.source_url, best.method, best.evidence)
                fo.alternates = [{"value": a.value, "source_url": a.source_url} for a in alts]
                out.fields[f] = fo
            else:
                out.fields[f] = FieldOutcome(f, "missing", reason="not found by deterministic extraction")
        return out
