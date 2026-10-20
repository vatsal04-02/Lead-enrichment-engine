# Lead Enrichment Engine

A cost-aware company enrichment engine: given a company website (single call, JSON list, or an uploaded CSV) it returns
contact/identity fields **with the source URL, extraction method, timestamp, freshness deadline and a defined verification
status for every value**. Missing data stays missing; nothing is invented.

Scope: the data engine, jobs, API and CSV pipeline. No frontend, payments or dashboard.

## How it works

```
company ──► Tier 1  PostgreSQL: reuse fresh fields, skip "already searched, nothing found" until retry date
        └─► Tier 2  homepage over pooled async httpx (cache, robots.txt, per-domain limits) → deterministic parse
        └─► Tier 3  1–3 targeted pages (/about, /contact …) only if a *valuable* field is still missing
        └─► Tier 4  Playwright, only if the homepage is a JavaScript shell and valuable fields are still missing
        └─► Tier 5  LLM, only for still-missing AI-eligible fields; extractive + grounded + schema-validated
```

* **Planner** (`planner.py`) decides before any I/O: which fields are missing / stale / preserved / recently-not-found,
  which page kinds can serve them, whether the AI tier is even eligible, and the page/retry caps.
* **Deterministic first** (`extract.py`): JSON-LD → microdata → `mailto:`/`tel:` → social links → meta tags → `<address>` →
  title heuristics → text regexes, ranked by method. The AI tier is a fallback, never a default.
* **Provenance** (`company_fields`): value, source URL, method, tier, extraction time, `refresh_after`, verification status +
  procedure, per-check booleans, alternates, content hash; value changes are written to `company_field_history`.
* **Quality scoring** (`scoring.py`) keeps *completeness* and *correctness evidence* separate (below).
* **Jobs** (`jobs.py`, `worker.py`): PostgreSQL only (no Redis): `FOR UPDATE SKIP LOCKED` claiming, leases + heartbeats,
  fenced completion, retry with exponential backoff + jitter, crash recovery, cancellation, per-job budgets.
* **Security** (`security.py`): SSRF guard with DNS-pinned connections; untrusted content is parsed, never executed.

### Quality vocabulary (per field, returned by the API and CSV export)

| flag | meaning |
|---|---|
| `present` | a value exists |
| `source_attributed` | a source URL and extraction method are recorded |
| `format_valid` | passes the field's format checks (syntax, digit count, year range, …) |
| `verified` | passed a **defined verification procedure** (see below) |
| `stale` | past its freshness deadline |

`quality = 0.4·present + 0.2·source_attributed + 0.2·format_valid + 0.2·verified`, halved if stale. Company-level:
`completeness` (weighted share of requested fields present), `quality` (weighted mean), `verified_share`.
Completeness says nothing about correctness. LLM self-reported confidence is never requested or used.

Verification procedures that exist: `http_reachable_domain_match` (website), `cross_source_agreement` (company_name, phone:
two independent extraction methods agree). **Email is never `verified`**: no mailbox-level verification (SMTP/provider API) is
implemented. Emails carry separate evidence instead: `syntax`, `published_on_source`, optional `domain_mx`
(`LE_EMAIL_DNS_CHECK=true`). `method_prior` is a static heuristic per extraction method, **not** a probability of correctness.

## Quick start

