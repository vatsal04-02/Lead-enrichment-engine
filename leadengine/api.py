"""REST API (FastAPI). Run with `leadengine serve`; run workers with `leadengine worker`.

Authentication is a single shared bearer token (LE_API_TOKEN): there is no per-user identity, tenancy or
rate limiting. Put it behind a reverse proxy/gateway before exposing it beyond a trusted network.
"""
import asyncio
import hmac
import json
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import jobs, metrics, store
from .budget import Budget
from .config import Settings, get_settings
from .csv_io import CSVError
from .domain import InvalidDomain, normalize
from .engine import Engine
from .fields import FIELDS, resolve_requested

ITEM_STATUSES = "queued|running|succeeded|failed|invalid|duplicate|skipped"


class BodyLimitMiddleware:
    """Reject over-long JSON bodies before they are buffered (Content-Length and streamed)."""

    def __init__(self, app, max_bytes: int, exempt_paths: tuple[str, ...]):
        self.app, self.max_bytes, self.exempt = app, max_bytes, exempt_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.exempt:
            return await self.app(scope, receive, send)
        for k, v in scope["headers"]:
            if k == b"content-length" and v.isdigit() and int(v) > self.max_bytes:
                return await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge()
            return msg
        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)


class _TooLarge(Exception):
    pass


def _clean_filename(name: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:255] or "upload.csv"


def _job_uuid(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "unknown job")


def create_app(settings: Optional[Settings] = None, engine: Optional[Engine] = None) -> FastAPI:
    settings = settings or get_settings()
    sync_sem = asyncio.Semaphore(max(1, settings.global_concurrency))   # bound ad-hoc synchronous enrichments

    class EnrichRequest(BaseModel):
        website: str = Field(max_length=2048)
        name: Optional[str] = Field(default=None, max_length=500)
        location: Optional[str] = Field(default=None, max_length=500)
        industry: Optional[str] = Field(default=None, max_length=500)
        fields: Optional[list[str]] = Field(default=None, max_length=len(FIELDS))
        force: bool = False

    class JobCompany(BaseModel):
        website: Optional[str] = Field(default=None, max_length=2048)
        domain: Optional[str] = Field(default=None, max_length=2048)
        name: Optional[str] = Field(default=None, max_length=500)
        location: Optional[str] = Field(default=None, max_length=500)
        industry: Optional[str] = Field(default=None, max_length=500)

    class JobRequest(BaseModel):
        companies: list[JobCompany] = Field(min_length=1, max_length=settings.max_companies_per_job)
        fields: Optional[list[str]] = Field(default=None, max_length=len(FIELDS))
        budget: Optional[dict] = None
        idempotency_key: Optional[str] = Field(default=None, max_length=200)

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
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_json_bytes, exempt_paths=("/v1/jobs/csv",))

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
            view = await store.company_view(c, domain.lower()[:253], req, include_history=history)
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
    async def create_csv_job(request: Request, filename: str = "upload.csv", fields: Optional[str] = Query(default=None, max_length=500),
                             mapping: Optional[str] = Query(default=None, max_length=2000, description='JSON, e.g. {"website":"URL"}'),
                             budget: Optional[str] = Query(default=None, max_length=500),
                             idempotency_key: Optional[str] = Query(default=None, max_length=200)):
        """Send the CSV as the raw request body (Content-Type: text/csv). It is spooled to disk, never held in memory."""
        try:
            mp = json.loads(mapping) if mapping else None
            bd = json.loads(budget) if budget else None
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"mapping/budget must be JSON: {e}")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_csv_bytes:
            raise HTTPException(413, f"upload exceeds LE_MAX_CSV_BYTES={settings.max_csv_bytes}")
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
                eng_(request).db, settings, path, filename=_clean_filename(filename),
                requested=fields.split(",") if fields else None, mapping=mp, budget=bd, idempotency_key=idempotency_key)
        except (CSVError, jobs.JobError, ValueError) as e:
            raise HTTPException(422, str(e))
        finally:
            os.unlink(path)

    async def _job(request, job_id: str) -> dict:
        st = await jobs.job_status(eng_(request).db, _job_uuid(job_id))
        if not st:
            raise HTTPException(404, "unknown job")
        return st

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    async def job(job_id: str, request: Request):
        return await _job(request, job_id)

    @app.get("/v1/jobs/{job_id}/items", dependencies=[Depends(auth)])
    async def items(job_id: str, request: Request, status: Optional[str] = Query(default=None, pattern=f"^({ITEM_STATUSES})$"),
                    after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
        await _job(request, job_id)
        rows = await jobs.job_items(eng_(request).db, _job_uuid(job_id), status=status, limit=limit, after=after)
        return {"items": rows, "next_after": rows[-1]["row_index"] if rows else None}

    @app.get("/v1/jobs/{job_id}/export.csv", dependencies=[Depends(auth)])
    async def export(job_id: str, request: Request, safe: bool = Query(default=False, description=
                     "also neutralise spreadsheet formulas in the ORIGINAL columns (they are echoed unchanged by default)")):
        await _job(request, job_id)
        return StreamingResponse(jobs.export_csv(eng_(request).db, _job_uuid(job_id), sanitize_originals=safe),
                                 media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="enriched-{_job_uuid(job_id)}.csv"'})

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(job_id: str, request: Request):
        await _job(request, job_id)
        return {"cancel_requested": await jobs.request_cancel(eng_(request).db, _job_uuid(job_id))}

    @app.post("/v1/jobs/{job_id}/retry-failed", dependencies=[Depends(auth)])
    async def retry(job_id: str, request: Request):
        await _job(request, job_id)
        return {"requeued": await jobs.retry_failed(eng_(request).db, _job_uuid(job_id))}

    @app.get("/v1/metrics", dependencies=[Depends(auth)])
    async def get_metrics(request: Request, job_id: Optional[str] = None):
        return await metrics.collect(eng_(request).db, _job_uuid(job_id) if job_id else None)

    @app.get("/v1/fields")
    async def list_fields():
        return {f: {"kind": s.kind, "freshness_days": s.freshness_days, "weight": s.weight, "ai_eligible": s.ai_eligible}
                for f, s in FIELDS.items() if f != "website"}

    return app
