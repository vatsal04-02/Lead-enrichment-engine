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
cp .env.example .env                           # then edit; LE_USER_AGENT is REQUIRED (the engine refuses the placeholder)
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

## Optional: ScrapeGraphAI extraction backend (off by default)

An opt-in alternative to the in-house tiers, built on the open-source `scrapegraphai` library with a local (Ollama) or an
explicitly configured remote model. **The default engine is unchanged; nothing here runs unless you enable it.** The library never
fetches anything: our guarded fetcher (SSRF guard, robots.txt, no auth/CAPTCHA bypass, page budgets, `fetch_log`) fetches the page
and passes page *content* to an isolated worker process whose network is limited to the model host. Model answers are stored only if
the page itself supports them (otherwise `extraction_state = invalid`), with real source URL, method `scrapegraph_llm`, timestamps and
`unverified` status. Calls, pages, tokens, timeouts and retries are capped per company and per job; usage is reported (local
inference is reported as tokens/latency — it is not claimed to be free).

```bash
LE_EXTRACTION_BACKEND=scrapegraph LE_SCRAPEGRAPH_ENABLED=true LE_SCRAPEGRAPH_PROVIDER=ollama LE_SCRAPEGRAPH_MODEL=llama3.2 \
LE_SCRAPEGRAPH_PYTHON=/path/to/py312/bin/python  python -m leadengine enrich acme.com
python -m benchmarks.compare_backends --database-url postgresql://u@h:5432/enrich_pilot --sites 60 --model stub-heuristic --sg-python /path/to/py312/bin/python
```

Rollback = unset `LE_EXTRACTION_BACKEND`. Install (Windows PowerShell and Linux), configuration, limits, security limitations, benchmark
interpretation and troubleshooting: **[docs/SCRAPEGRAPH_INTEGRATION.md](docs/SCRAPEGRAPH_INTEGRATION.md)**. Benchmarks against fixtures are
synthetic and say nothing about live-site accuracy.

## FlowHQ discovery & enrichment (find candidates, then enrich them)

`python -m leadengine.discovery` finds candidate companies from sources you choose and have reviewed (manual CSV, public listing pages you name,
OpenStreetMap open data), validates and de-duplicates them, then enriches with ScrapeGraphAI on a local Ollama model (`qwen3:4b`), falling back to
the in-house engine and finally to a manual-review record. Every field keeps its source URL, backend and status; output goes to a new
timestamped directory. **Dry run by default** - a live run needs `--live --confirm-terms-reviewed --confirm-count N`. No paid APIs, no outreach.

```bash
python -m leadengine.discovery doctor          # is Ollama / qwen3:4b / the library available? (read-only, never downloads)
python -m leadengine.discovery plan --csv my_candidates.csv --osm --location Pune
python -m leadengine.discovery run  --csv my_candidates.csv          # dry run: nothing contacted
```
Automatic sources: Wikidata (`--wikidata`, CC0) and local OpenStreetMap extracts (`--osm-file`), with a `check-sources` robots.txt preflight; why other sources
were rejected (closed/paid/terms): **[docs/DISCOVERY_SOURCES.md](docs/DISCOVERY_SOURCES.md)**. Details, commands for Windows and the live run, status and limitations: **[docs/DISCOVERY_ENGINE.md](docs/DISCOVERY_ENGINE.md)**.

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
* Spreadsheet formula injection: scraped values and error text are always neutralised (leading `= @ + -`/tab/CR, also after
  whitespace). Your **original columns are echoed unchanged** by default; add `?safe=true` (API) to neutralise them too.

## Live pilot (20–50 hand-picked, permitted sites)

`benchmarks/pilot.py` is the only supported way to contact real sites for evaluation. It cannot be pointed at arbitrary
websites (see its docstring for every guard rail). **Windows/PowerShell step-by-step: `docs/STAGE5_PILOT_GUIDE.md`.**
Templates: `benchmarks/templates/pilot_input.csv` (input), `benchmarks/templates/pilot_review.csv` (review sheet header),
`docs/templates/PILOT_RESULTS_TEMPLATE.md` (results record). Linux/macOS shape of the same flow:

