import os
import uuid

import pytest
import pytest_asyncio

from leadengine.config import Settings
from leadengine.engine import Engine
from leadengine.fixture_sites import FixtureServer

DB_URL = os.environ.get("LE_TEST_DATABASE_URL", "postgresql://enrich@127.0.0.1:55432/enrich")


def make_settings(**kw) -> Settings:
    base = dict(database_url=DB_URL, host_overrides="*.fixture.test=127.0.0.1",
                per_domain_min_delay=0.0, per_domain_concurrency=4, http_max_retries=2, backoff_base=0.05,
                backoff_max=0.2, browser_enabled=False, user_agent="LeadEnrichmentTest/1.0",
                allowed_ports="80,443", ai_provider="")
    base.update(kw)
    return Settings(**base)


@pytest.fixture(scope="session")
def fixtures():
    with FixtureServer(n_sites=60, cycle=True) as fx:
        yield fx


async def fresh_engine(fixtures, provider=None, **settings) -> Engine:
    """Engine on an emptied database (tests share one local test database)."""
    fixtures.state.reset()
    eng = Engine(make_settings(**settings), provider=provider)
    await eng.start()
    async with eng.db.connection() as c:
        await c.execute("TRUNCATE jobs, companies, http_cache, extraction_cache, enrichment_runs RESTART IDENTITY CASCADE")
    return eng


@pytest_asyncio.fixture
async def engine(fixtures):
    eng = await fresh_engine(fixtures)
    yield eng
    await eng.stop()


def site_of(fixtures, kind: str, nth: int = 0):
    sites = [s for s in fixtures.state.sites.values() if s.kind == kind]
    assert len(sites) > nth, f"fixture has no {kind} site #{nth}"
    return sites[nth]


def url_of(fixtures, site) -> str:
    return fixtures.url(site.host)
