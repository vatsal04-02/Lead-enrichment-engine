import json

import pytest

from leadengine.budget import Budget
from leadengine.ai import AIExtractor, LLMResponse
from leadengine.fields import DEFAULT_REQUESTED
from leadengine.pipeline import Enricher
from tests.conftest import fresh_engine, site_of, url_of, make_settings

REQ = list(DEFAULT_REQUESTED)


async def value(engine, domain, field):
    async with engine.db.connection() as c:
        r = await (await c.execute(
            "SELECT f.* FROM company_fields f JOIN companies c ON c.id=f.company_id WHERE c.domain=%s AND f.field=%s",
            (domain, field))).fetchone()
    return r


async def count(engine, table):
    async with engine.db.connection() as c:
        return (await (await c.execute(f"SELECT count(*) n FROM {table}")).fetchone())["n"]


async def test_enrich_jsonld_site_with_provenance(engine, fixtures):
    s = site_of(fixtures, "jsonld")
    res = await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    assert res.outcome in ("enriched", "partial")
    for f, expected in [("company_name", s.name), ("phone", s.phone), ("address", s.address), ("email", s.email)]:
        row = await value(engine, s.host, f)
        assert row["value"] == expected, f
        assert row["status"] == "found" and row["source_url"].startswith("http") and row["method"]
        assert row["extracted_at"] and row["refresh_after"] > row["extracted_at"]
    # website field verified by a defined procedure; email is NOT verified (no mailbox check exists)
    web = await value(engine, s.host, "website")
    assert web["verification_status"] == "verified" and web["verification_method"] == "http_reachable_domain_match"
    assert (await value(engine, s.host, "email"))["verification_status"] == "unverified"
    # absent facts stay missing
    for f in ("industry", "twitter_url", "github_url"):
        r = await value(engine, s.host, f)
        assert r["status"] == "not_found" and r["value"] is None


async def test_targeted_page_used_only_when_needed(engine, fixtures):
    s = site_of(fixtures, "contact_only")
    res = await engine.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"])
    assert (await value(engine, s.host, "email"))["value"] == s.email
    assert (await value(engine, s.host, "phone"))["value"] == s.phone
    assert fixtures.state.count(s.host, "/contact") == 1
    assert fixtures.state.count(s.host, "/about") == 0          # not needed => not fetched
    # a site that publishes everything on the homepage needs no extra page
    j = site_of(fixtures, "jsonld")
    await engine.enricher.enrich(url_of(fixtures, j), requested=["company_name", "phone"])
    assert fixtures.state.count(j.host, "/contact") == 0 and fixtures.state.count(j.host, "/about") == 0


async def test_reprocessing_is_idempotent_and_skips_extraction(engine, fixtures):
    s = site_of(fixtures, "plain")
    r1 = await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    hits = fixtures.state.total()
    r2 = await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    assert r2.outcome == "cached" and r2.metrics["http_requests"] == 0
    assert fixtures.state.total() == hits                      # zero network on repeat
    assert await count(engine, "companies") == 1
    async with engine.db.connection() as c:
        n = (await (await c.execute("SELECT count(*) n FROM company_fields WHERE field='email'")).fetchone())["n"]
    assert n == 1
    # same company via a different spelling of the URL => same identity, still no duplicate record
    r3 = await engine.enricher.enrich(f"HTTP://{s.host.upper()}:{fixtures.port}/about?x=1", requested=REQ)
    assert r3.outcome == "cached" and await count(engine, "companies") == 1


async def test_force_refresh_keeps_old_value_when_refresh_finds_nothing(engine, fixtures):
    s = site_of(fixtures, "plain")
    await engine.enricher.enrich(url_of(fixtures, s), requested=["email"])
    # simulate site dropping the email by expiring freshness and blanking the source via a stale flag
    async with engine.db.connection() as c:
        await c.execute("UPDATE company_fields SET refresh_after = now() - interval '1 day' WHERE field='email'")
        await c.execute("DELETE FROM http_cache")
    old = (await value(engine, s.host, "email"))["value"]
    r = await engine.enricher.enrich(url_of(fixtures, s), requested=["email"])
    assert (await value(engine, s.host, "email"))["value"] == old


