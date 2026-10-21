"""Grounding: decide which model statements may be stored. Pure functions, no model and no network.

  supported   the statement is tied to a passage (quote) that (1) verifiably occurs on a fetched page, (2) is specific - not a tagline, menu
              or boilerplate - and (3) actually contains the terms of THAT claim. A quote that exists but says something else supports nothing.
  inferred    a HYPOTHESIS (pain points, automation opportunities, outreach angle): model-generated, for human review, never a fact about the
              company. Kept only with a relevant, specific basis passage; pain points additionally need a statement about the company's OWN
              operations - a general industry problem, marketing copy addressed to visitors ("you ..."), or a description of what the product does for
              customers is not evidence that THIS company has that problem.
  unsupported dropped: the model produced it but nothing relevant on the pages backs it (counted, reason recorded, never stored)
  missing     the model said nothing / "NA"

Confidence is COMPUTED here from evidence coverage, distinctness and relevance; the model's own opinion never raises it.
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
HYPOTHESIS_PREFIX = "[hypothesis] "          # every inferred cell is prefixed with this in the CSV so it cannot be mistaken for a fact
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


# ------------------------------------------------------------------ relevance: does this quote say THIS claim?
STOP = frozenset("a an and are as at be but by for from has have in into is it its of on or our that the their them they this to was we were with you your yours "
                 "will can not no all any more most other than so such us who which what when where how also about over under out up one two new get make made "
                 "using use used good best leading world worlds top".split())
MIN_OVERLAP = {"business_description": 0.5, "industry_niche": 0.6, "target_customers": 0.6, "pain_points": 0.4,
               "ai_automation_opportunities": 0.35, "outreach_angle": 0.35}
BOILERPLATE = re.compile(r"cookie|privacy policy|all rights reserved|terms of (?:use|service)|sign ?up|sign ?in|log ?in\b|subscribe|learn more|read more|get started|"
                         r"request a demo|book a demo|contact (?:us|sales)|copyright|©|skip to (?:main )?content|toggle (?:menu|navigation)", re.I)
# Menu / navigation words and other labels that are not products or services by themselves.
NAV_WORDS = frozenset("product products pricing price prices docs documentation doc blog support resources resource enterprise enterprises solutions solution services "
                      "service platform platforms features feature login signin signup contact about aboutus careers career company partners partner explore download "
                      "downloads community learn home overview customers customer pricing plans plan demo faq help news events press team teams industries".split())
GENERIC_NAMES_SET = frozenset({"home", "homepage", "welcome", "about us", "about", "contact", "contact us", "untitled", "index", "official site"})
# "Teams struggle with ..." / "Organizations face ...": a description of a market problem, not a fact about this company.
MARKET_RE = re.compile(r"\b(?:teams?|organi[sz]ations?|compan(?:y|ies)|businesses|enterprises|developers|people|everyone|many|most|often|typically|usually|commonly|"
                       r"industry|industries|market)\b.{0,50}\b(?:struggl\w*|face[sd]?|challenges?|problems?|difficult\w*|waste[sd]?|wasting|lose|losing|fail\w*|frustrat\w*|"
                       r"inefficien\w*|silos?|fragment\w*|bottlenecks?|pain)\b|\bpain points?\b|\bstruggl\w+\b", re.I)
VISITOR_RE = re.compile(r"\b(?:you|your|yours|you're|you'll|you've)\b", re.I)         # copy addressed to the visitor describes the visitor, not the company
# Product-marketing verbs: what the product does for customers.
OFFER_RE = re.compile(r"\b(?:build|builds|building|automate|automates|offer|offers|provide|provides|deliver|delivers|help|helps|enable|enables|empower|empowers|let|lets|"
                      r"allow|allows|create|creates|design|designs|develop|develops|solution|solutions|platform|product|products|power|powers|accelerate|accelerates|"
                      r"streamline|streamlines|simplif\w+|transform\w*|unlock\w*)\b", re.I)
# Statements about the company's OWN operations or situation (the only acceptable basis for a pain-point hypothesis).
OPS_RE = re.compile(r"\b(?:we|our)\b[^.]{0,60}\b(?:manage[sd]?|handle[sd]?|process(?:es|ed)?|run|runs|operate[sd]?|receive[sd]?|maintain(?:s|ed)?|track(?:s|ed)?|"
                    r"manually|spreadsheets?|hiring|hire[sd]?|staff(?:ed)?|employ(?:s|ed)?)\b|\bteam of \d+|\b\d[\d,]*\+?\s+(?:employees|people|engineers|agents|staff|members|offices|locations)\b",
                    re.I)
DEFICIT_RE = re.compile(r"\b(?:struggl\w*|lack(?:s|ing)?|suffer\w*|losing|waste[sd]?|wasting|can'?t|cannot|don'?t have|missing out|falling behind|pain|broken|inefficien\w*)\b", re.I)


def stem(w: str) -> str:
    return w[:5] if len(w) > 5 and not any(c.isdigit() for c in w) else w


def terms(s: str) -> set[str]:
    """Content terms of a text: lower-cased word stems without stop words (digits kept whole)."""
    return {stem(t) for t in re.findall(r"[a-z0-9]+", norm(s)) if t not in STOP and (len(t) >= 3 or t.isdigit())}


def overlap(claim: str, quote: str) -> float:
    """Share of the CLAIM's content terms that the quote contains (0..1). Numbers in the claim must be in the quote, or the result is 0."""
    c, q = terms(claim), terms(quote)
    if not c:
        return 0.0
    if {t for t in c if any(ch.isdigit() for ch in t)} - q:
        return 0.0
    return len(c & q) / len(c)


