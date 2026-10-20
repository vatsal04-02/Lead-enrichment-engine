import csv
import io
import json

import httpx
import pytest
import pytest_asyncio

from leadengine.api import create_app
from leadengine.csv_io import spreadsheet_safe
from tests.conftest import fresh_engine, site_of, url_of


@pytest_asyncio.fixture
async def make_client(fixtures):
    engines = []

    async def make(**settings):
        eng = await fresh_engine(fixtures, **settings)
        engines.append(eng)
        app = create_app(eng.settings, engine=eng)
        app.state.engine = eng
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
        c.engine = eng
        return c
    yield make
    for e in engines:
        await e.stop()


async def test_oversized_json_bodies_and_lists_are_rejected(make_client):
    c = await make_client(max_companies_per_job=5, max_json_bytes=2000)
    big = {"companies": [{"website": f"a{i}.com"} for i in range(500)]}
    assert (await c.post("/v1/jobs", json=big)).status_code == 413                       # body size cap
    over = {"companies": [{"website": f"a{i}.com"} for i in range(6)]}
    r = await c.post("/v1/jobs", json=over)
    assert r.status_code == 422                                                           # list length cap
    c = await make_client()                                                               # default body cap
    assert (await c.post("/v1/enrich", json={"website": "a" * 5000 + ".com"})).status_code == 422
    assert (await c.post("/v1/enrich", json={"website": "a.com", "name": "n" * 600})).status_code == 422
    assert (await c.post("/v1/enrich", json={"website": "a.com", "fields": ["email"] * 50})).status_code == 422
    assert (await c.post("/v1/jobs", content=b"{not json", headers={"content-type": "application/json"})).status_code == 422


async def test_client_cannot_lift_budgets_through_the_api(make_client, fixtures):
    c = await make_client(budget_pages=100, budget_seconds=60, budget_est_cost_usd=1.0)
    r = await c.post("/v1/jobs", json={"companies": [{"website": "a.fixture.test"}], "budget": {"pages": 0, "seconds": 10**9}})
    assert r.status_code == 202
    st = (await c.get(f"/v1/jobs/{r.json()['job_id']}")).json()
    assert st["budget"]["pages"] == 100 and st["budget"]["seconds"] == 60
    bad = await c.post("/v1/jobs", json={"companies": [{"website": "a.fixture.test"}], "budget": {"nope": 1}})
    assert bad.status_code == 422


async def test_csv_upload_size_limit_and_filename_sanitised(make_client):
    c = await make_client(max_csv_bytes=200)
    body = "Company,Website\n" + "".join(f"x{i},x{i}.com\n" for i in range(100))
    assert (await c.post("/v1/jobs/csv", content=body.encode(), headers={"content-type": "text/csv"})).status_code == 413
    c2 = await make_client()
    r = await c2.post("/v1/jobs/csv", content=b"Company,Website\nx,x.com\n", params={"filename": "a\r\nSet-Cookie: x=1\x00" + "z" * 400 + ".csv"})
    assert r.status_code == 202
    ref = (await c2.get(f"/v1/jobs/{r.json()['job_id']}")).json()["input_ref"]
    assert "\n" not in ref and "\r" not in ref and "\x00" not in ref and len(ref) <= 255


@pytest.mark.parametrize("path", ["/v1/jobs/not-a-uuid", "/v1/jobs/not-a-uuid/items", "/v1/jobs/not-a-uuid/export.csv",
                                  "/v1/jobs/00000000-0000-0000-0000-000000000000"])
async def test_bad_or_unknown_job_ids_are_404_not_500(make_client, path):
    c = await make_client()
    assert (await c.get(path)).status_code == 404
    assert (await c.post("/v1/jobs/not-a-uuid/cancel")).status_code == 404


async def test_query_parameters_are_validated(make_client):
    c = await make_client()
    r = await c.post("/v1/jobs", json={"companies": [{"website": "a.fixture.test"}]})
    jid = r.json()["job_id"]
    assert (await c.get(f"/v1/jobs/{jid}/items", params={"status": "bogus"})).status_code == 422
    assert (await c.get(f"/v1/jobs/{jid}/items", params={"limit": 0})).status_code == 422
    assert (await c.get(f"/v1/jobs/{jid}/items", params={"limit": 100000})).status_code == 422
    assert (await c.get(f"/v1/jobs/{jid}/items", params={"after": -5})).status_code == 422


