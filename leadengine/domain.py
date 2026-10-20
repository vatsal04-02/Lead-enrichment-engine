"""Company identity: normalise a website/domain into the stable key used everywhere."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # bundled snapshot, no network

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


class InvalidDomain(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    key: str            # stable unique key, e.g. "acme.com"
    host: str           # host to crawl, e.g. "www.acme.com" or "acme.com"
    start_url: str      # normalised homepage URL
    scheme_assumed: bool = False  # input had no scheme: https was assumed, http is the fallback


def registrable(host: str) -> str:
    ext = _extract(host)
    if ext.top_domain_under_public_suffix:
        return ext.top_domain_under_public_suffix.lower()
    return host.lower()


@lru_cache(maxsize=200_000)  # normalised-identity cache: cheap, bounded, process-local
def normalize(website_or_domain: str) -> Identity:
    raw = (website_or_domain or "").strip()
    if not raw:
        raise InvalidDomain("empty website/domain")
    if "@" in raw and "/" not in raw:
        raise InvalidDomain("looks like an email address, not a website")
    has_scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw))
    candidate = raw if has_scheme else "https://" + raw
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise InvalidDomain(f"unsupported scheme {parts.scheme!r}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InvalidDomain("no host found")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidDomain("invalid hostname")
    try:
        ipaddress.ip_address(host)
        raise InvalidDomain("IP addresses are not accepted as company domains")
    except ValueError as e:
        if isinstance(e, InvalidDomain):
            raise
    if not _DOMAIN_RE.match(host):
        raise InvalidDomain(f"{host!r} is not a valid domain name")
    key = registrable(host)
    scheme = parts.scheme
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return Identity(key=key, host=host, start_url=f"{scheme}://{host}{port}/",
                    scheme_assumed=not has_scheme)


def same_site(url_host: str, key: str) -> bool:
    return registrable((url_host or "").lower()) == key


def name_slug(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())
