import asyncio
import csv
import io

import pytest

from leadengine import jobs
from leadengine.csv_io import CSVError, CSVReader, auto_map, spreadsheet_safe
from leadengine.engine import Engine
from leadengine.worker import Worker
from tests.conftest import fresh_engine, make_settings, site_of, url_of


def write_csv(path, rows, header=("Company", "Website", "City", "Notes"), delimiter=",", encoding="utf-8"):
    with open(path, "w", newline="", encoding=encoding) as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


def sites(fixtures, kinds):
    out = []
    for k in kinds:
        out.append(site_of(fixtures, k))
    return out


async def run_worker(engine, timeout=60):
    w = Worker(engine)
    await asyncio.wait_for(w.run(once=True), timeout)
    return w


async def item_rows(engine, job_id):
    return {r["row_index"]: r for r in await jobs.job_items(engine.db, job_id, limit=1000)}


# ---------------------------------------------------------------- CSV parsing
def test_csv_encoding_delimiter_mapping_and_validation(tmp_path):
    p = write_csv(tmp_path / "a.csv", [["Café Müller", "cafe-mueller.de", "Köln", "x"], ["", "", "", ""],
                                       ["NoSite GmbH", "", "Bonn", ""], ["Bad", "not a domain!!", "", ""],
                                       ["Extra", "a.com", "b", "c", "EXTRA"]], delimiter=";", encoding="latin-1")
    r = CSVReader(p)
    assert r.encoding.lower() in ("latin-1", "iso-8859-1", "cp1252", "latin_1", "iso8859-1") or r.encoding
    assert r.delimiter == ";"
    assert r.mapping == {"name": "Company", "website": "Website", "location": "City"}
    rows = list(r.rows())
    assert [x.row_index for x in rows] == [1, 2, 3, 4, 5]
    assert rows[0].original["Company"] == "Café Müller" and rows[0].domain == "cafe-mueller.de"
    assert rows[1].error == "blank row"
    assert "name-only" in rows[2].error
    assert "invalid website/domain" in rows[3].error
    assert "columns" in rows[4].error


def test_csv_rejects_binary_and_missing_columns(tmp_path):
    b = tmp_path / "b.csv"
    b.write_bytes(b"\x00\x01\x02\x03" * 100)
    with pytest.raises(CSVError):
        CSVReader(str(b))
    p = write_csv(tmp_path / "c.csv", [["x", "y"]], header=("a", "b"))
    with pytest.raises(CSVError, match="no website/domain column"):
        CSVReader(p)
    with pytest.raises(CSVError, match="not in file header"):
        CSVReader(write_csv(tmp_path / "d.csv", [["x"]], header=("a",)), {"website": "zzz"})


def test_spreadsheet_injection_neutralised():
    assert spreadsheet_safe('=HYPERLINK("http://evil")').startswith("'")
    assert spreadsheet_safe("@SUM(A1)").startswith("'")
    assert spreadsheet_safe("+1 512-555-0100") == "+1 512-555-0100"
    assert spreadsheet_safe("-cmd|' /C calc'!A0").startswith("'")
    assert spreadsheet_safe("info@acme.com") == "info@acme.com"


