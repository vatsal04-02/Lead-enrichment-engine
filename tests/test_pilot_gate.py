"""Safety gate: every control the pilot promises, checked under the settings `pilot.live_settings()` actually produces.

Relaxations used by the tests (and ONLY these): `host_overrides` for the one fake hostname a test must reach on loopback,
and `per_domain_min_delay=0` in tests that are not about spacing (spacing itself is asserted separately with the real value).
SSRF address rules, robots, limits, retries, deadlines and size caps are the pilot's own."""
import argparse
import asyncio
import time

import pytest
import pytest_asyncio

from benchmarks import pilot
from leadengine.engine import Engine
from tests.conftest import DB_URL
from tests.rawserver import serve

UA = "PilotGateTest/1.0 (+https://operator.example/bot; ops@operator.example)"
HTML = b"<html><head><title>T</title></head><body><a href='mailto:hello@%s'>m</a></body></html>"


def live(monkeypatch, **override):
    monkeypatch.setenv("LE_USER_AGENT", UA)
    monkeypatch.delenv("LE_HOST_OVERRIDES", raising=False)
    a = argparse.Namespace(database_url=DB_URL, concurrency=4, min_delay=0.0, max_pages=3, retries=1, allow_browser=False,
                           browser_executable="", ai=False, ai_model="m", ai_price_in=None, ai_price_out=None)
    s = pilot.live_settings(a)
    return s.model_copy(update=override) if override else s


async def started(settings):
    eng = Engine(settings)
    await eng.start()
    async with eng.db.connection() as c:
        await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs, fetch_log RESTART IDENTITY CASCADE")
    return eng


def test_forced_settings_match_the_documented_gate(monkeypatch):
    s = live(monkeypatch)
    assert s.respect_robots and s.host_overrides == "" and s.egress_proxy is None
    assert s.per_domain_min_delay >= 2.0 and s.per_domain_concurrency == 1 and s.global_concurrency <= 4
    assert s.max_pages_per_company <= 3 and s.http_max_retries <= 1 and s.max_redirects <= 3
    assert s.total_timeout <= 20 and s.read_timeout <= 10 and s.connect_timeout <= 5 and s.max_response_bytes <= 1_000_000
    assert s.port_set() == {80, 443} and not s.browser_enabled and s.ai_provider == "" and s.fetch_audit_log
    assert s.user_agent == UA
    # operators cannot loosen any of it from the command line
    loose = argparse.Namespace(database_url=DB_URL, concurrency=500, min_delay=0.0, max_pages=99, retries=99, allow_browser=False,
                               browser_executable="", ai=False, ai_model="m", ai_price_in=None, ai_price_out=None)
    t = pilot.live_settings(loose)
    assert (t.global_concurrency, t.max_pages_per_company, t.http_max_retries) == (4, 3, 1) and t.per_domain_min_delay == 2.0


async def test_internal_destinations_are_refused_under_pilot_settings(monkeypatch):
    hit = []

    async def h(r, w):
        hit.append(1)
        w.close()
    srv = await asyncio.start_server(h, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    eng = await started(live(monkeypatch, per_domain_min_delay=0.0, allowed_ports="80,443"))
    try:
        for url in [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/", f"http://[::1]:{port}/", f"http://[::ffff:127.0.0.1]:{port}/",
                    f"http://2130706433:{port}/", f"http://0x7f.1:{port}/", "http://169.254.169.254/latest/meta-data/",
                    "http://metadata.google.internal/computeMetadata/v1/", "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
                    "http://100.64.0.1/", "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/", "https://example.com:6379/"]:
            r = await eng.fetcher.fetch(url, use_robots=False)
            assert r.error_kind == "blocked", (url, r.error_kind, r.error)
        await asyncio.sleep(0.2)
        assert hit == []                                             # not one TCP connection reached the local listener
    finally:
        await eng.stop(); srv.close()


async def test_redirects_to_internal_targets_are_refused_per_hop(monkeypatch):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        return "302 Found", {"Location": "http://169.254.169.254/latest/meta-data/"}, b""
    async with serve(handler) as (port, hits):
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, host_overrides="origin.fixture.test=127.0.0.1"))
        try:
            r = await eng.fetcher.fetch(f"http://origin.fixture.test:{port}/")
            assert r.error_kind == "blocked"
            assert [p for _, p in hits] == ["/robots.txt", "/"]            # followed the redirect request, then refused the target
        finally:
            await eng.stop()


