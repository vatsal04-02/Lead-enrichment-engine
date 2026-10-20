"""Deterministic field-status and quality scoring. No LLM self-confidence is used anywhere.

Per field:
  present, source_attributed, format_valid, verified, stale   (booleans; defined in validate.py)
  quality = 0.4*present + 0.2*source_attributed + 0.2*format_valid + 0.2*verified, halved if stale
Overall (over the requested fields, weighted by FieldSpec.weight):
  completeness = weighted share of fields that are present        (answers "do we have a value?")
  quality      = weighted mean of per-field quality               (answers "how much do we trust it?")
  verified_share = weighted share of fields that are verified
Completeness deliberately says nothing about correctness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .fields import FIELDS
from .validate import format_valid


def field_status(row: Optional[dict], now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not row or row["status"] != "found" or not row["value"]:
        reason = None if not row else "not_found"
        return {"present": False, "source_attributed": False, "format_valid": False, "verified": False,
                "stale": bool(row) and row["refresh_after"] <= now and row["status"] == "found",
                "quality": 0.0, "state": "not_found" if reason else "missing"}
    present = True
    attributed = bool(row["source_url"] and row["method"])
    fv = format_valid(row["checks"] or {})
    verified = row["verification_status"] == "verified"
    stale = row["refresh_after"] <= now
    q = 0.4 * present + 0.2 * attributed + 0.2 * fv + 0.2 * verified
    if stale:
        q *= 0.5
    return {"present": present, "source_attributed": attributed, "format_valid": fv, "verified": verified,
            "stale": stale, "quality": round(q, 3), "state": "stale" if stale else "found"}


def score_company(rows: dict[str, dict], requested: list[str], now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    total = sum(FIELDS[f].weight for f in requested) or 1.0
    comp = qual = ver = 0.0
    fields = {}
    for f in requested:
        st = field_status(rows.get(f), now)
        w = FIELDS[f].weight
        comp += w * st["present"]
        qual += w * st["quality"]
        ver += w * st["verified"]
        fields[f] = st
    return {"completeness": round(comp / total, 3), "quality": round(qual / total, 3),
            "verified_share": round(ver / total, 3),
            "fields_present": sum(1 for s in fields.values() if s["present"]),
            "fields_requested": len(requested), "fields": fields}
