# Stage 5 — Controlled live pilot: operator guide (Windows / PowerShell)

Goal: measure what the engine really does on **~20 company websites you are permitted to fetch**, with a human check of
every value. Nothing in this guide contacts a website until step 7. Replace every `<PLACEHOLDER>`; the guide never assumes
your contact details, passwords, keys, sites or paths.

**Verification status (be aware):** the code was developed and tested (199 automated tests) on Linux. **Nothing has been run on
Windows.** The Windows-specific problems I could find by reading the code and its dependencies are fixed and unit-tested
(see §10), but step 5's offline dry run is your real Windows test. **No real website has ever been contacted by this project.**

---

## 0. Get exactly this code (commits are local to the build sandbox until you authorise a push)

The Stage 4/5 commits exist only in the sandbox repository; the remote branch has Stage 1–3. Until you authorise a push (or move
the commits another way), `git checkout` on your PC will **not** contain the pilot tool.

```powershell
cd <PATH_TO_YOUR_CLONE_OF_THE_REPO>
git fetch origin
git checkout claude/epic-shannon-areldd
git log --oneline -1
```
The last line must show the commit hash I report in my hand-off message. If it shows an older commit, `benchmarks\pilot.py` will
not have `score --review`; stop and ask for the push.

## 1. Check prerequisites

```powershell
$PSVersionTable.PSVersion                 # Windows PowerShell 5.1 or PowerShell 7+ are both fine
python --version                          # must be 3.11 or newer (pyproject: requires-python >=3.11)
py --list                                 # if `python` is old/missing, list installed versions; then use e.g. `py -3.12`
git --version
psql --version                            # PostgreSQL 13 or newer (the schema uses gen_random_uuid(), built in since 13)
Get-Service -Name "postgresql*"           # Status should be Running
Test-NetConnection -ComputerName localhost -Port 5432   # TcpTestSucceeded : True (use your port if not 5432)
```
If `psql` is "not recognized", add PostgreSQL's `bin` folder for this session (adjust the version number):
```powershell
$env:Path += ";C:\Program Files\PostgreSQL\<VERSION>\bin"
```

## 2. Python environment (inside the repo folder)

```powershell
cd <PATH_TO_YOUR_CLONE_OF_THE_REPO>
python -m venv .venv
# If activation says "running scripts is disabled", allow it for THIS window only:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"                   # first pilot: no browser, no AI extras
$env:PYTHONUTF8 = "1"                     # makes every file read/write UTF-8 (the tool also sets encodings explicitly)
Test-Path .env                            # if True, open it: a stray .env can feed other LE_* settings; the pilot forces its own limits regardless
```
`.[browser]` and `.[ai]` are optional extras; the browser tier does **not** run on native Windows (see §10), so skip it.

## 3. Create the dedicated pilot database (name must end in `_pilot`)

Option A — simplest (uses a superuser you already have; psql prompts for the password):
```powershell
psql -h localhost -p 5432 -U <SUPERUSER, usually postgres> -c "CREATE DATABASE enrich_pilot;"
```
Option B — separate login role (recommended). The password is typed into psql's own prompt, not into your shell history:
```powershell
psql -h localhost -p 5432 -U <SUPERUSER> -c "CREATE ROLE flowhq_pilot LOGIN;"
psql -h localhost -p 5432 -U <SUPERUSER> -c "\password flowhq_pilot"
psql -h localhost -p 5432 -U <SUPERUSER> -c "CREATE DATABASE enrich_pilot OWNER flowhq_pilot;"
```
Check it exists and that you are NOT connected to anything else:
```powershell
psql -h localhost -p 5432 -U <SUPERUSER> -lqt | Select-String "_pilot"
```
Use a name other than `enrich_pilot` if you like, but it must end in `_pilot`. The pilot **TRUNCATES** tables in this database
when you pass `--reset`; never point it at a database that holds anything you care about. The tool also refuses URLs
whose name does not end in `_pilot`, non-`postgresql://` URLs, and URLs with `?dbname=`/`options=`/`service=`.

