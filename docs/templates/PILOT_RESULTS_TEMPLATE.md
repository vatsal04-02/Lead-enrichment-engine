# FlowHQ live pilot — results (copy this file, fill it in, keep the copy with the run's output folder)

> Every number below must carry one status. **MEASURED** = counted/timed by the tool or by a human reviewer in this run.
> **ESTIMATED** = computed from measured quantities and a price/rate *you* supplied. **UNKNOWN** = not measured — leave it UNKNOWN,
> do not fill it with a guess. A field that was not found is *not found on the pages fetched*; it is **not** evidence that the
> company has no such contact detail. E-mail is never "verified": no mailbox-level verification exists in this project.

## 0. Run record

| item | value |
|---|---|
| Run date / operator | |
| Report file | `pilot-<stamp>.json` / `.md` (report → `mode` must say `LIVE pilot (real websites)`) |
| Code version | report → `git_commit` |
| Input file sha256 | report → `sample_sha256` |
| Companies in input | report → `sample_size` |
| Settings actually used | report → `settings` (delay, concurrency, pages, retries, browser, ai) |
| Browser tier | report → `results.browser_tier` (`requested`, `launched_successfully`, `error`) |
| AI tier | on / off; model; (never print or paste any key) |
| Machine / network | report → `hardware`; home / office / VPN; any proxy (the pilot refuses to use one) |

## 1. Safety gate attestation (tick before the run; keep with the results)

- [ ] I reviewed every row; each `permission_basis` is true and I can show it if asked
- [ ] `LE_USER_AGENT` names the operator and a working contact URL/e-mail; that URL explains the bot
- [ ] database is `*_pilot`, separate from any production data
- [ ] I did not edit any limit in code; I did not set `LE_HOST_OVERRIDES`
- [ ] sites that refuse (robots.txt, 401/403, CAPTCHA/anti-bot) stay failures — I will not work around them
- [ ] I will not send page text to an AI provider unless my permission basis covers it

## 2. Volume and outcomes

| # | metric | value | status | where it comes from |
|---|---|---|---|---|
| 1 | Companies in input | | MEASURED | `results.volume.input_rows` |
| 1b | Unique companies attempted | | MEASURED | `results.volume.unique_companies_attempted` |
| 2 | Successfully processed | | MEASURED | `results.volume.succeeded` ("processed without a fetch failure"; says nothing about quality) |
| 8 | Failed (blocked / unreachable / error) | | MEASURED | `results.volume.failed`; breakdown below |
| 8 | Skipped (budget) | | MEASURED | `results.volume.skipped` |
| 7 | Duplicate input rows | | MEASURED | `results.volume.duplicate_rows` — 0 by construction: the sample validator rejects duplicate domains |
| 7 | Duplicate company records stored | | MEASURED | 0 by construction: `companies.domain` is UNIQUE. Query to confirm: `SELECT domain, count(*) FROM companies GROUP BY 1 HAVING count(*)>1;` |
| 7 | Companies sharing an e-mail/phone with another company | | MEASURED | `.scored.json` → `duplicate_flags` (review-sheet column `duplicate_status`) |

### Failure breakdown (MEASURED)

`results.volume.failure_reasons` and `results.fetch_log_breakdown`:

| reason key | count | meaning |
|---|---|---|
| `unreachable_robots` | | robots.txt forbids it (or robots.txt itself returned 401/403) |
| `unreachable_http` | | 401/403/404/410… from the site (access control / missing); never retried |
| `unreachable_bot_challenge` | | anti-bot interstitial; recorded, never bypassed |
| `unreachable_redirected_off_domain` | | redirected to a different company's domain; nothing extracted |
| `unreachable_tls_cert` / `unreachable_tls` | | certificate / handshake failure |
| `unreachable_blocked` | | destination refused by the SSRF guard |
| `unreachable_redirect_loop`, `unreachable_too_large`, `unreachable_content_type` | | |
| `timeout`, `network`, `rate_limited` | | transient; retried at most once, then failed |
| `budget_exhausted` | | job budget reached before this company |

## 3. Field coverage — presence only (MEASURED, denominator = unique companies attempted)

Source: `results.coverage_presence_only`. *Presence ≠ correctness.*

| field | present | share | notes |
|---|---|---|---|
| domain | | | input value, by construction |
| website reachable & domain-verified | | | procedure `http_reachable_domain_match` |
| company_name | | | |
| email | | | `email_mailbox_verified` is always 0; `email_syntax_valid_share_of_emails` is syntax only |
| phone | | | |
| address | | | |
| description | | | |
| linkedin_url | | | |

## 4. Manually verified accuracy (MEASURED, human-reviewed — requires the completed review sheet)

Command: `python -m benchmarks.pilot score --review <review.csv> --run-json <pilot-stamp.json>` → writes `<review>.scored.json`.
Verdict vocabulary — present values: `correct | incorrect | misattributed | unsure`; blank cells: `missed | absent | unchecked`.

