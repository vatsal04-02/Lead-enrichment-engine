"""Regression tests for defects found in the Stage-4 audit. Each failed before its fix."""
import asyncio
import time

import pytest

from leadengine.config import Settings
from leadengine.engine import Engine
from tests.conftest import fresh_engine, make_settings
from tests.rawserver import serve

HTML = b"<html><head><title>T</title></head><body><a href='mailto:hello@%s'>m</a></body></html>"


async def stored(eng, domain, field):
    async with eng.db.connection() as c:
        return await (await c.execute(
            "SELECT f.* FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE c.domain=%s AND f.field=%s",
            (domain, field))).fetchone()


# ------------------------------------------------------------------ hard per-request deadline
async def test_slow_drip_response_hits_total_timeout(fixtures):
    async def drip():
        for _ in range(100):
            yield b"<p>x</p>"
            await asyncio.sleep(0.5)

    def handler(host, path):
        return "200 OK", {"Content-Type": "text/html"}, drip()
    async with serve(handler) as (port, _):
        eng = await fresh_engine(fixtures, total_timeout=1.5, read_timeout=1.0, respect_robots=False, http_max_retries=0)
        try:
            t0 = time.monotonic()
            r = await eng.fetcher.fetch(f"http://drip.fixture.test:{port}/")
            assert time.monotonic() - t0 < 4 and r.error_kind == "timeout" and r.transient
        finally:
            await eng.stop()


# ------------------------------------------------------------------ redirect to a different company
async def test_off_domain_redirect_is_not_attributed_to_the_input_company(fixtures):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        if host == "acme.fixture.test":
            return "302 Found", {"Location": f"http://other-co.fixture.test:{PORT[0]}/"}, b""
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"other-co.fixture.test"
    PORT = [0]
    async with serve(handler) as (port, hits):
        PORT[0] = port
        eng = await fresh_engine(fixtures)
        try:
            r = await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["email", "company_name"])
            assert r.outcome == "unreachable" and r.error_kind == "redirected_off_domain"
            assert await stored(eng, "acme.fixture.test", "email") is None             # nothing from the other company
            w = await stored(eng, "acme.fixture.test", "website")
            assert w["verification_status"] == "failed" and "other-co.fixture.test" in w["note"]
            n = len(hits)
            r2 = await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["email", "company_name"])
            assert r2.outcome == "unreachable" and len(hits) == n                       # remembered; no refetch
        finally:
            await eng.stop()


async def test_same_registrable_domain_redirect_still_works(fixtures):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        if host == "acme.fixture.test":
            return "301 Moved", {"Location": f"http://www.acme.fixture.test:{PORT[0]}/"}, b""
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"acme.fixture.test"
    PORT = [0]
    async with serve(handler) as (port, _):
        PORT[0] = port
        eng = await fresh_engine(fixtures)
        try:
            r = await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["email"])
            assert (await stored(eng, "acme.fixture.test", "email"))["value"] == "hello@acme.fixture.test"
        finally:
            await eng.stop()


# ------------------------------------------------------------------ robots.txt edge cases
@pytest.mark.parametrize("status,expect_blocked", [("403 Forbidden", True), ("401 Unauthorized", True),
                                                   ("404 Not Found", False), ("410 Gone", False)])
async def test_robots_authz_errors_mean_disallow_other_4xx_mean_allow(fixtures, status, expect_blocked):
    def handler(host, path):
        if path == "/robots.txt":
            return status, {"Content-Type": "text/plain"}, b"x"
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"a.fixture.test"
    async with serve(handler) as (port, hits):
        eng = await fresh_engine(fixtures)
        try:
            r = await eng.fetcher.fetch(f"http://rb.fixture.test:{port}/")
            if expect_blocked:
                assert r.error_kind == "robots" and not r.transient
                assert ("rb.fixture.test", "/") not in hits           # never requested the page
            else:
                assert r.ok
        finally:
            await eng.stop()