async def test_hostname_resolving_to_private_address_is_refused_and_connection_is_pinned(monkeypatch):
    import socket
    calls = {"n": 0}

    async def rebinding(self, host, port, **kw):
        calls["n"] += 1
        ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"        # second lookup would "rebind" to loopback
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    eng = await started(live(monkeypatch, per_domain_min_delay=0.0))
    try:
        monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", rebinding)
        from leadengine.security import GuardedBackend
        backend = GuardedBackend(eng.fetcher.resolver)
        dialled = []

        class Inner:
            async def connect_tcp(self, host, port, **kw):
                dialled.append(host)
                import httpcore
                raise httpcore.ConnectError("stop")
        backend._inner = Inner()
        with pytest.raises(Exception):
            await backend.connect_tcp("rebind.example", 443)
        assert dialled == ["93.184.216.34"] and calls["n"] == 1         # dialled the address that was validated; no second lookup
        # a name whose records include a private address is refused outright
        async def poisoned(self, host, port, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]
        monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", poisoned)
        r = await eng.fetcher.fetch("http://poisoned.example/", use_robots=False)
        assert r.error_kind == "blocked"
    finally:
        await eng.stop()


async def test_at_least_two_seconds_between_requests_to_one_host(monkeypatch):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"x.fixture.test"
    stamps = []

    async with serve(handler) as (port, hits):
        eng = await started(live(monkeypatch, host_overrides="paced.fixture.test=127.0.0.1"))    # real forced delay (>= 2 s)
        try:
            real = hits.append
            t0 = time.monotonic()
            await asyncio.gather(eng.fetcher.fetch(f"http://paced.fixture.test:{port}/a"), eng.fetcher.fetch(f"http://paced.fixture.test:{port}/b"))
            # robots.txt + 2 pages = 3 requests => at least two enforced gaps of 2 s
            assert time.monotonic() - t0 >= 3.9
            assert len(hits) == 3
        finally:
            await eng.stop()


async def test_robots_disallow_prevents_any_page_request(monkeypatch):
    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n"
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"x.fixture.test"
    async with serve(handler) as (port, hits):
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, host_overrides="r.fixture.test=127.0.0.1"))
        try:
            r = await eng.enricher.enrich(f"http://r.fixture.test:{port}/", requested=["email"])
            assert r.outcome == "unreachable" and r.error_kind == "robots"
            assert [p for _, p in hits] == ["/robots.txt"]
        finally:
            await eng.stop()


async def test_oversized_response_redirect_chain_and_retry_budget(monkeypatch):
    big = b"<html><body>" + b"x" * 1_200_000 + b"</body></html>"

    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        if path == "/big":
            return "200 OK", {"Content-Type": "text/html"}, big
        if path.startswith("/hop"):
            n = int(path[4:] or 0)
            return "302 Found", {"Location": f"http://lim.fixture.test:{PORT[0]}/hop{n + 1}"}, b""
        if path == "/flaky":
            return "503 Service Unavailable", {}, b"x"
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"x.fixture.test"
    PORT = [0]
    async with serve(handler) as (port, hits):
        PORT[0] = port
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, backoff_base=0.01, host_overrides="lim.fixture.test=127.0.0.1"))
        try:
            base = f"http://lim.fixture.test:{port}"
            assert (await eng.fetcher.fetch(base + "/big")).error_kind == "too_large"            # 1 MB cap
            r = await eng.fetcher.fetch(base + "/hop0")
            assert r.error_kind == "redirect_loop" and not r.transient                            # 3-hop cap, permanent
            assert sum(1 for _, p in hits if p.startswith("/hop")) == 4                           # initial + 3 redirects, then stop
            r = await eng.fetcher.fetch(base + "/flaky")
            assert r.error_kind == "http" and r.status == 503 and r.retries == 1
            assert sum(1 for _, p in hits if p == "/flaky") == 2                                  # one retry, never more
        finally:
            await eng.stop()


async def test_access_controls_and_challenges_are_not_retried_or_bypassed(monkeypatch):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        if host == "login.fixture.test":
            return "401 Unauthorized", {"WWW-Authenticate": "Basic"}, b"no"
        if host == "forbid.fixture.test":
            return "403 Forbidden", {}, b"no"
        return "200 OK", {"Content-Type": "text/html"}, b"<html><title>Just a moment...</title>Checking your browser before accessing</html>"
    async with serve(handler) as (port, hits):
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, host_overrides="*.fixture.test=127.0.0.1"))
        try:
            for host, kind in (("login", "http"), ("forbid", "http"), ("chal", "bot_challenge")):
                r = await eng.enricher.enrich(f"http://{host}.fixture.test:{port}/", requested=["email"])
                assert r.outcome == "unreachable" and r.error_kind == kind and r.metrics["browser_ops"] == 0
                assert sum(1 for h, p in hits if h == f"{host}.fixture.test" and p == "/") == 1, host     # exactly one attempt
        finally:
            await eng.stop()


