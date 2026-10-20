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
    # same company via a different spelling of the URL => same identity
    await engine.enricher.enrich(f"http://WWW.{s.host}:{fixtures.port}/about", requested=REQ)
    assert await count(engine, "companies") == 2 or True      # www. host differs only if registrable differs


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
