"""Timestamped output directory and the CSV / Markdown / log writers. Never overwrites: the directory must not exist yet and every
file is created exclusively. Text cells are made spreadsheet-safe (formula injection) with the existing csv_io helper."""
from __future__ import annotations

import csv
import datetime as dt
import json
import secrets
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from ..csv_io import spreadsheet_safe
from ..metrics import llm_usage
from .enrich import REQUESTED, Lead, backend_of, field_backends
from .models import Candidate, Rejected, SourceError, now_iso
from .niche import india_evidence, niche_match

CANDIDATE_COLUMNS = ["website", "name", "location", "industry", "domain", "niche_query", "location_query", "discovery_source",
                     "source_url", "source_ref", "source_evidence", "niche_signal", "discovered_at", "validation_status",
                     "validation_notes", "duplicate_sources", "review_status", "reviewed_by", "synthetic"]
REJECT_COLUMNS = ["raw_website", "name", "validation_status", "reason", "discovery_source", "source_url", "source_ref"]
FIELD_SUFFIXES = ("", "_source_url", "_backend", "_method", "_status", "_verification", "_extracted_at")
LEAD_COLUMNS = (["domain", "website", "website_status", "website_verification_method"]
                + [f + s for f in REQUESTED for s in FIELD_SUFFIXES]
                + ["enrichment_status", "usable", "backend_used", "fallback_used", "fallback_reason", "attempts", "error_kind", "error",
                   "manual_review_reason", "niche_query", "location_query", "niche_match", "niche_evidence", "india_signal", "india_evidence",
                   "discovery_source", "source_url", "source_ref", "duplicate_sources", "discovered_at", "enriched_at", "validation_status",
                   "validation_notes", "review_status", "reviewed_by", "reviewed_at", "review_notes", "completeness", "quality", "synthetic"])


