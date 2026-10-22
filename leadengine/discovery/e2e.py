"""python -m leadengine.discovery.e2e <prepare|summarize> ...   glue between `discovery discover` and `leadengine.profiles` for an end-to-end run.

  prepare    discovery_candidates.csv (+ rejected_candidates.csv next to it)  ->  a timestamped directory with
               discovery-<ts>.csv       every company the sources reported: Company, Website, City, Industry/Niche, SourceURL, DiscoveryStatus (+ provenance)
               profile-input-<ts>.csv   the selected, valid, de-duplicated companies (at most --target), balanced across cities, for `python -m leadengine.profiles`
  summarize  discovery CSV + enriched CSV (+ the profiles .summary.json)  ->  summary-<ts>.json with counts, distributions, model, time, tokens

Offline: reads and writes local files only, never contacts anything, never overwrites a file (exclusive create). It adds no company: every row comes from
a source's own report, and a value a source did not state stays empty.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from ..csv_io import spreadsheet_safe

DISCOVERY_COLUMNS = ["Company", "Website", "City", "Industry/Niche", "SourceURL", "DiscoveryStatus", "Domain", "DiscoverySource", "SourceRef", "Notes"]
PROFILE_INPUT_COLUMNS = ["Company", "Website", "City", "Industry/Niche", "SourceURL"]
S_SELECTED, S_NOT_SELECTED = "selected", "discovered_not_selected"        # valid, unique companies: inside / beyond the target
_SUFFIX = re.compile(r"\b(?:private|pvt|limited|ltd|llp|inc|incorporated|corporation|corp|co|company|india)\b\.?", re.I)


def identity_key(name: str) -> str:
    """Company identity for de-duplication beyond the domain: lower-cased name without legal suffixes and punctuation ('' if nothing is left)."""
    return re.sub(r"[^a-z0-9]+", " ", _SUFFIX.sub(" ", (name or "").casefold())).strip()


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("x", newline="", encoding="utf-8-sig") as f:             # "x": a previous result is never replaced
        wr = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: spreadsheet_safe(v) if isinstance(v, str) else v for k, v in r.items()})


def _round_robin(groups: dict[str, list[dict]]) -> list[dict]:
    """One row per group in turn (groups in order of first appearance): no single city or industry can fill the target on its own."""
    queues = [list(v) for v in groups.values()]
    out: list[dict] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def balanced_order(rows: list[dict]) -> list[dict]:
    """Round-robin across cities; inside a city, across industries (rows with no stated industry come last)."""
    by_city: dict[str, list[dict]] = {}
    for r in rows:
        by_city.setdefault((r.get("location") or "").strip().lower(), []).append(r)
    ordered = {}
    for city, rs in by_city.items():
        by_ind: dict[str, list[dict]] = {}
        for r in sorted(rs, key=lambda r: not (r.get("industry") or "").strip()):
            by_ind.setdefault((r.get("industry") or "").strip().lower(), []).append(r)
        ordered[city] = _round_robin(by_ind)
    return _round_robin(ordered)


def prepare(candidates_csv: str, out_dir: str, target: int = 100, label: str = "") -> dict:
    src = Path(candidates_csv)
    if not src.is_file():
        raise SystemExit(f"candidates file not found: {src}")
    cands = _read(src)
    rej_path = src.with_name("rejected_candidates.csv")
    rejected = _read(rej_path) if rej_path.is_file() else []
    valid = [c for c in cands if (c.get("validation_status") or "valid") == "valid" and (c.get("synthetic") or "").lower() != "true"]
    seen_domains: set[str] = set()
    seen_names: set[str] = set()
    unique: list[dict] = []
    status_of: dict[int, tuple[str, str]] = {                               # id(row) -> (status, note)
        id(c): ("synthetic_excluded", "marked synthetic: never a real business") for c in cands if (c.get("synthetic") or "").lower() == "true"}
    for c in valid:
        dom, name = (c.get("domain") or "").lower(), identity_key(c.get("name") or "")
        if dom in seen_domains:
            status_of[id(c)] = ("duplicate_domain", f"same registrable domain as an earlier row ({dom})")
        elif name and name in seen_names:
            status_of[id(c)] = ("duplicate_identity", f"same company name as an earlier row ({c.get('name')})")
        else:
            seen_domains.add(dom)
            if name:
                seen_names.add(name)
            unique.append(c)
    selected = balanced_order(unique)[: max(0, target)]
    chosen, unique_ids = {id(c) for c in selected}, {id(c) for c in unique}
    rows: list[dict] = []
    for c in cands:
        st, note = status_of.get(id(c)) or ((S_SELECTED if id(c) in chosen else S_NOT_SELECTED if id(c) in unique_ids else c.get("validation_status") or "valid"),
                                           c.get("validation_notes") or "")
        if st == S_NOT_SELECTED:
            note = f"valid and unique, but beyond the target of {target} (city-balanced selection)"
        rows.append({"Company": c.get("name", ""), "Website": c.get("website", ""), "City": c.get("location", ""), "Industry/Niche": c.get("industry", ""),
                     "SourceURL": c.get("source_url", ""), "DiscoveryStatus": st, "Domain": c.get("domain", ""),
                     "DiscoverySource": c.get("discovery_source", ""), "SourceRef": c.get("source_ref", ""), "Notes": note})
    for r in rejected:
        rows.append({"Company": r.get("name", ""), "Website": r.get("raw_website", ""), "City": "", "Industry/Niche": "", "SourceURL": r.get("source_url", ""),
                     "DiscoveryStatus": r.get("validation_status") or "rejected", "Domain": "", "DiscoverySource": r.get("discovery_source", ""),
                     "SourceRef": r.get("source_ref", ""), "Notes": r.get("reason", "")})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    d = Path(out_dir) / f"{stamp}{('-' + label) if label else ''}"
    d.mkdir(parents=True, exist_ok=False)
    disc, prof = d / f"discovery-{stamp}.csv", d / f"profile-input-{stamp}.csv"
    _write(disc, DISCOVERY_COLUMNS, rows)
    _write(prof, PROFILE_INPUT_COLUMNS, [{"Company": c.get("name", ""), "Website": c.get("website", ""), "City": c.get("location", ""),
                                          "Industry/Niche": c.get("industry", ""), "SourceURL": c.get("source_url", "")} for c in selected])
    return {"directory": str(d), "discovery_csv": str(disc), "profile_input_csv": str(prof), "rows_in_discovery_csv": len(rows),
            "valid_unique_companies": len(unique), "selected_for_enrichment": len(selected), "target": target,
            "reached_target": len(selected) >= target,
            "cities_selected": dict(Counter((c.get("location") or "(not stated)") for c in selected)),
            "status_counts": dict(Counter(r["DiscoveryStatus"] for r in rows))}


# ---------------------------------------------------------------------------------------------------------------- summary
def classify(row: dict) -> str:
    """Outcome of one enriched row. success = ok with medium/high confidence; partial = ok with low confidence, or the pages were read but no
    statement could be tied to them; anti_bot = the site served a challenge (never bypassed); robots = disallowed by robots.txt; failed = any other
    failure; skipped / not_processed are reported as such."""
    status, err = (row.get("status") or "").strip(), (row.get("error") or "").lower()
    if status == "ok":
        return "success" if (row.get("confidence") or "") in ("medium", "high") else "partial"
    if status == "no_evidence":
        return "partial"
    if status == "failed":
        if "bot_challenge" in err or "challenge" in err:
            return "anti_bot"
        if "robots" in err:
            return "robots"
        return "failed"
    return status or "unknown"


def summarize(discovery_csv: str, enriched_csv: str, profile_summary: Optional[str], out_dir: str, label: str = "") -> dict:
    disc, enr = _read(Path(discovery_csv)), _read(Path(enriched_csv))
    ps = json.loads(Path(profile_summary).read_text(encoding="utf-8")) if profile_summary else {}
    outcomes = Counter(classify(r) for r in enr)
    st = Counter(r.get("DiscoveryStatus", "") for r in disc)
    unique = st.get(S_SELECTED, 0) + st.get(S_NOT_SELECTED, 0)
    usage = ps.get("model_usage") or {}
    key = lambda s: (s or "").strip() or "(not stated)"
    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "discovery_csv": str(discovery_csv), "enriched_csv": str(enriched_csv), "profile_summary": str(profile_summary or ""),
        "total_discovered": len(disc), "unique_companies": unique, "selected_for_enrichment": st.get(S_SELECTED, 0),
        "discovery_status_counts": dict(st),
        "enriched_rows": len(enr), "successfully_enriched": outcomes.get("success", 0), "partially_enriched": outcomes.get("partial", 0),
        "failed": outcomes.get("failed", 0), "anti_bot_blocked": outcomes.get("anti_bot", 0), "robots_disallowed": outcomes.get("robots", 0),
        "skipped": outcomes.get("skipped", 0), "not_processed": outcomes.get("not_processed", 0),
        "outcome_definitions": {"successfully_enriched": "status ok and computed confidence medium or high",
                                "partially_enriched": "status ok with low confidence, or pages read but no claim could be tied to them (no_evidence)",
                                "failed": "any other failure (timeout, HTTP error, model error ...)",
                                "anti_bot_blocked": "the site served an anti-bot challenge; recorded and not bypassed",
                                "robots_disallowed": "robots.txt forbids the page; recorded and not fetched"},
        "confidence_counts": dict(Counter((r.get("confidence") or "-") for r in enr if classify(r) in ("success", "partial"))),
        "city_distribution_discovered": dict(Counter(key(r.get("City")) for r in disc if r.get("DiscoveryStatus") in (S_SELECTED, S_NOT_SELECTED)).most_common()),
        "city_distribution_enriched": dict(Counter(key(r.get("City")) for r in enr).most_common()),
        "industry_distribution_stated_by_source": dict(Counter(key(r.get("Industry/Niche")) for r in disc
                                                               if r.get("DiscoveryStatus") in (S_SELECTED, S_NOT_SELECTED)).most_common(30)),
        "industry_distribution_from_websites": dict(Counter(key(r.get("industry_niche")) for r in enr if classify(r) in ("success", "partial")).most_common(30)),
        "model": ps.get("model", ""), "endpoint": ps.get("endpoint", ""), "prompt_version": ps.get("prompt_version", ""),
        "elapsed_seconds_enrichment": ps.get("seconds"), "model_usage": usage,
        "paid_api_cost_usd": 0.0 if ps else None,
        "notes": ["Counts come from the files named above, not from any other source.",
                  "Industry/Niche stated by a source is often empty (Wikidata states an industry for few organisations); the website-derived industry is only "
                  "present where a verifiable quote supported it.",
                  "Presence of a value is not correctness; inferred fields are hypotheses for human review."]}
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    p = d / f"summary-{stamp}{('-' + label) if label else ''}.json"
    with p.open("x", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    summary["summary_file"] = str(p)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m leadengine.discovery.e2e", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="discovery_candidates.csv -> discovery CSV + profile input CSV (new timestamped directory)")
    p.add_argument("candidates"); p.add_argument("--target", type=int, default=100); p.add_argument("--out-root", default="e2e_out"); p.add_argument("--label", default="")
    s = sub.add_parser("summarize", help="discovery CSV + enriched CSV -> summary JSON")
    s.add_argument("--discovery", required=True); s.add_argument("--enriched", required=True); s.add_argument("--profile-summary")
    s.add_argument("--out-dir", required=True); s.add_argument("--label", default="")
    a = ap.parse_args(argv)
    res = (prepare(a.candidates, a.out_root, a.target, a.label) if a.cmd == "prepare"
           else summarize(a.discovery, a.enriched, a.profile_summary, a.out_dir, a.label))
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