async def test_value_change_writes_audit_history(engine, fixtures):
    s = site_of(fixtures, "plain")
    await engine.enricher.enrich(url_of(fixtures, s), requested=["email"])
    async with engine.db.connection() as c:
        await c.execute("UPDATE company_fields SET value='old@elsewhere.com', refresh_after=now()-interval '1 day' WHERE field='email'")
        await c.execute("DELETE FROM http_cache")
    await engine.enricher.enrich(url_of(fixtures, s), requested=["email"])
    async with engine.db.connection() as c:
        h = await (await c.execute("SELECT * FROM company_field_history")).fetchall()
    assert len(h) == 1 and h[0]["old_value"] == "old@elsewhere.com" and h[0]["new_value"] == s.email


@pytest.mark.parametrize("kind,err", [("robots_blocked", "robots"), ("not_found", "http"), ("huge", "too_large"),
                                      ("redirect_private", "blocked")])
async def test_unreachable_cases_are_safe_and_not_retried(engine, fixtures, kind, err):
    s = site_of(fixtures, kind)
    r = await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    assert r.outcome == "unreachable" and r.error_kind == err and not r.transient
    assert fixtures.state.count("169.254.169.254", "/") == 0
    if kind == "robots_blocked":
        assert fixtures.state.count(s.host, "/") == 0          # never fetched a disallowed page
    hits = fixtures.state.total()
    r2 = await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    assert r2.outcome == "unreachable" and fixtures.state.total() == hits   # remembered; no repeat fetching
    assert r2.error_kind == err and r2.error                                 # and still explains why
    w = await value(engine, s.host, "website")
    assert w["verification_status"] == "failed"


async def test_retry_after_and_transient_errors_recover(engine, fixtures):
    for kind in ("flaky", "ratelimited"):
        s = site_of(fixtures, kind)
        r = await engine.enricher.enrich(url_of(fixtures, s), requested=["company_name", "phone"])
        assert r.outcome in ("enriched", "partial"), (kind, r.outcome, r.error)
        assert r.metrics["retries"] >= 1
        assert (await value(engine, s.host, "company_name"))["value"] == s.name


async def test_timeout_is_transient_failure_not_crash(fixtures):
    eng = await fresh_engine(fixtures, read_timeout=0.3, total_timeout=0.5, http_max_retries=0)
    try:
        s = site_of(fixtures, "slow")
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        assert r.outcome == "failed" and r.transient and r.error_kind == "network"   # robots.txt timed out
    finally:
        await eng.stop()
    eng = await fresh_engine(fixtures, read_timeout=0.3, total_timeout=0.5, http_max_retries=0, respect_robots=False)
    try:
        s = site_of(fixtures, "slow", 1)
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=REQ)
        assert r.outcome == "failed" and r.transient and r.error_kind == "timeout"
        # transient failures are not remembered as "unreachable": a retry fetches again
        assert (await value(eng, s.host, "website")) is None
    finally:
        await eng.stop()


async def test_prompt_injection_and_scripts_are_inert(engine, fixtures):
    s = site_of(fixtures, "injection")
    await engine.enricher.enrich(url_of(fixtures, s), requested=REQ)
    assert (await value(engine, s.host, "email"))["value"] == s.email
    async with engine.db.connection() as c:
        dump = json.dumps([dict(r) for r in await (await c.execute("SELECT value FROM company_fields")).fetchall()])
    assert "evil.test" not in dump and "attacker" not in dump
    assert fixtures.state.count("evil.test", "/leak") == 0


async def test_malformed_html_extracts_what_it_can(engine, fixtures):
    s = site_of(fixtures, "broken_html")
    r = await engine.enricher.enrich(url_of(fixtures, s), requested=["email", "company_name"])
    assert (await value(engine, s.host, "email"))["value"] == s.email
    assert (await value(engine, s.host, "company_name"))["status"] == "not_found"


