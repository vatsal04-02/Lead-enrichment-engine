"""Candidate URL validation, blocked-domain screening and deduplication (pure functions; no network)."""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

import tldextract

from ..domain import InvalidDomain, Identity, name_slug, normalize
from .models import (BLOCKED, DUPLICATE, INVALID_URL, RESERVED, VALID, Candidate, RawHit, Rejected)

_ext = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)      # bundled snapshot: no network

RESERVED_SUFFIXES = ("test", "example", "invalid", "localhost", "local", "internal", "lan", "home.arpa", "onion", "corp", "intranet")
RESERVED_DOMAINS = {"example.com", "example.org", "example.net"}
SYNTHETIC_SUFFIX = "fixture.test"          # the repo's synthetic fixture sites; accepted only when a test explicitly allows them

# Not company websites: listing/profile pages, social networks, marketplaces, search/reference sites.
DIRECTORIES_AND_SOCIAL = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "pinterest.com",
    "tiktok.com", "threads.net", "reddit.com", "quora.com", "t.me", "wa.me", "whatsapp.com", "discord.com", "discord.gg",
    "github.com", "gitlab.com", "bitbucket.org", "stackoverflow.com", "producthunt.com", "behance.net", "dribbble.com",
    "clutch.co", "goodfirms.co", "sortlist.com", "designrush.com", "themanifest.com", "g2.com", "capterra.com",
    "trustpilot.com", "glassdoor.com", "glassdoor.co.in", "ambitionbox.com", "naukri.com", "indeed.com", "monsterindia.com",
    "justdial.com", "indiamart.com", "tradeindia.com", "sulekha.com", "yellowpages.com", "crunchbase.com", "zoominfo.com",
    "apollo.io", "tracxn.com", "owler.com", "dnb.com", "zaubacorp.com", "tofler.in", "inc42.com", "yourstory.com",
    "upwork.com", "fiverr.com", "freelancer.com", "toptal.com", "peopleperhour.com", "wikipedia.org", "wikidata.org",
    "google.com", "goo.gl", "bing.com", "duckduckgo.com", "yahoo.com", "maps.app.goo.gl", "openstreetmap.org",
    "apple.com", "play.google.com", "amazon.com", "amazon.in", "flipkart.com", "bit.ly", "t.co", "lnkd.in", "linktr.ee",
    "calendly.com", "typeform.com", "forms.gle", "airtable.com", "docs.google.com",
}
# Many unrelated businesses share these registrable domains, so a domain-level key would merge/mislabel them.
SHARED_HOSTING = {
    "blogspot.com", "wordpress.com", "wixsite.com", "wix.com", "weebly.com", "squarespace.com", "medium.com", "substack.com",
    "github.io", "gitlab.io", "netlify.app", "vercel.app", "herokuapp.com", "notion.site", "notion.so", "carrd.co",
    "webflow.io", "framer.website", "framer.app", "myshopify.com", "pages.dev", "web.app", "firebaseapp.com", "godaddysites.com",
    "tumblr.com", "ghost.io", "bubbleapps.io", "strikingly.com", "site123.me", "business.site", "my.canva.site", "canva.com",
}


def screen_host(host: str, key: str, *, allow_synthetic: bool = False) -> tuple[str, str, bool]:
    """-> (status, reason, synthetic). status VALID if usable as a real company website."""
    h = host.lower()
    synthetic = h == SYNTHETIC_SUFFIX or h.endswith("." + SYNTHETIC_SUFFIX)
    if synthetic:
        return (VALID, "synthetic fixture host (tests only)", True) if allow_synthetic else (
            RESERVED, "synthetic fixture host: never counted as a real business", True)
    parts = h.split(".")
    if parts[-1] in RESERVED_SUFFIXES or ".".join(parts[-2:]) in RESERVED_SUFFIXES or key in RESERVED_DOMAINS:
        return RESERVED, f"reserved/non-public name {h!r}", False
    ext = _ext(h)
    if not ext.suffix:
        return INVALID_URL, "no public suffix (not a registrable domain)", False
    if key in DIRECTORIES_AND_SOCIAL or any(key.endswith("." + b) for b in DIRECTORIES_AND_SOCIAL):
        return BLOCKED, "directory / social / marketplace / reference site, not a company website", False
    if key in SHARED_HOSTING:
        return BLOCKED, "shared hosting or site-builder domain (many unrelated sites share it)", False
    return VALID, "", False


