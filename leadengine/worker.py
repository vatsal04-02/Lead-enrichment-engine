"""Job worker: claims jobs, streams items through a bounded queue to a bounded set of tasks.

Memory and load are bounded by construction:
  * at most `queue_size` claimed items are buffered; the claimer only fetches more when there is room
    (backpressure) and when the DB pool is not saturated;
  * exactly `global_concurrency` consumer tasks run (no task per row);
  * a shared semaphore caps in-flight enrichments across all jobs in this process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from collections import deque
from typing import Optional

from . import jobs
from .budget import Budget
from .config import Settings
from .domain import InvalidDomain
from .engine import Engine
from .security import sanitize_for_log

log = logging.getLogger("leadengine.worker")


class Backpressure:
    """Slows the claimer when recent results are dominated by transient (overload-type) failures."""

    def __init__(self, window: int = 20):
        self.recent: deque[bool] = deque(maxlen=window)
        self.delay = 0.0

    def record(self, transient_failure: bool) -> None:
        self.recent.append(transient_failure)
        if len(self.recent) >= 10 and sum(self.recent) / len(self.recent) >= 0.8:
            self.delay = min(30.0, max(1.0, self.delay * 2))
        else:
            self.delay = max(0.0, self.delay / 2 if self.delay > 0.5 else 0.0)


class Worker:
    def __init__(self, engine: Engine, worker_id: Optional[str] = None):
        self.engine = engine
        self.s: Settings = engine.settings
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = asyncio.Event()
        self.sem = asyncio.Semaphore(self.s.global_concurrency)
        self.bp = Backpressure()
        self._active: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- top level
    async def run(self, *, once: bool = False, poll_seconds: float = 2.0) -> None:
        """Run until stopped. once=True returns when no runnable job remains (used by CLI/tests/benchmarks)."""
        while not self.stop.is_set():
            job = None
            if len(self._active) < self.s.max_concurrent_jobs:
                job = await jobs.claim_job(self.engine.db, self.s, self.id)
            if job:
                t = asyncio.create_task(self._run_job(job))
                self._active.add(t)
                t.add_done_callback(self._active.discard)
                continue
            if once and not self._active:
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=poll_seconds if not self._active else 0.5)
            except asyncio.TimeoutError:
                pass
        await self._drain()

    async def _drain(self) -> None:
        """After a stop request: give in-flight work `shutdown_grace_seconds`, then cancel what is left.
        Cancelled jobs hand their unfinished items back to the queue (see _run_job)."""
        if not self._active:
            return
        _, pending = await asyncio.wait(self._active, timeout=self.s.shutdown_grace_seconds)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def request_stop(self) -> None:
        self.stop.set()

    # ---------------------------------------------------------------- one job
    async def _run_job(self, job: dict) -> None:
        db, s = self.engine.db, self.s
        job_id = str(job["id"])
        cfg = job["config"]
        budget = Budget.from_config(cfg.get("budget", {}), (job["stats"] or {}).get("budget_spent"))
        mapping = cfg.get("mapping", {})
        requested = job["requested_fields"]
        queue: asyncio.Queue = asyncio.Queue(maxsize=s.queue_size)
        inflight: dict[int, dict] = {}
        lost_lease = asyncio.Event()
        done_claiming = asyncio.Event()
        log.info("job %s claimed by %s (attempt %s)", job_id, self.id, job["attempts"])

        async def beat():
            while not (done_claiming.is_set() and not inflight and queue.empty()):
                await asyncio.sleep(s.job_heartbeat_seconds)
                hb = await jobs.heartbeat(db, s, job_id, self.id, budget.snapshot())
                if not hb["owned"]:
                    lost_lease.set()
                    return
                if hb["cancel"]:
                    done_claiming.set()

        async def claimer():
            try:
                while not (self.stop.is_set() or lost_lease.is_set() or done_claiming.is_set()):
                    budget.allows("pages", 0)       # also evaluates the seconds / cost limits
                    if budget.exhausted_reasons & {"seconds", "est_cost_usd"}:
                        break
                    room = s.queue_size - queue.qsize() - len(inflight)
                    if room <= 0 or self.bp.delay > 0 or await db.pool_pressure() > 0.9:
                        await asyncio.sleep(max(0.05, self.bp.delay))
                        continue
                    items = await jobs.claim_items(db, s, job_id, self.id, min(room, s.claim_batch_size))
                    if not items:
                        r = await jobs.refresh_counts(db, job_id)
                        if r["open"] == 0:
                            break
                        # remaining items are in flight elsewhere or waiting out a retry backoff
                        await asyncio.sleep(0.5 if not inflight and queue.empty() else 0.1)
                        continue
                    for it in items:
                        inflight[it["row_index"]] = it
                        await queue.put(it)
            finally:
                done_claiming.set()

        async def consumer():
            while True:
                try:
                    it = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    if done_claiming.is_set() and queue.empty():
                        return
                    continue
                try:
                    if lost_lease.is_set() or self.stop.is_set():
                        continue
                    await self._process_item(job_id, it, mapping, requested, budget)
                finally:
                    inflight.pop(it["row_index"], None)

        tasks = [asyncio.create_task(claimer()), asyncio.create_task(beat())] + \
                [asyncio.create_task(consumer()) for _ in range(s.global_concurrency)]
        try:
            await asyncio.gather(*tasks[:1], *tasks[2:])
        except asyncio.CancelledError:      # grace period over: return unfinished items, then propagate
            await asyncio.shield(jobs.release_worker_items(db, job_id, self.id))
            raise
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if lost_lease.is_set():
            log.warning("job %s: lease lost; another worker owns it now", job_id)
            return
        if self.stop.is_set():
            n = await jobs.release_worker_items(db, job_id, self.id)
            await jobs.heartbeat(db, s, job_id, self.id, budget.snapshot())
            log.info("job %s: shutdown, released %s unfinished item(s) for resumption", job_id, n)
            return
        # budget stop / cancellation: remaining queued items are skipped, reported, never silently lost
        hb = await jobs.heartbeat(db, s, job_id, self.id, budget.snapshot())
        if hb["cancel"]:
            await jobs.skip_remaining(db, job_id, "cancelled", "job cancelled")
        elif budget.exhausted_reasons & {"seconds", "est_cost_usd"}:
            await jobs.skip_remaining(db, job_id, "budget_exhausted",
                                      "job budget exhausted: " + ",".join(sorted(budget.exhausted_reasons)))
        status = await jobs.finalize_job(db, job_id, self.id, budget.snapshot())
        log.info("job %s finished: %s", job_id, status)

    # ---------------------------------------------------------------- one item
    async def _process_item(self, job_id: str, it: dict, mapping: dict, requested: list[str], budget: Budget) -> None:
        db, s = self.engine.db, self.s
        row_index, orig = it["row_index"], it["input"]
        get = lambda k: (orig.get(mapping[k], "") or "").strip() or None if k in mapping else None
        website = get("domain") or get("website")
        try:
            async with self.sem:
                res = await self.engine.enricher.enrich(
                    website, name=get("name"), location=get("location"), industry=get("industry"),
                    requested=requested, budget=budget, job_id=job_id, row_index=row_index)
        except InvalidDomain as e:
            await jobs.complete_item(db, job_id, row_index, self.id, status="invalid", outcome=None, result={},
                                     error_kind="invalid_input", error=str(e))
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:     # a bug or infrastructure error for ONE company must not stop the batch
            log.exception("item %s/%s crashed", job_id, row_index)
            err = sanitize_for_log(f"{type(e).__name__}: {e}")
            if it["attempts"] >= s.item_max_attempts:
                await jobs.complete_item(db, job_id, row_index, self.id, status="failed", outcome=None, result={},
                                         error_kind="internal_error", error=err)
            else:
                await jobs.requeue_item(db, s, job_id, row_index, self.id, it["attempts"], "internal_error", err)
            self.bp.record(False)
            return
        self.bp.record(res.transient)
        if res.transient:
            if it["attempts"] >= s.item_max_attempts:
                await jobs.complete_item(db, job_id, row_index, self.id, status="failed", outcome=res.outcome,
                                         result=res.summary(), error_kind=res.error_kind or "transient",
                                         error=f"gave up after {it['attempts']} attempts: {res.error}",
                                         company_id=res.company_id)
            else:
                await jobs.requeue_item(db, s, job_id, row_index, self.id, it["attempts"],
                                        res.error_kind or "transient", res.error)
            return
        if res.error_kind == "budget":        # nothing could be fetched within budget: report, don't pretend
            await jobs.complete_item(db, job_id, row_index, self.id, status="skipped", outcome=res.outcome,
                                     result=res.summary(), error_kind="budget_exhausted",
                                     error="page budget exhausted before this company could be fetched",
                                     company_id=res.company_id)
            return
        if res.outcome == "unreachable":
            await jobs.complete_item(db, job_id, row_index, self.id, status="failed", outcome=res.outcome,
                                     result=res.summary(), error_kind=f"unreachable_{res.error_kind}",
                                     error=res.error, company_id=res.company_id)
            return
        await jobs.complete_item(db, job_id, row_index, self.id, status="succeeded", outcome=res.outcome,
                                 result=res.summary(), company_id=res.company_id)