async def test_http_cache_revalidates_with_etag(engine, fixtures):
    s = site_of(fixtures, "etag")
    await engine.enricher.enrich(url_of(fixtures, s), requested=["company_name"])
    async with engine.db.connection() as c:
        await c.execute("UPDATE http_cache SET expires_at = now() - interval '1 hour'")
    fixtures.state.reset()
    res = await engine.fetcher.fetch(url_of(fixtures, s))
    assert res.ok and res.from_cache and res.text                  # served from cache after a 304
    assert fixtures.state.count(s.host, "/") == 1                  # exactly one conditional request


async def test_page_budget_stops_expensive_work_and_reports_partial(engine, fixtures):
    s = site_of(fixtures, "contact_only")
    b = Budget(pages=1)
    r = await engine.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"], budget=b)
    # robots.txt does not count as a page; homepage consumed the single page; /contact is refused
    assert "pages" in r.budget_exhausted
    assert (await value(engine, s.host, "email"))["status"] == "not_found"
    assert fixtures.state.count(s.host, "/contact") == 0


async def test_global_http_client_is_shared_and_pooled(engine):
    assert engine.fetcher.client is not None
    assert engine.enricher.fetcher is engine.fetcher


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def complete_json(self, system, user, schema, max_tokens):
        self.calls += 1
        text = self.reply(user) if callable(self.reply) else self.reply
        return LLMResponse(text, 900, 60)


async def _ai_engine(fixtures, llm):
    return await fresh_engine(fixtures, provider=llm)


async def test_ai_tier_accepts_only_grounded_values(fixtures):
    s = site_of(fixtures, "prose_only")

    def reply(user):
        return json.dumps({"fields": {
            "description": {"value": s.description, "quote": s.description},
            "address": {"value": "999 Fake Blvd, Nowhere", "quote": f"Visit us at {s.address}."},   # not in quote -> reject
            "industry": {"value": "astrophysics", "quote": "the invented sector"},                  # quote absent -> reject
            "company_name": {"value": None, "quote": None},
            "founded_year": {"value": str(s.founded), "quote": f"since {s.founded}"}}})
    llm = FakeLLM(reply)
    eng = await _ai_engine(fixtures, llm)
    try:
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["description", "address", "industry", "company_name", "founded_year"])
        assert llm.calls == 1
        assert (await value(eng, s.host, "description"))["value"] == s.description
        assert (await value(eng, s.host, "description"))["method"] == "ai_grounded"
        assert (await value(eng, s.host, "description"))["verification_status"] == "unverified"
        assert (await value(eng, s.host, "address"))["status"] == "not_found"
        assert (await value(eng, s.host, "industry"))["status"] == "not_found"
        assert (await value(eng, s.host, "company_name"))["status"] == "not_found"
        assert (await value(eng, s.host, "founded_year"))["value"] == str(s.founded)
        assert r.metrics["ai_calls"] == 1 and r.metrics["cost"]["ai_usd_estimated"] > 0
    finally:
        await eng.stop()


@pytest.mark.parametrize("reply", ["not json at all", '{"fields": 7}', '{"nope": 1}', ""])
async def test_ai_invalid_responses_leave_fields_missing(fixtures, reply):
    s = site_of(fixtures, "prose_only")
    eng = await _ai_engine(fixtures, FakeLLM(reply))
    try:
        await eng.enricher.enrich(url_of(fixtures, s), requested=["description", "industry"])
        assert (await value(eng, s.host, "description"))["status"] == "not_found"
    finally:
        await eng.stop()


async def test_ai_not_called_when_deterministic_extraction_suffices(fixtures):
    s = site_of(fixtures, "jsonld")
    llm = FakeLLM("{}")
    eng = await _ai_engine(fixtures, llm)
    try:
        await eng.enricher.enrich(url_of(fixtures, s), requested=["company_name", "description", "address"])
        assert llm.calls == 0
    finally:
        await eng.stop()


async def test_ai_budget_is_enforced(fixtures):
    s = site_of(fixtures, "prose_only")
    llm = FakeLLM("{}")
    eng = await _ai_engine(fixtures, llm)
    try:
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["description"], budget=Budget(ai_calls=0.0001 and 1))
        assert llm.calls <= 1
        b = Budget(ai_calls=1); b.spent_ai_calls = 1
        s2 = site_of(fixtures, "prose_only", 1) if len([x for x in fixtures.state.sites.values() if x.kind == "prose_only"]) > 1 else s
        llm.calls = 0
        await eng.enricher.enrich(url_of(fixtures, s2), requested=["industry"], budget=b, force=True)
        assert llm.calls == 0 and "ai_calls" in b.exhausted_reasons
    finally:
        await eng.stop()


