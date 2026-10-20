import asyncio

from leadengine import jobs
from leadengine.db import Database
from leadengine.worker import Worker
from tests.conftest import fresh_engine, make_settings, site_of, url_of


async def test_migrations_are_recorded_idempotent_and_safe_under_concurrent_start(fixtures):
    eng = await fresh_engine(fixtures)
    try:
        dbs = [Database(make_settings()) for _ in range(3)]
        await asyncio.gather(*[d.open() for d in dbs])                 # three "processes" start at once
        for d in dbs:
            await d.init_schema()                                      # and re-apply
            await d.close()
        async with eng.db.connection() as c:
            versions = [r["version"] for r in await (await c.execute("SELECT version FROM schema_migrations ORDER BY 1")).fetchall()]
            idx = (await (await c.execute("SELECT indexdef FROM pg_indexes WHERE indexname='job_items_open_idx'")).fetchone())["indexdef"]
        assert versions == ["0001_job_items_open_idx.sql", "0002_fetch_log.sql"]
        assert "WHERE" in idx and "queued" in idx
    finally:
        await eng.stop()


async def test_claim_query_uses_the_open_items_index(fixtures):
    eng = await fresh_engine(fixtures)
    try:
        async with eng.db.connection() as c:
            j = await (await c.execute("INSERT INTO jobs (type,requested_fields,ready,status,total) VALUES ('t','{email}',true,'running',50000) RETURNING id")).fetchone()
            await c.execute("""INSERT INTO job_items (job_id,row_index,status,input,domain)
                               SELECT %s, g, CASE WHEN g <= 49900 THEN 'succeeded' ELSE 'queued' END, '{}'::jsonb, 'd'||g FROM generate_series(1,50000) g""", (j["id"],))
            await c.execute("ANALYZE job_items")
            plan = "\n".join(str(list(r.values())[0]) for r in await (await c.execute(
                """EXPLAIN SELECT job_id,row_index FROM job_items WHERE job_id=%s AND ((status='queued' AND (not_before IS NULL OR not_before<=now()))
                   OR (status='running' AND lease_expires_at<now())) ORDER BY row_index FOR UPDATE SKIP LOCKED LIMIT 16""", (j["id"],))).fetchall())
        assert "job_items_open_idx" in plan, plan
    finally:
        await eng.stop()


async def test_every_request_is_audited_with_job_context(fixtures):
    eng = await fresh_engine(fixtures)
    try:
        s = site_of(fixtures, "contact_only")
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)},
                                                                           {"website": "http://metadata.google.internal/"}], requested=["email", "phone"])
        await Worker(eng).run(once=True)
        await eng.audit.flush()
        async with eng.db.connection() as c:
            rows = await (await c.execute("SELECT * FROM fetch_log ORDER BY id")).fetchall()
        kinds = {(r["kind"], r["url"].split(str(fixtures.port))[-1] if str(fixtures.port) in r["url"] else r["url"], r["status"], r["error_kind"]) for r in rows}
        assert ("robots", "/robots.txt", 200, None) in kinds
        assert ("page", "/", 200, None) in kinds and ("page", "/contact", 200, None) in kinds
        blocked = [r for r in rows if r["error_kind"] == "blocked"]
        assert blocked and blocked[0]["host"] == "metadata.google.internal"          # the refusal itself is on record
        assert all(str(r["job_id"]) == info["job_id"] for r in rows)
        assert all(r["elapsed_ms"] is not None or r["error_kind"] for r in rows)
        # a repeat is served from cache and still logged as such
        n = len(rows)
        await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)}], requested=["email", "phone"])
        await Worker(eng).run(once=True)
        await eng.audit.flush()
        async with eng.db.connection() as c:
            assert (await (await c.execute("SELECT count(*) n FROM fetch_log")).fetchone())["n"] == n   # cached: planner skipped all I/O
    finally:
        await eng.stop()