def check_url(raw: str, *, allow_synthetic: bool = False) -> tuple[str, str, Optional[Identity], bool]:
    """-> (status, reason, identity|None, synthetic)."""
    raw = (raw or "").strip()
    if not raw:
        return INVALID_URL, "empty website", None, False
    if len(raw) > 2048 or re.search(r"[\s<>\"'\\^`{}|]", raw):
        return INVALID_URL, "contains whitespace or characters not valid in a URL", None, False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        try:
            netloc = urlsplit(raw).netloc
        except ValueError:
            return INVALID_URL, "unparseable URL", None, False
        if "@" in netloc:
            return INVALID_URL, "URL contains credentials", None, False
    try:
        ident = normalize(raw)
    except InvalidDomain as e:
        return INVALID_URL, str(e), None, False
    status, reason, synthetic = screen_host(ident.host, ident.key, allow_synthetic=allow_synthetic)
    if status == VALID and not synthetic and ":" in ident.start_url.split("//", 1)[1].split("/", 1)[0]:
        return INVALID_URL, "non-standard port", None, False
    return (VALID if status == VALID else status), reason, (ident if status == VALID else None), synthetic


def homepage_of(ident: Identity) -> str:
    return ident.start_url


def build_candidates(hits: Iterable[RawHit], *, keywords: Iterable[str] = (), allow_synthetic: bool = False) -> tuple[list[Candidate], list[Rejected]]:
    """Validate and deduplicate. Duplicates (same registrable domain) are merged: the first hit is kept, the other sources are
    recorded in `duplicate_sources` so provenance is not lost. Rejected hits keep their reason."""
    from .niche import hit_signal
    keywords = list(keywords)
    out: dict[str, Candidate] = {}
    rejected: list[Rejected] = []
    for h in hits:
        status, reason, ident, synthetic = check_url(h.website, allow_synthetic=allow_synthetic)
        if ident is None:
            rejected.append(Rejected(h.website[:200], status, reason, h.source, h.source_url, h.source_ref, h.name))
            continue
        c = out.get(ident.key)
        if c is not None:
            c.duplicate_sources.append((h.source, h.source_url or h.source_ref))
            if not c.name and h.name:
                c.name = h.name
            if not c.location and h.location:
                c.location = h.location
            rejected.append(Rejected(h.website[:200], DUPLICATE, f"same business as {ident.key} (already found via {c.discovery_source})",
                                     h.source, h.source_url, h.source_ref, h.name))
            continue
        carried = h.carried or {}
        out[ident.key] = Candidate(
            website=ident.start_url, domain=ident.key, name=(h.name or None), location=h.location, industry=h.industry,
            niche_query=h.niche_query, location_query=h.location_query, discovery_source=h.source, source_url=h.source_url,
            source_ref=h.source_ref, evidence=(h.evidence or "")[:300], niche_signal=hit_signal(h, keywords), discovered_at=h.discovered_at,
            validation_status=VALID, validation_notes=reason, review_status=carried.get("review_status") or "unreviewed",
            reviewed_by=carried.get("reviewed_by", ""), synthetic=synthetic)
    cands = list(out.values())
    _flag_possible_name_duplicates(cands)
    return cands, rejected


def _flag_possible_name_duplicates(cands: list[Candidate]) -> None:
    """Same company name on different registrable domains (acme.in / acme.com): kept, but flagged for the reviewer."""
    seen: dict[str, Candidate] = {}
    for c in cands:
        slug = name_slug(c.name)
        if len(slug) < 5:
            continue
        first = seen.get(slug)
        if first is None:
            seen[slug] = c
        else:
            note = f"same name as {first.domain}: possible duplicate business"
            c.validation_notes = (c.validation_notes + "; " if c.validation_notes else "") + note