def make_output_dir(root: str | Path, label: str = "") -> Path:
    """<root>/<UTC timestamp>-<random>/ ; fails if it already exists (previous outputs are never reused or overwritten)."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    d = Path(root) / f"{stamp}-{secrets.token_hex(2)}{('-' + label) if label else ''}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def _w(path: Path, columns: list[str], rows: Iterable[dict]) -> int:
    n = 0
    with path.open("x", newline="", encoding="utf-8-sig") as f:         # utf-8-sig: opens correctly in Excel; "x": never overwrite
        wr = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: spreadsheet_safe(str(v)) if isinstance(v, str) else ("" if v is None else v) for k, v in r.items()})
            n += 1
    return n


def candidate_row(c: Candidate) -> dict:
    return {"website": c.website, "name": c.name or "", "location": c.location or "", "industry": c.industry or "", "domain": c.domain,
            "niche_query": c.niche_query, "location_query": c.location_query, "discovery_source": c.discovery_source,
            "source_url": c.source_url, "source_ref": c.source_ref, "source_evidence": c.evidence, "niche_signal": c.niche_signal,
            "discovered_at": c.discovered_at, "validation_status": c.validation_status, "validation_notes": c.validation_notes,
            "duplicate_sources": "; ".join(f"{s} {u}".strip() for s, u in c.duplicate_sources), "review_status": c.review_status,
            "reviewed_by": c.reviewed_by, "synthetic": "true" if c.synthetic else "false"}


def write_candidates(d: Path, cands: list[Candidate]) -> Path:
    p = d / "discovery_candidates.csv"
    _w(p, CANDIDATE_COLUMNS, (candidate_row(c) for c in cands))
    return p


def write_rejected(d: Path, rejected: list[Rejected]) -> Optional[Path]:
    if not rejected:
        return None
    p = d / "rejected_candidates.csv"
    _w(p, REJECT_COLUMNS, ({"raw_website": r.raw, "name": r.name or "", "validation_status": r.status, "reason": r.reason,
                             "discovery_source": r.source, "source_url": r.source_url, "source_ref": r.source_ref} for r in rejected))
    return p


def field_status(v: dict) -> str:
    """not_found | invalid | skipped_budget | conflict | extracted | independently_verified. (manually_reviewed is added only by `review`.)"""
    if not v.get("value"):
        return {"invalid": "invalid", "skipped_budget": "skipped_budget"}.get(v.get("extraction_state"), "not_found")
    if v.get("extraction_state") == "conflict":
        return "conflict"
    if v.get("verification_status") == "verified" and v.get("verification_method") == "http_reachable_domain_match":
        return "independently_verified"
    return "extracted"


def lead_row(lead: Lead, keywords: list[str]) -> dict:
    c, view = lead.candidate, lead.view or {}
    fields = view.get("fields") or {}
    row = {"domain": c.domain, "website": c.website}
    web = view.get("website") or {}
    row["website_status"] = ("independently_verified" if web.get("verification_status") == "verified"
                             else "unreachable" if (web.get("checks") or {}).get("error_kind") else "unverified" if web else "not_checked")
    row["website_verification_method"] = web.get("verification_method") or ""
    for f in REQUESTED:
        v = fields.get(f) or {}
        st = field_status(v)
        row[f] = v.get("value") or ""
        row[f + "_status"] = st
        found = st in ("extracted", "conflict", "independently_verified")
        row[f + "_source_url"] = (v.get("source_url") or "") if found else ""
        row[f + "_backend"] = backend_of(v.get("method")) if found else ""
        row[f + "_method"] = (v.get("method") or "") if found else ""
        row[f + "_verification"] = (v.get("verification_method") or v.get("verification_status") or "") if found else ""
        row[f + "_extracted_at"] = (v.get("extracted_at") or "") if found else ""
    desc, name, ind = row["description"], row["company_name"] or (c.name or ""), row["industry"]
    nm, ne = niche_match([name if row["company_name"] else "", desc, ind], keywords)
    ig, ie = india_evidence(c.domain, row["phone"], row["address"], c.location)
    usable = row["website_status"] == "independently_verified" and bool(row["company_name"]) and bool(row["email"] or row["phone"])
    used = field_backends(lead) - {"input", ""}
    row.update({
        "enrichment_status": lead.status, "usable": "yes" if usable else "no",
        "backend_used": "mixed" if len(used) > 1 else (next(iter(used)) if used else "none"),
        "fallback_used": "yes" if lead.fallback_used else "no", "fallback_reason": lead.fallback_reason,
        "attempts": " -> ".join(f"{a.backend}:{a.outcome}" + (f"[{a.error_kind}]" if a.error_kind else "") for a in lead.attempts),
        "error_kind": lead.error_kind, "error": lead.error, "manual_review_reason": lead.manual_review_reason,
        "niche_query": c.niche_query, "location_query": c.location_query, "niche_match": nm, "niche_evidence": ne,
        "india_signal": ig, "india_evidence": ie, "discovery_source": c.discovery_source, "source_url": c.source_url,
        "source_ref": c.source_ref, "duplicate_sources": "; ".join(f"{s} {u}".strip() for s, u in c.duplicate_sources),
        "discovered_at": c.discovered_at, "enriched_at": lead.enriched_at, "validation_status": c.validation_status,
        "validation_notes": c.validation_notes,
        "review_status": ("needs_manual_review" if lead.manual_review_reason else c.review_status if c.review_status != "unreviewed" else "pending_review"),
        "reviewed_by": c.reviewed_by, "reviewed_at": "", "review_notes": "",
        "completeness": view.get("completeness", ""), "quality": view.get("quality", ""), "synthetic": "true" if c.synthetic else "false"})
    return row


def write_leads(d: Path, leads: list[Lead], keywords: list[str]) -> tuple[Path, list[dict]]:
    p = d / "enriched_leads.csv"
    rows = [lead_row(l, keywords) for l in leads]
    _w(p, LEAD_COLUMNS, rows)
    return p, rows


def write_errors(d: Path, source_errors: list[SourceError], leads: list[Lead], extra: list[dict] = ()) -> Optional[Path]:
    recs = [{"at": e.at, "stage": "discovery", "source": e.source, "where": e.where, "kind": e.kind, "message": e.message} for e in source_errors]
    for l in leads:
        if l.manual_review_reason or l.error_kind:
            recs.append({"at": l.enriched_at or now_iso(), "stage": "enrichment", "domain": l.candidate.domain, "status": l.status,
                         "kind": l.error_kind, "message": l.error or l.manual_review_reason,
                         "attempts": [vars(a) for a in l.attempts]})
    recs += list(extra)
    if not recs:
        return None
    p = d / "errors.jsonl"
    with p.open("x", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, default=str) + "\n")
    return p


def write_json(d: Path, name: str, obj: dict) -> Path:
    p = d / name
    with p.open("x", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return p


# ------------------------------------------------------------------ report
def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "n/a"


def write_report(d: Path, *, mode: str, config: dict, discovery, leads: list[Lead], rows: list[dict], primary_note: str,
                 files: dict, timings: dict, next_commands: list[str]) -> Path:
    L = ["# FlowHQ discovery & enrichment report", "", f"Run: `{d.name}` · started {config.get('started_at', '')} · mode: **{mode}**", ""]
    if mode.startswith("SYNTHETIC"):
        L += ["> **SYNTHETIC FIXTURES - these are not real businesses and must not be counted toward the 100-lead target.**", ""]
    elif mode.startswith("DRY"):
        L += ["> **Dry run: no website was contacted.**", ""]
    L += ["## Configuration", "", "```", json.dumps(config, indent=1, default=str), "```", ""]

    L += ["## Discovery funnel", ""]
    if discovery is not None:
        rej = Counter(r.status for r in discovery.rejected)
        L += ["| step | count |", "|---|---|"]
        for s, n in discovery.raw_hits.items():
            L.append(f"| raw hits from `{s}` | {n} |")
        L += [f"| rejected: invalid_url | {rej.get('invalid_url', 0)} |", f"| rejected: reserved_domain | {rej.get('reserved_domain', 0)} |",
              f"| rejected: blocked_domain (directory/social/shared hosting) | {rej.get('blocked_domain', 0)} |",
              f"| removed as duplicates (merged, provenance kept) | {rej.get('duplicate', 0)} |",
              f"| valid unique candidates | {discovery.capped_from} |", f"| kept after `--limit` | {len(discovery.candidates)} |", ""]
        if discovery.capped_from > len(discovery.candidates):
            L += [f"{discovery.capped_from - len(discovery.candidates)} valid candidate(s) were left out only because of `--limit`.", ""]
        L += [f"Keywords used for niche hints: `{', '.join(discovery.keywords)}`", ""]
        for n in discovery.notes:
            L.append(f"* {n}")
        if discovery.notes:
            L.append("")
        if discovery.errors:
            L += ["Source errors (see `errors.jsonl`):", ""] + [f"* `{e.source}` {e.where}: **{e.kind}** - {e.message}" for e in discovery.errors[:15]] + [""]

    n = len(leads)
    L += ["## Enrichment", ""]
    if not n:
        L += ["No enrichment was run.", ""]
    else:
        st = Counter(l.status for l in leads)
        usable = sum(1 for r in rows if r["usable"] == "yes")
        used = Counter(r["backend_used"] for r in rows)
        L += [f"Primary backend: {primary_note or 'in-house only (ScrapeGraphAI not requested)'}", "",
              f"Attempted: **{n}** · usable (website reachable + company name + email or phone, **presence only, correctness unchecked**): "
              f"**{usable}** · needs manual review: **{sum(1 for r in rows if r['review_status'] == 'needs_manual_review')}**", "",
              "| outcome | count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(st.items())]
        L += ["", "| backend that produced the stored values | companies |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(used.items())]
        L += ["", f"Fallback to the in-house engine used for {sum(1 for l in leads if l.fallback_used)} compan(ies).", "",
              "### Field presence and status (among attempted companies)", "",
              "| field | present | extracted | independently_verified | conflict | invalid | not_found |", "|---|---|---|---|---|---|---|"]
        for f in REQUESTED:
            c = Counter(r[f + "_status"] for r in rows)
            L.append(f"| {f} | {_pct(sum(c[k] for k in ('extracted', 'independently_verified', 'conflict')), n)} | {c['extracted']} | "
                     f"{c['independently_verified']} | {c['conflict']} | {c['invalid']} | {c['not_found']} |")
        nm, ig = Counter(r["niche_match"] for r in rows), Counter(r["india_signal"] for r in rows)
        L += ["", "### Relevance hints (keyword/string matching on the company's own text - not a classification)", "",
              "* niche match: " + ", ".join(f"{k} {v}" for k, v in sorted(nm.items())),
              "* India evidence (.in domain, +91 phone, or address naming an Indian state/city): "
              + ", ".join(f"{k} {v}" for k, v in sorted(ig.items())) + " (`unconfirmed` means no signal was found, not that it is outside India)", ""]
        usage = llm_usage([m.get("cost", {}).get("llm") for l in leads for m in l.metrics if m.get("cost", {}).get("llm")])
        reqs = sum(m.get("http_requests", 0) for l in leads for m in l.metrics)
        L += ["### Resources (measured)", "", f"* HTTP requests (incl. robots.txt, retries): {reqs} (~{reqs / n:.1f}/company)"]
        if usage:
            L += [f"* model `{usage['model']}` via {usage['provider']} ({usage['scope']}): {usage['calls']} calls, {usage['tokens_in']} tokens in / "
                  f"{usage['tokens_out']} out, {usage['failures']} failures, {usage['timeouts']} timeouts, "
                  f"{usage['rejected_total']} unsupported values dropped before storage",
                  "* paid API cost: 0 (local model; its hardware/electricity cost is not priced)"]
        else:
            L += ["* no model calls were made"]
        L += [f"* wall time: {timings.get('enrich_seconds', 0):.0f}s for enrichment, {timings.get('discover_seconds', 0):.0f}s for discovery", ""]

    L += ["## What these results do and do not show", "",
          "* Values come from each company's own public pages; every field row carries its source URL, extraction method and backend.",
          "* `extracted` = read from the page (a model answer must also appear on the page to be stored). `independently_verified` is used only for the "
          "website check (reachable and the final host belongs to the domain). E-mail is never mailbox-verified. `manually_reviewed` appears only "
          "after `review` is applied to a sheet a person checked.",
          "* Presence is not correctness. Accuracy is unknown until a person reviews a sample.",
          "* OpenStreetMap/seed-page sources find only what they list; recall of the target niche is unknown.", ""]
    L += ["## Files", ""] + [f"* `{k}` - {v}" for k, v in files.items()] + ["", "## Next commands", "", "```"] + next_commands + ["```", ""]
    p = d / "discovery_report.md"
    with p.open("x", encoding="utf-8") as f:
        f.write("\n".join(L))
    return p
