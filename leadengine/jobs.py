"""PostgreSQL-backed jobs: creation, CSV ingest, safe claiming with leases, completion, recovery.

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED inside short transactions. A job (and each
item) carries a lease that its worker renews; if a worker dies the lease expires and the work is
re-claimed. Item completion is fenced on (claimed_by, status='running'), so a zombie worker that
wakes up after losing its lease cannot overwrite the new owner's result.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable, Optional

from psycopg.types.json import Jsonb

from . import store
from .config import Settings
from .csv_io import CSVReader, CSVError, ParsedRow, spreadsheet_safe, status_string
from .db import Database
from .domain import InvalidDomain, normalize
from .fields import FIELDS, resolve_requested
from .scoring import field_status, score_company

CLAIM_LOCK = 727302
TERMINAL = ("succeeded", "failed", "invalid", "duplicate", "skipped")


class JobError(ValueError):
    pass


def default_budget(s: Settings, overrides: Optional[dict] = None) -> dict:
    b = {"pages": s.budget_pages, "ai_calls": s.budget_ai_calls if s.ai_provider else 0,
         "browser_ops": s.budget_browser_ops if s.browser_enabled else 0,
         "seconds": s.budget_seconds, "est_cost_usd": s.budget_est_cost_usd}
    if overrides:
        for k in b:
            if k in overrides:
                b[k] = overrides[k]
    return b


async def _insert_job(db: Database, *, type_: str, input_ref: str, requested: list[str], config: dict,
                      idempotency_key: Optional[str]) -> tuple[str, bool]:
    async with db.connection() as c:
        if idempotency_key:
            row = await (await c.execute("SELECT id FROM jobs WHERE idempotency_key=%s", (idempotency_key,))).fetchone()
            if row:
                return str(row["id"]), False
        row = await (await c.execute(
            """INSERT INTO jobs (type, input_ref, requested_fields, config, idempotency_key)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (type_, input_ref, requested, Jsonb(config), idempotency_key))).fetchone()
        return str(row["id"]), True


async def _insert_items(c, job_id: str, items: list[tuple]) -> None:
    async with c.cursor() as cur:
        await cur.executemany(
            """INSERT INTO job_items (job_id, row_index, status, input, domain, duplicate_of, known_company,
                   error_kind, error, finished_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s = 'queued' THEN NULL ELSE now() END)
               ON CONFLICT DO NOTHING""",
            [(job_id, i, st, Jsonb(inp), dom, dup, known, ek, err, st) for (i, st, inp, dom, dup, known, ek, err) in items])


async def _ingest_rows(db: Database, s: Settings, job_id: str, rows: Iterable[ParsedRow]) -> dict:
    seen: dict[str, int] = {}
    counts = {"total": 0, "queued": 0, "invalid": 0, "duplicate": 0, "known_company": 0}
    batch: list[ParsedRow] = []

    async def flush():
        if not batch:
            return
        async with db.connection() as c:
            existing = await store.existing_domains(c, list({r.domain for r in batch if r.domain}))
            items = []
            for r in batch:
                if r.error:
                    items.append((r.row_index, "invalid", r.original, r.domain, None, False, "invalid_input", r.error))
                    counts["invalid"] += 1
                elif r.domain in seen:
                    items.append((r.row_index, "duplicate", r.original, r.domain, seen[r.domain], False,
                                  "duplicate_in_file", f"same company as row {seen[r.domain]}"))
                    counts["duplicate"] += 1
                else:
                    seen[r.domain] = r.row_index
                    known = r.domain in existing
                    counts["known_company"] += known
                    items.append((r.row_index, "queued", r.original, r.domain, None, known, None, None))
                    counts["queued"] += 1
            await _insert_items(c, job_id, items)
        batch.clear()

    for r in rows:
        counts["total"] += 1
        if counts["total"] > s.max_companies_per_job:
            raise JobError(f"input exceeds LE_MAX_COMPANIES_PER_JOB={s.max_companies_per_job}")
        batch.append(r)
        if len(batch) >= s.ingest_batch_size:
            await flush()
    await flush()
    return counts


async def _mark_ready(db: Database, job_id: str, counts: dict, extra_config: Optional[dict] = None) -> None:
    async with db.connection() as c:
        await c.execute(
            """UPDATE jobs SET total=%s, ready=TRUE, stats = stats || %s,
                   config = config || %s WHERE id=%s""",
            (counts["total"], Jsonb({"ingest": counts}), Jsonb(extra_config or {}), job_id))
        # rows that were never going to be processed are terminal immediately
        await c.execute("""UPDATE jobs SET processed = (SELECT count(*) FROM job_items WHERE job_id=%s
                           AND status IN ('invalid','duplicate')) WHERE id=%s""", (job_id, job_id))


