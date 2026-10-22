"""Search result -> company candidate. Pure functions (no network). A result becomes a candidate only if its URL is the website of something that
could be a company; articles, listings, directories, social networks and media are rejected WITH a reason (never silently dropped)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from ...domain import InvalidDomain, normalize
from ..models import VALID
from ..urlcheck import check_url
from .providers import SearchResult
from .queries import Query

# News/media/government/reference sites: they appear in search results about companies but are not the company's website.
MEDIA_AND_REFERENCE = {
    "economictimes.com", "indiatimes.com", "timesofindia.com", "hindustantimes.com", "ndtv.com", "thehindu.com", "business-standard.com",
    "livemint.com", "moneycontrol.com", "financialexpress.com", "indianexpress.com", "forbes.com", "forbesindia.com", "techcrunch.com",
    "entrackr.com", "medianama.com", "analyticsindiamag.com", "mapsofindia.com", "indiabizclub.com", "shine.com", "internshala.com",
    "foundit.in", "wellfound.com", "99acres.com", "magicbricks.com", "housing.com", "nobroker.in", "practo.com", "lybrate.com", "1mg.com",
    "shiksha.com", "collegedunia.com", "careers360.com", "99designs.com", "mouthshut.com", "sulekha.com", "bing.com", "msn.com",
    "nic.in", "gov.in", "india.gov.in", "wikimedia.org", "archive.org", "scribd.com", "slideshare.net", "issuu.com", "pdfcoffee.com",
}
ARTICLE_TITLE = re.compile(r"\b(?:top|best)\s+\d+|\b\d+\s+(?:best|top|leading)\b|\bbest\s+\w+(?:\s+\w+){0,3}\s+(?:in|companies|firms|agencies)\b|\blist of\b|"
                           r"\bdirectory\b|\branking\b|\breviews?\b|\bvs\.?\b|\bhow to\b|\bwhat is\b|\bguide\b|\bcompanies in\b|\bnear me\b|\bjobs?\b|\bsalary\b", re.I)
ARTICLE_PATH = re.compile(r"/(?:blog|blogs|news|article|articles|post|posts|press|category|tag|jobs?|careers?|resources?|insights?|case-stud\w+|events?|"
                          r"best-[\w-]+|top-[\w-]+|list-of-[\w-]+)(?:/|$)", re.I)
HOME_LIKE_PATH = re.compile(r"^/?(?:(?:about|about-us|contact|contact-us|services|company|home|index)(?:\.html?|\.php)?/?)?$", re.I)
GENERIC_SEG = re.compile(r"^(?:home|homepage|welcome|official (?:site|website)|contact(?: us)?|about(?: us)?|services?|best .*|top .*)$", re.I)
SEPARATORS = re.compile(r"\s+[|\-–—:•·»]\s+|\s*[|•·»]\s*")


@dataclass
class Rejection:
    url: str
    reason: str
    detail: str
    query: str
    provider: str


def infer_name(title: str, domain_key: str) -> Optional[str]:
    """Company name from a result title: the segment that shares a token with the domain; else the last short non-generic segment. None if unsure."""
    stem = re.sub(r"[^a-z0-9]", "", domain_key.split(".")[0].lower())
    segs = [s.strip(" \t\"'()[]") for s in SEPARATORS.split(title or "") if s and s.strip()]
    segs = [s for s in segs if s and not GENERIC_SEG.match(s) and len(s) <= 60]
    if not segs:
        return None
    for s in segs:
        flat = re.sub(r"[^a-z0-9]", "", s.lower())
        if flat and len(stem) >= 3 and (stem in flat or flat in stem):
            return s
    short = [s for s in segs if len(s.split()) <= 4]
    return short[-1] if short else None


def from_result(r: SearchResult, q: Query, *, allow_synthetic: bool = False) -> tuple[Optional[dict], Optional[Rejection]]:
    """-> (candidate dict, None) or (None, Rejection)."""
    rej = lambda reason, detail="": (None, Rejection(r.url, reason, detail, r.query, r.provider))
    try:
        u = urlsplit(r.url)
    except ValueError:
        return rej("invalid_url", "unparseable")
    if u.scheme not in ("http", "https") or not u.hostname:
        return rej("invalid_url", "not an http(s) URL")
    status, why, ident, synthetic = check_url(f"{u.scheme}://{u.netloc}/", allow_synthetic=allow_synthetic)
    if ident is None:
        return rej(status, why)
    key = ident.key
    if key in MEDIA_AND_REFERENCE or any(key.endswith("." + m) for m in MEDIA_AND_REFERENCE):
        return rej("media_or_reference_site", key)
    path = u.path or "/"
    if ARTICLE_PATH.search(path) or (not HOME_LIKE_PATH.match(path) and path.count("/") >= 3):
        return rej("article_or_deep_page", path[:80])
    if ARTICLE_TITLE.search(r.title or "") and not HOME_LIKE_PATH.match(path):
        return rej("listicle_or_article_title", (r.title or "")[:80])
    return {"website": ident.start_url, "domain": key, "name": infer_name(r.title, key), "snippet": r.snippet, "title": r.title, "source_url": r.url,
            "provider": r.provider, "query": r.query, "industry_query": q.industry, "city_query": q.city, "rank": r.rank, "page": r.page,
            "synthetic": synthetic}, None
