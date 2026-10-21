"""Apply a person's field-level review to enriched_leads.csv. Writes a NEW file (never edits or overwrites the original).

Review CSV columns: domain, field, verdict (correct | incorrect | corrected), corrected_value (for 'corrected'), reviewed_by, notes.
  correct   -> <field>_status = manually_reviewed        (value and provenance unchanged)
  incorrect -> <field>_status = rejected_by_review       (value kept for the record, must not be used)
  corrected -> value replaced, status manually_reviewed, backend 'manual', method 'manual_correction', no source URL
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Optional

from ..csv_io import spreadsheet_safe
from .enrich import REQUESTED

VERDICTS = {"correct", "incorrect", "corrected"}


class ReviewError(ValueError):
    pass


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def apply_review(leads_csv: str | Path, review_csv: str | Path, out: Optional[str | Path] = None) -> Path:
    leads_csv, review_csv = Path(leads_csv), Path(review_csv)
    cols, rows = _read(leads_csv)
    rcols, reviews = _read(review_csv)
    need = {"domain", "field", "verdict", "reviewed_by"}
    if not need <= {c.strip().lower() for c in rcols}:
        raise ReviewError(f"review CSV needs columns {sorted(need)}; has {rcols}")
    by_domain = {r["domain"].strip().lower(): r for r in rows}
    problems, touched = [], {}
    for i, rv in enumerate(reviews, 2):
        rv = {k.strip().lower(): (v or "").strip() for k, v in rv.items() if k}
        d, f, verdict, who = rv["domain"].lower(), rv["field"], rv["verdict"].lower(), rv["reviewed_by"]
        if d not in by_domain:
            problems.append(f"row {i}: domain {d!r} is not in the leads file"); continue
        if f not in REQUESTED:
            problems.append(f"row {i}: unknown field {f!r}"); continue
        if verdict not in VERDICTS:
            problems.append(f"row {i}: verdict must be one of {sorted(VERDICTS)}"); continue
        if not who:
            problems.append(f"row {i}: reviewed_by is required"); continue
        if verdict == "corrected" and not rv.get("corrected_value"):
            problems.append(f"row {i}: 'corrected' needs corrected_value"); continue
        if verdict in ("correct", "incorrect") and not by_domain[d].get(f):
            problems.append(f"row {i}: {f} has no extracted value to review for {d}"); continue
        touched.setdefault(d, {})[f] = (verdict, rv.get("corrected_value", ""), who, rv.get("notes", ""))
    if problems:
        raise ReviewError("review rejected, no file written:\n  " + "\n  ".join(problems))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for d, fl in touched.items():
        r = by_domain[d]
        notes = []
        for f, (verdict, newv, who, note) in fl.items():
            if verdict == "correct":
                r[f + "_status"] = "manually_reviewed"
            elif verdict == "incorrect":
                r[f + "_status"] = "rejected_by_review"
            else:
                r[f], r[f + "_status"], r[f + "_backend"], r[f + "_method"] = newv, "manually_reviewed", "manual", "manual_correction"
                r[f + "_source_url"], r[f + "_extracted_at"], r[f + "_verification"] = "", now, "manual"
            r["reviewed_by"], r["reviewed_at"] = who, now
            if note:
                notes.append(f"{f}: {note}")
        have = [f for f in REQUESTED if r.get(f) or r.get(f + "_status") == "rejected_by_review"]
        r["review_status"] = "reviewed" if all(r.get(f + "_status") in ("manually_reviewed", "rejected_by_review") for f in have) else "partially_reviewed"
        r["review_notes"] = "; ".join(x for x in (r.get("review_notes"), *notes) if x)
        ok = lambda f: bool(r.get(f)) and r.get(f + "_status") != "rejected_by_review"
        r["usable"] = "yes" if (r.get("website_status") == "independently_verified" and ok("company_name") and (ok("email") or ok("phone"))) else "no"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = Path(out) if out else leads_csv.with_name(f"{leads_csv.stem}.reviewed-{stamp}.csv")
    with dest.open("x", newline="", encoding="utf-8-sig") as f:                  # "x": never overwrite
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: spreadsheet_safe(v) if isinstance(v, str) else v for k, v in r.items()})
    return dest