async def test_crawl_delay_is_honoured(fixtures):
    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, b"User-agent: *\nCrawl-delay: 1\n"
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"a.fixture.test"
    async with serve(handler) as (port, hits):
        eng = await fresh_engine(fixtures, per_domain_min_delay=0.0)
        try:
            t0 = time.monotonic()
            await eng.fetcher.fetch(f"http://cd.fixture.test:{port}/a")
            await eng.fetcher.fetch(f"http://cd.fixture.test:{port}/b")
            assert time.monotonic() - t0 >= 0.95
        finally:
            await eng.stop()


async def test_excessive_crawl_delay_means_skip_site(fixtures):
    def handler(host, path):
        if path == "/robots.txt":
            return "200 OK", {"Content-Type": "text/plain"}, b"User-agent: *\nCrawl-delay: 600\n"
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"a.fixture.test"
    async with serve(handler) as (port, hits):
        eng = await fresh_engine(fixtures)
        try:
            r = await eng.fetcher.fetch(f"http://slowcd.fixture.test:{port}/")
            assert r.error_kind == "robots" and "crawl-delay" in r.error.lower()
        finally:
            await eng.stop()


# ------------------------------------------------------------------ anti-bot challenges are respected, not fought
CHALLENGE = (b"<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing "
             b"<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/x.js'></script></body></html>")


@pytest.mark.parametrize("status,headers,body", [
    ("200 OK", {"Content-Type": "text/html"}, CHALLENGE),
    ("403 Forbidden", {"Content-Type": "text/html", "cf-mitigated": "challenge"}, CHALLENGE),
    ("503 Service Unavailable", {"Content-Type": "text/html", "cf-mitigated": "challenge"}, CHALLENGE),
])
async def test_bot_challenge_is_permanent_unreachable_without_browser_or_retry(fixtures, status, headers, body):
    def handler(host, path):
        if path == "/robots.txt":
            return "404 Not Found", {}, b""
        return status, headers, body
    async with serve(handler) as (port, hits):
        eng = await fresh_engine(fixtures, browser_enabled=True, http_max_retries=2, backoff_base=0.01)
        try:
            r = await eng.enricher.enrich(f"http://cf.fixture.test:{port}/", requested=["email", "company_name"])
            assert r.outcome == "unreachable" and r.error_kind == "bot_challenge" and not r.transient
            assert r.metrics["browser_ops"] == 0 and r.metrics["retries"] == 0
            assert sum(1 for h in hits if h[1] == "/") == 1
        finally:
            await eng.stop()


async def test_recaptcha_widget_on_a_normal_contact_page_is_not_a_challenge(fixtures):
    page = (b"<html><head><title>Contact | Acme</title></head><body><form><div class='g-recaptcha' data-sitekey='x'></div>"
            b"</form><a href='mailto:hello@acme.fixture.test'>mail</a></body></html>")

    def handler(host, path):
        return ("404 Not Found", {}, b"") if path == "/robots.txt" else ("200 OK", {"Content-Type": "text/html"}, page)
    async with serve(handler) as (port, _):
        eng = await fresh_engine(fixtures)
        try:
            r = await eng.enricher.enrich(f"http://acme.fixture.test:{port}/", requested=["email"])
            assert (await stored(eng, "acme.fixture.test", "email"))["value"] == "hello@acme.fixture.test"
        finally:
            await eng.stop()


# ------------------------------------------------------------------ identify yourself before crawling
async def test_placeholder_user_agent_refuses_to_start_without_overrides():
    from leadengine.engine import ConfigError
    s = Settings(database_url=make_settings().database_url)                  # default placeholder UA
    with pytest.raises(ConfigError, match="LE_USER_AGENT"):
        await Engine(s).start()
    eng = Engine(Settings(database_url=s.database_url, user_agent="MyBot/1.0 (+https://x.example/bot; ops@x.example)"))
    await eng.start()
    await eng.stop()