async def test_every_data_route_requires_the_token_and_uses_constant_time_compare(make_client, monkeypatch):
    c = await make_client(api_token="s3cret-token")
    jid = "00000000-0000-0000-0000-000000000000"
    routes = [("get", "/v1/metrics"), ("get", f"/v1/jobs/{jid}"), ("get", f"/v1/jobs/{jid}/items"),
              ("get", f"/v1/jobs/{jid}/export.csv"), ("post", f"/v1/jobs/{jid}/cancel"), ("post", f"/v1/jobs/{jid}/retry-failed"),
              ("get", "/v1/companies/a.com"), ("post", "/v1/enrich"), ("post", "/v1/jobs"), ("post", "/v1/jobs/csv")]
    for m, p in routes:
        r = await getattr(c, m)(p)
        assert r.status_code == 401, (m, p, r.status_code)
        r = await getattr(c, m)(p, headers={"authorization": "Bearer wrong"})
        assert r.status_code == 401, (m, p)
        r = await getattr(c, m)(p, headers={"authorization": "bearer s3cret-token"})      # scheme must match exactly
        assert r.status_code == 401, (m, p)
    import hmac
    seen = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: (seen.append(1), real(a, b))[1])
    await c.get("/v1/metrics", headers={"authorization": "Bearer s3cret-token"})
    assert seen
    assert (await c.get("/healthz")).status_code == 200          # liveness only; exposes nothing


def test_serve_refuses_to_expose_an_unauthenticated_api(monkeypatch):
    from leadengine import cli
    from leadengine.config import Settings
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(), raising=False)
    ns = type("A", (), {"host": "0.0.0.0", "port": 8000})()
    with pytest.raises(SystemExit, match="LE_API_TOKEN"):
        cli.cmd_serve(ns)


# ------------------------------------------------------------------ CSV formula injection
@pytest.mark.parametrize("v,safe", [("=1+1", False), ("@SUM(A1)", False), ("+cmd|' /C calc'!A0", False), ("-2+3", False),
                                    (" =1+1", False), ("\t=1", False), ("\r=1", False), ("\n=1+1", False), ("  @x", False),
                                    ("+1 512-555-0100", True), ("-5", True), ("(512) 555-0100", True), ("info@acme.com", True),
                                    ("https://acme.com/x", True), ("Acme =Co", True)])
def test_spreadsheet_safe_neutralises_formula_prefixes(v, safe):
    out = spreadsheet_safe(v)
    assert (out == v) == safe, (v, out)
    if not safe:
        assert out.startswith("'")


async def test_export_can_neutralise_original_columns_and_always_neutralises_scraped_and_error_text(make_client, fixtures):
    from leadengine import jobs
    from leadengine.worker import Worker
    c = await make_client()
    eng = c.engine
    s = site_of(fixtures, "plain")
    body = f'Company,Website\n"=HYPERLINK(""http://evil"")",{url_of(fixtures, s)}\n@bad,=cmd|calc\n'
    r = await c.post("/v1/jobs/csv", content=body.encode(), params={"fields": "email"})
    jid = r.json()["job_id"]
    await Worker(eng).run(once=True)
    default = list(csv.DictReader(io.StringIO((await c.get(f"/v1/jobs/{jid}/export.csv")).text)))
    assert default[0]["Company"].startswith("=HYPERLINK")                    # originals preserved by default
    safe = list(csv.DictReader(io.StringIO((await c.get(f"/v1/jobs/{jid}/export.csv", params={"safe": "true"})).text)))
    assert safe[0]["Company"].startswith("'=HYPERLINK") and safe[1]["Company"] == "'@bad" and safe[1]["Website"] == "'=cmd|calc"
    for rows in (default, safe):
        assert not rows[1]["le_error"].startswith(("=", "+", "-", "@"))