async def create_job_from_csv(db: Database, s: Settings, path: str, *, filename: str, requested: Optional[list[str]],
                              mapping: Optional[dict] = None, budget: Optional[dict] = None,
                              idempotency_key: Optional[str] = None) -> dict:
    reader = CSVReader(path, mapping)
    req = resolve_requested(requested)
    cfg = {"budget": default_budget(s, budget), "mapping": reader.mapping, "columns": reader.headers,
           "encoding": reader.encoding, "delimiter": reader.delimiter}
    job_id, created = await _insert_job(db, type_="enrich_csv", input_ref=filename, requested=req, config=cfg,
                                        idempotency_key=idempotency_key)
    if not created:
        return {"job_id": job_id, "created": False}
    try:
        counts = await _ingest_rows(db, s, job_id, reader.rows())
    except Exception:
        async with db.connection() as c:
            await c.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
        raise
    await _mark_ready(db, job_id, counts)
    return {"job_id": job_id, "created": True, "ingest": counts, "mapping": reader.mapping,
            "encoding": reader.encoding}


async def create_job_from_companies(db: Database, s: Settings, companies: list[dict], *, requested: Optional[list[str]],
                                    budget: Optional[dict] = None, idempotency_key: Optional[str] = None,
                                    input_ref: str = "api") -> dict:
    """companies: [{"website"|"domain": ..., "name"?, "location"?, "industry"?}]. Row ids are list positions."""
    if len(companies) > s.max_companies_per_job:
        raise JobError(f"too many companies ({len(companies)} > {s.max_companies_per_job})")
    req = resolve_requested(requested)
    mapping = {"website": "website", "name": "name", "location": "location", "industry": "industry"}
    cfg = {"budget": default_budget(s, budget), "mapping": mapping,
           "columns": ["website", "name", "location", "industry"]}
    job_id, created = await _insert_job(db, type_="enrich_companies", input_ref=input_ref, requested=req, config=cfg,
                                        idempotency_key=idempotency_key)
    if not created:
        return {"job_id": job_id, "created": False}

    def rows():
        for i, co in enumerate(companies, 1):
            orig = {"website": co.get("website") or co.get("domain") or "", "name": co.get("name") or "",
                    "location": co.get("location") or "", "industry": co.get("industry") or ""}
            pr = ParsedRow(i, orig, website=orig["website"] or None, name=orig["name"] or None,
                           location=orig["location"] or None, industry=orig["industry"] or None)
            try:
                if not pr.website:
                    pr.error = "missing website/domain"
                else:
                    pr.domain = normalize(pr.website).key
            except InvalidDomain as e:
                pr.error = f"invalid website/domain: {e}"
            yield pr
    try:
        counts = await _ingest_rows(db, s, job_id, rows())
    except Exception:
        async with db.connection() as c:
            await c.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
        raise
    await _mark_ready(db, job_id, counts)
    return {"job_id": job_id, "created": True, "ingest": counts}


# ------------------------------------------------------------------ claiming / leases
async def claim_job(db: Database, s: Settings, worker_id: str) -> Optional[dict]:
    """Claim one runnable job, honouring the global max-simultaneous-jobs limit. Also recovers
    jobs whose previous worker's lease expired."""
    async with db.connection() as c:
        await c.execute("SELECT pg_advisory_xact_lock(%s)", (CLAIM_LOCK,))
        live = (await (await c.execute(
            "SELECT count(*) n FROM jobs WHERE status='running' AND lease_expires_at > now()")).fetchone())["n"]
        if live >= s.max_concurrent_jobs:
            return None
        # abandoned ingests (crashed during upload) are failed, never claimed
        await c.execute("""UPDATE jobs SET status='failed', completed_at=now(),
                               error_summary = jsonb_build_object('error','ingest never completed')
                           WHERE ready=FALSE AND status='queued' AND created_at < now() - interval '1 hour'""")
        row = await (await c.execute(
            """SELECT id FROM jobs WHERE ready AND NOT cancel_requested
                  AND (status='queued' OR (status='running' AND lease_expires_at < now()))
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""")).fetchone()
        if not row:
            return None
        job = await (await c.execute(
            """UPDATE jobs SET status='running', claimed_by=%s, attempts=attempts+1,
                   lease_expires_at = now() + make_interval(secs => %s),
                   started_at = COALESCE(started_at, now())
               WHERE id=%s RETURNING *""", (worker_id, s.job_lease_seconds, row["id"]))).fetchone()
        return job