## 4. Environment variables (this PowerShell window only; nothing is written to disk)

```powershell
# Database URL — keeps the password out of history and URL-encodes special characters
$pw    = Read-Host "PostgreSQL password for <DB_USER>" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $pw).Password
$enc   = [System.Uri]::EscapeDataString($plain)
$env:LE_DATABASE_URL = "postgresql://<DB_USER>:$enc@localhost:5432/enrich_pilot"
Remove-Variable plain, enc, pw

# A genuine, contactable user agent. Websites' operators will see this string.
$env:LE_USER_AGENT = "<BOTNAME>/<VERSION> (+<URL_OF_A_PAGE_YOU_CONTROL_THAT_EXPLAINS_THE_BOT>; <YOUR_REAL_CONTACT_EMAIL>)"
```
Rules enforced by the code (`benchmarks/pilot.py: live_settings`): the string must contain a URL starting `+http(s)://` or an
e-mail address, and must not be the repository's placeholder. **Make the URL real**: put a short page there stating who you are,
what the bot does, which IP/machine it comes from, and how to ask you to stop. I do not invent this for you.

Do **not** set `LE_HOST_OVERRIDES`, `LE_RESPECT_ROBOTS=false` or any proxy; the pilot refuses/overrides them.
Verify what is set (the URL contains a password, so print only what is safe):
```powershell
$env:LE_USER_AGENT
($env:LE_DATABASE_URL -replace ':[^:@/]+@', ':***@')
Get-ChildItem Env:LE_* | Select-Object Name        # names only
```

## 5. Offline smoke test — proves your Windows setup before any live request (uses a local fake site, no internet)

```powershell
python -m leadengine init-db                                   # connects, applies schema + migrations; prints "schema ready"
python -m benchmarks.pilot run --fixtures --fixture-sites 10 --reset --concurrency 2 --min-delay 0 --out-dir .\pilot_out\dryrun
```
Expected: a JSON block listing `report`, `provenance_csv`, `fetch_log_jsonl`, `review_sheet`, `audit_sheet`. Open
`pilot_out\dryrun\pilot-<stamp>.md`: its first line must say **FIXTURE pilot (synthetic)**. Those numbers are synthetic and must
never be mixed with live results. If this step fails, fix it before going on (see §9). If a Windows firewall dialog appears for
Python, the fake site only listens on 127.0.0.1; "Cancel/deny" is fine.

## 6. Prepare the 20-company input

### 6.1 Choosing the 20 sites
Only sites you are **authorised** to fetch — I cannot tell you which those are, and no site in this repository has granted
permission. Acceptable bases: your own sites; clients/partners who gave **written** permission (note who/when); public
marketing sites whose terms of use and `robots.txt` you read and which do not prohibit automated access (note the date).
Skip: anything behind a login/paywall, sites that forbid bots, personal sites or pages about private individuals, anything needing a CAPTCHA.

Aim for variety, because the point is to learn where the engine fails:

| about | kind of site |
|---|---|
| 8 | ordinary server-rendered company sites (WordPress/custom) with a visible contact page |
| 4 | site-builder sites (Wix/Squarespace/Shopify/Webflow-style) |
| 3 | JavaScript-heavy sites (content appears only after scripts run) — expect missing fields on native Windows |
| 2 | non-English or multi-language sites |
| 2 | larger/corporate sites likely to use bot protection — failures here are *results*, not problems |
| 1 | a site with a restrictive `robots.txt` (only if you may fetch it) — expected: refused |

### 6.2 Fill in the input file (exact schema)
```powershell
New-Item -ItemType Directory -Force pilot_out | Out-Null
Copy-Item benchmarks\templates\pilot_input.csv pilot_out\pilot_input.csv
notepad pilot_out\pilot_input.csv         # or VS Code; or Excel -> "Save As: CSV UTF-8 (Comma delimited)"
```
Columns (from `REQUIRED_COLS` + `name`): `domain,name,permission_basis,reviewed_by`.