def generic_reason(quote: str, company: str = "") -> Optional[str]:
    """Why a passage is too generic to be evidence (boilerplate, or too little information beyond the company's own name), else None."""
    if BOILERPLATE.search(quote or ""):
        return "boilerplate_quote"
    if len(terms(quote) - terms(company)) < 3:
        return "too_generic_quote"
    return None


@dataclass
class Evidence:
    field: str
    url: str
    quote: str
    overlap: Optional[float] = None


@dataclass
class Profile:
    values: dict = field(default_factory=dict)          # field -> str | list[str]  (only supported/inferred values)
    support: dict = field(default_factory=dict)         # field -> supported | inferred | unsupported | missing
    evidence: list = field(default_factory=list)        # [Evidence]
    reasons: dict = field(default_factory=dict)         # field -> why it was dropped (unsupported) or trimmed
    model_confidence: str = ""

    @property
    def supported_extractive(self) -> int:
        return sum(1 for f in EXTRACTIVE if self.support.get(f) == "supported")

    @property
    def inferred_kept(self) -> int:
        return sum(1 for f in INFERRED if self.support.get(f) == "inferred")

    @property
    def unsupported_extractive(self) -> int:
        return sum(1 for f in EXTRACTIVE if self.support.get(f) == "unsupported")


def _tally(labels: list[str], what: str) -> str:
    counts: dict[str, int] = {}
    for x in labels:
        counts[x] = counts.get(x, 0) + 1
    return f"{len(labels)} {what}: " + ", ".join(f"{n} {k}" for k, n in counts.items())


def _cap(items: list[str]) -> list[str]:
    return [clean(i, MAX_ITEM_LEN) for i in items[:MAX_ITEMS]]


