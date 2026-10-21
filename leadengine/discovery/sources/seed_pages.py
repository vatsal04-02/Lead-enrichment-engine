"""Public listing pages chosen by a person ("seed pages"): e.g. a blog post or association member list of AI automation firms.

The engine does NOT search the web. You name the page(s); this source fetches each through the guarded Fetcher (robots.txt is
enforced, a bot challenge / 401 / 403 / robots disallow is a stop, never retried or bypassed) and reads the links that point to
OTHER sites. Links to directories / social networks / marketplaces are dropped by validation (they are not company websites).
Only the single page you gave is fetched - links are not followed.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ...domain import InvalidDomain, normalize, registrable
from ..models import SRC_SEED, RawHit, now_iso
from . import SourceContext

GENERIC_TEXT = {"website", "visit website", "visit site", "visit", "read more", "learn more", "click here", "here", "link", "more",
                "view", "view profile", "site", "homepage", "home", "contact", "contact us", "get in touch", "know more",
                "official website", "web", "url", "source", "www"}
MAX_LINKS_PER_PAGE = 500


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _name_from(a) -> str | None:
    text = _clean(a.get_text(" "))
    if not text:
        text = _clean(a.get("title") or a.get("aria-label") or "")
    if not text or text.lower() in GENERIC_TEXT or len(text) > 80 or len(text) < 2:
        return None
    if re.fullmatch(r"(https?://|www\.)\S+", text, re.I) or re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", text, re.I):
        return None                                    # anchor text that is just the URL is not a company name
    return text


def _context(a) -> str:
    """Nearest heading above the link, else the parent's text - evidence for the reviewer, trimmed."""
    h = a.find_previous(["h1", "h2", "h3", "h4"])
    parent = _clean(a.parent.get_text(" ")) if a.parent else ""
    ctx = " | ".join(x for x in (_clean(h.get_text(" ")) if h else "", parent) if x)
    return ctx[:300]


class SeedPagesSource:
    name = SRC_SEED
    needs_network = True

    def __init__(self, urls: list[str]):
        self.urls = urls

    def planned_requests(self, ctx: SourceContext) -> list[str]:
        return [f"GET {u}  (+ GET robots.txt of its host)" for u in self.urls]

    async def discover(self, ctx: SourceContext) -> list[RawHit]:
        hits: list[RawHit] = []
        for seed in self.urls:
            res = await ctx.fetcher.fetch(seed)
            if not res.ok:
                kind = res.error_kind or f"http_{res.status}"
                ctx.error(self.name, seed, kind, res.error or f"HTTP {res.status}"
                          + ("" if kind not in ("robots", "bot_challenge") else " - not attempting to bypass"))
                continue
            base = res.final_url or seed
            try:
                seed_key = normalize(base).key
            except InvalidDomain:
                seed_key = ""
            soup = BeautifulSoup(res.text, "lxml")
            n = 0
            for a in soup.find_all("a", href=True):
                href = (a["href"] or "").strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                    continue
                url = urljoin(base, href)
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    continue
                if registrable(parts.hostname) == seed_key:
                    continue                                   # same-site navigation
                n += 1
                if n > MAX_LINKS_PER_PAGE:
                    ctx.error(self.name, seed, "truncated", f"more than {MAX_LINKS_PER_PAGE} outbound links; the rest were ignored")
                    break
                hits.append(RawHit(website=url, name=_name_from(a), source=SRC_SEED, source_url=base,
                                   source_ref=f"link text {_clean(a.get_text(' '))[:60]!r}", evidence=_context(a),
                                   niche_query="; ".join(ctx.niches), location_query="; ".join(ctx.locations),
                                   discovered_at=now_iso()))
        return hits