| field | correct | incorrect | misattributed | unsure | precision (95% Wilson interval) | missed | absent | unchecked | reviewed recall |
|---|---|---|---|---|---|---|---|---|---|
| company_name | | | | | | | | | |
| email | | | | | | | | | |
| phone | | | | | | | | | |

Keys: `per_field.<field>.{correct,incorrect,misattributed,unsure,missed,absent,unchecked,precision,precision_wilson95,recall_reviewed}`.

| # | metric | value | status | where it comes from |
|---|---|---|---|---|
| 6 | Incorrect values (all reviewed fields) | | MEASURED | `.scored.json` → `incorrect_values` |
| 6 | **Misattributed** values (belong to another entity) | | MEASURED | `.scored.json` → `misattributed_values` — any non-zero count needs a root-cause note |
| 5 | Audited accuracy, per field | see table | MEASURED | as above; with ~20 companies even 20/20 correct only bounds precision from below at ≈ 0.84 |
| 3 | **Usable records, audited** | | MEASURED | `.scored.json` → `usable_audited` (+ `usable_audited_wilson95`). Definition: company name **correct** AND (e-mail **correct** OR phone **correct**) |
| 3 | Usable records, presence-only (upper bound) | | MEASURED | `results.usable_record_rate.presence_only` |
| | Reviewer "usable_record=yes" count / disagreements with the verdicts | | MEASURED | `.scored.json` → `usable_reviewer_yes`, `usable_disagreements_with_verdicts` |
| | address / description / linkedin accuracy | UNKNOWN | UNKNOWN | not in the review sheet; add columns yourself if you need them |
| | Recall (share of real contact details the engine found) | partial | MEASURED only for cells you checked | `recall_reviewed` counts only rows where you verified blanks (`missed`/`absent`); `unchecked` is excluded, so it is biased |

## 5. Effort, speed and reliability (MEASURED)

| # | metric | value | status | where it comes from |
|---|---|---|---|---|
| 9 | HTTP requests per company | | MEASURED | `results.performance.http_requests_per_company` (includes robots.txt and retries) |
| 10 | Browser fallback rate | | MEASURED | `results.performance.browser_fallback_rate` — **only meaningful if `browser_tier.launched_successfully` is true**; on native Windows the tier cannot run, so JS-only sites show up as missing fields instead |
| 12 | Median processing time (ms) | | MEASURED | `results.performance.latency_ms_p50` (includes the ≥2 s per-host politeness delay) |
| 12 | p95 processing time (ms) | | MEASURED | `results.performance.latency_ms_p95` |
| | Throughput (companies/min) | | MEASURED | `results.performance.throughput_companies_per_minute` — limited by the politeness delay; do **not** extrapolate to larger runs |
| | Retry rate | | MEASURED | `results.performance.retry_rate` |
| | Wall time (s) | | MEASURED | `results.performance.wall_seconds` |

## 6. AI usage and cost

| # | metric | value | status | where it comes from |
|---|---|---|---|---|
| 11 | AI calls per company | | MEASURED | `results.performance.ai_calls_per_company` (0 if AI was off) |
| 11 | Tokens in / out (actual) | | MEASURED | `results.ai_cost.tokens_in`, `tokens_out` (from the provider's reported usage) |
| 13 | Cost per 1,000 **attempted** records (AI only) | | ESTIMATED (or MEASURED = 0 when no AI calls were made) | `results.ai_cost.per_1000_attempted_usd` — tokens × the prices you passed |
| 14 | Cost per 1,000 **usable** records (AI only) | | ESTIMATED | `.scored.json` → `ai_cost_per_1000_usable_audited_usd` (preferred: audited usable). The run report's `per_1000_usable_usd` uses presence-only usable and is therefore optimistic |
| | Infrastructure / compute cost | UNKNOWN unless `--compute-usd-per-hour` was given | ESTIMATED / UNKNOWN | `results.ai_cost.infrastructure`; otherwise excluded from every figure above |
| | Prices used (USD / million tokens, in / out) and where you read them | | | pass-through of `--ai-price-in/out`; record the provider page and date |

*13 and 14 are not "the cost of enrichment": HTTP fetching, your machine, your time, storage and review labour are excluded.*

## 7. Provenance and audit evidence (kept with the run)

- [ ] `pilot-<stamp>-provenance.csv` — every stored value with `source_url`, `method`, `tier`, `extracted_at`
- [ ] `pilot-<stamp>-fetch-log.jsonl` — every request (pages, robots.txt, refusals) with status and error kind
- [ ] `pilot-<stamp>-review.csv` — completed by a human, plus the `.scored.json`
- [ ] Spot check: 5 random `fetch-log` hosts show spacing ≥ 2 s between requests to the same host

## 8. Conclusions (write after reading §2–§6; do not pre-fill)

- What worked:
- What failed and why (group by §2 failure reasons and by missed fields):
- Misattributed/incorrect values and their root cause:
- Which unknowns remain UNKNOWN:
- Decision / next step:
