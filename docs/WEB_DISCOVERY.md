# Web-search discovery (`python -m leadengine.discovery.websearch`)

Pipeline: **search queries → result collection → candidate extraction → website validation → de-duplication → website enrichment → CSV/JSON export.**
It keeps searching (more industries × cities × phrasings × result pages) until it has the requested number of *valid* companies or a budget runs out, and always says which.

How this relates to lead-intelligence products: they combine a company index, web signals and enrichment. This engine does the part that is legitimately reproducible
without their databases: find candidate company websites with ordinary web search, check each one by visiting its own public pages, extract identity and publicly listed
contact details, and ground everything in what those pages say. It never touches Apollo, LinkedIn, Clutch, Justdial, IndiaMART or similar platforms (blocked in `urlcheck.py`).

## Search providers (checked 2026-10; terms change - re-check)
| provider | cost / key | notes |
|---|---|---|
| `searxng` (default) | free, no key | a metasearch engine **you run** (e.g. Docker on localhost). Needs `json` in `search.formats` of its `settings.yml`; its limiter must allow your client. It queries upstream engines from your address, so *their* terms and rate limits apply: keep `--search-min-interval` at 2 s or more. |
| `brave` | **metered, key + card**: `LE_BRAVE_API_KEY` | free tier removed Feb 2026 for about USD 5/month of credit; overage is billed; storage/AI-training restrictions in its terms. Only used if you select it. |
| `static` | free, offline | replays a JSON file of saved results (`{"results": {"<query>": [{"title","url","snippet"}]}}`), e.g. results you collected by hand. |

Not implemented on purpose: scraping Google/Bing/DuckDuckGo result pages (forbidden by their terms, challenged), Google Programmable Search (closed to new customers), Bing Search API (retired Aug 2025).
**No browser automation is used**: search comes from an API, and company sites are fetched with the engine's guarded HTTP fetcher (robots.txt, SSRF guard, ≥2 s per host, a challenge/401/403 is recorded and the site is skipped, never bypassed).

### Running SearXNG locally (Windows, Docker)
```powershell
mkdir searxng; # create searxng\settings.yml containing at least:
#   use_default_settings: true
#   server: { secret_key: "<random>", limiter: false }     # limiter off is fine for a private localhost instance
#   search: { formats: [html, json] }
docker run -d --name searxng -p 127.0.0.1:8080:8080 -v ${PWD}/searxng:/etc/searxng searxng/searxng
python -m leadengine.discovery.websearch probe --live --confirm-terms-reviewed --confirm-count 1     # one query: do JSON results come back?
```
(Instance configuration keys are from SearXNG documentation and third-party guides read during development; they were not run in the development sandbox. If `probe` says `format_disabled`, fix `search.formats`.)

## Commands
```powershell
$env:LE_USER_AGENT = "YourBot/1.0 (+https://your-site/bot; you@your-domain)"; $env:LE_SCRAPEGRAPH_PYTHON = "C:\path\to\py312\Scripts\python.exe"
python -m leadengine.discovery.websearch plan --target 100                     # DRY RUN (the default): prints queries, budgets and the exact --confirm-count
python -m leadengine.discovery.websearch run  --target 100 --enrich --live --confirm-terms-reviewed --confirm-count <N> --label e2e100
```
`--enrich` requires a ready local model (`python -m leadengine.discovery doctor`); discovery outputs are written even if enrichment cannot run, and pages already fetched during validation are
reused by enrichment (no second request for the same page). Options: `--industry "Label=phrase;phrase"`, `--city`, `--max-queries`, `--max-pages-per-query`, `--max-sites`, `--max-seconds`, `--concurrency`, `--cache-dir`.

## Output (a new folder per run: `websearch_out/<UTC time>-websearch[-label]/`, never overwritten)
`discovery.csv` / `discovery.json` (every candidate with status), `profile_input.csv`, `rejected_results.csv` (search results rejected, with reason), `search_log.jsonl`, `fetch_log.jsonl`
(every company-site request), `run.log`, `summary.json`; with `--enrich`: `enriched.csv`, `enriched.summary.json`, `summary-<time>.json` (success / partial / failed / anti-bot counts, model, tokens).

`discovery.csv` columns: Company, Website, City, Industry/Niche, SourceURL, DiscoveryStatus, Confidence, MissingFields, Description, Email, Phone, Address, LinkedIn, ContactSourceURL, IndiaEvidence,
IndustryEvidence, NameSource, Domain, SearchProvider, SearchQuery, SearchRank, AlsoFoundVia, Detail.
* **City / Industry/Niche are what was searched for.** `IndiaEvidence` says whether the site itself confirms India (`.in`, `+91`, an Indian address, or the searched city in its text); `IndustryEvidence` lists niche terms found in the company's own text. The verified industry comes from the enrichment step (`industry_niche`, quote-grounded).
* **DiscoveryStatus**: `selected` (valid, unique, inside the target), `discovered_not_selected`, `india_unconfirmed`, `not_a_company_site`, `parked_or_empty`, `robots_disallowed`, `anti_bot_blocked`, `redirected_off_domain`, `unreachable`, `duplicate_identity`, `not_visited` (site budget reached).
* **Confidence** (0-1) is a heuristic over evidence found (company-like site reached .25, name confirmed on the site .15, India evidence .20/.10, description .10, email or phone .10, address .10, niche terms .10, +.05 per extra independent search hit up to 2). It is not a probability. `MissingFields` lists what the site did not provide.
* Emails/phones are the ones the company publishes on its own pages; they are never mailbox-verified.
* De-duplication: by registrable domain (a second sighting only adds `AlsoFoundVia`), then by normalised company name after the site confirmed its real name.

## Limits and what is not verified
Search results for "IT company in Pune" include listicles, directories and media; those are rejected with a reason, and what survives depends on the search backend. The yield of *valid Indian B2B
companies per query* was **not measured against a real search backend in development** (the development sandbox has no public network). Everything above is tested with a loopback fake search endpoint
and fixture sites only. If the target is not reached, `summary.json.stop_reason` says why (`queries_exhausted`, `site_budget_exhausted`, `time_budget_exhausted`, `search_provider_failing`).
