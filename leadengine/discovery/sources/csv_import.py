"""Manual candidate import. This is the always-available discovery path: a person finds candidates by any compliant means
(directories they are allowed to use, LinkedIn they browsed themselves, referrals) and lists them in a CSV.

Reuses leadengine.csv_io.CSVReader (encoding/delimiter detection, header aliases, row numbering, bad-row reporting).
Recognised optional columns (case-insensitive): source, source_url, evidence|notes, niche, reviewed_by. A CSV that is itself a
previous `discovery_candidates.csv` keeps its original discovery_source / source_url / discovered_at.
"""
from __future__ import annotations

from pathlib import Path

from ...csv_io import CSVError, CSVReader
from ..models import SRC_CSV, RawHit, now_iso
from . import SourceContext

OPTIONAL = {"source": ("source", "discovery source", "discovery_source", "found via"),
            "source_url": ("source url", "source_url", "found at", "reference url"),
            "evidence": ("evidence", "notes", "note", "comment", "comments", "source evidence", "source_evidence"),
            "niche": ("niche", "niche query", "niche_query"),
            "reviewed_by": ("reviewed by", "reviewed_by"),
            "discovered_at": ("discovered at", "discovered_at"),
            "review_status": ("review status", "review_status")}


def _col(headers: list[str], names: tuple) -> str | None:
    low = {h.strip().lower().replace("_", " "): h for h in headers}
    for n in names:
        if n.replace("_", " ") in low:
            return low[n.replace("_", " ")]
    return None


class CsvImportSource:
    name = SRC_CSV
    needs_network = False

    def __init__(self, path: str, mapping: dict | None = None):
        self.path, self.mapping = path, mapping

    def planned_requests(self, ctx: SourceContext) -> list[str]:
        return []                                   # reads a local file only

    async def discover(self, ctx: SourceContext) -> list[RawHit]:
        try:
            rd = CSVReader(self.path, self.mapping)
        except (CSVError, OSError) as e:
            ctx.error(self.name, self.path, "csv_unreadable", e)
            return []
        cols = {k: _col(rd.headers, v) for k, v in OPTIONAL.items()}
        fname = Path(self.path).name
        hits: list[RawHit] = []
        for row in rd.rows():
            if row.error and not row.website and not row.domain:
                ctx.error(self.name, f"{fname}#row {row.row_index}", "bad_row", row.error)
                continue
            o = row.original
            g = lambda k: ((o.get(cols[k]) or "").strip() if cols.get(k) else "")
            carried = {k: g(k) for k in ("review_status", "reviewed_by") if g(k)}
            src = g("source") or SRC_CSV
            hits.append(RawHit(
                website=row.website or row.domain or "", name=row.name, location=row.location, industry=row.industry,
                source=src if src else SRC_CSV, source_url=g("source_url"),
                source_ref=f"{fname}#row {row.row_index}", evidence=g("evidence"), niche_query=g("niche"),
                location_query="", discovered_at=g("discovered_at") or now_iso(), carried=carried))
            if len(hits) >= max(ctx.limit * 5, 1000):
                ctx.error(self.name, fname, "truncated", f"stopped reading after {len(hits)} rows (limit safeguard)")
                break
        return hits