```bash
createdb enrich_pilot                                   # database name MUST end in _pilot (the pilot truncates it with --reset)
export LE_DATABASE_URL=postgresql://USER@HOST/enrich_pilot LE_USER_AGENT="YourBot/1.0 (+https://your.site/bot; ops@your.site)"
mkdir -p pilot_out && cp benchmarks/templates/pilot_input.csv pilot_out/pilot_input.csv    # fill in 20-50 rows
python -m benchmarks.pilot validate --sample pilot_out/pilot_input.csv                    # offline
python -m benchmarks.pilot run --sample pilot_out/pilot_input.csv --confirm-permitted --confirm-sites N --reset --out-dir pilot_out/run1
# fill the verdict columns of pilot_out/run1/pilot-<ts>-review.csv by checking each source URL, then:
python -m benchmarks.pilot score --review pilot_out/run1/pilot-<ts>-review.csv --run-json pilot_out/run1/pilot-<ts>.json
```

Outputs: report (`.md/.json`, every metric labelled MEASURED / ESTIMATED / UNKNOWN), `*-review.csv` (one pre-filled row per
company for the human check), `*-provenance.csv` (every value with source URL/method/time), `*-fetch-log.jsonl` (every request).

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

## Database and migrations

`schema.sql` is the idempotent baseline; forward-only numbered migrations live in `leadengine/migrations/*.sql` and are recorded in
`schema_migrations` (applied exactly once, under an advisory lock, in one transaction — safe when several processes start at once).
Migrations must be additive; destructive changes (drops/rewrites) are never applied automatically. Cache/log tables
(`http_cache`, `extraction_cache`, `enrichment_runs`, `fetch_log`) have **no retention job yet** (see the Stage-4 report).

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
* Browser tier: HTTP(S) requests from Chromium are intercepted and fulfilled by the same guarded fetcher (images/fonts/media
  aborted; request-count and byte caps; service workers blocked). **WebSockets bypass request interception**, so every
  WebSocket is refused (`route_web_socket`), Chromium's own DNS is disabled (`--host-resolver-rules=MAP * ~NOTFOUND`) and WebRTC
  is restricted to proxied UDP. (Before Stage 4 this paragraph claimed websockets were aborted; they were not — a page's WebSocket
  reached a private listener. Fixed and regression-tested.) Residual, untestable here: blind UDP via WebRTC.
* If `LE_EGRESS_PROXY` is set, the proxy makes the connection: the engine pre-resolves and refuses definite private targets,
  but rebinding protection then depends on the proxy (untested here).
* Page text is data: it is parsed, whitespace/control-character-stripped and length-capped, stored as strings, and escaped in
  CSV output. The LLM tier treats page text as untrusted, must return a schema-valid reply whose values are **verbatim
  extractions** (value ⊂ quote ⊂ page text we sent); anything else is dropped. No page content is ever executed.
* robots.txt is respected (`LE_RESPECT_ROBOTS`, default on): 401/403 on robots.txt ⇒ disallow; other 4xx ⇒ allow;
  5xx/timeout ⇒ disallow, retried later; `Crawl-delay` is honoured (raises the per-host delay; above `LE_MAX_CRAWL_DELAY` the site
  is skipped). Robots compliance is not a substitute for reading a site's terms.
* The engine never works around access controls: 401/403 are permanent failures (never retried), anti-bot interstitials
  (`cf-mitigated: challenge`, "Just a moment…", PerimeterX/DataDome markers on small pages) are recorded as `bot_challenge` and
  neither retried nor rendered in the browser. A reCAPTCHA *widget* on a normal page is not treated as a challenge.
* Redirects to a different registrable domain (rebrand, parking page, wrong input) are rejected as `redirected_off_domain`:
  nothing from the destination is attributed to the input company.
* Every request (pages, robots.txt, browser sub-requests, refusals) is appended to `fetch_log` (`LE_FETCH_AUDIT_LOG`).
* Each request has a hard wall-clock deadline (`LE_TOTAL_TIMEOUT`) in addition to per-read timeouts (a slow-drip server can no
  longer hold a connection); TLS certificate failures are permanent and never downgraded to HTTP; handshake failures are not retried.
