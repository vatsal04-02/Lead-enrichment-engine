"""REST API (FastAPI). Run with `leadengine serve`; run workers with `leadengine worker`."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import jobs, metrics, store
from .budget import Budget
from .config import Settings, get_settings
from .csv_io import CSVError
from .domain import InvalidDomain, normalize
from .engine import Engine
from .fields import FIELDS, resolve_requested


class EnrichRequest(BaseModel):
    website: str
    name: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None
    fields: Optional[list[str]] = None
    force: bool = False


class JobCompany(BaseModel):
    website: Optional[str] = None
    domain: Optional[str] = None
    name: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None


class JobRequest(BaseModel):
    companies: list[JobCompany] = Field(min_length=1)
    fields: Optional[list[str]] = None
    budget: Optional[dict] = None
    idempotency_key: Optional[str] = None


def create_app(settings: Optional[Settings] = None, engine: Optional[Engine] = None) -> FastAPI:
    settings = settings or get_settings()
    sync_sem = asyncio.Semaphore(max(1, settings.global_concurrency))   # bound ad-hoc synchronous enrichments

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        eng = engine or Engine(settings)
        if engine is None:
            await eng.start()
        app.state.engine = eng
        yield
        if engine is None:
            await eng.stop()

    app = FastAPI(title="Lead Enrichment Engine", version="0.1.0", lifespan=lifespan)

    def auth(authorization: Optional[str] = Header(default=None)):
        if settings.api_token is not None:
            expected = f"Bearer {settings.api_token.get_secret_value()}".encode()
            if not hmac.compare_digest((authorization or "").encode(), expected):
                raise HTTPException(401, "missing or invalid bearer token")

    eng_ = lambda r: r.app.state.engine

    @app.get("/healthz")
    async def healthz(request: Request):
        async with eng_(request).db.connection() as c:
            await c.execute("SELECT 1")
        return {"ok": True}

    @app.post("/v1/enrich", dependencies=[Depends(auth)])
    async def enrich(req: EnrichRequest, request: Request):
        eng = eng_(request)
        try:
            fields = resolve_requested(req.fields)
            ident = normalize(req.website)
        except (ValueError, InvalidDomain) as e:
            raise HTTPException(422, str(e))
        budget = Budget.from_config(jobs.default_budget(settings))
        async with sync_sem:
            res = await eng.enricher.enrich(req.website, name=req.name, location=req.location,
                                            industry=req.industry, requested=fields, budget=budget, force=req.force)
        async with eng.db.connection() as c:
            view = await store.company_view(c, ident.key, fields)
        return {"outcome": res.outcome, "error_kind": res.error_kind or None, "error": res.error or None,
                "transient_failure": res.transient, "company": view, "plan": res.plan, "metrics": res.metrics,
                "budget_exhausted": res.budget_exhausted}

    @app.get("/v1/companies/{domain}", dependencies=[Depends(auth)])
    async def company(domain: str, request: Request, fields: Optional[str] = None, history: bool = False):
        try:
            req = resolve_requested(fields.split(",") if fields else None)
        except ValueError as e:
            raise HTTPException(422, str(e))
        async with eng_(request).db.connection() as c:
            view = await store.company_view(c, domain.lower(), req, include_history=history)
        if not view:
            raise HTTPException(404, "unknown company")
        return view

    @app.post("/v1/jobs", dependencies=[Depends(auth)], status_code=202)
    async def create_job(req: JobRequest, request: Request):
        try:
            return await jobs.create_job_from_companies(
                eng_(request).db, settings, [c.model_dump() for c in req.companies], requested=req.fields,
                budget=req.budget, idempotency_key=req.idempotency_key)
        except (jobs.JobError, ValueError) as e:
            raise HTTPException(422, str(e))

    @app.post("/v1/jobs/csv", dependencies=[Depends(auth)], status_code=202)
    async def create_csv_job(request: Request, filename: str = "upload.csv", fields: Optional[str] = None,
                             mapping: Optional[str] = Query(default=None, description='JSON, e.g. {"website":"URL"}'),
                             budget: Optional[str] = None, idempotency_key: Optional[str] = None):
        """Send the CSV as the raw request body (Content-Type: text/csv). It is spooled to disk, never held in memory."""
        try:
            mp = json.loads(mapping) if mapping else None
            bd = json.loads(budget) if budget else None
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"mapping/budget must be JSON: {e}")
        fd, path = tempfile.mkstemp(suffix=".csv")
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_csv_bytes:
                        raise HTTPException(413, f"upload exceeds LE_MAX_CSV_BYTES={settings.max_csv_bytes}")
                    f.write(chunk)
            return await jobs.create_job_from_csv(
                eng_(request).db, settings, path, filename=filename, requested=fields.split(",") if fields else None,
                mapping=mp, budget=bd, idempotency_key=idempotency_key)
        except (CSVError, jobs.JobError, ValueError) as e:
            raise HTTPException(422, str(e))
        finally:
            os.unlink(path)

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    async def job(job_id: str, request: Request):
        st = await _job(request, job_id)
        return st

    async def _job(request, job_id):
        try:
            st = await jobs.job_status(eng_(request).db, job_id)
        except Exception:
            raise HTTPException(404, "unknown job")
        if not st:
            raise HTTPException(404, "unknown job")
        return st

    @app.get("/v1/jobs/{job_id}/items", dependencies=[Depends(auth)])
    async def items(job_id: str, request: Request, status: Optional[str] = None, after: int = 0, limit: int = 100):
        await _job(request, job_id)
        rows = await jobs.job_items(eng_(request).db, job_id, status=status, limit=limit, after=after)
        return {"items": rows, "next_after": rows[-1]["row_index"] if rows else None}

    @app.get("/v1/jobs/{job_id}/export.csv", dependencies=[Depends(auth)])
    async def export(job_id: str, request: Request):
        await _job(request, job_id)
        return StreamingResponse(jobs.export_csv(eng_(request).db, job_id), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="enriched-{job_id}.csv"'})

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(job_id: str, request: Request):
        await _job(request, job_id)
        return {"cancel_requested": await jobs.request_cancel(eng_(request).db, job_id)}

    @app.post("/v1/jobs/{job_id}/retry-failed", dependencies=[Depends(auth)])
    async def retry(job_id: str, request: Request):
        await _job(request, job_id)
        return {"requeued": await jobs.retry_failed(eng_(request).db, job_id)}

    @app.get("/v1/metrics", dependencies=[Depends(auth)])
    async def get_metrics(request: Request, job_id: Optional[str] = None):
        return await metrics.collect(eng_(request).db, job_id)

    @app.get("/v1/fields")
    async def list_fields():
        return {f: {"kind": s.kind, "freshness_days": s.freshness_days, "weight": s.weight, "ai_eligible": s.ai_eligible}
                for f, s in FIELDS.items() if f != "website"}

    return app