# ---------------------------------------------------------------- end to end
async def test_csv_job_end_to_end_isolates_failures_and_exports(engine, fixtures, tmp_path):
    good = sites(fixtures, ["jsonld", "plain", "contact_only"])
    bad = sites(fixtures, ["not_found", "robots_blocked"])
    rows = [[s.name, url_of(fixtures, s), "Austin", f"note{i}"] for i, s in enumerate(good + bad)]
    rows.insert(1, ["Dup of first", url_of(fixtures, good[0]), "", "dup"])          # in-file duplicate
    rows.append(["Broken row", "ht!tp://%%", "", ""])                                 # invalid
    p = write_csv(tmp_path / "in.csv", rows)
    info = await jobs.create_job_from_csv(engine.db, engine.settings, p, filename="in.csv",
                                          requested=["company_name", "email", "phone"])
    assert info["ingest"] == {"total": 7, "queued": 5, "invalid": 1, "duplicate": 1, "known_company": 0}
    await run_worker(engine)

    st = await jobs.job_status(engine.db, info["job_id"])
    assert st["status"] == "partially_completed"
    c = st["counts"]
    assert (c["total"], c["succeeded"], c["failed"], c["invalid"], c["duplicate"], c["queued"], c["running"]) == (7, 3, 2, 1, 1, 0, 0)
    assert st["completed_at"] and st["started_at"] and st["stats"]["measured"]["runs"] >= 5
    items = await item_rows(engine, info["job_id"])
    assert items[2]["status"] == "duplicate" and items[2]["duplicate_of"] == 1
    assert items[5]["error_kind"] == "unreachable_http" and "404" in items[5]["error"]
    assert items[6]["error_kind"] == "unreachable_robots"
    assert items[7]["status"] == "invalid" and "invalid website" in items[7]["error"]
    assert items[1]["status"] == items[3]["status"] == items[4]["status"] == "succeeded"

    text = "".join([chunk async for chunk in jobs.export_csv(engine.db, info["job_id"])])
    out = list(csv.DictReader(io.StringIO(text)))
    assert list(out[0].keys())[:4] == ["Company", "Website", "City", "Notes"]          # originals preserved, in order
    assert [r["le_row"] for r in out] == ["1", "2", "3", "4", "5", "6", "7"]
    assert out[0]["Notes"] == "note0" and out[1]["Notes"] == "dup"
    assert out[0]["company_name"] == good[0].name and out[0]["company_name__source_url"].startswith("http")
    assert out[0]["email__status"].startswith("present+source_attributed+format_valid")
    assert "verified" not in out[0]["email__status"].split("+")                       # email is never verified
    assert out[1]["company_name"] == good[0].name and out[1]["le_status"] == "duplicate"
    assert out[3]["email"] == good[2].email and out[3]["phone"] == good[2].phone         # contact_only: via /contact
    assert out[3]["company_name__status"] == "missing" and out[3]["company_name"] == ""   # never invented
    assert out[4]["le_status"] == "failed" and out[4]["le_error"]
    assert out[6]["le_status"] == "invalid" and out[6]["company_name__status"] == "missing"


async def test_rerunning_a_completed_csv_triggers_no_extraction(engine, fixtures, tmp_path):
    ss = sites(fixtures, ["jsonld", "plain", "contact_only", "not_found"])
    p = write_csv(tmp_path / "in.csv", [[s.name, url_of(fixtures, s), "", ""] for s in ss])
    a = await jobs.create_job_from_csv(engine.db, engine.settings, p, filename="in.csv", requested=None)
    await run_worker(engine)
    fixtures.state.reset()
    b = await jobs.create_job_from_csv(engine.db, engine.settings, p, filename="in.csv", requested=None)
    assert b["ingest"]["known_company"] == 4                                           # detected against PostgreSQL
    await run_worker(engine)
    assert fixtures.state.total() == 0                                                 # not a single request
    items = await item_rows(engine, b["job_id"])
    assert {i["outcome"] for i in items.values() if i["status"] == "succeeded"} == {"cached"}
    async with engine.db.connection() as c:
        assert (await (await c.execute("SELECT count(*) n FROM companies")).fetchone())["n"] == 4


async def test_idempotency_key_returns_same_job(engine):
    a = await jobs.create_job_from_companies(engine.db, engine.settings, [{"website": "a.com"}], requested=["email"], idempotency_key="k1")
    b = await jobs.create_job_from_companies(engine.db, engine.settings, [{"website": "a.com"}], requested=["email"], idempotency_key="k1")
    assert a["job_id"] == b["job_id"] and a["created"] and not b["created"]


async def test_max_companies_per_job_enforced(fixtures):
    eng = await fresh_engine(fixtures, max_companies_per_job=3)
    try:
        with pytest.raises(jobs.JobError):
            await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": f"a{i}.com"} for i in range(5)], requested=None)
        async with eng.db.connection() as c:
            n = (await (await c.execute("SELECT count(*) n FROM jobs WHERE total=0 AND NOT ready")).fetchone())["n"]
        assert n == 0                                                                  # failed ingest leaves no orphan job
    finally:
        await eng.stop()


