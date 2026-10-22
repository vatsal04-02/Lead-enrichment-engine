"""Company website validation: visit the candidate's own public pages through the guarded Fetcher (robots.txt, SSRF guard, per-host rate limits,
challenge = stop), and extract identity + publicly listed contact details with the deterministic extractors. No model is involved here.

Verdicts (`status`): valid | india_unconfirmed | directory_or_aggregator | identity_unconfirmed | not_a_company_site | parked_or_empty | robots_disallowed | anti_bot_blocked | redirected_off_domain | unreachable.
Only `valid` counts towards the target. `confidence` is a documented heuristic over the evidence found (NOT a probability)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ...extract import extract_page, pick_best
from ...profiles.pages import collect_pages
from ..niche import find_terms, india_evidence
from ..urlcheck import DIRECTORIES_AND_SOCIAL, SHARED_HOSTING
from ..urlcheck import normalize
from ...domain import registrable
from .candidates import name_matches_domain

CITY_ALIASES = {"bengaluru": ["bengaluru", "bangalore"], "delhi": ["delhi", "new delhi"], "gurugram": ["gurugram", "gurgaon"], "kochi": ["kochi", "cochin"],
                "mumbai": ["mumbai", "bombay"], "chennai": ["chennai", "madras"], "kolkata": ["kolkata", "calcutta"]}
PARKED = re.compile(r"domain (?:is )?for sale|buy this domain|this domain (?:may be|is) for sale|parked (?:free|domain)|coming soon|under construction|"
                    r"website (?:is )?(?:expired|suspended)|account suspended|default web site page|index of /", re.I)
# Wording of listing/review/ranking platforms. Two distinct phrases on a site's own pages mean it is an aggregator, not the company's official website.
AGGREGATOR_PHRASES = [re.compile(p, re.I) for p in (
    r"\b(?:top|best)\s+\d+\b[^.]{0,40}\b(?:companies|firms|agencies|vendors|providers)\b", r"\blist of (?:the )?(?:top |best )?(?:companies|firms|agencies|vendors)\b",
    r"\bwrite a review\b", r"\bclaim (?:your|this) (?:profile|listing|company)\b", r"\b(?:verified|client|customer) reviews\b",
    r"\bcompare (?:companies|agencies|vendors|firms|providers)\b", r"\bbrowse (?:companies|agencies|vendors|firms|listings)\b", r"\bget listed\b",
    r"\b(?:submit|add|list) your (?:company|business|agency|firm)\b", r"\b(?:find|hire) the best\b[^.]{0,40}\b(?:companies|firms|agencies|vendors)\b")]
EXTERNAL_HOST = re.compile(r'href=["\']https?://([^/"\'?#:]+)', re.I)
SOCIAL_AND_DIRECTORIES = DIRECTORIES_AND_SOCIAL | SHARED_HOSTING
REPORT_FIELDS = ("company_name", "description", "email", "phone", "address", "linkedin_url")


@dataclass
class SiteRecord:
    status: str = ""
    detail: str = ""
    name: str = ""
    name_source: str = ""
    description: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    linkedin_url: str = ""
    contact_source_url: str = ""
    pages: list = field(default_factory=list)              # [(url, kind)]
    india_signal: str = ""                                  # "yes" | "unconfirmed"
    india_evidence: str = ""
    industry_evidence: str = ""                             # niche terms found in the company's OWN page text
    confidence: float = 0.0
    name_confirmed: bool = False
    official_basis: str = ""                                # why the site looks like the company's own website
    missing_fields: list = field(default_factory=list)


def city_terms(city: str) -> list[str]:
    c = (city or "").strip().lower()
    return CITY_ALIASES.get(c, [c] if c else [])


def looks_like_aggregator(pages, body: str, key: str) -> str:
    """'' or the reason: listing/review wording (2+ distinct phrases), or one phrase plus links out to many different third-party sites."""
    phrases = sum(1 for p in AGGREGATOR_PHRASES if p.search(body[:20000]))
    outs = {registrable(h.lower()) for p in pages for h in EXTERNAL_HOST.findall(p.html or "")}
    outs = {o for o in outs if o and o != key and not any(o == b or o.endswith("." + b) for b in SOCIAL_AND_DIRECTORIES)}
    if phrases >= 2 or (phrases >= 1 and len(outs) >= 15):
        return f"listing/review wording ({phrases} phrase(s)) and {len(outs)} outbound third-party sites"
    return ""


def official_identity(key: str, names: list[str], email: str, cands: list) -> str:
    """Why this site looks like the company's OWN website ('' if nothing supports it): its name matches the domain, it lists an email on its own domain,
    or it declares itself an organisation in structured data (JSON-LD)."""
    if any(name_matches_domain(n, key) for n in names if n):
        return "name_matches_domain"
    if email and registrable(email.rsplit("@", 1)[-1].lower()) == key:
        return "email_on_own_domain"
    if any(c.field == "company_name" and c.method == "json_ld" for c in cands):
        return "json_ld_organization"
    return ""


def score(rec: SiteRecord, name_confirmed: bool, extra_hits: int = 0) -> float:
    """0.25 company-like site reached | 0.15 name confirmed on the site | 0.20 India evidence (tld/+91/address) or 0.10 city mentioned |
    0.10 description | 0.10 email or phone | 0.10 address | 0.10 niche terms | +0.05 per extra independent search hit (max 2)."""
    if rec.status not in ("valid", "india_unconfirmed"):
        return 0.0
    s = 0.25 + (0.15 if name_confirmed else 0.0)
    s += 0.20 if (rec.india_signal == "yes" and not rec.india_evidence.startswith("city_mentioned")) else (0.10 if rec.india_evidence else 0.0)
    s += 0.10 * bool(rec.description) + 0.10 * bool(rec.email or rec.phone) + 0.10 * bool(rec.address) + 0.10 * bool(rec.industry_evidence)
    return round(min(1.0, s + 0.05 * min(2, extra_hits)), 2)


async def validate_site(fetcher, cand: dict, keywords: list[str], *, max_pages: int = 4) -> SiteRecord:
    rec = SiteRecord()
    ident = normalize(cand["website"])
    ps = await collect_pages(fetcher, ident, max_pages=max_pages)
    rec.pages = [(p.url, p.kind) for p in ps.pages]
    if not ps.pages:
        kind = ps.error_kind
        rec.status = ("robots_disallowed" if kind == "robots" else "anti_bot_blocked" if kind == "bot_challenge" else
                      "redirected_off_domain" if kind == "redirected_off_domain" else "unreachable")
        rec.detail = f"{kind}: {ps.error}"[:200]
        return rec
    cands, text, has_links = [], [], False
    for p in ps.pages:
        ex = extract_page(p.html, p.url, p.kind, ident.key)
        cands += ex.candidates
        text.append(ex.visible_text or "")
        has_links = has_links or any(ex.links.get(k) for k in ("about", "contact", "services"))
    body = " ".join(text)
    best = pick_best(cands, ident.key)
    val = lambda f: best[f][0].value if f in best else ""
    src = lambda f: best[f][0].source_url if f in best else ""
    rec.description, rec.email, rec.phone, rec.address, rec.linkedin_url = (val("description"), val("email"), val("phone"), val("address"), val("linkedin_url"))
    rec.contact_source_url = src("email") or src("phone") or src("address")
    nm = best.get("company_name")
    searched = cand.get("name") or ""
    flat = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    name_confirmed = False
    if nm:
        rec.name, rec.name_source = nm[0].value, f"site:{nm[0].method}"
        name_confirmed = True
    elif searched:
        rec.name, rec.name_source = searched, "search_result_title"
    if searched and flat(searched) and flat(searched) in flat(body[:20000] + " " + " ".join(p.url for p in ps.pages)):
        name_confirmed = True
        if not nm:
            rec.name_source = "search_result_title (also on the site)"
    if PARKED.search(body[:3000]) or len(body.strip()) < 120 and not (rec.email or rec.phone):
        rec.status, rec.detail = "parked_or_empty", "parked, placeholder or (almost) empty page"
        return rec
    company_like = bool(has_links or rec.email or rec.phone or rec.address or rec.description or "company_name" in best)
    if not company_like:
        rec.status, rec.detail = "not_a_company_site", "no contact details, description or about/contact/services pages found"
        return rec
    why = looks_like_aggregator(ps.pages, body, ident.key)
    if why:
        rec.status, rec.detail = "directory_or_aggregator", why
        return rec
    names = [searched, rec.name] + [c.value for c in cands if c.field == "company_name"]
    rec.official_basis = official_identity(ident.key, names, rec.email, cands)
    if not rec.official_basis:
        rec.status, rec.detail = "identity_unconfirmed", "nothing ties this site to the company it was found for (name does not match the domain, no email on the domain, no structured-data organisation)"
        return rec
    rec.india_signal, rec.india_evidence = india_evidence(ident.key, rec.phone, rec.address)
    if rec.india_signal != "yes":
        hit = next((c for c in city_terms(cand.get("city_query", "")) if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", body.lower())), "")
        if hit:
            rec.india_signal, rec.india_evidence = "yes", f"city_mentioned:{hit}"
    rec.status = "valid" if rec.india_signal == "yes" else "india_unconfirmed"
    rec.detail = "" if rec.status == "valid" else "no India evidence on the site (no .in domain, +91 phone, Indian address or the searched city)"
    rec.industry_evidence = ", ".join(find_terms(body.lower(), keywords)[:8])
    rec.missing_fields = [f for f in REPORT_FIELDS if not getattr(rec, {"company_name": "name"}.get(f, f))]
    if not rec.industry_evidence:
        rec.missing_fields.append("industry_evidence")
    rec.name_confirmed = name_confirmed
    rec.confidence = score(rec, name_confirmed)
    return rec
