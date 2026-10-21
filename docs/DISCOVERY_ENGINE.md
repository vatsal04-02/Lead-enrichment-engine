# FlowHQ live business discovery & enrichment

Finds candidate Indian AI-automation agencies / B2B companies from sources **you choose and have reviewed**, then enriches them with the
existing engine. ₹0 paid APIs. Nothing contacts a real host until you explicitly say so, and it has never been run against the real web
by the authors of this code (see [Status](#status-and-limitations)).

## Flow

```
sources (you choose)        validate + dedupe                 enrich (existing engine)                       output directory
manual CSV            ─┐                                   ┌─ 1. ScrapeGraphAI + local Ollama (qwen3:4b)
public listing pages  ─┼─► URL checks, blocked-domain  ──► ├─ 2. fallback: in-house HTTP/HTML scraper   ──► discovery_candidates.csv
OpenStreetMap (Overpass)─┘   screen, dedupe by domain      └─ 3. both failed: manual-review record        enriched_leads.csv, discovery_report.md,
                                                                                                          errors.jsonl, fetch logs, run_config.json
```

### Discovery sources implemented

| source | flag | what it does | compliance notes |
|---|---|---|---|
| Manual CSV | `--csv FILE` | imports candidates a person found by any compliant means (needs a website/domain column; optional `source`, `source_url`, `notes`, `city`, `industry`). **The always-available path.** | reads a local file; no network |
| Public listing pages | `--seed-url URL` / `--seed-file FILE` | fetches *only the pages you name* (e.g. a published "AI automation agencies in India" list) and takes links to other sites. Directories, social networks, marketplaces and shared-hosting domains are dropped. | guarded fetcher: robots.txt enforced; a 401/403/CAPTCHA/bot challenge is recorded and **not** retried or bypassed; ≥2 s per host; you must read each site's terms first |
| OpenStreetMap | `--osm --location CITY` | one read-only Overpass query per city for mapped offices (IT/company/consulting/software/…) with a `website` tag, plus mapped objects whose *name* matches the niche keywords; coarse India bounding box. Output includes the ODbL attribution. | open data, free; public instance is shared infrastructure—keep cities few or set `--overpass-endpoint` to your own; coverage of small agencies is partial |
| Wikidata (SPARQL) | `--wikidata --location CITY` | organisations whose headquarters is in the city and that state an official website (CC0). Notable companies only. | guarded fetcher: **robots.txt enforced**, identifying User-Agent, one bounded query per place; a refusal is reported with the lawful alternatives, never bypassed |
| OpenStreetMap local extract | `--osm-file FILE` (repeatable) | mapped offices with a website tag, read from a GeoJSON / OSM XML file you downloaded; **no network** | ODbL attribution in the report; robots does not apply to a local file |

`--location "Delhi NCR"` expands to Delhi, New Delhi, Gurugram, Noida, Ghaziabad, Faridabad. `--only-niche-signal` keeps only candidates whose own source
text mentions a niche keyword (useful for these broad sources). `check-sources` fetches **only the robots.txt files** of the remote sources (live-gated)
and tells you which are allowed *before* a run. `--protect-database NAME` / `LE_DISCOVERY_PROTECTED_DATABASES` fence off databases (your live pilot)
from a live run. Why these sources and not others, with quotas/keys and what could not be verified: **[docs/DISCOVERY_SOURCES.md](DISCOVERY_SOURCES.md)**.

**Deliberately not implemented:** scraping Google/Bing/DuckDuckGo result pages, LinkedIn, Clutch/GoodFirms/Justdial or any directory that
forbids automated access, and any paid search/enrichment API. Search-engine scraping violates those services' terms and runs into bot
challenges, which this project never bypasses. If you want those sources, find candidates by hand and use `--csv`.

### Enrichment fallback chain (recorded, never silent)

1. **Primary – ScrapeGraphAI on local Ollama** (`--model qwen3:4b --ollama-url http://127.0.0.1:11434`). The existing backend
   (`docs/SCRAPEGRAPH_INTEGRATION.md`): our guarded fetcher fetches, only page text reaches the isolated worker, answers are stored only
   if the page itself contains them. For `qwen3*` models "thinking" is switched off (`think=false`) because a reasoning model otherwise
   spends the small output budget on deliberation and returns nothing.
2. **Fallback – in-house engine**, same fetcher/rate limits. Used (a) for the whole run if the primary cannot start (Ollama down, model not
   installed, wrong interpreter) – the report and `errors.jsonl` say why; (b) per company when the model call fails/times out; (c) per company
   when the model run finds nothing on a reachable site. **Access refusals are never retried with the other engine** (robots, bot
   challenge, 401/403, off-domain redirect, unreachable site).
3. **Manual review** – companies that still have nothing are rows with `review_status = needs_manual_review` and a `manual_review_reason`
   (attempts, error kinds). `--backend scrapegraph` refuses to run without the primary; `--backend inhouse` skips it by choice.

### Field provenance and status (every extracted field)

For each of company_name, description, email, phone, address, industry, linkedin_url, `enriched_leads.csv` has `<field>`, `_source_url`,
`_backend` (`scrapegraph` | `inhouse` | `manual`), `_method`, `_status`, `_verification`, `_extracted_at`.

| `_status` | meaning |
|---|---|
| `extracted` | read from the company's own page (a model answer must also appear on that page) |
| `conflict` | several distinct supported values; the best is stored, alternates kept in the database |
| `independently_verified` | only the **website** check (`website_status`: homepage fetched and the final host belongs to the domain) |
| `manually_reviewed` | a person confirmed or corrected it with `review` (below) |
| `rejected_by_review` | a person marked it wrong; value kept for the record, do not use |
| `invalid` / `not_found` / `skipped_budget` | the model produced something the page does not support / nothing stated / a budget stopped the attempt |

E-mail is never mailbox-verified. `cross_source_agreement` (two extraction methods on the same site agree) is shown in `_verification` but the
field stays `extracted`, because it is not independent of the site. Niche and India columns are **evidence hints** (`niche_match` =
strong/weak/none/unknown from the company's own text; `india_signal` = yes only with a `.in` domain, +91 phone or an address naming an
Indian state/city, otherwise `unconfirmed`).

## Output directory (new every run; nothing is overwritten)

`discovery_out/<UTC timestamp>-<id>/`: `discovery_candidates.csv`, `enriched_leads.csv`, `discovery_report.md`, `rejected_candidates.csv`
(invalid / blocked / reserved / duplicate rows with reasons), `errors.jsonl` (only when something failed), `fetch_log.jsonl` +
`discovery_fetch_log.jsonl` (every request made), `run_config.json` (settings, no secrets). `discovery_candidates.csv` has
`website,name,location,industry` columns, so the existing `submit-csv` / `run-csv` workflow reads it directly; `enrich --candidates` reads it back
with provenance intact. Synthetic fixture hosts (`*.fixture.test`) are rejected as `reserved_domain` and can never be counted as real businesses.

## Safety and cost controls

* No contact without `--live --confirm-terms-reviewed --confirm-count N` where N is the exact number `plan` prints; otherwise a **dry run**
  (offline parts run, network targets are listed). `LE_USER_AGENT` must identify you with a contact; `LE_HOST_OVERRIDES` must be empty.
* Forced limits (mirror the Stage 5 pilot, asserted equal by a test): robots on, SSRF guard fully on, ≤4 companies at once (default 2), one
  connection per host, ≥2 s (default 3 s) between requests to a host, ≤3 pages per company, ≤1 retry, 1 MB responses, 20 s hard deadline,
  browser off, no in-house AI tier, a run time budget (`--max-seconds`, default 3600) and a page budget; model: ≤2 calls and ≤2 pages per
  company, one worker, timeout `--model-timeout` (default 120 s).
* **The confirmed number is binding.** `--confirm-count` is checked against the plan *and again at execution*: if the candidate list grew after it was
  counted (e.g. the CSV was edited), the run is refused before anything is written or contacted. A network source that plans zero requests (e.g.
  `--osm` without `--location`) is never "authorized" by default.
* **A live run writes to whatever database the environment names.** `--database-url` defaults to `LE_DATABASE_URL`, so set it deliberately: the run
  prints `database: host:port/name` (no credentials) before it starts and records it in `run_config.json`. Use a dedicated database.
* Tests never read your environment: the gate suite (`tests/test_discovery_gates.py`) removes ambient `LE_*` variables, uses synthetic hosts only, and
  asserts that every refusal happens before `execute()`, before any Engine/Fetcher exists, and with zero DNS/network attempts.
* Existing data is preserved: there is no reset/truncate path; the database is only added to (use a dedicated one, e.g. `enrich_discovery`); each
  run's output contains only its own companies.
* No outreach of any kind is implemented.

## Commands

Windows PowerShell (repo root, `.venv` active, `LE_DATABASE_URL` pointing at a **dedicated** database, ScrapeGraphAI interpreter set up as in
`docs/SCRAPEGRAPH_INTEGRATION.md`):

```powershell
# 0. check the local model path (read-only; never downloads a model)
python -m leadengine.discovery doctor --sg-python C:\leadengine-sg\.venv-sg\Scripts\python.exe
#    if qwen3:4b is missing it prints:  ollama pull qwen3:4b   (you run that yourself)
python -m leadengine.discovery doctor --sg-python C:\leadengine-sg\.venv-sg\Scripts\python.exe --smoke   # one local extraction on a built-in synthetic page

# 1. DRY TESTING - nothing is contacted
python -m leadengine.discovery plan --csv docs\templates\discovery_candidates_template.csv --seed-file docs\templates\seed_urls_template.txt --osm --location Pune --location Bengaluru
python -m leadengine.discovery run  --csv my_candidates.csv --out-root discovery_out        # offline parts only, writes a dry-run directory
python -m pytest tests\test_discovery_unit.py tests\test_discovery_integration.py -q        # local servers only

# 2. SEPARATELY INITIATED LIVE RUN (only after you reviewed every source's terms/robots and the plan)
$env:LE_USER_AGENT   = "FlowHQBot/1.0 (+https://your-site/bot; you@your-domain)"
$env:LE_DATABASE_URL = "postgresql://USER@HOST:PORT/enrich_discovery"
$env:LE_SCRAPEGRAPH_PYTHON = "C:\leadengine-sg\.venv-sg\Scripts\python.exe"
python -m leadengine.discovery run --csv my_candidates.csv --limit 100 --live --confirm-terms-reviewed --confirm-count <N from the dry run>

# 3. after reviewing the output, record a person's checks (writes a NEW file; see docs\templates\discovery_review_template.csv)
python -m leadengine.discovery review --leads discovery_out\<run>\enriched_leads.csv --review reviewed.csv
```

Target run for Lucknow / Delhi NCR / Bengaluru (dry, then separately initiated; use a **dedicated** database and fence off your pilot database):

```powershell
$env:LE_DISCOVERY_PROTECTED_DATABASES = "flowhq_live_pilot"
$env:LE_DATABASE_URL = "postgresql://USER@HOST:PORT/enrich_discovery"        # NOT the pilot database
$niches = @("--niche","AI automation agency","--niche","business process automation","--niche","IT services","--keyword","WhatsApp automation","--keyword","workflow automation")
$places = @("--location","Lucknow","--location","Delhi NCR","--location","Bengaluru")

python -m leadengine.discovery check-sources --wikidata --osm                  # dry: lists the robots.txt files it would read (2 hosts)
python -m leadengine.discovery check-sources --wikidata --osm --live --confirm-terms-reviewed --confirm-count 2   # reads ONLY robots.txt; exit 1 = refused
python -m leadengine.discovery plan --wikidata --osm-file offices.geojson --csv my_candidates.csv @niches @places --only-niche-signal --limit 100
python -m leadengine.discovery run  --wikidata --osm-file offices.geojson --csv my_candidates.csv @niches @places --only-niche-signal --limit 100   # dry run
# live, only after reading the terms: add  --live --confirm-terms-reviewed --confirm-count <N printed by the dry run>
```
(`--osm` is left out of `run` because the public Overpass instance refused this engine via robots.txt; add it only if `check-sources` says your endpoint is allowed.)

Two-step variant: `discover` (candidates only) → look at `discovery_candidates.csv` → `enrich --candidates discovery_out\<run>\discovery_candidates.csv`.

## Status and limitations

* **Not run against the real web.** Verified only against local servers (fixture sites, a listing-page server, an Overpass stand-in, a stub
  Ollama server). The live path (real DNS, real robots files, real sites' markup, Overpass load limits) is untested; expect surprises.
* Ollama and `qwen3:4b` were **not available in the development sandbox**, so no real local model has run. Whether `qwen3:4b` extracts well, and how
  slow it is on your CPU (raise `--model-timeout`), is unknown until you run `doctor --smoke` and a small pilot.
* OpenStreetMap lists few small agencies, and a listing page is only as good as its author; reaching 100 *relevant* Indian agencies from these
  sources is **not assured**. The report states how many leads were actually found and makes no claim beyond the counts. "Usable" means
  reachable website + company name + e-mail or phone present; **presence is not correctness**.
* Niche relevance and India location are keyword/string hints, not classifications. Seed-page links point at whatever the page links to;
  if a list links to directory profiles instead of company sites those candidates are dropped (not followed).
* Exact-name duplicates on different domains are flagged, not merged; the dedupe key is the registrable domain.
* Windows: the worker/engine path is the same as the ScrapeGraphAI integration (selector loop, blocking pipes in a thread); untested on native Windows.
