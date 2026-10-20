import csv
import io

import httpx
import pytest
import pytest_asyncio

from leadengine.api import create_app
from leadengine import jobs
from leadengine.worker import Worker
from tests.conftest import fresh_engine, site_of, url_of


@pytest_asyncio.fixture
async def client(fixtures):
    eng = await fresh_engine(fixtures)
    app = create_app(eng.settings, engine=eng)
    app.state.engine = eng
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        c.engine = eng
        yield c
    await eng.stop()


async def test_enrich_endpoint_returns_field_level_status_and_scores(client, fixtures):
    s = site_of(fixtures, "jsonld")
    r = await client.post("/v1/enrich", json={"website": url_of(fixtures, s), "fields": ["company_name", "email", "twitter_url"]})
    assert r.status_code == 200
    d = r.json()
    f = d["company"]["fields"]
    assert f["company_name"]["value"] == s.name and f["company_name"]["source_url"].startswith("http")
    assert f["company_name"]["present"] and f["company_name"]["source_attributed"] and f["company_name"]["format_valid"]
    assert f["email"]["verified"] is False and f["email"]["verification_status"] == "unverified"
    assert f["twitter_url"]["present"] is False and f["twitter_url"]["state"] == "not_found" and f["twitter_url"]["value"] is None
    # requested weights: company_name 1.0 + email 1.0 + twitter_url 0.25; two of three present
    assert d["company"]["completeness"] == pytest.approx(2.0 / 2.25, abs=0.001)
    assert d["metrics"]["cost"]["total_usd_estimated"] >= 0 and "estimate" in d["metrics"]["cost"]["basis"]
    # second call: nothing re-fetched
    r2 = await client.post("/v1/enrich", json={"website": url_of(fixtures, s), "fields": ["company_name", "email", "twitter_url"]})
    assert r2.json()["outcome"] == "cached" and r2.json()["metrics"]["http_requests"] == 0
    g = await client.get(f"/v1/companies/{s.host}", params={"history": "true"})
    assert g.status_code == 200 and "history" in g.json()
    assert (await client.get("/v1/companies/unknown.example")).status_code == 404


async def test_validation_errors(client):
    assert (await client.post("/v1/enrich", json={"website": "not a url!!"})).status_code == 422
    assert (await client.post("/v1/enrich", json={"website": "a.com", "fields": ["nope"]})).status_code == 422
    assert (await client.post("/v1/jobs", json={"companies": []})).status_code == 422


async def test_csv_job_over_http_then_export(client, fixtures):
    ss = [site_of(fixtures, "plain"), site_of(fixtures, "jsonld"), site_of(fixtures, "not_found")]
    body = "Company,URL\n" + "\n".join(f"{s.name},{url_of(fixtures, s)}" for s in ss) + "\n"
    r = await client.post("/v1/jobs/csv", content=body.encode(), params={"filename": "x.csv", "fields": "email,phone"},
                          headers={"content-type": "text/csv"})
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    await Worker(client.engine).run(once=True)
    st = (await client.get(f"/v1/jobs/{jid}")).json()
    assert st["status"] == "partially_completed" and st["counts"]["succeeded"] == 2 and st["counts"]["failed"] == 1
    items = (await client.get(f"/v1/jobs/{jid}/items", params={"status": "failed"})).json()["items"]
    assert items[0]["row_index"] == 3 and items[0]["error"]
    exp = await client.get(f"/v1/jobs/{jid}/export.csv")
    rows = list(csv.DictReader(io.StringIO(exp.text)))
    assert [r["Company"] for r in rows] == [s.name for s in ss] and rows[0]["email"] == ss[0].email
    m = (await client.get("/v1/metrics", params={"job_id": jid})).json()
    assert m["runs"] == 3 and "tier2_http" in m["tiers"] and m["tiers"]["tier2_http"]["latency_ms_p95"] >= 0
    bad = await client.post("/v1/jobs/csv", content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})
    assert bad.status_code == 422


async def test_auth_token_enforced(fixtures):
    eng = await fresh_engine(fixtures, api_token="s3cret")
    app = create_app(eng.settings, engine=eng)
    app.state.engine = eng
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
            assert (await c.get("/v1/metrics")).status_code == 401
            assert (await c.get("/v1/metrics", headers={"authorization": "Bearer s3cret"})).status_code == 200
            assert (await c.get("/healthz")).status_code == 200
    finally:
        await eng.stop()