* The engine refuses to start with the placeholder `LE_USER_AGENT` (no anonymous crawling).
* API: a single shared bearer token (constant-time comparison); without `LE_API_TOKEN` the API only starts on loopback. No
  per-user identity, tenancy or rate limiting — front it with a gateway. Request bodies, lists and string fields are size-capped;
  clients can lower but never raise job budgets; secrets are `SecretStr` (masked in `repr`/dumps) and credential-looking
  substrings are redacted from stored/logged error text.
* Secrets: configuration only via environment; API keys are never logged or returned; errors stored/returned are sanitised.
* Optional ScrapeGraphAI backend: the library gets no URL and no network (worker allow-list = the model host only; start-up self-check
  proves 7 guards or refuses to start); telemetry off; model output is grounded against the page before it is stored. Details and limits:
  `docs/SCRAPEGRAPH_INTEGRATION.md`.
* The `LE_HOST_OVERRIDES` setting exists **only** for tests/benchmarks to point fixture hostnames at localhost.

## Measured results (local synthetic fixtures only — not the real web)

Tables: `benchmarks/results/local-20261020-194507.md` (no per-domain delay) and `…-194645.md` (0.5 s per-domain delay), run on the
Stage-4-hardened code (engine code identical to `a4bd432`); the earlier pre-hardening runs (`…-190805`, `…-191329`) are kept for
comparison and agree within 1 %. Definitions and limitations: `benchmarks/README.md`.

* Hardware: 4 logical vCPU Intel Xeon @ 2.1 GHz, 15.7 GiB RAM, Python 3.13.16, PostgreSQL 16.15 **on the same machine, fsync off**,
  fixture server in a separate process, simulated 40 ms response latency. Sample: 300 synthetic sites + 15 seeded duplicate rows,
  **one run per configuration** (no repeats, so no confidence intervals).
* With the default 0.5 s per-host delay: 483 companies/min at concurrency 16, **655 at 32** (best), 604 at 64. Engine process
  ≤ 0.41 core, peak RSS 86–90 MB.
* Without a per-host delay (engine ceiling): 252/min at 2, 868 at 8, 1,135 at 16, **1,167 at 32**. Not CPU saturation (≤ 0.63 core);
  runs last only 15–70 s, so the deliberately slow/flaky fixtures' tail matters: treat the plateau as ±1 step.
* Per company: 3.8 HTTP requests (incl. robots.txt), ~1.8 KB, 0 AI calls (none configured), browser fallback for 5 % of companies
  (the JS-shell fixtures; browser p50 ≈ 8.8 s vs ≈ 0.6 s for plain HTTP).
* Against ground truth: required-field coverage 97.9 %, **0 wrong / 0 invented values**, usable-record rate 63 % (by design:
  blocked/404/oversized/metadata-redirect sites 7 %, no-name contact pages, JS shells whose name is only in an `<h1>`, prose-only
  pages that only the AI tier could read), duplicate rate 4.8 % (none enriched twice). These fixtures were written by the same
  author as the extractor, so this is a regression signal, not a real-world accuracy estimate.
* Cost: no unit prices configured ⇒ no USD cost reported; measured usage only. **No cost per 1,000 records is claimed.**
* Stage-4 claim query at 200k job rows: 35–57 ms → 1.6–3.2 ms after the open-items index (measured).

### Not measured / not verified (needs credentials, network or time)

* **Live websites**: this sandbox blocks outbound requests to arbitrary sites. `benchmarks/pilot.py` was exercised only on
  synthetic fixtures; **no real site has ever been contacted**. Real-world coverage, accuracy, latency, cost are unknown.
* **The LLM tier** is tested only with a fake provider; never called against the real API.
* **`LE_EGRESS_PROXY` mode**, multi-worker-process load, and tables beyond ~10^5 rows (other than the claim-query probe) were not tested.
* No retention/GC job for `http_cache`, `extraction_cache`, `enrichment_runs`, `fetch_log`; no PII deletion tooling.
* E-mail addresses are never mailbox-verified.