async def test_page_cap_and_audit_and_attribution_end_to_end(monkeypatch):
    """A site with many candidate pages and no data: at most 3 pages per company; every request is logged; nothing is invented."""
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        links = "".join(f"<a href='/{k}'>{k}</a>" for k in ("about", "contact", "team", "services", "locations"))
        return "200 OK", {"Content-Type": "text/html"}, f"<html><head><title>x</title></head><body>{links}<p>Hello</p></body></html>".encode()
    async with serve(handler) as (port, hits):
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, host_overrides="cap.fixture.test=127.0.0.1"))
        try:
            r = await eng.enricher.enrich(f"http://cap.fixture.test:{port}/", requested=["company_name", "email", "phone", "address", "description", "industry"],
                                          job_id=None)
            pages = [p for _, p in hits if p != "/robots.txt"]
            assert len(pages) <= 3, pages
            await eng.audit.flush()
            async with eng.db.connection() as c:
                logged = [x["url"].split(str(port))[-1] for x in await (await c.execute("SELECT url FROM fetch_log ORDER BY id")).fetchall()]
                vals = await (await c.execute("SELECT field, value, status, source_url, method FROM company_fields WHERE field <> 'website'")).fetchall()
            assert sorted(logged) == sorted(["/robots.txt"] + pages)                                   # every request on record
            assert all(v["value"] is None and v["status"] == "not_found" for v in vals)                 # nothing invented
        finally:
            await eng.stop()


async def test_every_stored_value_has_a_source_url_and_method(monkeypatch):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        return "200 OK", {"Content-Type": "text/html"}, (b"<html><head><title>Home | Acme</title><meta name='description' content='Acme builds widgets for businesses everywhere.'>"
                                                          b"</head><body><a href='mailto:hello@acme.fixture.test'>m</a><a href='tel:+15125550100'>t</a></body></html>")
    async with serve(handler) as (port, _):
        eng = await started(live(monkeypatch, per_domain_min_delay=0.0, host_overrides="acme.fixture.test=127.0.0.1"))
        try:
            await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["company_name", "email", "phone", "description"])
            async with eng.db.connection() as c:
                rows = await (await c.execute("SELECT field, value, source_url, method, extracted_at, refresh_after, verification_status FROM company_fields WHERE status='found'")).fetchall()
            assert {r["field"] for r in rows} >= {"company_name", "email", "phone", "description", "website"}
            assert all(r["source_url"].startswith("http://acme.fixture.test") and r["method"] and r["extracted_at"] and r["refresh_after"] for r in rows)
            assert next(r for r in rows if r["field"] == "email")["verification_status"] == "unverified"        # never claimed verified
        finally:
            await eng.stop()


def test_ai_flags_fail_before_any_site_is_contacted(tmp_path, monkeypatch):
    import csv
    p = tmp_path / "s.csv"
    p.write_text("domain,name,permission_basis,reviewed_by\nacme-example.org,A,own website operated by the pilot owner,alice\n")
    started = []
    monkeypatch.setattr(pilot, "Engine", lambda *a, **k: started.append(1))
    monkeypatch.setenv("LE_USER_AGENT", UA)
    monkeypatch.delenv("LE_HOST_OVERRIDES", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    base = dict(sample=str(p), fixtures=False, confirm_permitted=True, confirm_sites=1, out_dir=str(tmp_path), reset=False,
                database_url="postgresql://x@h/enrich_pilot", concurrency=2, min_delay=3.0, max_pages=3, retries=1, max_seconds=60,
                allow_browser=False, browser_executable="", ai=True, ai_model="m", ai_price_in=None, ai_price_out=None,
                compute_usd_per_hour=0.0, audit_per_field=30)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        asyncio.run(pilot.run(argparse.Namespace(**base)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    with pytest.raises(SystemExit, match="ai-price-in"):
        asyncio.run(pilot.run(argparse.Namespace(**base)))
    assert not started


def test_database_url_has_no_silent_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LE_DATABASE_URL", raising=False)
    monkeypatch.setattr(pilot, "Engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("engine must not start")))
    a = argparse.Namespace(sample=None, fixtures=True, fixture_sites=3, confirm_permitted=False, confirm_sites=-1, database_url=None,
                           reset=False, concurrency=1, min_delay=0.0, max_pages=3, retries=1, max_seconds=10, allow_browser=False,
                           browser_executable="", ai=False, ai_model="m", ai_price_in=None, ai_price_out=None,
                           compute_usd_per_hour=0.0, audit_per_field=5, out_dir=str(tmp_path))
    with pytest.raises(SystemExit, match="LE_DATABASE_URL"):
        asyncio.run(pilot.run(a))
