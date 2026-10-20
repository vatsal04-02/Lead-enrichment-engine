"""PostgreSQL access: one async connection pool, idempotent schema bootstrap."""
from __future__ import annotations

from importlib import resources
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings, get_settings

SCHEMA_LOCK_ID = 727301


class Database:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.pool = AsyncConnectionPool(
            self.settings.database_url,
            min_size=self.settings.db_pool_min,
            max_size=self.settings.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )

    async def open(self, init: bool = True) -> "Database":
        await self.pool.open(wait=True, timeout=15)
        if init:
            await self.init_schema()
        return self

    async def close(self) -> None:
        await self.pool.close()

    def connection(self):
        return self.pool.connection()

    async def init_schema(self) -> None:
        sql = resources.files("leadengine").joinpath("schema.sql").read_text()
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            await conn.execute(sql)

    async def pool_pressure(self) -> float:
        """0..1 — fraction of pool in use; used for backpressure."""
        st = self.pool.get_stats()
        in_use = st.get("pool_size", 0) - st.get("pool_available", 0)
        return in_use / max(1, self.settings.db_pool_max)