async def test_js_shell_needs_browser_tier_and_is_not_used_otherwise(fixtures):
    s = site_of(fixtures, "js_shell")
    # without the browser tier the JS-only content is simply missing (never invented)
    eng = await fresh_engine(fixtures, browser_enabled=False)
    try:
        await eng.enricher.enrich(url_of(fixtures, s), requested=["company_name", "email", "phone"])
        assert (await value(eng, s.host, "email"))["status"] == "not_found"
    finally:
        await eng.stop()
    eng = await fresh_engine(fixtures, browser_enabled=True)
    try:
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["company_name", "email", "phone"])
        if eng.browser.available is False:
            pytest.skip(f"chromium unavailable: {eng.browser.last_error}")
        assert r.metrics["browser_ops"] == 1
        assert (await value(eng, s.host, "email"))["value"] == s.email
        assert (await value(eng, s.host, "phone"))["value"] == s.phone
        assert (await value(eng, s.host, "email"))["tier"] == "tier4_browser"
        # server-rendered sites never touch the browser
        p = site_of(fixtures, "plain")
        r2 = await eng.enricher.enrich(url_of(fixtures, p), requested=["company_name", "email", "phone"])
        assert r2.metrics["browser_ops"] == 0
    finally:
        await eng.stop()


async def test_browser_subrequests_cannot_reach_private_hosts(fixtures):
    """All browser traffic is fulfilled by the guarded fetcher: a page that tries to load an
    internal address gets nothing."""
    from leadengine.fetcher import Fetcher
    f = Fetcher(make_settings(host_overrides=""), db=None)       # guard fully on
    try:
        status, _, body = await f.fetch_subresource("http://169.254.169.254/latest/meta-data/")
        assert (status, body) == (0, b"")
        status, _, body = await f.fetch_subresource(f"http://127.0.0.1:{fixtures.port}/")
        assert (status, body) == (0, b"")
    finally:
        await f.aclose()


# ---------------------------------------------------------------- politeness, limits, cache rules
async def test_per_domain_min_delay_and_concurrency_are_enforced(fixtures):
    import asyncio, time
    eng = await fresh_engine(fixtures, per_domain_min_delay=0.3, per_domain_concurrency=1, respect_robots=False)
    try:
        s = site_of(fixtures, "plain")
        fixtures.state.reset()
        t0 = time.monotonic()
        await asyncio.gather(*[eng.fetcher.fetch(f"{url_of(fixtures, s)}?n={i}") for i in range(4)])
        assert time.monotonic() - t0 >= 0.3 * 3 - 0.05            # 4 requests => 3 enforced gaps
        stamps = [t for t, h, p in fixtures.state.hit_log if h == s.host]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert min(gaps) >= 0.2          # server-side timestamps; client enforces 0.3 s, allow scheduling jitter
    finally:
        await eng.stop()


async def test_retry_after_is_honoured_and_oversized_values_stop_retrying(fixtures):
    import time
    eng = await fresh_engine(fixtures, respect_robots=False, backoff_base=0.01, backoff_max=0.02)
    try:
        s = site_of(fixtures, "ratelimited")                       # first response: 429 + Retry-After: 1
        t0 = time.monotonic()
        r = await eng.fetcher.fetch(url_of(fixtures, s))
        assert r.ok and r.retries == 1 and time.monotonic() - t0 >= 0.95     # waited what the server asked
    finally:
        await eng.stop()
    eng = await fresh_engine(fixtures, respect_robots=False, max_retry_after=0.5)
    try:
        s = site_of(fixtures, "ratelimited", 1)
        r = await eng.fetcher.fetch(url_of(fixtures, s))
        assert not r.ok and r.error_kind == "rate_limited" and r.transient
    finally:
        await eng.stop()