# ---------------------------------------------------------------- claiming / recovery
async def test_concurrent_item_claims_never_overlap(engine):
    cos = [{"website": f"x{i}.fixture.test"} for i in range(60)]
    j = await jobs.create_job_from_companies(engine.db, engine.settings, cos, requested=["email"])

    async def grab(name):
        got = []
        while True:
            items = await jobs.claim_items(engine.db, engine.settings, j["job_id"], name, 7)
            if not items:
                return got
            got += [i["row_index"] for i in items]
    results = await asyncio.gather(*[grab(f"w{i}") for i in range(6)])
    flat = [x for r in results for x in r]
    assert sorted(flat) == list(range(1, 61)) and len(flat) == len(set(flat))


async def test_max_simultaneous_jobs_is_enforced_database_wide(fixtures):
    eng = await fresh_engine(fixtures, max_concurrent_jobs=2)
    try:
        for i in range(3):
            await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": f"a{i}.fixture.test"}], requested=["email"])
        got = [await jobs.claim_job(eng.db, eng.settings, f"w{i}") for i in range(3)]
        assert [g is not None for g in got] == [True, True, False]
    finally:
        await eng.stop()


async def test_crashed_worker_job_is_recovered_and_zombie_is_fenced(engine, fixtures, tmp_path):
    ss = sites(fixtures, ["jsonld", "plain"])
    p = write_csv(tmp_path / "in.csv", [[s.name, url_of(fixtures, s), "", ""] for s in ss])
    info = await jobs.create_job_from_csv(engine.db, engine.settings, p, filename="in.csv", requested=["email", "phone"])
    jid = info["job_id"]
    # worker A claims the job and one item, then "crashes" (never heartbeats again)
    job = await jobs.claim_job(engine.db, engine.settings, "A")
    taken = await jobs.claim_items(engine.db, engine.settings, jid, "A", 1)
    assert job and len(taken) == 1
    assert await jobs.claim_job(engine.db, engine.settings, "B") is None              # lease still valid
    async with engine.db.connection() as c:
        await c.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id=%s", (jid,))
        await c.execute("UPDATE job_items SET lease_expires_at = now() - interval '1 second' WHERE job_id=%s AND status='running'", (jid,))
    await run_worker(engine)                                                          # worker B takes over
    st = await jobs.job_status(engine.db, jid)
    assert st["status"] == "completed" and st["counts"]["succeeded"] == 2 and st["worker_claims"] == 2
    # zombie A wakes up and tries to write: fenced out
    ok = await jobs.complete_item(engine.db, jid, taken[0]["row_index"], "A", status="failed", outcome=None, result={})
    assert ok is False
    items = await item_rows(engine, jid)
    assert items[taken[0]["row_index"]]["status"] == "succeeded"


async def test_queued_jobs_survive_restart(fixtures, tmp_path):
    e1 = await fresh_engine(fixtures)
    s1 = e1.settings
    ss = sites(fixtures, ["jsonld"])
    info = await jobs.create_job_from_companies(e1.db, s1, [{"website": url_of(fixtures, ss[0])}], requested=["email"])
    await e1.stop()                                                                   # "restart"
    e2 = Engine(make_settings())
    await e2.start()          # no truncation: the queued job must still be there
    try:
        await run_worker(e2)
        assert (await jobs.job_status(e2.db, info["job_id"]))["status"] == "completed"
    finally:
        await e2.stop()


async def test_graceful_shutdown_releases_items_and_job_resumes(fixtures):
    eng = await fresh_engine(fixtures, global_concurrency=2, queue_size=4, claim_batch_size=2,
                             job_heartbeat_seconds=1, per_domain_min_delay=0.0, respect_robots=False)
    try:
        slow = [s for s in fixtures.state.sites.values() if s.kind == "slow"][:4]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)} for s in slow], requested=["company_name"])
        w = Worker(eng)
        t = asyncio.create_task(w.run(once=True))
        await asyncio.sleep(0.8)
        w.request_stop()
        await asyncio.wait_for(t, 30)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "queued" and st["counts"]["running"] == 0               # handed back, nothing lost
        done_before = st["counts"]["succeeded"]
        await run_worker(eng)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "completed" and st["counts"]["succeeded"] == 4 and done_before < 4
    finally:
        await eng.stop()


