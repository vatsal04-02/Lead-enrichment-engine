# Stage 4 — Real-world validation & production-readiness audit

Branch `claude/epic-shannon-areldd`. Method: every claim in the previous completion report was re-checked against code and by
running probes; suspected defects were reproduced (failing test or probe) **before** being fixed; each fix has a regression test.
Nothing in this stage contacted a real website (this sandbox's egress policy blocks them).

## 1. Audit result — were the earlier claims true?

| Claim | Verdict |
|---|---|
| 98 tests pass | **True** (re-run: 98 passed). Now 172. |
| Tiered extraction, PostgreSQL jobs, provenance, CSV, API, CLI, metrics exist | **True**, verified by running them. |
| Browser tier cannot reach private networks ("websockets aborted") | **False.** Chromium WebSockets bypass request interception; a page reached a private listener (probe: 1 connection). Fixed. |
| Redirects are re-validated | True for SSRF; **but** an off-domain redirect attributed the destination company's e-mail to the input company. Fixed. |
| Timeouts / resource limits | **Partly false**: `LE_TOTAL_TIMEOUT` was not a deadline (slow-drip response held a fetch 60 s+ against a 3 s limit). Fixed. gzip bombs, redirects, size caps: true. |
| Budgets cap spend | True for server defaults; **clients could send `{"pages":0}` and disable them.** Fixed. |
| robots.txt respected | Mostly; robots 401/403 was treated as allow-all and `Crawl-delay` was ignored. Fixed. |
| Idempotency / dedupe / crash recovery | True. Crash recovery was only simulated with SQL; now also tested with a real `kill -9` of a worker process. |
| Backpressure, time/cost budgets | Implemented but had **no tests**; now covered. |
| Cost / benchmark numbers | Fixture numbers reproduced within 1 % on the hardened code. They are synthetic. |
| "Live benchmark runner exists" | It existed but had no per-site permission record, no audit log, no forced limits; replaced by `benchmarks/pilot.py`. **Still never run on a real site.** |

## 2. Severity-ranked issues

| # | Sev | Issue (how reproduced) | Status |
|---|---|---|---|
| 1 | **High** | Browser WebSocket bypassed the SSRF guard (probe: connection reached a private listener) | Fixed: `route_web_socket` refuses all WebSockets, `--host-resolver-rules`, WebRTC policy. Test `test_browser_page_cannot_reach_internal_services_by_any_channel` (fails on old code, verified). |
| 2 | **High** | Off-domain redirect misattributed another company's data (probe: `acme.test`→`other-company.test` stored `hello@other-company.test`) | Fixed: `redirected_off_domain`, nothing extracted, remembered. |
| 3 | **High** | No hard request deadline → slow-drip connections tie up workers indefinitely (60 s vs 3 s limit) | Fixed (`asyncio.timeout(total_timeout)`; also for browser sub-requests). |
| 4 | **High** | Client-supplied job budgets could lift/disable server limits | Fixed: server limits are ceilings; unknown keys/negatives rejected. |
| 5 | Medium | Heartbeat task could die silently on one DB error (not in `gather`, no handler) → lease lapses mid-job | Fixed: retries; abandons only when the lease has really lapsed. Test fails on old code (verified). Crashed job tasks are now logged. |
| 6 | Medium | robots 401/403 ⇒ allow; Crawl-delay ignored | Fixed (403/401 ⇒ disallow; Crawl-delay honoured; >30 s ⇒ skip). |
| 7 | Medium | Anti-bot interstitials treated as content (and 503 challenges retried, JS shells could be rendered) | Fixed: `bot_challenge` is permanent, never retried/rendered. reCAPTCHA widgets on normal pages are not misclassified (test). |
| 8 | Medium | API: no body/list/field limits; non-constant-time token compare; invalid job id → blanket `except` 404; unauthenticated API on any bind address | Fixed (413/422 caps, `hmac.compare_digest`, UUID parsing, `serve` refuses non-loopback without `LE_API_TOKEN`). |
| 9 | Medium | Secrets: `repr(Settings)` exposed API token and DB password; no log redaction | Fixed (`SecretStr`; `sanitize_for_log` redacts `user:pass@`, Bearer, `sk-…`, `password=`). |
| 10 | Medium | TLS failures retried like network errors; no cert/handshake classification | Fixed (`tls_cert` permanent, `tls` not retried; http fallback only for assumed-scheme + handshake failure). |
| 11 | Medium | No audit trail of outbound requests (needed for the pilot) | Added: append-only `fetch_log` + JSONL export, with job context. |
| 12 | Medium | Placeholder user agent accepted ⇒ anonymous crawling | Engine refuses to start with it (unless fixture overrides). |
| 13 | Medium | AI: truncated/refused replies not detected; 1024 output tokens too low with thinking | Fixed (stop_reason checked, 2048). **Unverified against the real API.** |
| 14 | Low | Claim query scales with finished rows (35–57 ms @200k) | Fixed: partial index → 1.6–3.2 ms (measured). Idle "any open items?" check no longer counts all rows. |
| 15 | Low | No schema versioning | Added `schema_migrations` + forward-only numbered migrations (additive only; concurrent-start safe, tested). |
| 16 | Low | CSV: formula prefix after whitespace/newline not neutralised; error text unsanitised; originals echoed | Fixed; `?safe=true` also neutralises original columns (default keeps them verbatim, by requirement). |
| 17 | Low | Chromium crash left the renderer permanently dead | Fixed (relaunch; tested). |
| 18 | Low | Config accepted nonsense (0 concurrency, heartbeat ≥ lease) | Validated at startup. |
| 19 | Low | README said websockets were aborted | Corrected. |

Verified **not** to be problems: gzip decompression bombs, obfuscated IPs (`2130706433`, `0x7f.1`, `127.1`, octal), clean-venv
(non-editable) install, `parse_workers>0`, DB pool-pressure statistics keys.

## 3. Actual test results

`python -m pytest tests -q` → **172 passed in 64 s** (PostgreSQL 16.15, Python 3.13.16, Chromium present).
Per file: security 38, pipeline 32, api_hardening 26, jobs 22, hardening 19, pilot 17, extract 9, api 4, schema/audit 3, browser 2.
Not tested by automation: real websites, real Anthropic API, `LE_EGRESS_PROXY`, multi-process load, SIGTERM via the CLI signal
handler (graceful shutdown is tested through `Worker.request_stop`).

## 4. Commands to run the pilot on your own computer

> Superseded by `docs/STAGE5_PILOT_GUIDE.md` (Windows/PowerShell, per-company review sheet, `score --review`). The `score --audit` flow below only applies to fixture runs.

```bash
git checkout claude/epic-shannon-areldd && python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,browser,ai]'                 # browser/ai optional; `playwright install chromium` if you want Tier 4
createdb enrich_pilot                              # or: scripts/dev_postgres.sh (local throwaway cluster, includes enrich_pilot)
export LE_DATABASE_URL=postgresql://USER@HOST/enrich_pilot
export LE_USER_AGENT="YourBot/1.0 (+https://your.site/bot; ops@your.site)"      # must identify you and include a contact
cp benchmarks/templates/pilot_input.csv my_sites.csv        # 20–50 rows: domain,name,permission_basis,reviewed_by
python -m benchmarks.pilot validate --sample my_sites.csv                        # offline
python -m benchmarks.pilot run --sample my_sites.csv --confirm-permitted --confirm-sites <ROWS> --reset
#   add --allow-browser --browser-executable PATH, and/or --ai --ai-price-in X --ai-price-out Y (needs ANTHROPIC_API_KEY)
#   add --compute-usd-per-hour R to include infrastructure in the cost estimate
# fill the `verdict` column of benchmarks/results/pilot-<ts>-audit.csv by opening each source_url, then:
python -m benchmarks.pilot score --audit benchmarks/results/pilot-<ts>-audit.csv
```
Forced by the tool (cannot be relaxed from the CLI): ≥2 s between requests per host, 1 connection per host, ≤4 companies at once,
≤3 pages/company, 1 retry, 1 MB responses, 20 s deadline, robots enforced, SSRF guard on, browser/AI off unless opted in, max 50
sites, bare domains only, a written permission basis for every row, DB name must end in `_pilot`.
It does not bypass logins, CAPTCHAs or challenge pages; those sites are recorded as failures.

## 5. Benchmark results (genuinely measured — synthetic fixtures only)

Machine: 4 vCPU Xeon 2.1 GHz, 15.7 GiB, local PostgreSQL 16 (fsync off), simulated 40 ms latency, 300 sites + 15 duplicate rows,
one run per setting. Reports: `benchmarks/results/local-20261020-194507.*`, `…-194645.*` (and the pre-hardening `…-190805`, `…-191329`).

| per-host delay | concurrency | companies/min | p50 ms | p95 ms | engine CPU (cores) | RSS MB |
|---|---|---|---|---|---|---|
| 0.5 s (default) | 16 / **32** / 64 | 483 / **655** / 604 | 1565 / 1638 / 2301 | 4531 / 5122 / 6348 | 0.28 / 0.37 / 0.41 | 86 / 88 / 90 |
| none | 2 / 8 / 16 / **32** | 252 / 868 / 1135 / **1167** | 207 / 251 / 303 / 753 | 1258 / 1820 / 4343 / 5092 | 0.15 / 0.45 / 0.57 / 0.63 | 85–88 |

Constant across settings: 3.8 HTTP requests/company, 0 AI calls/company, browser fallback 5 %, retry rate 0–1.9 %, duplicate rate
4.8 %, 21 failures/300 (all intentional fixtures), required-field coverage 97.9 %, 0 wrong/invented values, usable-record rate 63 %.
Pilot machinery dry run on 50 synthetic sites: `benchmarks/results/pilot-20261020-194811.*` (labelled FIXTURE; every metric tagged
MEASURED/ESTIMATED/UNKNOWN; accuracy and audited usable-rate UNKNOWN until a human audit; e-mail mailbox-verified = 0).
**Unknown (not measured): everything about real websites — coverage, accuracy, latency, failure/block rates, cost per 1,000 records
(attempted or usable), real LLM token usage.**

## 6. Remaining production blockers

1. **No real-world evidence.** Run the pilot (above) and manually audit before any quality or cost claim.
2. **LLM tier never exercised against the real API** (token use, refusals, JSON-schema behaviour, actual cost).
3. **API security model is minimal**: one shared bearer token, no tenancy/per-user auth, no rate limiting, no TLS (needs a gateway).
4. **Data governance**: no retention/GC for `http_cache` (stores page bodies), `extraction_cache`, `enrichment_runs`, `fetch_log`;
   no deletion/opt-out tooling for personal data (individual e-mails/phones); no site do-not-crawl list.
5. **E-mail is never verified** (no SMTP/provider verification) and name-only rows can't be enriched (no discovery).
6. **Operations**: no CI, lockfile or container image; no structured logs/metrics export/alerts; no multi-worker load test; browser
   runs `--no-sandbox` when root; cross-process duplicate fetches of one domain are possible (benign, idempotent).
7. **Network egress hardening** should also be enforced outside the app (deny RFC1918/metadata at the host/VPC level); proxy mode untested.

## 7. Recommended next stage (Stage 5)

1. Execute the live pilot on 20–50 permitted sites; complete the manual audit; fix the top extraction failure classes it reveals.
2. Add a small real-API LLM validation (≈50 pages) to measure token use, refusal/JSON failure rate and cost per usable record.
3. Retention + privacy: cache/log GC jobs, per-domain opt-out list, data-subject deletion command.
4. Productionise: CI, pinned lockfile, container, structured logging + Prometheus metrics, gateway-fronted API with per-key limits.
5. Then multi-worker load test at 10^4–10^5 rows and tune concurrency on real latency.