* `domain` — bare registrable domain only: `acme.com`. No `https://`, paths, ports, subdomains, e-mail addresses, IPs.
* `name` — optional, your own label.
* `permission_basis` — a real sentence (≥15 characters; `TODO`, `n/a`, `ok`… are rejected), e.g. `own website operated by <YOUR_COMPANY>`,
  `written permission from <NAME> on <DATE>`, `public marketing site; ToS and robots.txt reviewed <DATE>`.
* `reviewed_by` — who verified the permission.
* Lines starting `#` are ignored. Maximum **50** rows (the tool refuses more). Duplicate domains (including `www.` variants) are rejected.
* Windows tools save CSV in different encodings/delimiters (PowerShell 5.1 `>` writes UTF-16; Excel may write Windows-1252 or `;`).
  The loader accepts UTF-8/UTF-8-BOM/UTF-16/Windows-1252 and `,` `;` tab `|`. Still, prefer "CSV UTF-8".

Validate offline (no network). Every problem is listed with the real file line number:
```powershell
python -m benchmarks.pilot validate --sample .\pilot_out\pilot_input.csv
```
Success looks like: `OK: 20 sites validated (no network access performed)`.

## 7. Safety gate — rehearse the refusals, then run the pilot

What the pilot enforces, and where the evidence is (`tests\test_pilot_gate.py` unless noted — all passing on Linux):

| control | enforced by | evidence |
|---|---|---|
| explicit opt-in | `--confirm-permitted` **and** `--confirm-sites <exact row count>` | `test_live_run_refuses_without_explicit_opt_in…` (`test_pilot.py`) |
| max 50 rows | `MAX_SITES` in `load_sample` | `test_sample_validation_rejects_duplicates…` |
| permission basis per row, bare domains | `load_sample` | `test_sample_validation_rejects_unsafe_or_unjustified_rows` |
| identifying user agent | `live_settings` + engine refuses the placeholder | `test_live_settings_force_conservative_limits…` |
| ≥ 2 s between requests to one host, 1 connection per host | forced `per_domain_min_delay=max(2, …)`, `per_domain_concurrency=1` | `test_at_least_two_seconds_between_requests_to_one_host` |
| robots.txt (401/403 ⇒ disallow, Crawl-delay honoured) | fetcher | `test_robots_disallow_prevents_any_page_request`, `test_hardening.py` |
| SSRF: private/loopback/metadata/IPv6/obfuscated IPs, redirects, DNS rebinding | `security.py`, per hop | `test_internal_destinations_are_refused…`, `test_redirects_to_internal_targets…`, `test_hostname_resolving_to_private_address…` |
| timeout (20 s hard deadline), 1 MB responses, ≤3 redirects, ≤1 retry | forced settings | `test_oversized_response_redirect_chain_and_retry_budget`, `test_hardening.py` slow-drip test |
| ≤3 pages/company, ≤4 companies at once | forced settings | `test_page_cap_and_audit_and_attribution_end_to_end` |
| no bypass of 401/403/CAPTCHA/anti-bot | permanent failures, never retried or rendered | `test_access_controls_and_challenges_are_not_retried_or_bypassed` |
| audit log + field-level attribution | `fetch_log`, `company_fields` | `test_every_stored_value_has_a_source_url_and_method`, `test_page_cap_and_audit…` |
| `_pilot` database only | `check_db_name` | `test_database_guard_rejects_url_tricks` |
| browser/AI off unless asked; AI needs key + prices before any request | `live_settings`, `run` | `test_ai_flags_fail_before_any_site_is_contacted` |

**Not verifiable here:** real DNS behaviour (rebinding is tested with a simulated resolver and the pinning logic), behaviour on
Windows, and any real site's response. The Linux tests used loopback servers.

