"""Grounding: decide which model statements may be stored. Pure functions, no model and no network.

  supported   the statement is tied to a passage (quote) that verifiably occurs on a fetched page, or the value itself occurs verbatim
  inferred    a hypothesis (pain points, automation opportunities, outreach angle) that cites at least one verifiable passage; model-generated,
              for human review - never a fact about the company
  unsupported dropped: the model produced it but nothing on the pages backs it (counted, never stored)
  missing     the model said nothing / "NA"

Confidence is COMPUTED here from how much was grounded; the model's own opinion is kept separately and never trusted for this.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

MISSING = {"", "na", "n/a", "none", "null", "unknown", "not specified", "not available", "not stated", "not provided", "nil", "-", "--",
           "no information", "not mentioned"}
MIN_QUOTE = 12                 # normalised characters: shorter "quotes" would match almost anything
EXTRACTIVE = ("company_name", "business_description", "products_services", "industry_niche", "target_customers")
INFERRED = ("pain_points", "ai_automation_opportunities", "outreach_angle")
LIMITS = {"company_name": 120, "business_description": 600, "industry_niche": 120, "target_customers": 300, "outreach_angle": 400}
MAX_ITEMS, MAX_ITEM_LEN = 8, 140
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\.\.\.\]")


def norm(s: str) -> str:
    """Comparison form: NFKC, case-folded, quotes/dashes unified, punctuation -> space, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", str(s or "")).casefold()
    s = s.translate({0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x2013: "-", 0x2014: "-"})
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def clean(s, limit: int) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub(" ", str(s or ""))).strip()[:limit]


def is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, tuple)):
        return all(is_missing(x) for x in v)
    return norm(str(v)) in MISSING or str(v).strip().lower() in MISSING


def as_list(v) -> list[str]:
    """Models sometimes return a string where a list was asked for: split on newlines/semicolons/bullets, never invent items."""
    if v is None:
        return []
    if isinstance(v, str):
        parts = re.split(r"[\n;•]+", v)
    elif isinstance(v, (list, tuple)):
        parts = [x if isinstance(x, str) else str(x) for x in v]
    else:
        parts = [str(v)]
    return [p.strip(" -*\t") for p in parts if p and not is_missing(p.strip(" -*\t"))]


def find_quote(quote, pages: dict[str, str]) -> Optional[str]:
    """URL of the first page whose text contains the quote (all ellipsis-separated segments, each >= MIN_QUOTE chars), else None."""
    q = str(quote or "").strip().strip("\"'")
    if not q or is_missing(q) or len(q) > 600:
        return None
    segs = [norm(s) for s in _ELLIPSIS.split(q)]
    segs = [s for s in segs if s]
    if not segs or any(len(s) < MIN_QUOTE for s in segs):
        return None
    for url, text in pages.items():
        if all(s in text for s in segs):
            return url
    return None


@dataclass
class Evidence:
    field: str
    url: str
    quote: str


@dataclass
class Profile:
    values: dict = field(default_factory=dict)          # field -> str | list[str]  (only supported/inferred values)
    support: dict = field(default_factory=dict)         # field -> supported | inferred | unsupported | missing
    evidence: list = field(default_factory=list)        # [Evidence]
    model_confidence: str = ""

    @property
    def supported_extractive(self) -> int:
        return sum(1 for f in EXTRACTIVE if self.support.get(f) == "supported")

    @property
    def inferred_kept(self) -> int:
        return sum(1 for f in INFERRED if self.support.get(f) == "inferred")


def _cap(items: list[str]) -> list[str]:
    return [clean(i, MAX_ITEM_LEN) for i in items[:MAX_ITEMS]]


def ground_profile(raw: dict, pages: dict[str, str]) -> Profile:
    """raw = the model's JSON; pages = {url: normalised text of exactly what the model was shown for that page}."""
    raw = {str(k).lower(): v for k, v in (raw or {}).items()}
    P = Profile()
    P.model_confidence = (norm(str(raw.get("model_confidence", ""))) if norm(str(raw.get("model_confidence", ""))) in ("high", "medium", "low") else "")

    def ev(f: str, url: str, quote: str) -> None:
        P.evidence.append(Evidence(f, url, clean(quote, 300)))

    def verbatim(value: str) -> Optional[str]:
        n = norm(value)
        return next((u for u, t in pages.items() if len(n) >= 3 and n in t), None)

    # --- company name: it must appear on the pages
    v = raw.get("company_name")
    if is_missing(v):
        P.support["company_name"] = "missing"
    elif (u := verbatim(str(v))):
        P.values["company_name"], P.support["company_name"] = clean(v, LIMITS["company_name"]), "supported"
        ev("company_name", u, clean(v, 120))
    else:
        P.support["company_name"] = "unsupported"

    # --- summary-style fields: a verifiable quote, or the value itself verbatim on a page
    for f, qf in (("business_description", "description_quote"), ("industry_niche", "industry_quote"), ("target_customers", "customers_quote")):
        v, q = raw.get(f), raw.get(qf)
        if is_missing(v):
            P.support[f] = "missing"
            continue
        qurl = find_quote(q, pages)
        vurl = verbatim(str(v)) if len(norm(str(v))) >= MIN_QUOTE else None      # a lone common word on the page is not support
        if qurl or vurl:
            P.values[f], P.support[f] = clean(v, LIMITS[f]), "supported"
            ev(f, qurl or vurl, clean(q if qurl else v, 300))
        else:
            P.support[f] = "unsupported"

    # --- products/services: each item must itself occur on a page (item-level grounding)
    items = as_list(raw.get("products_services"))
    if not items:
        P.support["products_services"] = "missing"
    else:
        kept = []
        for it in items[:MAX_ITEMS * 2]:
            u = verbatim(it) if len(norm(it)) >= 3 else None
            if u:
                kept.append(it)
                ev("products_services", u, clean(it, 140))
        if kept:
            P.values["products_services"], P.support["products_services"] = _cap(kept), "supported"
        else:
            P.support["products_services"] = "unsupported"

    # --- hypotheses: need at least one verifiable basis passage; labelled inferred
    for f, bf in (("pain_points", "pain_points_basis"), ("ai_automation_opportunities", "opportunities_basis"), ("outreach_angle", "outreach_basis")):
        v = raw.get(f)
        if is_missing(v):
            P.support[f] = "missing"
            continue
        bases = as_list(raw.get(bf))
        good = [(u, b) for b in bases if (u := find_quote(b, pages))]
        if not good:
            P.support[f] = "unsupported"
            continue
        P.values[f] = clean(v, LIMITS["outreach_angle"]) if f == "outreach_angle" else _cap(as_list(v))
        if not P.values[f]:
            P.support[f] = "missing"
            P.values.pop(f, None)
            continue
        P.support[f] = "inferred"
        for u, b in good[:3]:
            ev(f, u, b)
    return P


def confidence(p: Profile, pages_ok: int) -> str:
    """high: >=4 of the 5 extractive fields grounded on >=2 pages; medium: >=2 grounded; low: 1; none: nothing grounded. Computed from evidence,
    not from what the model says about itself."""
    n = p.supported_extractive
    if n == 0:
        return "none"
    if n >= 4 and pages_ok >= 2:
        return "high"
    return "medium" if n >= 2 else "low"
