"""Glue: build the primary (ScrapeGraphAI/Ollama) enricher with an explicit, reported fallback, and export the run's fetch log."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..backends import BackendConfigError, BackendUnavailable, make_llm_backend
from ..config import Settings
from ..domain import registrable
from ..pipeline import Enricher
from .doctor import DEFAULT_MODEL, DEFAULT_URL


def scrapegraph_settings(base: Settings, *, model: str, base_url: str, python: str, request_timeout: float = 120.0) -> Settings:
    """The base crawl settings plus the model backend, tuned for a small local model: thinking off for Ollama reasoning models
    (qwen3), a longer per-call timeout (CPU inference), max 2 pages / 2 calls per company, one worker, no retries."""
    s = base.model_copy(update={
        "extraction_backend": "scrapegraph", "scrapegraph_enabled": True, "scrapegraph_provider": "ollama", "scrapegraph_model": model,
        "scrapegraph_base_url": base_url, "scrapegraph_python": python or None, "scrapegraph_request_timeout": request_timeout,
        "scrapegraph_ollama_thinking": "off" if model.lower().startswith("qwen3") else "default",
        "scrapegraph_max_calls_per_company": 2, "scrapegraph_max_pages_per_company": 2, "scrapegraph_workers": 1, "scrapegraph_retries": 0})
    s._check()
    return s


async def build_primary(engine, base: Settings, *, mode: str, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_URL, python: str = "",
                        request_timeout: float = 120.0) -> tuple[Optional[Enricher], object, str]:
    """-> (primary enricher | None, backend | None, human-readable note for the report).
    mode 'inhouse'    : no model; the in-house engine only.
    mode 'auto'       : try ScrapeGraphAI/Ollama; if it cannot start, use the in-house engine and SAY SO (recorded, never silent).
    mode 'scrapegraph': same, but refuse to run without it."""
    if mode == "inhouse":
        return None, None, "in-house engine only (--backend inhouse)"
    backend = None
    try:
        s = scrapegraph_settings(base, model=model, base_url=base_url, python=python, request_timeout=request_timeout)
        backend = make_llm_backend(s)
        await backend.preflight()
    except (BackendConfigError, BackendUnavailable, ValueError) as e:
        if backend is not None:
            await backend.aclose()
        if mode == "scrapegraph":
            raise
        return None, None, (f"ScrapeGraphAI/Ollama ({model}) was NOT available: {e}. "
                            f"The whole run used the in-house engine instead (recorded fallback).")
    en = Enricher(s, engine.db, engine.fetcher, engine.enricher.ai, engine.browser, engine._pool, llm_backend=backend)
    return en, backend, f"ScrapeGraphAI with local Ollama model `{model}` (thinking off for reasoning models); in-house engine as per-company fallback"


async def export_fetch_log(engine, domains: set[str], since, out_dir: Path) -> Optional[Path]:
    """Every request this run made for the candidate sites (from the engine's fetch_log table), as JSON lines."""
    await engine.audit.flush()
    async with engine.db.connection() as c:
        rows = await (await c.execute(
            "SELECT ts, kind, host, url, status, from_cache, bytes, elapsed_ms, retries, error_kind, error FROM fetch_log "
            "WHERE ts >= %s ORDER BY ts", (since,))).fetchall()
    keep = [r for r in rows if registrable(r["host"] or "") in domains]
    if not keep:
        return None
    p = out_dir / "fetch_log.jsonl"
    with p.open("x", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps({**r, "ts": r["ts"].isoformat()}, default=str) + "\n")
    return p