async def test_transient_item_failures_retry_with_backoff_then_fail(fixtures):
    eng = await fresh_engine(fixtures, read_timeout=0.2, total_timeout=0.3, http_max_retries=0, respect_robots=False,
                             item_max_attempts=2, item_retry_base_seconds=0.2)
    try:
        s = site_of(fixtures, "slow", 2)
        ok = site_of(fixtures, "plain", 1)
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)}, {"website": url_of(fixtures, ok)}], requested=["email"])
        await run_worker(eng)
        items = await item_rows(eng, info["job_id"])
        assert items[1]["status"] == "failed" and items[1]["attempts"] == 2 and "gave up after 2 attempts" in items[1]["error"]
        assert items[2]["status"] == "succeeded"                                       # neighbour unaffected
    finally:
        await eng.stop()


async def test_job_budget_exhaustion_is_reported_not_silent(fixtures):
    eng = await fresh_engine(fixtures)
    try:
        ss = [s for s in fixtures.state.sites.values() if s.kind == "plain"][:4]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)} for s in ss],
                                                    requested=["email"], budget={"pages": 2})
        await run_worker(eng)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "partially_completed"
        assert st["counts"]["skipped"] >= 1 and st["error_summary"].get("budget_exhausted", 0) >= 1
        assert st["stats"]["budget_spent"]["pages"] <= 2
        # a later retry with more budget finishes the skipped ones and does not redo the completed ones
        async with eng.db.connection() as c:
            await c.execute("UPDATE jobs SET config = jsonb_set(config, '{budget,pages}', '100') WHERE id=%s", (info["job_id"],))
        assert await jobs.retry_failed(eng.db, info["job_id"]) >= 1
        await run_worker(eng)
        assert (await jobs.job_status(eng.db, info["job_id"]))["status"] == "completed"
    finally:
        await eng.stop()


async def test_cancel_queued_job(engine):
    info = await jobs.create_job_from_companies(engine.db, engine.settings, [{"website": "a.fixture.test"}], requested=["email"])
    assert await jobs.request_cancel(engine.db, info["job_id"])
    st = await jobs.job_status(engine.db, info["job_id"])
    assert st["status"] == "cancelled" and st["counts"]["skipped"] == 1


async def test_shutdown_grace_expiry_cancels_and_requeues(fixtures):
    eng = await fresh_engine(fixtures, global_concurrency=2, queue_size=4, claim_batch_size=2, job_heartbeat_seconds=1,
                             respect_robots=False, shutdown_grace_seconds=0.1)
    try:
        slow = [s for s in fixtures.state.sites.values() if s.kind == "slow"][:4]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)} for s in slow], requested=["company_name"])
        w = Worker(eng)
        t = asyncio.create_task(w.run(once=True))
        await asyncio.sleep(0.6)                      # requests (1.2 s each) are in flight
        w.request_stop()
        await asyncio.wait_for(t, 10)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "queued" and st["counts"]["running"] == 0 and st["counts"]["queued"] == 4
        await run_worker(eng)
        assert (await jobs.job_status(eng.db, info["job_id"]))["counts"]["succeeded"] == 4
    finally:
        await eng.stop()


# ---------------------------------------------------------------- Stage-4 reliability regressions
async def test_heartbeat_survives_transient_db_errors(fixtures, monkeypatch):
    """A single failed heartbeat used to kill the heartbeat task silently; the lease then expired mid-job."""
    eng = await fresh_engine(fixtures, job_heartbeat_seconds=0.3, job_lease_seconds=3, global_concurrency=2,
                             respect_robots=False)
    try:
        calls = {"n": 0}
        real = jobs.heartbeat

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("db blip")
            return await real(*a, **k)
        monkeypatch.setattr(jobs, "heartbeat", flaky)
        slow = [s for s in fixtures.state.sites.values() if s.kind == "slow"][:4]
        info = await jobs.create_job_from_companies(eng.db, eng.settings, [{"website": url_of(fixtures, s)} for s in slow], requested=["company_name"])
        await run_worker(eng)
        assert calls["n"] >= 3                                    # kept beating after the failure
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "completed" and st["counts"]["succeeded"] == 4
    finally:
        await eng.stop()