async def heartbeat(db: Database, s: Settings, job_id: str, worker_id: str, budget_spent: dict) -> dict:
    """Renew job + item leases and persist budget spend. Returns {'owned': bool, 'cancel': bool}."""
    async with db.connection() as c:
        row = await (await c.execute(
            """UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s),
                   stats = stats || jsonb_build_object('budget_spent', %s::jsonb)
               WHERE id=%s AND claimed_by=%s AND status='running'
               RETURNING cancel_requested""",
            (s.job_lease_seconds, Jsonb(budget_spent), job_id, worker_id))).fetchone()
        if not row:
            return {"owned": False, "cancel": False}
        await c.execute("""UPDATE job_items SET lease_expires_at = now() + make_interval(secs => %s)
                           WHERE job_id=%s AND claimed_by=%s AND status='running'""",
                        (s.job_lease_seconds, job_id, worker_id))
        return {"owned": True, "cancel": row["cancel_requested"]}


async def claim_items(db: Database, s: Settings, job_id: str, worker_id: str, n: int) -> list[dict]:
    async with db.connection() as c:
        # poison-pill guard: an item whose lease expired too many times (it keeps killing workers) fails
        await c.execute(
            """UPDATE job_items SET status='failed', error_kind='worker_crash',
                   error='gave up after repeated worker failures', finished_at=now()
               WHERE job_id=%s AND status='running' AND lease_expires_at < now() AND attempts >= %s""",
            (job_id, s.item_max_attempts))
        cur = await c.execute(
            """UPDATE job_items SET status='running', claimed_by=%s, attempts=attempts+1, started_at=now(),
                   lease_expires_at = now() + make_interval(secs => %s)
               WHERE (job_id, row_index) IN (
                   SELECT job_id, row_index FROM job_items
                   WHERE job_id=%s AND ((status='queued' AND (not_before IS NULL OR not_before <= now()))
                                        OR (status='running' AND lease_expires_at < now()))
                   ORDER BY row_index FOR UPDATE SKIP LOCKED LIMIT %s)
               RETURNING job_id, row_index, input, domain, attempts, known_company""",
            (worker_id, s.job_lease_seconds, job_id, n))
        return await cur.fetchall()


async def complete_item(db: Database, job_id: str, row_index: int, worker_id: str, *, status: str,
                        outcome: Optional[str], result: dict, error_kind: Optional[str] = None,
                        error: Optional[str] = None, company_id: Optional[int] = None) -> bool:
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE job_items SET status=%s, outcome=%s, result=%s, error_kind=%s, error=%s, company_id=%s,
                   finished_at=now(), claimed_by=NULL, lease_expires_at=NULL
               WHERE job_id=%s AND row_index=%s AND claimed_by=%s AND status='running'""",
            (status, outcome, Jsonb(result), error_kind, error, company_id, job_id, row_index, worker_id))
        return cur.rowcount == 1


async def requeue_item(db: Database, s: Settings, job_id: str, row_index: int, worker_id: str, attempts: int,
                       error_kind: str, error: str, *, penalize: bool = True) -> bool:
    """Transient failure: back to queued after exponential backoff + jitter (not_before)."""
    delay = min(300.0, s.item_retry_base_seconds * (2 ** max(0, attempts - 1)))
    delay = random.uniform(delay / 2, delay)
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE job_items SET status='queued', claimed_by=NULL, lease_expires_at=NULL,
                   not_before = now() + make_interval(secs => %s), error_kind=%s, error=%s,
                   attempts = CASE WHEN %s THEN attempts ELSE GREATEST(attempts-1,0) END
               WHERE job_id=%s AND row_index=%s AND claimed_by=%s AND status='running'""",
            (delay if penalize else 0, error_kind, error, penalize, job_id, row_index, worker_id))
        return cur.rowcount == 1


async def release_worker_items(db: Database, job_id: str, worker_id: str) -> int:
    """Graceful shutdown: hand unfinished items back without counting an attempt."""
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE job_items SET status='queued', claimed_by=NULL, lease_expires_at=NULL,
                   attempts=GREATEST(attempts-1,0)
               WHERE job_id=%s AND claimed_by=%s AND status='running'""", (job_id, worker_id))
        await c.execute("""UPDATE jobs SET status='queued', claimed_by=NULL, lease_expires_at=NULL
                           WHERE id=%s AND claimed_by=%s AND status='running'""", (job_id, worker_id))
        return cur.rowcount


async def skip_remaining(db: Database, job_id: str, error_kind: str, error: str) -> int:
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE job_items SET status='skipped', error_kind=%s, error=%s, finished_at=now()
               WHERE job_id=%s AND status='queued'""", (error_kind, error, job_id))
        return cur.rowcount