# ------------------------------------------------------------------ job budgets: clients cannot lift server limits
def test_client_budget_cannot_exceed_or_disable_server_limits():
    from leadengine import jobs
    s = Settings(budget_pages=1000, budget_ai_calls=50, ai_provider="anthropic", budget_seconds=600,
                 budget_est_cost_usd=5.0, budget_browser_ops=20, browser_enabled=True)
    b = jobs.default_budget(s, {"pages": 0, "seconds": 0, "est_cost_usd": 0, "ai_calls": 10**9, "browser_ops": 5})
    assert b == {"pages": 1000, "ai_calls": 50, "browser_ops": 5, "seconds": 600, "est_cost_usd": 5.0}
    assert jobs.default_budget(s, {"pages": 10})["pages"] == 10
    with pytest.raises(jobs.JobError):
        jobs.default_budget(s, {"pages": -1})
    with pytest.raises(jobs.JobError):
        jobs.default_budget(s, {"bogus": 1})
    with pytest.raises(jobs.JobError):
        jobs.default_budget(s, {"pages": "lots"})
    # an operator who configured "unlimited" (0) lets clients choose their own limit
    free = Settings(budget_pages=0)
    assert jobs.default_budget(free, {"pages": 7})["pages"] == 7


def test_invalid_configuration_is_rejected_at_startup():
    from pydantic import ValidationError
    for bad in [dict(global_concurrency=0), dict(queue_size=0), dict(per_domain_concurrency=0), dict(total_timeout=0),
                dict(job_heartbeat_seconds=60, job_lease_seconds=60), dict(max_response_bytes=0), dict(http_max_retries=-1),
                dict(claim_batch_size=0), dict(max_pages_per_company=0)]:
        with pytest.raises(ValidationError):
            Settings(**bad)


def test_secrets_never_appear_in_settings_repr_or_logs():
    s = Settings(api_token="tok-supersecret", database_url="postgresql://user:hunter2@db.internal/prod",
                 egress_proxy="http://pu:ppw@proxy:3128")
    shown = repr(s) + str(s) + str(s.model_dump())
    for secret in ("tok-supersecret", "hunter2", "ppw"):
        assert secret not in shown
    assert s.api_token.get_secret_value() == "tok-supersecret"
    from leadengine.security import sanitize_for_log
    out = sanitize_for_log("ConnectError via http://pu:ppw@proxy:3128 Authorization: Bearer abc.def-123 key sk-ant-api03-ZZZZ_yy")
    assert "ppw" not in out and "abc.def-123" not in out and "ZZZZ" not in out


# ------------------------------------------------------------------ TLS failures are classified, not retried
def _selfsigned(tmp_path):
    import subprocess
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=tls.fixture.test"], check=True, capture_output=True)
    return str(crt), str(key)


async def test_invalid_certificate_is_permanent_not_retried_and_not_downgraded(fixtures, tmp_path):
    import ssl, shutil
    if not shutil.which("openssl"):
        pytest.skip("openssl CLI not available")
    crt, key = _selfsigned(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    conns = []

    async def h(r, w):
        conns.append(1)
        w.close()
    srv = await asyncio.start_server(h, "127.0.0.1", 0, ssl=ctx)
    port = srv.sockets[0].getsockname()[1]
    eng = await fresh_engine(fixtures, http_max_retries=2, backoff_base=0.01, respect_robots=False)
    try:
        r = await eng.fetcher.fetch(f"https://tls.fixture.test:{port}/")
        assert r.error_kind == "tls_cert" and not r.transient and r.retries == 0
    finally:
        await eng.stop(); srv.close()


async def test_plain_http_server_on_https_url_is_a_single_non_retried_tls_failure(fixtures):
    def handler(host, path):
        return "200 OK", {"Content-Type": "text/html"}, HTML % b"a.fixture.test"
    async with serve(handler) as (port, hits):
        eng = await fresh_engine(fixtures, http_max_retries=2, backoff_base=0.01, respect_robots=False)
        try:
            r = await eng.fetcher.fetch(f"https://plain.fixture.test:{port}/")
            assert r.error_kind == "tls" and not r.transient and r.retries == 0
            # no scheme in the input => https first, then plain http as a documented fallback
            res = await eng.enricher.enrich(f"plain.fixture.test:{port}", requested=["email"])
            assert (await stored(eng, "plain.fixture.test", "email"))["value"] == "hello@a.fixture.test"
        finally:
            await eng.stop()