async def test_two_workers_never_process_the_same_item_twice(fixtures):
    eng = await fresh_engine(fixtures, global_concurrency=4, max_concurrent_jobs=2)
    try:
        cos = [{"website": f"http://c{i}.fixture.test:{fixtures.port}/"} for i in range(40)]
        a = await jobs.create_job_from_companies(eng.db, eng.settings, cos[:20], requested=["email"])
        b = await jobs.create_job_from_companies(eng.db, eng.settings, cos[20:], requested=["email"])
        w1, w2 = Worker(eng), Worker(eng)
        await asyncio.wait_for(asyncio.gather(w1.run(once=True), w2.run(once=True)), 60)
        async with eng.db.connection() as c:
            rows = await (await c.execute("SELECT job_id, count(*) n, max(attempts) mx, min(attempts) mn FROM job_items GROUP BY job_id")).fetchall()
            runs = (await (await c.execute("SELECT count(*) n, count(DISTINCT (job_id,row_index)) d FROM enrichment_runs")).fetchone())
        assert all(r["n"] == 20 and r["mx"] == 1 and r["mn"] == 1 for r in rows)
        assert runs["n"] == runs["d"] == 40                       # one enrichment run per item, no duplicates
    finally:
        await eng.stop()


async def test_same_company_enriched_concurrently_yields_one_record_and_one_fetch(engine, fixtures):
    s = site_of(fixtures, "plain")
    rs = await asyncio.gather(*[engine.enricher.enrich(url_of(fixtures, s), requested=["email", "phone"]) for _ in range(6)])
    assert all(r.outcome in ("enriched", "partial", "cached") for r in rs)
    async with engine.db.connection() as c:
        assert (await (await c.execute("SELECT count(*) n FROM companies")).fetchone())["n"] == 1
        assert (await (await c.execute("SELECT count(*) n FROM company_fields WHERE field='email'")).fetchone())["n"] == 1
    assert fixtures.state.count(s.host, "/") <= 2                 # single-flight; at most one race window


async def test_sigkilled_worker_process_loses_no_work(fixtures):
    """Real crash, not a simulated one: kill -9 a worker subprocess mid-job, then let a second worker finish."""
    import os, signal, subprocess, sys
    eng = await fresh_engine(fixtures)
    try:
        slow = [s for s in fixtures.state.sites.values() if s.kind == "slow"][:4]
        plain = [s for s in fixtures.state.sites.values() if s.kind == "plain"][:8]
        cos = [{"website": url_of(fixtures, s)} for s in slow + plain]
        n = len(cos)
        assert n >= 8
        info = await jobs.create_job_from_companies(eng.db, eng.settings, cos, requested=["email", "company_name"])
        env = {**os.environ, "LE_DATABASE_URL": eng.settings.database_url.get_secret_value(),
               "LE_USER_AGENT": "CrashTest/1.0", "LE_HOST_OVERRIDES": "*.fixture.test=127.0.0.1", "LE_ALLOWED_PORTS": "80,443",
               "LE_PER_DOMAIN_MIN_DELAY": "0", "LE_BROWSER_ENABLED": "false", "LE_JOB_LEASE_SECONDS": "3",
               "LE_JOB_HEARTBEAT_SECONDS": "1", "LE_GLOBAL_CONCURRENCY": "2", "LE_RESPECT_ROBOTS": "false"}
        p = subprocess.Popen([sys.executable, "-m", "leadengine", "worker", "--once"], env=env, cwd=os.getcwd(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):                                   # wait until it is genuinely mid-job
            await asyncio.sleep(0.1)
            st = await jobs.job_status(eng.db, info["job_id"])
            if st["counts"]["running"] > 0:
                break
        p.send_signal(signal.SIGKILL)
        p.wait(timeout=10)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "running" and st["counts"]["running"] > 0          # orphaned: nobody owns it any more
        assert st["counts"]["succeeded"] < n
        await asyncio.sleep(3.2)                               # leases expire
        await run_worker(eng)
        st = await jobs.job_status(eng.db, info["job_id"])
        assert st["status"] == "completed" and st["counts"]["succeeded"] == n and st["counts"]["running"] == 0
        assert st["worker_claims"] == 2                         # killed worker + recovery worker
        async with eng.db.connection() as c:
            att = (await (await c.execute("SELECT max(attempts) m FROM job_items")).fetchone())["m"]
            dup = (await (await c.execute("SELECT count(*) n FROM (SELECT company_id, field FROM company_fields GROUP BY 1,2 HAVING count(*)>1) x")).fetchone())["n"]
            comps = (await (await c.execute("SELECT count(*) n FROM companies")).fetchone())["n"]
        assert att <= 2 and dup == 0 and comps == n
    finally:
        await eng.stop()
