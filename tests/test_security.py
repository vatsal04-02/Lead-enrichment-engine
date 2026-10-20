import ipaddress
import socket

import httpcore
import pytest

from leadengine.config import Settings
from leadengine.fetcher import Fetcher
from leadengine.security import (BlockedDestination, GuardedBackend, Resolver, ip_is_forbidden, validate_url)

S = Settings(database_url="postgresql://x")


@pytest.mark.parametrize("url", [
    "ftp://example.com/", "file:///etc/passwd", "gopher://example.com", "http://localhost/", "http://foo.localhost/",
    "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fd00:ec2::254]/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://metadata.google.internal/", "http://user:pw@example.com/", "http://example.com:22/", "http://example.com:6379/",
    "http://printer.local/", "http:///nohost", "http://[2002:7f00:1::]/",
])
def test_validate_url_blocks(url):
    with pytest.raises(BlockedDestination):
        validate_url(url, S)


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com:8080/x", "https://sub.example.co.uk/a?b=1"])
def test_validate_url_allows(url):
    assert validate_url(url, S).host


@pytest.mark.parametrize("ip,bad", [("8.8.8.8", False), ("1.1.1.1", False), ("127.0.0.1", True), ("10.1.2.3", True),
                                    ("169.254.169.254", True), ("::1", True), ("::ffff:10.0.0.1", True),
                                    ("2606:4700:4700::1111", False), ("100.100.100.200", True), ("224.0.0.1", True)])
def test_ip_classification(ip, bad):
    assert ip_is_forbidden(ipaddress.ip_address(ip)) is bad


@pytest.mark.asyncio
async def test_hostname_resolving_to_private_is_blocked(monkeypatch):
    r = Resolver(S)

    async def fake(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]   # one bad record poisons the host
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake)
    with pytest.raises(BlockedDestination):
        await r.resolve_public("rebind.example", 80)


@pytest.mark.asyncio
async def test_connection_is_pinned_to_the_validated_ip(monkeypatch):
    """DNS rebinding: a second lookup would return 127.0.0.1; we must never perform one."""
    calls = {"n": 0}

    async def fake(self, host, port, **kw):
        calls["n"] += 1
        ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", fake)
    backend = GuardedBackend(Resolver(S))
    dialled = []

    class Inner:
        async def connect_tcp(self, host, port, **kw):
            dialled.append(host)
            raise httpcore.ConnectError("stop")
    backend._inner = Inner()
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("rebind.example", 80)
    assert dialled == ["93.184.216.34"] and calls["n"] == 1


@pytest.mark.asyncio
async def test_fetcher_refuses_private_targets_end_to_end(fixtures):
    fixtures.state.reset()
    f = Fetcher(Settings(database_url="postgresql://x", respect_robots=False,
                         allowed_ports=str(fixtures.port)), db=None)     # NO host_overrides: guard fully on
    try:
        site = next(iter(fixtures.state.sites.values()))
        res = await f.fetch(f"http://127.0.0.1:{fixtures.port}/")
        assert res.error_kind == "blocked"
        res = await f.fetch("http://169.254.169.254/latest/meta-data/")
        assert res.error_kind == "blocked"
        res = await f.fetch("file:///etc/passwd")
        assert res.error_kind == "blocked"
        assert fixtures.state.total() == 0
    finally:
        await f.aclose()
