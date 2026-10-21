"""Collect a company's public pages (Home, then About, Services, Contact) through the guarded Fetcher, and build the single document the model
reads. The fetcher enforces robots.txt, per-host rate limits, the SSRF guard, size/time caps and stops on challenges/401/403: a refused page is
recorded and skipped, never retried differently."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..backends.normalize import drop_hidden_elements, make_digest
from ..domain import Identity, same_site
from ..extract import extract_page
from .ground import norm

KIND_ORDER = ("about", "services", "contact")
# Text that addresses the reader/model instead of describing the company. Removed from what the model sees (and from the evidence texts), and reported.
INJECTION_RE = re.compile(
    r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|prompts?|messages?|rules?)"
    r"|system\s+prompt|new\s+instructions?\s*:|developer\s+mode|<\|im_start\|>|\[/?inst\]|(?:you\s+are|act\s+as)\s+now\s+an?\s+\w+\s+(?:assistant|ai|model)", re.I)


def sanitize_html(html: str) -> tuple[str, list[str]]:
    """Drop hidden elements and instruction-like text blocks before the model sees the page. -> (html, warnings). Warnings are facts for the
    reviewer ("removed 1 hidden element", "removed 1 instruction-like block"), never silently swallowed."""
    soup = BeautifulSoup(html or "", "lxml")
    warnings = []
    hidden = drop_hidden_elements(soup)
    if hidden:
        warnings.append(f"removed {hidden} hidden element(s)")
    removed = 0
    for node in list(soup.find_all(string=INJECTION_RE)):
        holder = node.find_parent(["p", "li", "div", "span", "td", "section", "article", "h1", "h2", "h3", "h4", "blockquote"]) or node.parent
        if holder is not None and holder.name not in ("html", "body"):
            holder.decompose()
        else:
            node.extract()
        removed += 1
    if removed:
        warnings.append(f"removed {removed} instruction-like text block(s) (possible prompt injection)")
    return str(soup), warnings


@dataclass
class Page:
    url: str
    kind: str
    html: str
    digest: str = ""            # what the model sees for this page (script-free, head info restated as visible text)
    text: str = ""              # normalised plain text of exactly that digest: the ground truth for evidence checks


@dataclass
class PageSet:
    pages: list = field(default_factory=list)
    failures: list = field(default_factory=list)       # [{"url","kind","error_kind","error"}]
    error_kind: str = ""                                # set when the HOME page itself could not be used
    error: str = ""


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


async def collect_pages(fetcher, ident: Identity, *, max_pages: int = 4) -> PageSet:
    """Home first; then the best About, Services and Contact link found on it (at most max_pages in total). Never guesses URLs."""
    ps = PageSet()
    home = await fetcher.fetch(ident.start_url)
    if (not home.ok and home.error_kind in ("network", "timeout", "tls") and ident.scheme_assumed
            and ident.start_url.startswith("https://") and "DNS" not in home.error):
        home = await fetcher.fetch("http://" + ident.start_url[len("https://"):])
    if not home.ok:
        ps.error_kind, ps.error = home.error_kind or f"http_{home.status}", home.error or f"HTTP {home.status}"
        return ps
    final = home.final_url or home.url
    host = _host(final)
    if not same_site(host, ident.key):
        ps.error_kind, ps.error = "redirected_off_domain", f"the site redirected to {host}; nothing from it is attributed to this company"
        return ps
    ex = extract_page(home.text, final, "home", ident.key)
    ps.pages.append(Page(final, "home", home.text))
    fetched = {final}
    for kind in KIND_ORDER:
        if len(ps.pages) >= max_pages:
            break
        urls = [u for u in ex.links.get(kind, []) if u not in fetched]
        if not urls:
            continue
        fetched.add(urls[0])
        r = await fetcher.fetch(urls[0])
        rfinal = r.final_url or r.url
        if not r.ok:
            ps.failures.append({"url": urls[0], "kind": kind, "error_kind": r.error_kind or f"http_{r.status}", "error": r.error or f"HTTP {r.status}"})
        elif not same_site(_host(rfinal), ident.key):
            ps.failures.append({"url": urls[0], "kind": kind, "error_kind": "redirected_off_domain", "error": "page redirected to another site"})
        else:
            ps.pages.append(Page(rfinal, kind, r.text))
    return ps


def build_document(pages: list, total_chars: int) -> tuple[str, dict[str, str], list[str]]:
    """-> (HTML for the model, {url: normalised text the model was shown for that page}, warnings). The document starts with '<' so it can never be
    mistaken for a URL by the library. Page headings are added AFTER the evidence texts are computed, so a model that quotes our own
    'PAGE (kind): url' heading cannot 'verify' anything."""
    per = max(1500, total_chars // max(1, len(pages)))
    texts: dict[str, str] = {}
    parts, warnings = [], []
    for p in pages:
        clean_html, w = sanitize_html(p.html)
        warnings += [f"{p.url}: {x}" for x in w]
        p.digest = make_digest(clean_html, per)
        soup = BeautifulSoup(p.digest, "lxml")
        p.text = norm(soup.get_text(" "))
        texts[p.url] = p.text
        inner = (soup.body.decode_contents() if soup.body else p.digest)
        parts.append(f"<section><h2>PAGE ({p.kind}): {p.url}</h2>{inner}</section>")
    return "<html><body>" + "".join(parts) + "</body></html>", texts, warnings
