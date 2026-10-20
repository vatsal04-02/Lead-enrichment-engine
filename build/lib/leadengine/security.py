"""SSRF protection and a pinned-IP HTTP transport.

Defence layers:
  1. validate_url(): scheme/port/userinfo/hostname checks before any I/O.
  2. GuardedBackend: resolves the hostname itself, rejects if ANY resolved address is
     non-public, then connects to the validated IP literal. Because the address that was
     checked is the address that is dialled, DNS rebinding between check and connect is
     impossible. This runs for every connection, including each redirect hop.
  3. The fetcher follows redirects manually and re-runs (1) on every hop.

With an explicit egress proxy the proxy performs the connection, so (2) degrades to a
pre-resolution check (check_host_resolves_public) and rebinding protection is the
proxy's responsibility. This is documented in the README.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpcore
import httpx

from .config import Settings

BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
BLOCKED_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data"}


class BlockedDestination(httpcore.ConnectError):
    """Raised when a URL or resolved address points somewhere we refuse to connect to."""


def ip_is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_forbidden(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return ip_is_forbidden(ip.sixtofour)
        if ip.teredo is not None:
            return True
    # is_global is False for private, loopback, link-local (incl. 169.254.169.254),
    # CGNAT 100.64/10, multicast, reserved, unspecified, documentation ranges.
    return (not ip.is_global) or ip.is_multicast or ip.is_reserved or ip.is_unspecified


@dataclass(frozen=True)
class CheckedURL:
    url: str
    scheme: str
    host: str
    port: int


def validate_url(url: str, settings: Settings) -> CheckedURL:
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        raise BlockedDestination(f"malformed URL: {e}") from e
    if parts.scheme not in ("http", "https"):
        raise BlockedDestination(f"unsupported scheme {parts.scheme!r}")
    if parts.username or parts.password:
        raise BlockedDestination("credentials in URL are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedDestination("missing host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise BlockedDestination("invalid port") from e
    if settings.override_for(host) is None:
        if port not in settings.port_set():
            raise BlockedDestination(f"port {port} not allowed")
        if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
            raise BlockedDestination(f"host {host!r} is internal")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and ip_is_forbidden(ip):
            raise BlockedDestination(f"address {host} is not public")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise BlockedDestination("invalid hostname") from e
    return CheckedURL(url=url, scheme=parts.scheme, host=host, port=port)


class Resolver:
    """getaddrinfo with a short positive cache and optional host overrides (fixtures only)."""

    def __init__(self, settings: Settings, ttl: float = 60.0):
        self.settings = settings
        self.ttl = ttl
        self._cache: dict[tuple[str, int], tuple[float, list[str]]] = {}

    async def resolve(self, host: str, port: int) -> list[str]:
        key = (host, port)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        ov = self.settings.override_for(host)
        if ov:
            return [ov]
        try:
            ipaddress.ip_address(host)
            addrs = [host]
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror as e:
                raise httpcore.ConnectError(f"DNS resolution failed for {host}: {e}") from e
            addrs = []
            for fam, _, _, _, sa in infos:
                if sa[0] not in addrs:
                    addrs.append(sa[0])
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[key] = (now + self.ttl, addrs)
        return addrs

    async def resolve_public(self, host: str, port: int) -> list[str]:
        addrs = await self.resolve(host, port)
        if not addrs:
            raise httpcore.ConnectError(f"no addresses for {host}")
        if self.settings.override_for(host) is None:
            for a in addrs:
                if ip_is_forbidden(ipaddress.ip_address(a)):
                    raise BlockedDestination(f"{host} resolves to non-public address {a}")
        return addrs


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver: Resolver):
        self.resolver = resolver
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = await self.resolver.resolve_public(host, port)
        last: Optional[Exception] = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(addr, port, timeout=timeout,
                                                     local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedDestination("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool dials only validated, pinned addresses."""

    def __init__(self, settings: Settings, resolver: Resolver):
        super().__init__(http2=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=30.0,
            http1=True,
            http2=False,
            network_backend=GuardedBackend(resolver),
        )


def build_http_client(settings: Settings) -> tuple[httpx.AsyncClient, Resolver]:
    """One shared, pooled client. Redirects are handled by the fetcher, not httpx."""
    resolver = Resolver(settings)
    timeout = httpx.Timeout(settings.total_timeout, connect=settings.connect_timeout,
                            read=settings.read_timeout)
    headers = {"User-Agent": settings.user_agent,
               "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
               "Accept-Language": "en;q=0.8,*;q=0.5"}
    limits = httpx.Limits(max_connections=settings.http_max_connections,
                          max_keepalive_connections=settings.http_max_keepalive)
    if settings.egress_proxy:
        client = httpx.AsyncClient(proxy=settings.egress_proxy, timeout=timeout, headers=headers,
                                   limits=limits, follow_redirects=False, trust_env=False)
    else:
        client = httpx.AsyncClient(transport=GuardedTransport(settings, resolver), timeout=timeout,
                                   headers=headers, follow_redirects=False, trust_env=False)
    return client, resolver


def sanitize_for_log(text: str, limit: int = 300) -> str:
    """Strip control characters from untrusted strings before logging/storing errors."""
    return "".join(c if c.isprintable() else " " for c in text)[:limit]