Rehearse the guards — each command below must be REFUSED, and none touches the network:
```powershell
python -m benchmarks.pilot run --sample .\pilot_out\pilot_input.csv                      # -> "Refusing to contact live websites…"
$saved = $env:LE_USER_AGENT; $env:LE_USER_AGENT = "bot"
python -m benchmarks.pilot run --sample .\pilot_out\pilot_input.csv --confirm-permitted --confirm-sites 20   # -> "LE_USER_AGENT must identify you…"
$env:LE_USER_AGENT = $saved
```
(With the unedited template both refuse earlier, at "sample file rejected".) Only when `validate` prints `OK`:

```powershell
python -m benchmarks.pilot run --sample .\pilot_out\pilot_input.csv --confirm-permitted --confirm-sites <ROW_COUNT> --reset --out-dir .\pilot_out\run1
```
`<ROW_COUNT>` must equal the number of data rows (20). Defaults: 2 companies at a time, ≥3 s between requests per host, ≤3 pages
per company, 1 retry, 20 s per-request deadline, browser off, AI off, 30-minute time budget (`--max-seconds`). Those values cannot be
loosened (delay is raised to 2 s minimum, concurrency capped at 4, pages at 3, retries at 1). The run prints progress to the
console and then a JSON block of output paths. Stop early with **Ctrl+C**; just re-run with `--reset` (it starts from a clean pilot DB).

I cannot tell you how long it takes (no live measurement exists); the politeness delay dominates, so expect minutes, not seconds.

## 8. Outputs, the review sheet and scoring

Everything lands in `pilot_out\run1\` (this folder is git-ignored):

| file | what it is |
|---|---|
| `pilot-<stamp>.md` / `.json` | the report: every metric labelled MEASURED / ESTIMATED / UNKNOWN, effective settings, browser status |
| `pilot-<stamp>-review.csv` | **one row per company, pre-filled** with extracted name/email/phone, each field's source URL, website status, duplicate flags, failure reason; you fill the verdict columns |
| `pilot-<stamp>-provenance.csv` | every stored value: source URL, method, tier, extraction time, refresh date, verification status |
| `pilot-<stamp>-fetch-log.jsonl` | **audit log**: every request (pages, robots.txt, refusals) with host, status, bytes, error kind, timestamp |
| database `enrich_pilot` | the same data; `fetch_log`, `company_fields`, `enrichment_runs`, `job_items` |

Inspect the audit log straight from the database:
```powershell
psql $env:LE_DATABASE_URL -c "SELECT kind, error_kind, count(*) FROM fetch_log GROUP BY 1,2 ORDER BY 1,2;"
```
(psql accepts the URL form; if your psql rejects it, use `-h localhost -U <DB_USER> -d enrich_pilot`.)

### Review procedure (human, per company — about 5 minutes each)
Open `pilot-<stamp>-review.csv` in Excel (it is UTF-8 with BOM, so accents display correctly) — keep it as CSV.
The columns follow `benchmarks\templates\pilot_review.csv`:

`input_domain, input_name, extracted_company_name, company_name_source_url, website_final_url, website_status, extracted_email,
email_source_url, extracted_phone, phone_source_url, company_name_verdict, email_verdict, phone_verdict, duplicate_status,
usable_record, failure_reason, reviewer_notes`

1. Open each `*_source_url` in your own browser and compare. Fill the three **verdict** columns:
   * value present → `correct` | `incorrect` | `misattributed` (it is real but belongs to a different company/person/department than intended) | `unsure`
   * cell blank → look at the site yourself: `missed` (it is published and the engine didn't find it) | `absent` (the site doesn't publish it) | `unchecked`
2. `usable_record`: `yes` if the company name is correct **and** the e-mail or phone is correct, else `no`.
3. `duplicate_status` is pre-filled (`unique`, or `shared_email_with:<domain>` / `shared_phone_with:<domain>`); edit it if you see another duplicate.
4. `failure_reason` is pre-filled for failed sites; add context in `reviewer_notes`.
5. Do not mark any e-mail "verified": the project has no mailbox verification. "present in the site's own page" is all you are checking.

Score it (writes `<review>.scored.json` next to the sheet):
```powershell
python -m benchmarks.pilot score --review .\pilot_out\run1\pilot-<stamp>-review.csv --run-json .\pilot_out\run1\pilot-<stamp>.json
```
Rows with an invalid verdict (for example `correct` on an empty cell) are listed under `problems`. Then fill
`docs\templates\PILOT_RESULTS_TEMPLATE.md` (copy it into `pilot_out\run1\`); every row names its source key.

## 9. Troubleshooting

| symptom | cause / action |
|---|---|
| `running scripts is disabled` on `Activate.ps1` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` (this window only) |
| `psql` / `createdb` not recognised | add PostgreSQL `bin` to `$env:Path` (step 1) |
| `password authentication failed` | wrong password, or special characters not URL-encoded — redo the `$enc` lines in step 4 |
| `no database given: set LE_DATABASE_URL` | step 4 not done in *this* window (variables are per-window) |
| `database name must end in _pilot` / `must not contain ['dbname'…]` | by design; fix the URL — do not rename a production DB to satisfy it |
| `Psycopg cannot use the 'ProactorEventLoop'` | you are not starting through `python -m benchmarks.pilot` / `python -m leadengine` (they select the right loop); report it if it still happens |
| `sample file must have columns [...] (found: ...)` | wrong header, or Excel saved with another delimiter/encoding; re-save "CSV UTF-8" |
| `sample file rejected: line N …` | N is the real line in your file; each problem is listed — fix all, re-run `validate` |
| `Refusing to contact live websites` | missing `--confirm-permitted`, or `--confirm-sites` ≠ row count |
| `LE_USER_AGENT must identify you…` | step 4 |
| report says `robots` / `unreachable_robots` for a site | the site (or its robots.txt access rules) forbids you: a valid result; do not try to bypass |
| `unreachable_http` with 401/403 | access control / bot blocking; never retried, never bypassed |
| `bot_challenge` | an anti-bot challenge page was served; recorded and left alone |
| many `network` / DNS errors | your network/DNS, or a corporate proxy. **The pilot forces direct connections and refuses proxies; it will not be loosened.** Run from a network with direct internet |
| `browser_tier.error` is not empty / JS-only sites have no fields | the browser tier cannot run on native Windows; those fields are *missing because of that*, record it, or use WSL2 for a browser-enabled run |
| report written but a path in it is odd | all outputs are UTF-8; open `.json/.md` with VS Code, not an old Notepad |
| run seems stuck | politeness delay ≥2 s per host + 20 s deadlines; `--max-seconds` (default 1800) ends it; Ctrl+C then `--reset` |
| you pressed Ctrl+C and want to retry | rerun the same command (it includes `--reset`) |

## 10. Windows-specific notes and what was changed for them

Found by reading code/dependencies (not by running Windows) and fixed with unit tests: psycopg refuses Windows' default
`ProactorEventLoop` (now `leadengine.compat.configure_event_loop()` selects the selector loop in the CLI and the pilot); a
`import resource` (Unix-only) on the reporting path (now optional); reports/CSV writers used the locale encoding (now explicit
UTF-8; CSVs for Excel are UTF-8-BOM); the sample loader rejected UTF-16/Windows-1252/`;` files; `git`/`psql` missing crashed the
report step; the worker's POSIX signal handler (now falls back to `signal.signal`). **Still true on native Windows:** the selector
loop cannot launch Playwright's browser, so tier 4 is unavailable; the pilot says so in the report (`results.browser_tier`).
For a browser-enabled run use WSL2 (`wsl --install`, then follow the repository README's Linux commands inside Ubuntu).

## 11. Later, after the website pilot has been reviewed: validating the Anthropic (LLM) tier — NOT part of this stage

Nothing below was executed; no request was ever sent to the Anthropic API during this stage.

* **What it does** (`leadengine/ai.py`): for fields still missing after deterministic tiers (only `company_name, description, address,
  industry, founded_year` are AI-eligible), sends the **already-fetched, trimmed visible text** (≤ `LE_AI_MAX_INPUT_CHARS`=12000) of that
  company's pages and a JSON schema; accepts only values that are quoted verbatim from that text; at most one call per company.
  This sends third-party page content to Anthropic — enable it only if your permission basis covers that.
* **Install:** `pip install -e ".[ai]"` (anthropic SDK).
* **Key:** environment variable `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`), read by the SDK — it is not a setting of this project and is never
  stored, logged or put in a report (error text is redacted for `sk-…`, Bearer tokens, `password=`). Set it for one window only:
  ```powershell
  $k = Read-Host "Anthropic API key" -AsSecureString
  $env:ANTHROPIC_API_KEY = [System.Net.NetworkCredential]::new("", $k).Password; Remove-Variable k
  # … after the run:
  Remove-Item Env:ANTHROPIC_API_KEY
  ```
