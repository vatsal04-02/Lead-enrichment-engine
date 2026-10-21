"""Conservative crawl settings and the live-run gate. These mirror the Stage 5 pilot's forced limits (a test asserts they stay
equal); none of them can be relaxed from the command line."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ..config import Settings
from ..security import sanitize_for_log


class DiscoveryError(SystemExit):
    """User-facing refusal (exit text is the message)."""


def require_identifying_user_agent() -> str:
    ua = os.environ.get("LE_USER_AGENT", "")
    if not ua or "example.invalid" in ua or not re.search(r"(\+https?://|@\w)", ua):
        raise DiscoveryError('LE_USER_AGENT must identify you and include a contact, e.g. '
                             'LE_USER_AGENT="FlowHQBot/1.0 (+https://flowhq.example/bot; ops@flowhq.example)"')
    return ua


def forced_settings(*, database_url: str, user_agent: str, concurrency: int = 2, min_delay: float = 3.0, max_pages: int = 3,
                    retries: int = 1, **extra) -> Settings:
    """The conservative crawl limits, with no environment checks (also used to show a dry run's configuration truthfully).
    Forced: robots on, SSRF guard fully on (no host overrides, ports 80/443), <=4 hosts at once, 1 connection per host, >=2 s between
    requests to a host, <=3 pages/company, <=1 retry, 1 MB responses, hard 20 s deadline, browser OFF, in-house tier-5 AI OFF,
    every request audited."""
    return Settings(
        database_url=database_url, user_agent=user_agent, respect_robots=True, host_overrides="",
        allowed_ports="80,443", egress_proxy=None, global_concurrency=min(max(concurrency, 1), 4), per_domain_concurrency=1,
        per_domain_min_delay=max(2.0, min_delay), max_pages_per_company=min(max_pages, 3),
        max_pages_per_domain=min(max_pages, 3) + 1, http_max_retries=min(retries, 1), connect_timeout=5.0, read_timeout=10.0,
        total_timeout=20.0, max_response_bytes=1_000_000, max_redirects=3, browser_enabled=False, browser_concurrency=1,
        ai_provider="", fetch_audit_log=True, item_max_attempts=2, job_heartbeat_seconds=5, job_lease_seconds=30, **extra)


def live_settings(*, database_url: str, concurrency: int = 2, min_delay: float = 3.0, max_pages: int = 3, retries: int = 1,
                  **extra) -> Settings:
    """Settings for contacting real websites: forced_settings() plus the live-only environment checks."""
    if os.environ.get("LE_HOST_OVERRIDES"):
        raise DiscoveryError("LE_HOST_OVERRIDES must be empty for a live run (it relaxes the SSRF guard).")
    for var in ("LE_RESPECT_ROBOTS", "LE_ALLOWED_PORTS", "LE_MAX_REDIRECTS", "LE_EGRESS_PROXY"):
        if os.environ.get(var):
            print(f"note: {var} is set in the environment and is ignored/overridden by discovery", flush=True)
    return forced_settings(database_url=database_url, user_agent=require_identifying_user_agent(), concurrency=concurrency,
                           min_delay=min_delay, max_pages=max_pages, retries=retries, **extra)


def database_target(url: Optional[str]) -> str:
    """host:port/dbname of a PostgreSQL URL, WITHOUT credentials - safe to print and to store in run_config.json."""
    if hasattr(url, "get_secret_value"):                     # Settings.database_url is a SecretStr
        url = url.get_secret_value()
    if not url:
        return "(none)"
    try:
        p = urlsplit(url)
        return f"{p.hostname or '?'}:{p.port or 5432}/{(p.path or '/').lstrip('/') or '?'}"
    except ValueError:
        return "(unparseable)"


def live_gate(live: bool, terms_reviewed: bool, confirm_count: Optional[int], planned: int) -> bool:
    """False = dry run (nothing may be contacted). True = the person confirmed this exact, reviewed plan.
    Contacting real hosts needs explicit, specific confirmation (same idea as the pilot's --confirm-sites)."""
    if not live:
        if terms_reviewed or confirm_count is not None:
            raise DiscoveryError("--confirm-* flags only apply together with --live.")
        return False
    if not terms_reviewed:
        raise DiscoveryError("--live also needs --confirm-terms-reviewed (you have read the terms/robots of every source and site).")
    if confirm_count != planned:
        raise DiscoveryError(f"--confirm-count must equal the number of targets this run may contact ({planned}); got {confirm_count}.")
    return True


class JsonlAudit:
    """Fetch audit for the database-free discovery phase: one JSON line per request in <out>/discovery_fetch_log.jsonl."""

    def __init__(self, path: Path):
        self.path = path
        self.count = 0

    async def record(self, *, job_id, kind: str, url: str, res) -> None:
        self.count += 1
        line = {"kind": kind, "host": urlsplit(url).hostname, "url": url[:500], "status": res.status or None, "from_cache": res.from_cache,
                "bytes": res.bytes or 0, "elapsed_ms": int(res.elapsed_ms) if res.elapsed_ms else None,
                "error_kind": res.error_kind or None, "error": sanitize_for_log(res.error, 300) if res.error else None}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
