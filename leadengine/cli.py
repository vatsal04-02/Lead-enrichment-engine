"""Command line: `python -m leadengine <command>` (or the `leadengine` script)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from . import jobs, metrics, store
from .budget import Budget
from .compat import configure_event_loop
from .config import get_settings
from .domain import normalize
from .engine import Engine
from .fields import resolve_requested
from .worker import Worker


def _fields(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


async def cmd_init_db(a):
    async with Engine() as e:
        print("schema ready")


async def cmd_enrich(a):
    async with Engine() as e:
        req = resolve_requested(_fields(a.fields))
        b = Budget.from_config(jobs.default_budget(e.settings))
        res = await e.enricher.enrich(a.website, requested=req, budget=b, force=a.force)
        async with e.db.connection() as c:
            view = await store.company_view(c, normalize(a.website).key, req)
        print(json.dumps({"outcome": res.outcome, "error": res.error or None, "company": view,
                          "metrics": res.metrics, "plan": res.plan}, indent=2, default=str))


async def cmd_submit_csv(a):
    async with Engine() as e:
        out = await jobs.create_job_from_csv(
            e.db, e.settings, a.file, filename=a.file.split("/")[-1], requested=_fields(a.fields),
            mapping=json.loads(a.mapping) if a.mapping else None,
            budget=json.loads(a.budget) if a.budget else None, idempotency_key=a.idempotency_key)
        print(json.dumps(out, indent=2))


async def _run_worker(e: Engine, once: bool):
    w = Worker(e)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, w.request_stop)
        except NotImplementedError:      # Windows event loops: fall back to the signal module (SIGINT / Ctrl+C)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(w.request_stop))
    await w.run(once=once)


async def cmd_worker(a):
    async with Engine() as e:
        await _run_worker(e, a.once)


async def cmd_status(a):
    async with Engine() as e:
        print(json.dumps(await jobs.job_status(e.db, a.job_id), indent=2, default=str))


async def cmd_export(a):
    async with Engine() as e:
        out = open(a.output, "w", newline="") if a.output else sys.stdout
        async for chunk in jobs.export_csv(e.db, a.job_id):
            out.write(chunk)
        if a.output:
            out.close()


async def cmd_run_csv(a):
    """Local end-to-end: ingest -> process with an in-process worker -> export."""
    async with Engine() as e:
        info = await jobs.create_job_from_csv(
            e.db, e.settings, a.file, filename=a.file.split("/")[-1], requested=_fields(a.fields),
            mapping=json.loads(a.mapping) if a.mapping else None, budget=json.loads(a.budget) if a.budget else None)
        print(json.dumps(info), file=sys.stderr)
        await _run_worker(e, once=True)
        st = await jobs.job_status(e.db, info["job_id"])
        print(json.dumps({k: st[k] for k in ("job_id", "status", "counts", "error_summary")}, default=str), file=sys.stderr)
        with open(a.output, "w", newline="") as out:
            async for chunk in jobs.export_csv(e.db, info["job_id"]):
                out.write(chunk)
        print(f"wrote {a.output}", file=sys.stderr)


async def cmd_cancel(a):
    async with Engine() as e:
        print(await jobs.request_cancel(e.db, a.job_id))


async def cmd_retry(a):
    async with Engine() as e:
        print(await jobs.retry_failed(e.db, a.job_id))


async def cmd_metrics(a):
    async with Engine() as e:
        print(json.dumps(await metrics.collect(e.db, a.job_id), indent=2))


def cmd_serve(a):
    import uvicorn
    from .api import create_app
    settings = get_settings()
    if a.host not in ("127.0.0.1", "::1", "localhost") and settings.api_token is None:
        raise SystemExit("Refusing to listen on a non-loopback address without authentication: set LE_API_TOKEN "
                         "(and put the API behind TLS / a gateway).")
    if settings.host_overrides:
        logging.getLogger("leadengine").warning("LE_HOST_OVERRIDES is set: SSRF address rules are relaxed for %s "
                                                "(test/benchmark use only)", settings.host_overrides)
    uvicorn.run(create_app(settings), host=a.host, port=a.port)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["profile"]:           # `python -m leadengine profile INPUT.csv ...`: local-model company profile enrichment (no database)
        from .profiles.cli import main as profile_main
        raise SystemExit(profile_main(args[1:]))
    p = argparse.ArgumentParser(prog="leadengine")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    s = sub.add_parser("enrich"); s.add_argument("website"); s.add_argument("--fields"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_enrich)
    for name, fn in (("submit-csv", cmd_submit_csv), ("run-csv", cmd_run_csv)):
        s = sub.add_parser(name); s.add_argument("file"); s.add_argument("--fields"); s.add_argument("--mapping", help='JSON {"website":"Col"}')
        s.add_argument("--budget", help='JSON {"pages":1000,"ai_calls":0}')
        if name == "submit-csv":
            s.add_argument("--idempotency-key")
        else:
            s.add_argument("-o", "--output", required=True)
        s.set_defaults(fn=fn)
    s = sub.add_parser("worker"); s.add_argument("--once", action="store_true", help="exit when no runnable job remains"); s.set_defaults(fn=cmd_worker)
    for name, fn in (("job-status", cmd_status), ("cancel", cmd_cancel), ("retry-failed", cmd_retry)):
        s = sub.add_parser(name); s.add_argument("job_id"); s.set_defaults(fn=fn)
    s = sub.add_parser("export"); s.add_argument("job_id"); s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("metrics"); s.add_argument("--job-id"); s.set_defaults(fn=cmd_metrics)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000); s.set_defaults(fn=cmd_serve)
    a = p.parse_args(argv)
    configure_event_loop()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .backends import BackendConfigError, BackendUnavailable
    try:
        if asyncio.iscoroutinefunction(a.fn):
            asyncio.run(a.fn(a))
        else:
            a.fn(a)
    except (BackendConfigError, BackendUnavailable) as e:   # optional extraction backend: say what is wrong; never fall back to another engine
        raise SystemExit(f"error: extraction backend not usable: {e}\n(unset LE_EXTRACTION_BACKEND / LE_SCRAPEGRAPH_ENABLED to use the default in-house engine)")


if __name__ == "__main__":
    main()
