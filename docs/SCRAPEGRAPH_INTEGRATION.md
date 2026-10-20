# Optional ScrapeGraphAI extraction backend

**Status: optional, off by default, never contacted a real website.** The in-house engine (tiers 1–5) is unchanged and remains
the default. Everything in this document was verified on synthetic localhost fixtures against a stub model server only; see
[What has and has not been verified](#what-has-and-has-not-been-verified).

## 1. Library audit (done before any code was written)

Audited: open-source `scrapegraphai` **2.3.1** (wheel read, not just the README).

| question | finding |
|---|---|
| License | MIT. |
| Python | **≥ 3.12.** This project supports ≥ 3.11, so the library cannot be a hard dependency. It lives in its own interpreter (see §3). |
| Entry point used | `SmartScraperGraph(prompt, source, config, schema)`. |
| `source` semantics | A string starting with `http` is a **URL the library fetches itself** (`requests`, a stealth Chromium, BrowserBase or Scrape.do). Anything else is treated as local HTML/content and is not fetched. |
| Hosted service | `llm.model == "scrapegraphai/smart-scraper"` routes to ScrapeGraphAI's hosted API. Refused by this integration. |
| Model config | `llm.model_instance` + `model_tokens` bypasses provider inference. We build the LangChain chat model ourselves (Ollama, OpenAI-compatible, Anthropic). |
| Telemetry | **On by default**; the payload includes page content when the source is a URL. Disabled in the worker (`SCRAPEGRAPHAI_TELEMETRY_ENABLED=false` and `disable_telemetry()`), verified at start-up. |
| Tokenizer | `num_tokens_calculus` always loads tiktoken's gpt-4o encoding → a network download, even with Ollama. Replaced by an offline estimator in the worker. |
| Prompt content | HTML is converted to markdown first: `<head>`, `<meta>` and JSON-LD are **dropped**. We restate title/meta/JSON-LD as visible paragraphs in the digest we send. |
| Browser needs | Only for URL sources (not used). The integration never lets the library fetch or launch a browser. |
| Usage | Token counts via LangChain `usage_metadata`; read through a callback that also enforces the call cap. |

### Conflicts between the library and this project's security model, and how each is resolved

| conflict | resolution |
|---|---|
| The library fetches URLs itself (no SSRF guard, no robots.txt, no redirect validation, can drive a stealth browser that evades bot detection). | **It never fetches.** Our guarded `Fetcher` fetches every page (SSRF/DNS pinning, redirect validation, robots.txt, 401/403/CAPTCHA = permanent failure, page budgets, `fetch_log`). The adapter hands the worker page *content* only; the worker refuses any `source` that is not HTML. |
| Library telemetry exfiltrates page content by default. | Disabled and asserted in the start-up self-check. |
| The library imports hosted-SDK / stealth / proxy / search modules. | Made un-importable in the worker (`scrapegraph_py`, `undetected_*`, `free_proxy`, `ddgs`, `browserbase`, `selenium`); proven blocked by the self-check. |
| A library bug or prompt-injected page could still try a network call or spawn a process. | Worker guards: socket/DNS allow-list = the model's host:port only; `subprocess`/`exec*`/`fork` blocked; scrubbed environment and temporary `HOME`. The self-check runs 7 probes (public IP, metadata IP, DNS lookup, spawn, browser launch, stealth import, hosted SDK) on every start and refuses to run unless **all** are blocked. |
| The model may invent values. | Grounding (§5): a value is stored only if the page itself supports it. |

**Boundary statement:** the library's own network and browser behavior *cannot* be fully controlled from inside its process, so
the safer boundary is used: it runs in a separate, guarded worker process and receives no URL. If that boundary cannot be proven
at start-up the engine refuses to start; it does not weaken the safeguards and does not fall back silently.

## 2. Architecture

```
guarded Fetcher ──page HTML──► CompanyLLMSession (caps, budgets, cache) ──digest──► ScrapeGraphBackend ──JSON lines──► worker process
 (unchanged)                         │                                                   │ (parent: timeouts,                  │ (guards, library,
                                     ▼                                                   │  size cap, retries, kill)           │  allow-listed model host only)
                          normalize.py: ground + validate ──► Candidate ──► existing scoring / provenance / company_fields
```

* `leadengine/backends/base.py` – the common interface (`ExtractionBackend.extract_page`) and result types.
* `leadengine/backends/inhouse.py` – the in-house engine behind the same interface (used by tests/benchmarks; the runtime
  pipeline path for the default engine is untouched).
* `leadengine/backends/scrapegraph.py` – validated config (`ResolvedConfig`), worker pool, bounded retries, error mapping.
* `leadengine/backends/sg_worker.py` – the guarded worker (stdlib + scrapegraphai only; stdout reserved for the protocol).
* `leadengine/backends/normalize.py` – digest builder and grounding/normalisation (pure functions).
* `leadengine/backends/session.py` – per-company page/call caps, budgets, cache, usage accounting, candidate creation.
* `benchmarks/stub_llm.py` – a local stand-in model server for tests (**a rule-based responder, not a language model**).

Backend selection is explicit: `LE_EXTRACTION_BACKEND=inhouse` (default) or `scrapegraph`. When `scrapegraph` is selected the
in-house tier 5 (Anthropic) is skipped for that run; the deterministic tiers still run and the model's grounded values compete
with them through the normal scoring (`scrapegraph_llm` has a low static prior and never outranks structured data on confidence).

### What is stored

Same tables as before. For a model-extracted value: `method = scrapegraph_llm`, `tier = tier_scrapegraph`, `source_url` = the real
page the value was found on, `extracted_at`/`refresh_after`, `verification_status = unverified` (a model never "verifies"
anything; e-mail is never mailbox-verified), and `checks.extraction_state` ∈ `extracted | missing | invalid | conflict | skipped_budget`.
`invalid` means the model produced something the page does not support — it is **not stored** and is counted. `conflict` means
several distinct supported values; the best one is stored and the others are kept as alternates. The `invalid`/`missing`
distinction is visible in `company_view` and the API (`extraction_state`).

## 3. Install

The library needs Python ≥ 3.12 and pulls in a large dependency tree (LangChain, Playwright, …). Keep it out of your main
environment unless that environment is already ≥ 3.12.

### Windows (PowerShell)

```powershell
# 0. main project (as before)
cd C:\Users\vatsa\Lead-enrichment-engine
py -3.11 -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 1. a SEPARATE Python 3.12+ environment for the optional library (install Python 3.12 first if `py -0` doesn't list it)
py -0
py -3.12 -m venv C:\leadengine-sg\.venv-sg
C:\leadengine-sg\.venv-sg\Scripts\python.exe -m pip install --upgrade pip
C:\leadengine-sg\.venv-sg\Scripts\python.exe -m pip install "scrapegraphai>=2.3.1,<2.4"
# only for provider=anthropic:
C:\leadengine-sg\.venv-sg\Scripts\python.exe -m pip install langchain-anthropic

# 2. tell the engine where it is (current session)
$env:LE_SCRAPEGRAPH_PYTHON = "C:\leadengine-sg\.venv-sg\Scripts\python.exe"
```

Linux/macOS equivalent: `python3.12 -m venv ~/.venv-sg && ~/.venv-sg/bin/pip install 'scrapegraphai>=2.3.1,<2.4'` and
`export LE_SCRAPEGRAPH_PYTHON=~/.venv-sg/bin/python`. If your main interpreter is already ≥ 3.12 you can instead
`pip install -e ".[scrapegraph]"` and leave `LE_SCRAPEGRAPH_PYTHON` unset.

You do **not** need `playwright install`: the integration never launches the library's browser.

### Local model with Ollama (recommended first)

```powershell
ollama serve                      # if not already running as a service
ollama pull llama3.2              # any model you choose; there is NO default model
$env:LE_EXTRACTION_BACKEND = "scrapegraph"
$env:LE_SCRAPEGRAPH_ENABLED = "true"
$env:LE_SCRAPEGRAPH_PROVIDER = "ollama"
$env:LE_SCRAPEGRAPH_MODEL = "llama3.2"
$env:LE_SCRAPEGRAPH_BASE_URL = "http://127.0.0.1:11434"
```

Start-up asks Ollama for `/api/tags` (no generation) and refuses to start if the model is not installed, with the
`ollama pull …` command in the error.

### Remote provider (explicit opt-in; sends page content to a third party)

```powershell
$env:LE_SCRAPEGRAPH_PROVIDER = "openai"               # or anthropic
$env:LE_SCRAPEGRAPH_MODEL = "<model you chose>"
$env:LE_SCRAPEGRAPH_BASE_URL = "https://<your OpenAI-compatible host>/v1"   # https required for non-loopback hosts
$env:LE_SCRAPEGRAPH_API_KEY_ENV = "OPENAI_API_KEY"    # the NAME of the variable
$env:OPENAI_API_KEY = Read-Host "key"                 # set the key yourself; it is never written to disk, logs or the database
$env:LE_SCRAPEGRAPH_PRICE_IN_PER_MTOK = "<USD per million input tokens>"
$env:LE_SCRAPEGRAPH_PRICE_OUT_PER_MTOK = "<USD per million output tokens>"
```

Only the provider and model you name are used. A missing key, missing package or unreachable server is a start-up error — never a
silent switch to another provider or to the in-house engine. The worker is given only the one key variable you named.

### Required / optional environment variables

| variable | needed | notes |
|---|---|---|
| `LE_EXTRACTION_BACKEND` | to enable | `inhouse` (default) / `scrapegraph` |
| `LE_SCRAPEGRAPH_ENABLED` | to enable | must be `true` as well (two switches on purpose) |
| `LE_SCRAPEGRAPH_PROVIDER`, `LE_SCRAPEGRAPH_MODEL` | yes | `ollama` / `openai` / `anthropic`; no defaults |
| `LE_SCRAPEGRAPH_BASE_URL` | Ollama: default `http://127.0.0.1:11434`; openai-compatible: required | no credentials in the URL |
| `LE_SCRAPEGRAPH_PYTHON` | if the main interpreter is < 3.12 or lacks the library | path to the Python ≥ 3.12 interpreter |
| `LE_SCRAPEGRAPH_API_KEY_ENV` + that variable | remote providers | name only in config |
| `LE_SCRAPEGRAPH_PRICE_IN/OUT_PER_MTOK` | remote providers (for cost estimates) | local models: leave 0 |
| `LE_USER_AGENT`, `LE_DATABASE_URL` | as before | |

## 4. Cost and resource controls (all enforced in code, all tested)

| control | setting (default) | what happens at the limit |
|---|---|---|
| model calls per company | `LE_SCRAPEGRAPH_MAX_CALLS_PER_COMPANY` (2) | further pages are skipped (`pages_skipped.call_cap`) |
| pages sent to the model per company | `LE_SCRAPEGRAPH_MAX_PAGES_PER_COMPANY` (2) | skipped (`page_cap`) |
| input size | `LE_SCRAPEGRAPH_MAX_INPUT_CHARS` (12000) | digest truncated |
| output tokens | `LE_SCRAPEGRAPH_MAX_OUTPUT_TOKENS` (512) | sent to the model server as `num_predict`/`max_tokens` |
| worker response size | `LE_SCRAPEGRAPH_MAX_WORKER_RESPONSE_BYTES` (200000) | worker killed, call counted as failed |
| timeout per request | `LE_SCRAPEGRAPH_REQUEST_TIMEOUT` (60 s) | worker killed and replaced; call counted as timed out |
| retries | `LE_SCRAPEGRAPH_RETRIES` (0, max 2) | bounded **within** the remaining per-company call allowance |
| concurrent model requests | `LE_SCRAPEGRAPH_WORKERS` (1) | others wait |
| per-job model calls | `LE_BUDGET_AI_CALLS` (job budget `ai_calls`) | `skipped_budget`; the job continues with deterministic tiers |
| per-job tokens | `LE_BUDGET_AI_TOKENS` (2,000,000; job budget `ai_tokens`) | same |
| estimated USD | existing `LE_BUDGET_EST_COST_USD` | same as before |
| after a model error on a company | automatic | no further pages are sent for that company; the failure is recorded, "not found" is **not** written |

Job budgets are ceilings: API/CLI clients can lower them, never raise them. With the default engine, job budgets are unchanged
(`ai_tokens` appears only when the scrapegraph backend is active).

Tracked per run (`enrichment_runs.cost.llm`) and aggregated in `metrics` and the pilot report (`model_usage`): provider, model,
scope (`local`/`external`), calls, tokens in/out, latency, failures, timeouts, retries, pages sent/cached/skipped, rejected
(unsupported) values, estimated external USD, blocked worker network attempts (must be 0).

**Local inference is not free.** Ollama usage is reported as tokens and latency, with `external_usd_estimated = 0` and a note that
hardware, electricity and time are not priced. External USD is *measured tokens × the prices you configured* and is labelled
ESTIMATED.

## 5. How values are accepted (grounding)

The model is a reader, not a source of truth. Every answer is checked against the page text we sent:

* e-mail: valid syntax and the address occurs on the page · phone: plausible digit count and the digits occur on the page ·
  social/profile URL: valid for that platform and occurs on the page · year: in range and occurs · address: every comma-separated
  part occurs on the page · other text: occurs verbatim (case/whitespace-insensitive).
* Anything else is dropped, counted (`rejected`), and shown as `extraction_state = invalid`. Nothing is guessed or completed.
* A page that the guarded fetcher refused (robots, 401/403, challenge, off-domain redirect, page budget) is never sent to the model.
* Prompt-injection text on a page can at most change what the model *says*; it cannot change what is *stored* unless the page
  itself contains the claimed value (which is then attributed to that page, as for any other extractor).

## 6. Running both backends on the synthetic fixtures

```powershell
# default engine (unchanged)
python -m benchmarks.run_local --sites 200
python -m benchmarks.pilot run --fixtures --fixture-sites 40 --database-url postgresql://USER@HOST:PORT/enrich_pilot --reset --out-dir pilot_out\inhouse

# optional backend, SIMULATED rule-based responder instead of a model (needs only the Python>=3.12 library env)
python -m benchmarks.pilot run --fixtures --fixture-sites 40 --backend scrapegraph --sg-stub `
    --sg-python C:\leadengine-sg\.venv-sg\Scripts\python.exe --database-url postgresql://USER@HOST:PORT/enrich_pilot --reset --out-dir pilot_out\sg-stub

# optional backend, REAL local model on the synthetic fixtures
python -m benchmarks.pilot run --fixtures --fixture-sites 40 --backend scrapegraph --sg-provider ollama --sg-model llama3.2 `
    --sg-base-url http://127.0.0.1:11434 --sg-python C:\leadengine-sg\.venv-sg\Scripts\python.exe `
    --database-url postgresql://USER@HOST:PORT/enrich_pilot --reset --out-dir pilot_out\sg-ollama

# side-by-side benchmark of both engines on identical synthetic sites (JSON + CSV + Markdown in benchmarks\results\)
python -m benchmarks.compare_backends --database-url postgresql://USER@HOST:PORT/enrich_pilot --sites 60 --model stub-heuristic `
    --sg-python C:\leadengine-sg\.venv-sg\Scripts\python.exe
python -m benchmarks.compare_backends ... --model stub-noisy       # responder that invents values 35% of the time: shows the grounding filter at work
python -m benchmarks.compare_backends ... --model ollama --sg-model llama3.2 --sg-base-url http://127.0.0.1:11434
```

The database name must end in `_pilot` (it is truncated). These commands contact only `127.0.0.1` fixtures and your model server.

### Interpreting the comparison report

* **Section 1 – SYNTHETIC.** Known ground truth from the generator. `precision` = correct / stored; `recall` = correct / published
  by the generator; `unsupported_claims` = stored values not literally on the attributed page; `source_attribution_complete` =
  stored values with source URL + method + timestamp **and** the value present on that page. Latency is per-company wall time
  under the benchmark's concurrency on localhost, so only the *relative* difference is informative.
* With `--model stub-*` the scrapegraph column measures the integration, **not a model**: the stub is a rule-based reader that was
  written against the fixture page shapes, so equal scores there prove plumbing (grounding, budgets, provenance), not quality.
  `stub-noisy` shows that invented values never reach storage. Only `--model ollama` exercises a real model — still on synthetic pages.
* **Section 2 – HUMAN-REVIEWED LIVE.** Empty (`UNKNOWN`) until you pass `pilot score` outputs from audited, permitted live pilots
  with `--live-scored`. Synthetic accuracy must never be quoted as live accuracy.
* **Section 3 – UNKNOWN.** What cannot be known from fixtures.

## 7. Security limitations (known, not hidden)

* The worker guards are in-process Python hooks (socket/DNS/subprocess patches + import blocks), not an OS sandbox. They stop the
  library and ordinary bugs/injected content from reaching the network or spawning processes, and they are verified on every start,
  but a determined native-code escape inside the worker is out of scope. For a stronger boundary run the worker interpreter in a
  container/VM with no network except the model host.
* A remote provider receives page text (up to `MAX_INPUT_CHARS` per call). Do not enable one for pages you may not share. API keys
  are passed to the worker through a single named environment variable and are not logged; the stub tests assert this.
* The telemetry/tokenizer/markdown findings in §1 are properties of version 2.3.x. The version range is pinned (`<2.4`); re-audit
  before widening it.
* The browser tier of the in-house engine is unaffected (and, as before, cannot run on native Windows).
* Model output can be wrong *and* supported by the page (e.g. picking a partner's e-mail on a page). Grounding stops fabrication,
  not mis-selection; that is what the human audit in a live pilot measures.

## 8. Rollback / troubleshooting

* **Rollback:** unset `LE_EXTRACTION_BACKEND` and `LE_SCRAPEGRAPH_ENABLED` (or set the first to `inhouse`). Nothing else changes; no
  migration is involved. Already stored `scrapegraph_llm` rows stay valid and are attributed as such.
* *"requires LE_SCRAPEGRAPH_ENABLED=true"* – both switches are mandatory.
* *"library not importable / interpreter …"* – wrong `LE_SCRAPEGRAPH_PYTHON`, Python < 3.12, or `scrapegraphai` not installed there.
* *"model … is not installed in Ollama"* – `ollama pull <model>`; *"server unreachable"* – start Ollama / check the base URL.
* *"guard … NOT blocked"* – the start-up self-check failed: the library version changed behavior. Do not run; re-audit.
* Timeouts on a slow CPU: raise `LE_SCRAPEGRAPH_REQUEST_TIMEOUT` (each call is bounded; there is no unbounded wait).
* Windows: the engine runs on the selector event loop; the worker talks over plain blocking pipes in a thread, so this is
  compatible. Windows-specific behavior of the real worker is **untested** (see below).

## What has and has not been verified

Verified in the Linux development sandbox, with the real library (2.3.1, Python 3.12) in the worker and a **stub** model server:
preflight guard probes, telemetry off, model-presence check, end-to-end extraction with attribution/provenance/cost accounting,
grounding against invented values, timeouts/hangs/HTTP 500/garbage output, budgets and caps, concurrency ≤ workers, worker
shutdown, refused-fetch scenarios, protocol hardening and API-key handling.

**Not verified:** any real language model's extraction quality; any real website; the worker on native Windows; macOS; remote
providers (only an OpenAI-compatible stub); Ollama itself (only an API-compatible stub).