* **Model and settings** (`config.py` / pilot flags): `--ai-model` (`LE_AI_MODEL`, default `claude-haiku-5-5`), `LE_AI_MAX_OUTPUT_TOKENS` (2048; includes any
  thinking tokens), `LE_AI_TIMEOUT` (30 s), SDK retries 2, request uses `output_config` = JSON schema + `effort: low`. Replies whose `stop_reason`
  is not `end_turn` (truncated/refused) are discarded. Job budget `ai_calls` = number of rows.
* **Token accounting:** actual `input_tokens`/`output_tokens` from the API response are summed per run (`enrichment_runs.cost.ai_tokens_in/out`) and
  reported as `results.ai_cost.tokens_in/out` (MEASURED).
* **Cost:** the project does not know current prices. Pass them: `--ai-price-in <USD per million input tokens> --ai-price-out <USD per million output tokens>`
  (read them from Anthropic's pricing page on the day and record the date). `--ai` refuses to start without the key and both prices. USD figures are ESTIMATED.
* **How to test it (after the website pilot):** (1) keep the reviewed no-AI run as the baseline; (2) re-run the same sample with
  `python -m benchmarks.pilot run --sample … --confirm-permitted --confirm-sites <N> --reset --ai --ai-price-in <X> --ai-price-out <Y> --out-dir .\pilot_out\run2_ai`;
  (3) in `*-provenance.csv` filter `method = ai_grounded` — review exactly those values; (4) read `results.tiers.tier5_ai` (attempts, fields found, `note` lists
  rejected-as-ungrounded fields); (5) compare precision/misattribution against the baseline with the same review procedure.
  Start with a handful of sites; the first run exists to see real token counts, refusals and JSON failures, none of which have ever been observed.

## 12. Cleanup

```powershell
deactivate                                                  # leave the venv
Remove-Item Env:LE_DATABASE_URL, Env:LE_USER_AGENT, Env:PYTHONUTF8, Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
# keep pilot_out\ as your evidence folder, or delete it (it holds third-party company data and your permission records):
Remove-Item -Recurse -Force .\pilot_out
# drop the pilot database (irreversible) — only the _pilot database:
psql -h localhost -p 5432 -U <SUPERUSER> -c "DROP DATABASE enrich_pilot;"
psql -h localhost -p 5432 -U <SUPERUSER> -c "DROP ROLE IF EXISTS flowhq_pilot;"      # only if you created it in step 3
```

## 13. What to bring back for analysis

1. `pilot-<stamp>.json` (or `.md`) and `<review>.scored.json` — contain no secrets (settings list excludes the DB URL; the user agent *does* appear, i.e. your contact).
2. The completed `pilot-<stamp>-review.csv` and the filled results template (these contain company data; share what you are comfortable with).
3. `pilot-<stamp>-fetch-log.jsonl` (hosts, URLs, statuses) — for the politeness/robots audit.
4. The console output of `validate`, the dry run and the live run; any error text (after removing passwords/keys).
5. Anything surprising you saw while reviewing.
