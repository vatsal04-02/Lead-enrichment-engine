"""Regression tests for the test-isolation guard itself (tests/netguard.py).

The discovery dry-run tests must prove "nothing outside this machine was contacted" WITHOUT tripping on asyncio's own start-up, which on
Windows connects a loopback socket pair. These tests pin both halves: loopback/event-loop initialisation works, and a simulated
prohibited request is still rejected (and recorded even when the caller swallows the error). No test here sends a packet off this machine:
the guard refuses before the real call, and prohibited targets are TEST-NET/documentation addresses and reserved names.
"""
import asyncio
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from leadengine.discovery.models import SRC_SEED
from leadengine.discovery.pipeline import RunPlan, SYNTH_LABEL, execute
from leadengine.discovery.safety import forced_settings
from leadengine.discovery.sources.seed_pages import SeedPagesSource
from tests.netguard import (NetworkGuard, ProhibitedNetworkAccess, emulated_windows_socketpair, is_loopback_host, netguard)  # noqa: F401

TEST_NET = "203.0.113.9"        # RFC 5737 documentation range: never routable, and the guard refuses before any packet is sent


# ------------------------------------------------------------------ the half that must keep working: loopback and event-loop start-up
def test_asyncio_run_starts_under_the_guard_using_the_windows_style_loopback_socketpair(netguard):
    assert socket.socketpair is emulated_windows_socketpair or sys.platform == "win32"      # the Windows start-up path is exercised everywhere
    ran = []

    async def main():
        ran.append(True)
        await asyncio.sleep(0)
    asyncio.run(main())
    assert ran == [True] and netguard.attempts == []


def test_loopback_tcp_is_allowed_for_blocking_and_asyncio_clients(netguard):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:        # create_connection -> getaddrinfo + connect, both loopback
            conn, _ = srv.accept()
            conn.sendall(b"hi")
            assert c.recv(2) == b"hi"
            conn.close()

        async def aio():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await w.wait_closed()
        asyncio.run(aio())
        assert socket.getaddrinfo("localhost", port) and socket.gethostbyname("localhost").startswith("127.")
    finally:
        srv.close()
    assert netguard.attempts == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX on this platform (Windows)")
def test_unix_domain_sockets_are_local_ipc_and_allowed(netguard, tmp_path):
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen()
    c = socket.socket(socket.AF_UNIX)
    c.connect(path)
    c.close()
    srv.close()
    assert netguard.attempts == []


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("127.1.2.3", True), ("::1", True), ("[::1]", True), ("localhost", True),
                                           ("LOCALHOST", True), ("::ffff:127.0.0.1", True), (None, True), (b"127.0.0.1", True),
                                           ("example.org", False), ("203.0.113.9", False), ("10.0.0.5", False), ("192.168.1.1", False),
                                           ("0.0.0.0", False), ("169.254.169.254", False), ("127.0.0.1.evil.example", False), ("", False)])
def test_loopback_classification(host, expected):
    assert is_loopback_host(host) is expected


# ------------------------------------------------------------------ the half that must keep failing: prohibited requests
def test_outbound_calls_are_rejected_before_any_packet_and_recorded(netguard):
    s = socket.socket()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        calls = {
            "connect": lambda: s.connect((TEST_NET, 80)),
            "connect_ex": lambda: s.connect_ex((TEST_NET, 80)),
            "sendto": lambda: u.sendto(b"x", (TEST_NET, 53)),
            "create_connection": lambda: socket.create_connection(("example.org", 80), timeout=1),
            "getaddrinfo": lambda: socket.getaddrinfo("example.org", 80),
            "gethostbyname": lambda: socket.gethostbyname("example.org"),
            "gethostbyname_ex": lambda: socket.gethostbyname_ex("example.org"),
        }
        for name, call in calls.items():
            with pytest.raises(ProhibitedNetworkAccess, match="prohibited network access"):
                call()
    finally:
        s.close()
        u.close()
    assert len(netguard.attempts) >= len(calls)
    assert any(TEST_NET in a for a in netguard.attempts) and any("example.org" in a for a in netguard.attempts)
    netguard.attempts.clear()                                   # these were deliberate probes: do not fail the teardown check


def test_asyncio_and_http_client_requests_to_outside_hosts_are_rejected(netguard):
    import httpx

    async def go():
        for coro in (asyncio.open_connection(TEST_NET, 80), asyncio.get_running_loop().getaddrinfo("example.org", 80)):
            with pytest.raises(BaseException) as ei:
                await asyncio.wait_for(coro, 2)
            assert isinstance(ei.value, ProhibitedNetworkAccess) or "prohibited network access" in str(ei.value)
        async with httpx.AsyncClient(timeout=2) as c:
            with pytest.raises(BaseException):
                await c.get(f"http://{TEST_NET}/")
    asyncio.run(go())
    assert netguard.attempts, "the attempts must be recorded"
    netguard.attempts.clear()


def test_a_swallowed_prohibited_attempt_still_fails_the_test_at_teardown(monkeypatch):
    g = NetworkGuard().install(monkeypatch)
    try:
        socket.getaddrinfo("example.org", 80)
    except Exception:
        pass                                                    # code under test hides the error...
    with pytest.raises(AssertionError, match="may have been swallowed"):
        g.assert_clean()                                        # ...but the fixture teardown still catches it


def test_the_old_blanket_connect_ban_really_did_fail_asyncio_startup_on_the_windows_socketpair(tmp_path):
    """Documents the bug this guard fixes. Runs in a subprocess so the broken loop cannot affect this test session."""
    script = textwrap.dedent(f"""
        import asyncio, socket, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from tests.netguard import emulated_windows_socketpair
        socket.socketpair = emulated_windows_socketpair                  # what asyncio calls at start-up on Windows
        def refuse(*a, **k): raise AssertionError("network access attempted during a dry run")
        socket.socket.connect = refuse                                   # the old fixture
        try:
            asyncio.run(asyncio.sleep(0))
        except AssertionError:
            print("OLD-GUARD-FALSE-POSITIVE")
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert "OLD-GUARD-FALSE-POSITIVE" in r.stdout, r.stdout + r.stderr


# ------------------------------------------------------------------ the guard is sensitive on the REAL discovery code path
async def test_an_authorized_plan_that_reaches_for_an_outside_host_trips_the_guard_but_a_dry_run_does_not(tmp_path, netguard):
    """Positive control: the same plan that the dry-run tests run, but authorized, DOES try to resolve/contact the seed host - and the
    guard sees it. So 'attempts == []' in the dry-run tests is meaningful, not vacuous."""
    def plan(authorized):
        return RunPlan(command="discover", sources=[SeedPagesSource(["http://listing.example.org/ai-agencies"])], authorized=authorized,
                       settings=forced_settings(database_url="postgresql://u@h/db", user_agent="x/1 (+https://a.b; a@b.c)", min_delay=0.0),
                       out_root=str(tmp_path / ("live" if authorized else "dry")), mode_label=SYNTH_LABEL)
    dry = await execute(plan(False))
    assert dry["candidates"] == 0 and netguard.attempts == []
    live = await execute(plan(True))                           # contacts nothing real: the guard refuses; the failure is recorded as a source error
    assert netguard.attempts, "an authorized run must have attempted a lookup/connection, and the guard must have seen it"
    assert live["discovery"].errors and live["discovery"].errors[0].source == SRC_SEED
    netguard.attempts.clear()