Requirements: Python ≥ 3.11, PostgreSQL ≥ 13.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,browser,ai]'            # browser/ai extras are optional
scripts/dev_postgres.sh                        # throwaway local cluster on :55432 (dev only; fsync off)
cp .env.example .env                           # then edit; set LE_USER_AGENT before touching real sites
python -m leadengine init-db                   # schema is also applied automatically on startup
```

```bash
python -m leadengine enrich acme.com --fields company_name,email,phone     # one company, JSON out
python -m leadengine run-csv companies.csv -o enriched.csv                 # ingest → process → export, in one process
# or the long-running shape:
python -m leadengine serve --port 8000                                     # REST API
python -m leadengine worker                                                # one or more worker processes
python -m leadengine submit-csv companies.csv --fields email,phone,address
python -m leadengine job-status <job_id>   |   export <job_id> -o out.csv   |   cancel <id>   |   retry-failed <id>
python -m leadengine metrics [--job-id <id>]
```

Tests (use their own database `enrich_test`, which they truncate): `python -m pytest tests -q`

### Credentials / external requirements

| need | for | how |
|---|---|---|
| PostgreSQL URL | everything | `LE_DATABASE_URL` |
| A contactable user agent | fetching real sites | `LE_USER_AGENT="Bot/1.0 (+https://your.site/bot; ops@your.site)"` |
| `ANTHROPIC_API_KEY` + `pip install '.[ai]'` + `LE_AI_PROVIDER=anthropic` | Tier 5 only | read by the Anthropic SDK; never logged/returned. Model `LE_AI_MODEL` (default `claude-haiku-5-5`, chosen for cost); set `LE_AI_PRICE_IN_PER_MTOK/OUT` to your model's price so cost estimates are meaningful |
| Chromium + `pip install '.[browser]'` | Tier 4 only | `LE_BROWSER_EXECUTABLE=/path/to/chromium` if Playwright's own build isn't installed |
| Nothing else | | no Redis, queue, or other service |

Without AI/browser configured the engine runs Tiers 1–3 and reports fields they cannot recover as `not_found`.

## REST API (`/v1`, optional bearer token `LE_API_TOKEN`)

| route | purpose |
|---|---|
| `POST /v1/enrich` | one company, synchronous: `{website, fields?, force?, name?, location?, industry?}` → outcome, per-field status, scores, plan, measured cost |
| `GET /v1/companies/{domain}?fields=&history=true` | stored record with provenance (+ audit history) |
| `POST /v1/jobs` | `{companies:[{website,name?,…}], fields?, budget?, idempotency_key?}` |
| `POST /v1/jobs/csv?filename=&fields=&mapping=&budget=&idempotency_key=` | raw CSV body (streamed to disk, never held in memory) |
| `GET /v1/jobs/{id}` · `/items?status=&after=&limit=` · `/export.csv` | status/counters/stats · row-level results · streamed result CSV |
| `POST /v1/jobs/{id}/cancel` · `/retry-failed` | |
| `GET /v1/metrics?job_id=` · `GET /v1/fields` · `GET /healthz` | measured per-tier metrics · field catalogue |

## CSV workflow

* Encoding detection (BOM / UTF-8 / charset-normalizer) and delimiter detection (`, ; \t |`); binary files are rejected.
* Column mapping auto-detected (`domain|website|url…`, `company|name…`, `location|city…`, `industry|sector…`) or explicit
  (`--mapping '{"website":"Company URL"}'`). A website/domain column is required; name-only rows cannot be enriched (no company
  search provider exists) and are reported as `invalid` with that reason.
* Streams in batches of `LE_INGEST_BATCH_SIZE`; row ids are the 1-based data-row numbers and never change.
  Invalid rows, blank rows, ragged rows, in-file duplicates (same registrable domain; reported as `duplicate_of_row`) and
  companies already in PostgreSQL (`le_known_company_before`) are all reported, never dropped.
* Export keeps the **original columns first, unchanged**, then `le_row, le_status, le_outcome, le_error, le_domain,
  le_known_company_before, le_duplicate_of_row, le_completeness, le_quality`, then per requested field
  `<f>`, `<f>__source_url`, `<f>__status` (e.g. `present+source_attributed+format_valid+verified`, or `missing`).
  Scraped values are neutralised against spreadsheet formula injection. Only requested fields are enriched/exported.
* Export reads the *current* stored record for each row's domain; re-export after a refresh shows refreshed values.

## Jobs, recovery, budgets

* Job: id, type, status (`queued|running|completed|partially_completed|failed|cancelled`), total/processed/succeeded/failed,
  created/started/completed timestamps, claim count, error summary by kind, stats (measured usage, budget spent).
  Items: `queued|running|succeeded|failed|invalid|duplicate|skipped`, attempts, backoff `not_before`, outcome, error kind/text.
* A worker claims a job with a lease (`LE_JOB_LEASE_SECONDS`) and renews it by heartbeat; items carry leases too. If a worker
  dies the lease expires and another worker re-claims the job and its unfinished items. A write from a worker that lost its
  lease is rejected (fenced). An item that repeatedly kills workers is failed after `LE_ITEM_MAX_ATTEMPTS`.
* `LE_MAX_CONCURRENT_JOBS` is enforced database-wide; `LE_MAX_COMPANIES_PER_JOB` at ingest.
* Graceful shutdown (SIGTERM/SIGINT): stop claiming, finish in-flight work within `LE_SHUTDOWN_GRACE_SECONDS`, hand unfinished
  items back to the queue without charging an attempt; the job resumes on the next worker.
* Budgets per job (`pages, ai_calls, browser_ops, seconds, est_cost_usd`; defaults from `LE_BUDGET_*`, 0 = unlimited). When a
  budget is exhausted the expensive operation is refused, the company gets a partial result (flagged `budget_exhausted`) or is
  `skipped` with `error_kind=budget_exhausted` if nothing could be fetched; the job ends `partially_completed`.
  `POST …/retry-failed` after raising the budget finishes only the skipped/failed rows; completed rows are never redone.
  Budgets are enforced exactly by the single worker holding the job lease.
* Backpressure: a bounded claim→worker queue (`LE_QUEUE_SIZE`), exactly `LE_GLOBAL_CONCURRENCY` consumer tasks, claiming pauses
  when the DB pool is >90% busy or ≥80% of recent results are transient failures (exponentially increasing delay).

## Caching and freshness

* HTTP cache in PostgreSQL honours `Cache-Control` (`no-store`/`private` never stored; `max-age`; `no-cache` revalidates),
  `Expires`, `ETag`/`Last-Modified` (conditional requests; `304` refreshes the entry). No headers ⇒ `LE_HTTP_CACHE_DEFAULT_TTL`.
* Concurrent requests for the same URL share one network fetch (single-flight).
* Parsed output is cached by `(url, content hash, extractor version)`; unchanged pages are not re-parsed.
* Field freshness: per-field defaults in `fields.py` (email/phone 90 d, address/description/social 180 d, name/logo/industry
  365 d), overridable with `LE_FRESHNESS_DAYS="email=60,address=180"`. Cached values are never "permanently correct": each
  carries `extracted_at` and `refresh_after`. Searched-but-not-found fields are retried after `LE_NOT_FOUND_RETRY_DAYS`;
  permanently unreachable sites are remembered for the same period. A refresh that finds nothing keeps the old value (marked).
* Company identity: registrable domain (public-suffix aware, `www.`/scheme/path/case folded), unique in PostgreSQL, so
  re-processing can never create a second record.

## Security model

* URLs: only `http`/`https`; no credentials in URLs; ports limited to `LE_ALLOWED_PORTS`; `localhost`/`.local`/`.internal`/metadata
  names refused.
* The transport resolves hostnames itself, rejects the connection if **any** resolved address is non-public
  (private, loopback, link-local incl. 169.254.169.254, CGNAT, multicast, reserved, IPv4-mapped/6to4/Teredo forms) and then
  dials **the validated IP**, so DNS rebinding between check and connect cannot happen. Redirects are followed manually and
  every hop is re-validated (and robots-checked). Response size cap (decoded, so gzip bombs are bounded), redirect cap,
  timeouts, content-type allow-list (HTML/XHTML/plain).
* Browser tier: Chromium never touches the network directly; every request is intercepted and fulfilled by the same guarded
  fetcher (images/fonts/media/websockets aborted; request-count and byte caps; service workers blocked).
* If `LE_EGRESS_PROXY` is set, the proxy makes the connection: the engine pre-resolves and refuses definite private targets,
  but rebinding protection then depends on the proxy (untested here).
* Page text is data: it is parsed, whitespace/control-character-stripped and length-capped, stored as strings, and escaped in
  CSV output. The LLM tier treats page text as untrusted, must return a schema-valid reply whose values are **verbatim
  extractions** (value ⊂ quote ⊂ page text we sent); anything else is dropped. No page content is ever executed.
* robots.txt is respected (`LE_RESPECT_ROBOTS`, default on); unreachable robots.txt ⇒ treated as disallow, retried later.
  Robots compliance is not a substitute for reading a site's terms.
* Secrets: configuration only via environment; API keys are never logged or returned; errors stored/returned are sanitised.
* The `LE_HOST_OVERRIDES` setting exists **only** for tests/benchmarks to point fixture hostnames at localhost.

## Measured results (local fixtures only — not the real web)

Full tables: `benchmarks/results/local-20261020-190805.md` (no per-domain delay) and `…-191329.md` (0.5 s per-domain delay);
definitions and limitations: `benchmarks/README.md`.

* Hardware: 4 logical vCPU Intel Xeon @ 2.1 GHz, 15.7 GiB RAM, Python 3.13.16, PostgreSQL 16.15 **on the same machine with fsync off**,
  fixture server in a separate process, simulated 40 ms response latency. Sample: 300 synthetic sites + 15 seeded duplicate rows,
  one run per configuration (no repeats, so no confidence intervals), code at commit `ed14b45`.
* Politeness-limited (0.5 s per-domain delay, the default): 139 companies/min at concurrency 4 → 502 at 16 → **658 at 32** (best),
  then 629 at 64 and 583 at 128 (more concurrency only adds queueing latency). Engine process ≤ 0.43 core, peak RSS 88–95 MB.
* Engine ceiling (no per-domain delay): 133/min at 1 → 549 at 4 → 888 at 8 → 1,139 at 16 → **1,169 at 32**, 1,018 at 64. The plateau
  is not CPU saturation (≤ 0.59 core); with only 300 sites each run lasts 15–135 s and the deliberately slow/flaky fixtures'
  tail (1–2.4 s) matters, so treat the plateau location as ±1 step.
* Per company (0.5 s delay, c=32): 3.8 HTTP requests, ~1.8 KB downloaded, 0 AI calls (none configured), browser used for 5 % of
  companies (all JS-shell fixtures); the browser tier is by far the slowest (p50 ≈ 8.8 s vs ≈ 0.6 s for tier 2).
* Quality vs ground truth: required-field coverage (name/email/phone) 97.9 %; **0 wrong and 0 invented values** among returned
  fields (includes prompt-injection fixtures); usable-record rate 63 % — the rest are by design: sites that block robots/404/
  huge/redirect-to-metadata (7 %), contact-only pages with no name, JS shells whose name lives only in an `<h1>`, and prose-only
  pages that only the (here disabled) AI tier could read. Duplicate rate 4.8 % (none enriched twice).
* Cost: no AI/browser/HTTP unit prices were configured, so the runs report **no USD cost**; measured usage is in the JSON
  (1,144 HTTP requests, 553 KB, 15 browser renders, 0 AI calls for 300 companies). **No cost per 1,000 records is claimed.**

### Not measured / not verified (needs credentials, network or time)

* **Live websites**: this sandbox's egress policy blocks arbitrary sites (HTTP 403 on CONNECT), so `benchmarks/run_live.py` was
  written and its refusal guards exercised only by reading; it has **never been run**. Real-web coverage, accuracy, latency and
  cost are therefore unknown. Run it (`--confirm-permitted`, real `LE_USER_AGENT`, `--audit 25`) before quoting any number.
* **The LLM tier** is covered by unit/integration tests with a fake provider (grounding, schema, budget, invalid replies) but was
  **never called against the real Anthropic API** (no key here). `AnthropicProvider` follows the documented SDK call
  (`output_config` json_schema + `effort: low`) but is unexercised.
* **`LE_EGRESS_PROXY` mode**, multi-worker-process scaling, and tables larger than a few thousand rows were not tested.
* No migrations framework: `schema.sql` is idempotent (`CREATE … IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`); destructive
  schema changes would need manual migration.