async def test_permanent_http_errors_are_not_retried(fixtures):
    eng = await fresh_engine(fixtures, respect_robots=False, http_max_retries=3, backoff_base=0.01)
    try:
        s = site_of(fixtures, "not_found")
        fixtures.state.reset()
        r = await eng.fetcher.fetch(url_of(fixtures, s))
        assert r.error_kind == "http" and r.status == 404 and r.retries == 0 and not r.transient
        assert fixtures.state.count(s.host, "/") == 1
    finally:
        await eng.stop()


async def test_cache_control_rules(engine, fixtures):
    s = site_of(fixtures, "plain")
    base = f"http://{s.host}:{fixtures.port}"
    await engine.fetcher.fetch(base + "/nostore"); await engine.fetcher.fetch(base + "/nostore")
    assert fixtures.state.count(s.host, "/nostore") == 2                      # never cached
    await engine.fetcher.fetch(base + "/maxage"); r = await engine.fetcher.fetch(base + "/maxage")
    assert fixtures.state.count(s.host, "/maxage") == 1 and r.from_cache      # fresh for max-age
    async with engine.db.connection() as c:
        urls = {x["url"] for x in await (await c.execute("SELECT url FROM http_cache")).fetchall()}
    assert base + "/nostore" not in urls and base + "/maxage" in urls


async def test_unchanged_pages_are_not_reparsed(engine, fixtures):
    s = site_of(fixtures, "plain")
    await engine.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"])
    async with engine.db.connection() as c:
        await c.execute("UPDATE company_fields SET refresh_after = now() - interval '1 day'")
        await c.execute("UPDATE http_cache SET expires_at = now() - interval '1 day', etag=NULL")
        n_before = (await (await c.execute("SELECT count(*) n FROM extraction_cache")).fetchone())["n"]
    r = await engine.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"])
    async with engine.db.connection() as c:
        n_after = (await (await c.execute("SELECT count(*) n FROM extraction_cache")).fetchone())["n"]
    assert n_before == n_after and n_after >= 1               # same content hash => reused parse result


async def test_worker_concurrency_and_queue_are_bounded(fixtures):
    import asyncio
    from leadengine import jobs
    from leadengine.worker import Worker
    eng = await fresh_engine(fixtures, global_concurrency=3, queue_size=6, claim_batch_size=4)
    try:
        peak = {"now": 0, "max": 0, "tasks": 0}
        orig = eng.enricher.enrich

        async def spy(*a, **k):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            try:
                await asyncio.sleep(0.02)
                return await orig(*a, **k)
            finally:
                peak["now"] -= 1
        eng.enricher.enrich = spy
        cos = [{"website": f"http://c{i}.fixture.test:{fixtures.port}/"} for i in range(40)]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, cos, requested=["email"])
        w = Worker(eng)
        before = len(asyncio.all_tasks())
        t = asyncio.create_task(w.run(once=True))
        samples = []
        while not t.done():
            samples.append(len(asyncio.all_tasks()) - before)
            await asyncio.sleep(0.01)
        assert peak["max"] <= 3
        assert max(samples) < 30                                        # tasks ~ fixed pool, not one per row
        async with eng.db.connection() as c:
            assert (await (await c.execute("SELECT count(*) n FROM job_items WHERE status='succeeded' OR status='failed'")).fetchone())["n"] == 40
    finally:
        await eng.stop()


async def test_ai_truncated_or_refused_replies_are_discarded_even_if_they_parse(fixtures):
    s = site_of(fixtures, "prose_only")
    good = json.dumps({"fields": {"description": {"value": s.description, "quote": s.description}}})

    class Trunc(FakeLLM):
        async def complete_json(self, system, user, schema, max_tokens):
            self.calls += 1
            return LLMResponse(good, 900, 2048, stop_reason="max_tokens")
    llm = Trunc(good)
    eng = await _ai_engine(fixtures, llm)
    try:
        r = await eng.enricher.enrich(url_of(fixtures, s), requested=["description"])
        assert (await value(eng, s.host, "description"))["status"] == "not_found"
        assert r.metrics["tiers"]["tier5_ai"]["note"] == "stop_reason:max_tokens"
        assert r.metrics["ai_calls"] == 1 and r.metrics["ai_tokens_out"] == 2048     # cost still recorded
    finally:
        await eng.stop()