async def refresh_counts(db: Database, job_id: str) -> dict:
    async with db.connection() as c:
        r = await (await c.execute(
            """SELECT count(*) FILTER (WHERE status IN ('succeeded','failed','invalid','duplicate','skipped')) processed,
                      count(*) FILTER (WHERE status='succeeded') succeeded,
                      count(*) FILTER (WHERE status='failed') failed,
                      count(*) FILTER (WHERE status='invalid') invalid,
                      count(*) FILTER (WHERE status='duplicate') duplicate,
                      count(*) FILTER (WHERE status='skipped') skipped,
                      count(*) FILTER (WHERE status IN ('queued','running')) open
               FROM job_items WHERE job_id=%s""", (job_id,))).fetchone()
        await c.execute("UPDATE jobs SET processed=%s, succeeded=%s, failed=%s WHERE id=%s",
                        (r["processed"], r["succeeded"], r["failed"] + r["invalid"], job_id))
        return r


async def finalize_job(db: Database, job_id: str, worker_id: str, budget_snapshot: dict) -> str:
    r = await refresh_counts(db, job_id)
    async with db.connection() as c:
        job = await (await c.execute("SELECT cancel_requested, total FROM jobs WHERE id=%s", (job_id,))).fetchone()
        errs = await (await c.execute(
            """SELECT error_kind, count(*) n FROM job_items WHERE job_id=%s AND error_kind IS NOT NULL
               GROUP BY 1 ORDER BY 2 DESC""", (job_id,))).fetchall()
        if r["open"] and not job["cancel_requested"]:
            return "running"
        if job["cancel_requested"]:
            status = "cancelled"
        elif r["succeeded"] + r["duplicate"] == 0 and r["failed"] + r["invalid"] + r["skipped"] > 0:
            status = "failed"
        elif r["failed"] + r["invalid"] + r["skipped"] > 0:
            status = "partially_completed"
        else:
            status = "completed"
        await c.execute(
            """UPDATE jobs SET status=%s, completed_at=now(), lease_expires_at=NULL, claimed_by=NULL,
                   error_summary=%s, stats = stats || %s
               WHERE id=%s AND (claimed_by=%s OR claimed_by IS NULL)""",
            (status, Jsonb({e["error_kind"]: e["n"] for e in errs}),
             Jsonb({"budget_spent": budget_snapshot, "final_counts": r}), job_id, worker_id))
    return status


