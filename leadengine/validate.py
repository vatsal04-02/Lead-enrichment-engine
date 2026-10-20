"""Format checks and defined verification procedures.

Vocabulary (kept deliberately separate; see scoring.py):
  present            a value exists
  source_attributed  a source URL + extraction method are recorded
  format_valid       passes the field's format check (checks below)
  verified           passed a *defined verification procedure* (verify() below)
  stale              older than its freshness window

Verification procedures implemented:
  http_reachable_domain_match   website: homepage fetched OK and final host belongs to the input domain
  cross_source_agreement        company_name, phone: two independent extraction methods agree
Email is NEVER marked verified: no mailbox-level verification (SMTP/RCPT or provider API) is
implemented. Instead we record syntax, published-on-source evidence and (optionally) DNS MX checks.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Optional
from urllib.parse import urlsplit

from .domain import name_slug, registrable
from .extract import EMAIL_RE, Candidate

GENERIC_NAMES = {"home", "welcome", "homepage", "index", "untitled", "website", "official website"}


def format_checks(field: str, kind: str, value: str, site_key: str = "") -> dict:
    v = (value or "").strip()
    if not v:
        return {"nonempty": False}
    checks: dict = {"nonempty": True}
    if kind == "email":
        checks["syntax"] = EMAIL_RE.fullmatch(v) is not None
        checks["info_same_domain"] = registrable(v.rsplit("@", 1)[-1].lower()) == site_key
        checks["info_role_address"] = v.split("@")[0].lower() in ("info", "contact", "hello", "sales", "office", "support")
    elif kind == "phone":
        digits = re.sub(r"\D", "", v)
        checks["digit_count_7_15"] = 7 <= len(digits) <= 15
        checks["info_country_code"] = v.startswith("+")
    elif kind == "url":
        try:
            p = urlsplit(v)
            checks["url_syntax"] = p.scheme in ("http", "https") and bool(p.hostname)
        except ValueError:
            checks["url_syntax"] = False
    elif kind == "year":
        checks["year_in_range"] = v.isdigit() and 1800 <= int(v) <= date.today().year
    else:
        if field == "company_name":
            checks["not_generic"] = v.lower() not in GENERIC_NAMES and 2 <= len(v) <= 120
        elif field == "description":
            checks["length_ok"] = 20 <= len(v) <= 600
        else:
            checks["length_ok"] = 2 <= len(v) <= 250
    return checks


def format_valid(checks: dict) -> bool:
    """Syntax/format only. info_* (descriptive), domain_* (DNS) and published_* (evidence) keys are separate."""
    if not checks.get("nonempty"):
        return False
    return all(v for k, v in checks.items()
               if isinstance(v, bool) and not k.startswith(("info_", "domain_", "published_")))


def _norm(field: str, value: str) -> str:
    return re.sub(r"\D", "", value) if field == "phone" else name_slug(value)


def verify(field: str, chosen: Candidate, all_candidates: list[Candidate]) -> tuple[str, Optional[str]]:
    """Return (verification_status, procedure) for a chosen candidate."""
    if field in ("company_name", "phone"):
        target = _norm(field, chosen.value)
        families = set()
        for c in all_candidates:
            if c.field != field:
                continue
            n = _norm(field, c.value)
            if n == target or (field == "company_name" and len(n) >= 4 and len(target) >= 4 and (n in target or target in n)):
                families.add("jsonld" if c.method in ("json_ld", "microdata") else
                             "markup" if c.method in ("meta", "title", "tel", "address_tag", "link") else c.method)
        if len(families) >= 2:
            return "verified", "cross_source_agreement"
    return "unverified", None


async def mx_check(domain: str, timeout: float = 3.0) -> Optional[bool]:
    """True if the domain has MX (or A) records, False if it definitely has none, None if unknown."""
    try:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver
    except ImportError:
        return None
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    try:
        await resolver.resolve(domain, "MX")
        return True
    except (dns.resolver.NoAnswer,):
        try:
            await resolver.resolve(domain, "A")
            return True
        except dns.resolver.NXDOMAIN:
            return False
        except Exception:
            return None
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.exception.DNSException, asyncio.TimeoutError, OSError):
        return None