def ground_profile(raw: dict, pages: dict[str, str], kinds: Optional[dict[str, str]] = None) -> Profile:
    """raw = the model's JSON; pages = {url: normalised text of exactly what the model was shown for that page}; kinds = {url: page kind}."""
    raw = {str(k).lower(): v for k, v in (raw or {}).items()}
    kinds = kinds or {}
    P = Profile()
    mc = norm(str(raw.get("model_confidence", "")))
    P.model_confidence = mc if mc in ("high", "medium", "low") else ""

    def ev(f: str, url: str, quote: str, ov: Optional[float] = None) -> None:
        P.evidence.append(Evidence(f, url, clean(quote, 300), None if ov is None else round(ov, 2)))

    def verbatim(value: str) -> Optional[str]:
        n = norm(value)
        return next((u for u, t in pages.items() if len(n) >= 3 and n in t), None)

    def drop(f: str, why: str) -> None:
        P.support[f], P.reasons[f] = "unsupported", why

    def judge(f: str, claim: str, quote, company: str) -> tuple[bool, Optional[str], Optional[float], str]:
        """Does `quote` verifiably occur AND is it specific AND does it contain this claim's terms?  -> (ok, url, overlap, reason)"""
        url = find_quote(quote, pages)
        if not url:
            return False, None, None, "quote_not_found_on_pages"
        g = generic_reason(str(quote), company)
        if g:
            return False, url, None, g
        ov = overlap(claim, str(quote))
        if ov < MIN_OVERLAP[f]:
            return False, url, ov, f"quote_does_not_support_claim (covers {ov:.0%} of the claim's terms, needs {MIN_OVERLAP[f]:.0%})"
        return True, url, ov, ""

    # --- company name: it must appear on the pages, and not be a page label
    v = raw.get("company_name")
    company = ""
    if is_missing(v):
        P.support["company_name"] = "missing"
    elif norm(str(v)) in GENERIC_NAMES_SET:
        drop("company_name", "generic_page_label")
    elif (u := verbatim(str(v))):
        company = clean(v, LIMITS["company_name"])
        P.values["company_name"], P.support["company_name"] = company, "supported"
        ev("company_name", u, clean(v, 120))
    else:
        drop("company_name", "name_not_found_on_pages")

    # --- summary-style fields: a specific quote that contains THIS claim's terms, or the claim itself verbatim on a page
    for f, qf in (("business_description", "description_quote"), ("industry_niche", "industry_quote"), ("target_customers", "customers_quote")):
        v, q = raw.get(f), raw.get(qf)
        if is_missing(v):
            P.support[f] = "missing"
            continue
        ok, url, ov, why = judge(f, str(v), q, company)
        if ok:
            P.values[f], P.support[f] = clean(v, LIMITS[f]), "supported"
            ev(f, url, q, ov)
            continue
        vurl = verbatim(str(v)) if len(norm(str(v))) >= MIN_QUOTE and not generic_reason(str(v), company) else None
        if vurl:                                         # the model copied the claim itself from a page
            P.values[f], P.support[f] = clean(v, LIMITS[f]), "supported"
            ev(f, vurl, clean(v, 300), 1.0)
        else:
            drop(f, why)

    # --- products/services: each item must occur on a page and be a real offering, not a menu label
    items = as_list(raw.get("products_services"))
    pq = raw.get("products_quote")
    pq_url = find_quote(pq, pages) if pq and not generic_reason(str(pq), company) else None
    if not items:
        P.support["products_services"] = "missing"
    else:
        kept, rejected = [], []               # rejected = category labels only: model-supplied text never goes into the diagnostics
        for it in items[:MAX_ITEMS * 2]:
            n = norm(it)
            u = verbatim(it) if len(n) >= 3 else None
            if not u:
                rejected.append("not on the pages")
            elif n in NAV_WORDS or n == norm(company):
                rejected.append("menu label or company name")
            else:
                in_quote = bool(pq_url) and terms(it) <= terms(str(pq))
                on_services = any(kinds.get(url) == "services" and n in txt for url, txt in pages.items())
                if len(n.split()) >= 2 or in_quote or on_services:
                    kept.append(it)
                    ev("products_services", u, clean(it, 140), 1.0)
                else:
                    rejected.append("single word with no supporting quote or services page")
        if kept:
            P.values["products_services"], P.support["products_services"] = _cap(kept), "supported"
            if rejected:
                P.reasons["products_services"] = _tally(rejected, "item(s) dropped")
        else:
            drop("products_services", _tally(rejected, "item(s) dropped, none survived"))

    # --- hypotheses: explicitly labelled, need a RELEVANT, SPECIFIC basis about the company; pain points need its own operations
    shared = {find_quote(raw.get("description_quote"), pages) and norm(str(raw.get("description_quote"))),
              find_quote(raw.get("customers_quote"), pages) and norm(str(raw.get("customers_quote")))} - {None, False, ""}

    def basis_ok(f: str, b: str) -> tuple[Optional[str], str]:
        url = find_quote(b, pages)
        if not url:
            return None, "basis_not_found_on_pages"
        if BOILERPLATE.search(b) or len(terms(b) - terms(company)) < 3:
            return None, "basis_too_generic"
        if VISITOR_RE.search(b):
            return None, "basis_is_addressed_to_visitors"
        if MARKET_RE.search(b):
            return None, "basis_describes_a_general_market_problem"
        if f == "pain_points" and (not OPS_RE.search(b) or OFFER_RE.search(b)):
            return None, "basis_is_not_a_statement_about_the_companys_own_operations"
        if f == "outreach_angle" and norm(b) in shared:
            return None, "basis_is_the_generic_description_or_customer_statement"
        return url, ""

    for f, bf in (("pain_points", "pain_points_basis"), ("ai_automation_opportunities", "opportunities_basis"), ("outreach_angle", "outreach_basis")):
        v = raw.get(f)
        if is_missing(v):
            P.support[f] = "missing"
            continue
        bases = as_list(raw.get(bf))
        good, why_bad = [], []
        for b in bases:
            u, why = basis_ok(f, b)
            (good if u else why_bad).append((u, b) if u else why)
        if not good:
            drop(f, (why_bad[0] if why_bad else "no_basis_given"))
            continue
        texts = [clean(v, LIMITS["outreach_angle"])] if f == "outreach_angle" else _cap(as_list(v))
        kept = []
        for tx in texts:
            if f == "pain_points" and (MARKET_RE.search(tx) or re.search(r"\b(?:often|typically|usually|commonly|many|most)\b", tx, re.I)):
                P.reasons[f] = "dropped a generic industry-wide statement"
                continue
            if f == "outreach_angle" and (DEFICIT_RE.search(tx) or (VISITOR_RE.search(tx) and re.search(r"\b(?:need|struggl|lack)\w*", tx, re.I))):
                P.reasons[f] = "dropped: asserts an unverified problem or need"
                continue
            best = max(((overlap(tx, b), u, b) for u, b in good), default=(0.0, None, ""))
            if best[0] >= MIN_OVERLAP[f]:
                kept.append(tx)
                ev(f, best[1], best[2], best[0])
            else:
                P.reasons[f] = P.reasons.get(f) or f"dropped: not supported by its basis (covers {best[0]:.0%}, needs {MIN_OVERLAP[f]:.0%})"
        if not kept:
            drop(f, P.reasons.get(f) or "no_relevant_basis")
            continue
        P.values[f] = kept[0] if f == "outreach_angle" else kept
        P.support[f] = "inferred"
    return P


