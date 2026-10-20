"""Tier 2/3 deterministic extraction: HTML -> field candidates (+ links, JS-render hint).

Fetched HTML is untrusted data. It is parsed, never executed, and extracted strings are
whitespace-normalised, control-character-stripped and length-capped before use.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .domain import name_slug, registrable

EXTRACTOR_VERSION = "1"

# static heuristic priors per method (NOT correctness probabilities)
METHOD_PRIOR = {"json_ld": 0.9, "microdata": 0.85, "mailto": 0.85, "tel": 0.85, "link": 0.75,
                "meta": 0.7, "address_tag": 0.6, "title": 0.4, "text_regex": 0.5, "ai_grounded": 0.4,
                "input": 0.3}
METHOD_RANK = {"json_ld": 1, "microdata": 2, "mailto": 2, "tel": 2, "link": 3, "meta": 4,
               "address_tag": 4, "title": 6, "text_regex": 7, "ai_grounded": 9, "input": 10}

MAX_LEN = {"company_name": 120, "description": 600, "address": 250, "industry": 120,
           "email": 254, "phone": 40, "founded_year": 4}

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
PHONE_RE = re.compile(r"(?<![\w.])(\+?\(?\d[\d\s().\-]{8,18}\d)(?![\w])")
YEAR_RE = re.compile(r"\b(?:founded|established|est\.?|since|incorporated)\s*(?:in\s*)?(\d{4})\b", re.I)
JUNK_EMAIL = re.compile(r"(example\.|@sentry|wixpress|@2x|\.(png|jpg|jpeg|gif|svg|webp|css|js)$|"
                        r"yourdomain|domain\.com|email\.com|youremail|your@|name@|user@|test@test|noreply|no-reply)", re.I)
ROLE_LOCAL = ("info", "contact", "hello", "sales", "office", "support", "enquiries", "inquiries", "admin")

SOCIAL = {
    "linkedin_url": ("linkedin.com",),
    "twitter_url": ("twitter.com", "x.com"),
    "facebook_url": ("facebook.com", "fb.com"),
    "instagram_url": ("instagram.com",),
    "github_url": ("github.com",),
    "youtube_url": ("youtube.com",),
}
SOCIAL_SKIP_SEGMENTS = {"share", "sharer", "sharer.php", "intent", "home", "search", "login", "signup",
                        "dialog", "plugins", "hashtag", "watch", "embed", "feed", "pages", "tr", "p", "explore", "i"}

LINK_KINDS = {
    "about": ("about", "company", "who-we-are", "our-story", "about-us", "our-company", "story"),
    "contact": ("contact", "get-in-touch", "reach-us", "locations", "location", "find-us", "contact-us"),
    "team": ("team", "leadership", "people", "management"),
    "services": ("services", "solutions", "what-we-do", "products", "capabilities", "offerings"),
}
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3|css|js|xml|ico)(\?|$)", re.I)


@dataclass
class Candidate:
    field: str
    value: str
    method: str
    source_url: str
    evidence: str = ""
    tier: str = ""

    @property
    def rank(self) -> int:
        return METHOD_RANK.get(self.method, 8)

    def to_json(self) -> dict:
        return {"field": self.field, "value": self.value, "method": self.method,
                "source_url": self.source_url, "evidence": self.evidence}


@dataclass
class PageExtraction:
    candidates: list[Candidate] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=dict)
    visible_text: str = ""
    title: str = ""
    js_hint: bool = False

    def to_json(self) -> dict:
        return {"candidates": [c.to_json() for c in self.candidates], "links": self.links,
                "visible_text": self.visible_text[:20000], "title": self.title, "js_hint": self.js_hint}

    @classmethod
    def from_json(cls, d: dict) -> "PageExtraction":
        return cls([Candidate(**c) for c in d["candidates"]], d["links"], d["visible_text"],
                   d["title"], d["js_hint"])


def clean(text: Optional[str], limit: int = 600) -> str:
    if not text:
        return ""
    text = htmllib.unescape(str(text))
    text = _CTRL.sub(" ", text)
    return _WS.sub(" ", text).strip()[:limit]


def _year(s: str) -> Optional[str]:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", s or "")
    if m and 1800 <= int(m.group(1)) <= date.today().year:
        return m.group(1)
    return None


# ---------------------------------------------------------------- JSON-LD
ORG_TYPES = ("organization", "corporation", "localbusiness", "company", "professionalservice", "store",
             "restaurant", "medicalbusiness", "financialservice", "legalservice", "realestateagent",
             "educationalorganization", "ngo", "airline", "automotivebusiness", "homeandconstructionbusiness")


def _iter_ld_nodes(obj) -> Iterable[dict]:
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_ld_nodes(o)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "mainEntity", "publisher", "provider", "author"):
            if key in obj:
                yield from _iter_ld_nodes(obj[key])


def _is_org(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and (x.lower() in ORG_TYPES or x.lower().endswith(("business", "organization")))
               for x in types)


def _fmt_address(a) -> str:
    if isinstance(a, str):
        return clean(a, 250)
    if isinstance(a, dict):
        parts = [a.get("streetAddress"), a.get("addressLocality"),
                 " ".join(x for x in [a.get("addressRegion"), a.get("postalCode")] if x),
                 a.get("addressCountry") if isinstance(a.get("addressCountry"), str)
                 else (a.get("addressCountry") or {}).get("name") if isinstance(a.get("addressCountry"), dict) else None]
        return clean(", ".join(clean(p, 100) for p in parts if p), 250)
    return ""


def extract_json_ld(soup: BeautifulSoup, url: str) -> list[Candidate]:
    out: list[Candidate] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            continue
        for node in _iter_ld_nodes(data):
            if not _is_org(node):
                continue
            add = lambda f, v: v and out.append(Candidate(f, v, "json_ld", url))
            if isinstance(node.get("name"), str):
                add("company_name", clean(node["name"], 120))
            if isinstance(node.get("description"), str):
                add("description", clean(node["description"], 600))
            if isinstance(node.get("email"), str):
                add("email", clean(node["email"].replace("mailto:", ""), 254))
            if isinstance(node.get("telephone"), str):
                add("phone", clean(node["telephone"], 40))
            if node.get("address"):
                add("address", _fmt_address(node["address"]))
            if isinstance(node.get("foundingDate"), (str, int)):
                add("founded_year", _year(str(node["foundingDate"])) or "")
            if isinstance(node.get("industry"), str):
                add("industry", clean(node["industry"], 120))
            logo = node.get("logo")
            logo = logo.get("url") if isinstance(logo, dict) else logo
            if isinstance(logo, str) and logo:
                add("logo_url", urljoin(url, logo))
            same = node.get("sameAs")
            for link in ([same] if isinstance(same, str) else same if isinstance(same, list) else []):
                if isinstance(link, str):
                    f = social_field(link)
                    if f:
                        out.append(Candidate(f, normalize_social(f, link), "json_ld", url))
    return out


# ---------------------------------------------------------------- social
def social_field(href: str) -> Optional[str]:
    try:
        host = (urlsplit(href).hostname or "").lower()
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    for f, hosts in SOCIAL.items():
        if host in hosts or any(host.endswith("." + h) for h in hosts):
            return f
    return None


def normalize_social(f: str, href: str) -> str:
    p = urlsplit(href)
    path = p.path.rstrip("/")
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"https://{'www.' if f in ('linkedin_url',) else ''}{host}{path}"


def valid_social(f: str, href: str) -> bool:
    try:
        p = urlsplit(href)
    except ValueError:
        return False
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return False
    first = segs[0].lower()
    if f == "linkedin_url":
        return first in ("company", "school", "showcase") and len(segs) >= 2
    if f == "youtube_url":
        return first.startswith("@") or first in ("channel", "c", "user")
    if first in SOCIAL_SKIP_SEGMENTS or (p.query and "u=" in p.query):
        return False
    if f in ("twitter_url", "instagram_url", "github_url"):
        return len(segs) == 1 or f == "github_url"
    return True


# ---------------------------------------------------------------- main
def _cf_email(hexstr: str) -> Optional[str]:
    try:
        b = bytes.fromhex(hexstr)
        k = b[0]
        return bytes(c ^ k for c in b[1:]).decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


def normalize_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    if 7 <= len(digits) <= 15:
        return clean(raw, 40)
    return None


def good_email(e: str) -> bool:
    return bool(e) and len(e) <= 254 and not JUNK_EMAIL.search(e) and EMAIL_RE.fullmatch(e) is not None


def extract_page(html: str, url: str, page_kind: str, site_key: str) -> PageExtraction:
    soup = BeautifulSoup(html, "lxml")
    res = PageExtraction()
    cands = res.candidates

    cands.extend(extract_json_ld(soup, url))

    # microdata / itemprop (Organization-ish)
    def itemprop(name: str) -> str:
        t = soup.find(attrs={"itemprop": name})
        if not t:
            return ""
        return clean(t.get("content") or t.get_text(" "), 250)
    street, loc, reg, pc = itemprop("streetAddress"), itemprop("addressLocality"), itemprop("addressRegion"), itemprop("postalCode")
    if street and loc:
        cands.append(Candidate("address", clean(", ".join(x for x in [street, loc, f"{reg} {pc}".strip()] if x), 250),
                               "microdata", url))
    for name_prop, f in (("telephone", "phone"), ("email", "email")):
        v = itemprop(name_prop)
        if v:
            cands.append(Candidate(f, v.replace("mailto:", ""), "microdata", url))

    # meta
    def meta(*names) -> str:
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return clean(t["content"], 600)
        return ""
    v = meta("og:site_name", "application-name")
    if v:
        cands.append(Candidate("company_name", clean(v, 120), "meta", url))
    v = meta("og:description", "description", "twitter:description")
    if v and len(v) >= 20:
        cands.append(Candidate("description", v, "meta", url))
    ogimg = None
    if page_kind == "home":
        icon = soup.find("link", rel=lambda r: r and "icon" in " ".join(r).lower() and "apple-touch" in " ".join(r).lower())
        if icon and icon.get("href"):
            ogimg = urljoin(url, icon["href"])
            cands.append(Candidate("logo_url", ogimg, "meta", url))

    res.title = clean(soup.title.string if soup.title and soup.title.string else "", 200)
    # title -> name, only if one segment matches the domain label (avoids "Home | Acme" ambiguity)
    if res.title:
        label = name_slug(site_key.split(".")[0])
        for seg in re.split(r"\s*[|–—:·]\s*|\s+-\s+", res.title):
            if label and len(label) >= 3 and label in name_slug(seg) and 2 <= len(seg) <= 80:
                cands.append(Candidate("company_name", clean(seg, 120), "title", url))
                break

    # links: mailto / tel / social / cloudflare-protected emails
    social_seen: dict[str, set] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            addr = unquote(href[7:].split("?")[0]).strip()
            if good_email(addr):
                cands.append(Candidate("email", addr, "mailto", url))
        elif low.startswith("tel:"):
            ph = normalize_phone(unquote(href[4:]))
            if ph:
                cands.append(Candidate("phone", ph, "tel", url))
        elif low.startswith(("http://", "https://", "//")):
            f = social_field(urljoin(url, href))
            if f and valid_social(f, urljoin(url, href)):
                nv = normalize_social(f, urljoin(url, href))
                if nv.lower() not in social_seen.setdefault(f, set()):
                    social_seen[f].add(nv.lower())
                    cands.append(Candidate(f, nv, "link", url))
    for t in soup.find_all(attrs={"data-cfemail": True}):
        addr = _cf_email(t["data-cfemail"])
        if addr and good_email(addr):
            cands.append(Candidate("email", addr, "mailto", url, "cloudflare-protected"))

    for addr_tag in soup.find_all("address"):
        txt = clean(addr_tag.get_text(" "), 250)
        if 10 <= len(txt) <= 250 and re.search(r"\d", txt):
            cands.append(Candidate("address", txt, "address_tag", url))
            break

    # links for targeted tier
    res.links = discover_links(soup, url, site_key)

    # visible text (script/style/noscript removed after structured data was read)
    for t in soup(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    text = clean(soup.get_text(" "), 60000)
    res.visible_text = text
    res.js_hint = _looks_js_rendered(soup, text, html)

    # text fallbacks (lower-ranked methods)
    for m in EMAIL_RE.finditer(text):
        if good_email(m.group(0)):
            cands.append(Candidate("email", m.group(0), "text_regex", url))
    if page_kind in ("contact", "home", "about"):
        for m in PHONE_RE.finditer(text):
            ph = normalize_phone(m.group(1))
            if ph and re.search(r"[\s().\-+]", m.group(1)) and not re.fullmatch(r"\d{4}[-\s]\d{2}[-\s]\d{2}", ph):
                cands.append(Candidate("phone", ph, "text_regex", url, text[max(0, m.start() - 20):m.end() + 5]))
    ym = YEAR_RE.search(text)
    if ym and 1800 <= int(ym.group(1)) <= date.today().year:
        cands.append(Candidate("founded_year", ym.group(1), "text_regex", url, ym.group(0)))

    res.candidates = [c for c in cands if c.value and len(c.value) <= MAX_LEN.get(c.field, 2048)]
    return res


def _looks_js_rendered(soup: BeautifulSoup, text: str, raw_html: str) -> bool:
    if len(text) >= 400:
        return False
    scripts = len(BeautifulSoup(raw_html, "lxml").find_all("script", src=True))
    root = bool(soup.select_one("#root, #app, #__next, #__nuxt, [data-reactroot]"))
    return scripts >= 2 or root or bool(re.search(r"enable javascript|requires javascript", raw_html, re.I))


def discover_links(soup: BeautifulSoup, base_url: str, site_key: str) -> dict[str, list[str]]:
    scored: dict[str, dict[str, int]] = {k: {} for k in LINK_KINDS}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")) or SKIP_EXT.search(href):
            continue
        absu = urljoin(base_url, href).split("#")[0]
        try:
            p = urlsplit(absu)
        except ValueError:
            continue
        if p.scheme not in ("http", "https") or registrable((p.hostname or "").lower()) != site_key:
            continue
        path = p.path.lower().strip("/")
        if not path or path.count("/") > 2:
            continue
        label = clean(a.get_text(" "), 60).lower()
        norm = f"{p.scheme}://{p.netloc}/{path}"
        for kind, words in LINK_KINDS.items():
            score = 0
            last = path.split("/")[-1]
            if last in words:
                score += 3
            elif any(w in last for w in words):
                score += 2
            if label in words or any(w == label for w in words):
                score += 2
            elif any(w in label for w in words) and len(label) <= 30:
                score += 1
            if score:
                scored[kind][norm] = max(scored[kind].get(norm, 0), score)
    return {k: [u for u, _ in sorted(v.items(), key=lambda kv: (-kv[1], len(kv[0])))][:3]
            for k, v in scored.items() if v}


# ---------------------------------------------------------------- merge
def pick_best(cands: list[Candidate], site_key: str) -> dict[str, tuple[Candidate, list[Candidate]]]:
    """Best candidate per field, plus distinct alternates. Deterministic ordering."""
    by_field: dict[str, list[Candidate]] = {}
    for c in cands:
        by_field.setdefault(c.field, []).append(c)
    out = {}
    for f, lst in by_field.items():
        def key(c: Candidate):
            sub = 0
            if f == "email":
                dom = c.value.rsplit("@", 1)[-1].lower()
                same = registrable(dom) == site_key
                role = c.value.split("@")[0].lower() in ROLE_LOCAL
                sub = (0 if same else 1, 0 if role else 1)
            elif f == "phone":
                sub = (0 if c.value.startswith("+") else 1,)
            else:
                sub = (0,)
            return (c.rank, sub, -len(c.value) if f == "description" else 0)
        lst = sorted(lst, key=key)
        best = lst[0]
        seen, alts = {best.value.lower()}, []
        for c in lst[1:]:
            if c.value.lower() not in seen:
                seen.add(c.value.lower())
                alts.append(c)
        out[f] = (best, alts[:5])
    return out
