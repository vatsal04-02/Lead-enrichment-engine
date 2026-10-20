"""All tunables, read from LE_* environment variables (see .env.example).

Defaults are deliberately conservative. Raise concurrency only after running
the benchmark (benchmarks/README.md) against your own infrastructure.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LE_", env_file=".env", extra="ignore")

    # --- storage ---
    database_url: str = "postgresql://enrich@127.0.0.1:55432/enrich"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- concurrency / backpressure ---
    global_concurrency: int = 8          # companies enriched at once per worker process
    queue_size: int = 32                 # bounded hand-off queue between claimer and workers
    http_max_connections: int = 50
    http_max_keepalive: int = 20
    per_domain_concurrency: int = 2
    per_domain_min_delay: float = 0.5    # seconds between request starts to one host
    max_tracked_domains: int = 20000     # idle domain-limiter states beyond this are evicted

    # --- HTTP ---
    user_agent: str = "LeadEnrichmentBot/0.1 (+https://example.invalid/bot; set LE_USER_AGENT)"
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    total_timeout: float = 20.0
    max_response_bytes: int = 2_000_000
    max_redirects: int = 5
    http_max_retries: int = 2
    backoff_base: float = 0.5
    backoff_max: float = 8.0
    max_retry_after: float = 30.0        # a longer Retry-After is not waited out; the fetch fails
    respect_robots: bool = True
    max_crawl_delay: float = 30.0        # robots.txt Crawl-delay above this => the site is skipped, not hammered or waited out
    http_cache_default_ttl: int = 86400  # used when the origin sends no freshness info
    http_cache_max_body_bytes: int = 1_000_000
    egress_proxy: Optional[str] = None   # explicit proxy; weakens DNS-rebinding pinning (see README)

    # --- SSRF guard ---
    allowed_ports: str = "80,443,8080,8443"
    # TEST/BENCHMARK ONLY: "host=ip,*.suffix=ip". Listed hosts resolve to the given address and are exempt
    # from the private-address/port rules. Every other host stays fully guarded. Never set in production.
    host_overrides: str = ""

    # --- extraction limits ---
    max_pages_per_company: int = 4       # homepage + targeted pages
    max_pages_per_domain: int = 6        # hard cap per host within one company run
    browser_enabled: bool = True
    browser_concurrency: int = 1
    browser_timeout: float = 20.0
    browser_executable: Optional[str] = None   # e.g. /opt/pw-browsers/chromium when Playwright's own build isn't installed
    ai_provider: str = ""                # "" (disabled) | "anthropic"
    ai_model: str = "claude-haiku-5-5"
    ai_max_input_chars: int = 12000
    ai_max_output_tokens: int = 1024
    ai_price_in_per_mtok: float = 0.10   # USD; set to your model's price
    ai_price_out_per_mtok: float = 0.50
    ai_timeout: float = 30.0

    email_dns_check: bool = False        # MX/A lookup for email domains (recorded as domain_mx)
    parse_workers: int = 0               # 0 = parse in the default thread pool; N>0 = N parser processes

    # --- freshness ---
    default_freshness_days: int = 90
    freshness_days: str = ""             # "email=60,address=180"
    not_found_retry_days: int = 14       # how long a "looked, found nothing" result suppresses re-extraction
    min_completeness_to_skip: float = 0.0  # 0 => skip only when every requested field is fresh/decided

    # --- jobs ---
    max_concurrent_jobs: int = 2
    max_companies_per_job: int = 100000
    max_csv_bytes: int = 200_000_000
    job_lease_seconds: int = 60
    job_heartbeat_seconds: int = 15
    item_max_attempts: int = 3
    item_retry_base_seconds: float = 2.0   # backoff for transient item failures (exp. + jitter, capped 300s)
    claim_batch_size: int = 16
    shutdown_grace_seconds: float = 20.0
    ingest_batch_size: int = 1000

    # --- default per-job budgets (0 = unlimited) ---
    budget_pages: int = 100000
    budget_ai_calls: int = 1000
    budget_browser_ops: int = 200
    budget_seconds: int = 21600
    budget_est_cost_usd: float = 10.0

    # --- cost model (estimates; unset => 0 and reported as excluded) ---
    cost_per_http_request_usd: float = 0.0
    browser_cost_per_second_usd: float = 0.0
    compute_usd_per_hour: float = 0.0

    # --- API ---
    api_token: Optional[str] = None      # if set, required as "Authorization: Bearer <token>"

    def port_set(self) -> set[int]:
        return {int(p) for p in self.allowed_ports.split(",") if p.strip()}

    def overrides(self) -> dict[str, str]:
        out = {}
        for part in self.host_overrides.split(","):
            if "=" in part:
                h, ip = part.split("=", 1)
                out[h.strip().lower()] = ip.strip()
        return out

    def override_for(self, host: str) -> Optional[str]:
        ov = self.overrides()
        if host in ov:
            return ov[host]
        for k, ip in ov.items():
            if k.startswith("*.") and host.endswith(k[1:]):
                return ip
        return None

    def freshness_for(self, field: str, default: int) -> int:
        for part in self.freshness_days.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == field:
                    return int(v)
        return default


@lru_cache
def get_settings() -> Settings:
    return Settings()