def _level(p: Profile, use_model: bool) -> str:
    facts = [f for f in EXTRACTIVE if p.support.get(f) == "supported"]
    if not facts:
        return "none"
    body = [f for f in facts if f != "company_name"]
    if not body:
        return "low"
    ev = [e for e in p.evidence if e.field in body]
    pages = {e.url for e in ev}
    quotes = {norm(e.quote) for e in ev if e.field != "products_services"}
    if any(e.field == "products_services" for e in ev):
        quotes.add("\0products")                         # a list of items found together is one piece of evidence, not one per item
    core = "business_description" in body and "products_services" in body
    model_ok = not use_model or p.model_confidence in ("", "high")
    if core and len(body) >= 3 and len(pages) >= 2 and len(quotes) >= 3 and p.unsupported_extractive == 0 and model_ok:
        return "high"
    if len(body) >= 2 and ("business_description" in body or "products_services" in body) and p.unsupported_extractive <= 2:
        return "medium"
    return "low"


def confidence(p: Profile, pages_ok: int = 0) -> str:
    """Evidence-based confidence in the FACTUAL fields (hypotheses never count). Not a count of populated fields.

      none    nothing factual is supported
      low     only the name, or a single supported field
      medium  a supported description or product list plus at least one more supported field, drawn from relevant quotes
      high    description AND products supported, plus >=1 more of (industry, customers); supported by >=2 different pages and >=3 DIFFERENT
              quotes (one quote reused for several fields counts once); no factual field dropped as unsupported; and the model did not rate
              itself below high (a model that doubts its own answer can cap the result at medium, never raise it).
    Never high when any factual claim from the model had to be dropped; capped at low beyond two. pages_ok is accepted for compatibility only."""
    return _level(p, True)


def confidence_notes(p: Profile, conf: str) -> list[str]:
    """Human-readable reasons to look twice (disagreement with the model's own rating, dropped claims)."""
    rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    notes = []
    alone = _level(p, False)
    if alone != conf:
        notes.append(f"the evidence alone would support '{alone}' but the model rated its own answer '{p.model_confidence}', so confidence is '{conf}'")
    elif p.model_confidence and rank[p.model_confidence] < rank[conf]:
        notes.append(f"the model rated its own answer '{p.model_confidence}', lower than the evidence-based confidence '{conf}'")
    if p.unsupported_extractive:
        notes.append(f"{p.unsupported_extractive} factual claim(s) from the model were dropped as unsupported")
    return notes
