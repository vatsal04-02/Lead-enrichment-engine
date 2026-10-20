"""Append-only audit trail of outbound requests, written in batches off the request path."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlsplit

from .security import sanitize_for_log

log = logging.getLogger("leadengine.audit")

INSERT = """INSERT INTO fetch_log (job_id, kind, host, url, status, from_cache, bytes, elapsed_ms, retries, error_kind, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


class FetchAudit:
    def __init__(self, db, queue_size: int = 5000, batch: int = 200, interval: float = 0.5):
        self.db, self.batch, self.interval = db, batch, interval
        self.q: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._task: Optional[asyncio.Task] = None
        self.failed_batches = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._writer())

    async def record(self, *, job_id, kind: str, url: str, res) -> None:
        """Awaiting put() applies backpressure instead of dropping audit rows."""
        host = (urlsplit(url).hostname or "")[:253]
        await self.q.put((job_id, kind, host, url[:2000], res.status or None, res.from_cache, res.bytes or 0,
                          int(res.elapsed_ms) if res.elapsed_ms else None, res.retries, res.error_kind or None,
                          sanitize_for_log(res.error, 300) if res.error else None))

    async def _flush_some(self) -> None:
        rows = []
        while len(rows) < self.batch and not self.q.empty():
            rows.append(self.q.get_nowait())
        if not rows:
            return
        try:
            async with self.db.connection() as c:
                async with c.cursor() as cur:
                    await cur.executemany(INSERT, rows)
        except Exception:
            self.failed_batches += 1
            log.exception("audit batch of %d rows failed; re-queued", len(rows))
            for r in rows:
                self.q.put_nowait(r) if not self.q.full() else None
            await asyncio.sleep(1.0)

    async def _writer(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self._flush_some()

    async def flush(self) -> None:
        while not self.q.empty():
            await self._flush_some()

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.flush()