async def request_cancel(db: Database, job_id: str) -> bool:
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE jobs SET cancel_requested=TRUE WHERE id=%s AND status IN ('queued','running')""", (job_id,))
        # a queued job nobody has claimed is cancelled right away
        await c.execute(
            """WITH j AS (UPDATE jobs SET status='cancelled', completed_at=now() WHERE id=%s AND status='queued'
                          RETURNING id)
               UPDATE job_items SET status='skipped', error_kind='cancelled', error='job cancelled', finished_at=now()
               WHERE job_id IN (SELECT id FROM j) AND status='queued'""", (job_id,))
        return cur.rowcount == 1


async def retry_failed(db: Database, job_id: str) -> int:
    """Re-queue failed (not invalid/duplicate) items. Completed work is never repeated."""
    async with db.connection() as c:
        cur = await c.execute(
            """UPDATE job_items SET status='queued', attempts=0, not_before=NULL, error_kind=NULL, error=NULL,
                   finished_at=NULL WHERE job_id=%s AND status IN ('failed','skipped') AND error_kind <> 'invalid_input'""",
            (job_id,))
        n = cur.rowcount
        if n:
            await c.execute("""UPDATE jobs SET status='queued', completed_at=NULL, cancel_requested=FALSE,
                                   claimed_by=NULL, lease_expires_at=NULL WHERE id=%s""", (job_id,))
        return n


# ------------------------------------------------------------------ reads
async def job_status(db: Database, job_id: str) -> Optional[dict]:
    async with db.connection() as c:
        j = await (await c.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))).fetchone()
        if not j:
            return None
        live = await (await c.execute(
            """SELECT count(*) filter (where status='queued') queued, count(*) filter (where status='running') running,
                      count(*) filter (where status IN ('succeeded','failed','invalid','duplicate','skipped')) processed,
                      count(*) filter (where status='succeeded') succeeded,
                      count(*) filter (where status='failed') failed, count(*) filter (where status='invalid') invalid,
                      count(*) filter (where status='duplicate') duplicate, count(*) filter (where status='skipped') skipped
               FROM job_items WHERE job_id=%s""", (job_id,))).fetchone()
        agg = await (await c.execute(
            """SELECT count(*) runs, coalesce(sum(duration_ms),0) ms, coalesce(sum(http_requests),0) http_requests,
                      coalesce(sum(http_cache_hits),0) cache_hits, coalesce(sum(bytes_downloaded),0) bytes,
                      coalesce(sum(ai_calls),0) ai_calls, coalesce(sum(browser_ops),0) browser_ops,
                      coalesce(sum(retries),0) retries, coalesce(sum(cost_usd_est),0) cost_usd_est
               FROM enrichment_runs WHERE job_id=%s""", (job_id,))).fetchone()
    def ts(v):
        return v.isoformat() if v else None
    return {
        "job_id": str(j["id"]), "type": j["type"], "status": j["status"], "input_ref": j["input_ref"],
        "requested_fields": j["requested_fields"],
        "counts": {"total": j["total"], **{k: live[k] for k in live}, "failed_or_invalid": live["failed"] + live["invalid"]},
        "created_at": ts(j["created_at"]), "started_at": ts(j["started_at"]), "completed_at": ts(j["completed_at"]),
        "worker_claims": j["attempts"], "cancel_requested": j["cancel_requested"], "ready": j["ready"],
        "error_summary": j["error_summary"], "budget": j["config"].get("budget"),
        "stats": {**j["stats"], "measured": {k: (float(v) if k == "cost_usd_est" else int(v)) for k, v in agg.items()},
                  "cost_note": "cost_usd_est is an ESTIMATE from configured unit prices; tokens/requests are measured"},
    }


async def job_items(db: Database, job_id: str, *, status: Optional[str] = None, limit: int = 100,
                    after: int = 0) -> list[dict]:
    q = "SELECT row_index, status, outcome, domain, duplicate_of, known_company, attempts, error_kind, error, result FROM job_items WHERE job_id=%s AND row_index>%s"
    args: list = [job_id, after]
    if status:
        q += " AND status=%s"
        args.append(status)
    q += " ORDER BY row_index LIMIT %s"
    args.append(min(limit, 1000))
    async with db.connection() as c:
        return await (await c.execute(q, args)).fetchall()


async def export_csv(db: Database, job_id: str) -> AsyncIterator[str]:
    """Stream the result CSV: original columns first (unchanged), then le_* columns per requested field."""
    async with db.connection() as c:
        j = await (await c.execute("SELECT config, requested_fields FROM jobs WHERE id=%s", (job_id,))).fetchone()
    if not j:
        raise JobError("unknown job")
    cols: list[str] = j["config"].get("columns", [])
    req: list[str] = j["requested_fields"]
    extra = ["le_row", "le_status", "le_outcome", "le_error", "le_domain", "le_known_company_before",
             "le_duplicate_of_row", "le_completeness", "le_quality"]
    fcols = [x for f in req for x in (f, f"{f}__source_url", f"{f}__status")]
    buf = io.StringIO()
    w = csv.writer(buf)

    def emit(row):
        buf.seek(0); buf.truncate()
        w.writerow(row)
        return buf.getvalue()

    yield emit(cols + extra + fcols)
    async with db.pool.connection() as conn:
        async with conn.cursor(name=f"export_{job_id.replace('-', '')}") as cur:
            await cur.execute(
                """SELECT i.row_index, i.input, i.status, i.outcome, i.error, i.domain, i.known_company,
                          i.duplicate_of,
                          (SELECT jsonb_object_agg(f.field, to_jsonb(f)) FROM company_fields f
                           WHERE f.company_id = c.id) AS fields
                   FROM job_items i LEFT JOIN companies c ON c.domain = i.domain
                   WHERE i.job_id=%s ORDER BY i.row_index""", (job_id,))
            now = datetime.now(timezone.utc)
            async for r in cur:
                raw = r["fields"] or {}
                rows = {}
                for f, d in raw.items():
                    d["extracted_at"] = d["extracted_at"]
                    d["refresh_after"] = datetime.fromisoformat(d["refresh_after"])
                    rows[f] = d
                sc = score_company(rows, req, now) if rows else None
                orig = [r["input"].get(h, "") for h in cols]
                out = [*orig, r["row_index"], r["status"], r["outcome"] or "", r["error"] or "", r["domain"] or "",
                       r["known_company"], r["duplicate_of"] or "",
                       sc["completeness"] if sc else "", sc["quality"] if sc else ""]
                for f in req:
                    d = rows.get(f)
                    st = field_status(d, now)
                    out += [spreadsheet_safe(d["value"]) if d and d["status"] == "found" else "",
                            d["source_url"] if d and d["status"] == "found" and d["source_url"] else "",
                            status_string(st)]
                yield emit(out)
