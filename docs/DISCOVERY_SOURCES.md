# Discovery source assessment (₹0, no paid APIs, no restricted scraping)

Target: up to 100 Indian B2B prospects in **Lucknow, Delhi NCR, Bengaluru** - AI automation agencies, business-process automation, IT services
(keywords incl. "WhatsApp automation", "workflow automation"). Assessed 2026-10-21.

**How reliable this table is.** The development sandbox cannot fetch these hosts, so nothing below was verified first-hand (no robots.txt was read,
no query was run). Claims come from search results and third-party summaries and are marked *(unverified)* where they matter. **Re-check the
terms of any source before relying on it.** The engine does not trust this table either: it reads each source's robots.txt at run time and refuses
what it forbids, and `check-sources` shows you the verdict beforehand.

## Verdict

| # | source | free / key | licence / terms | robots / quota | finds company websites for the target? | status |
|---|---|---|---|---|---|---|
| 1 | **Wikidata SPARQL** (`--wikidata`) | free, no key | data CC0; identifying User-Agent required *(from Wikimedia guidance, unverified here)* | robots of `query.wikidata.org/sparql` **unverified**; ~60 s query time/min/client *(unverified)* | organisations HQ'd in the city with an official-website statement. **Notable companies only**: plausible for established IT-services firms, weak for small AI-automation agencies | **implemented** |
| 2 | **OpenStreetMap local extract** (`--osm-file`) | free, no key | ODbL 1.0, attribution (the report includes it) | none: a file you downloaded (bulk extract), no crawling | mapped offices (`office=it/company/consulting/software/...`) with a `website` tag. Partial coverage; count unknown | **implemented** |
| 3 | OpenStreetMap **Overpass API** (public instance, `--osm`) | free | ODbL; fair use ≈10k requests/day, identify yourself *(wiki, unverified)* | **blocked: the engine observed `robots.txt` disallowing the request** (your run). Mirrors have their own policies *(unverified)* | same data as #2 | kept; works only with an endpoint whose robots.txt permits it (`--overpass-endpoint`); check with `check-sources` |
| 4 | Google Programmable Search / Custom Search JSON API | key | - | **closed to new customers; ends 1 Jan 2027** *(search results)* | - | not viable |
| 5 | Brave Search API | key + card | - | free tier reportedly removed Feb 2026 for ~$5/month metered credit; card required, overage billed *(search results)* | - | **not implemented: conflicts with "no paid APIs"**. Your call if a metered credit is acceptable |
| 6 | Search-engine result pages (Google, Bing, DuckDuckGo HTML) | - | automated scraping violates their terms; bot challenges | - | - | **never implemented** |
| 7 | Restricted directories (LinkedIn, Clutch, GoodFirms, Justdial, IndiaMART, ...) | - | automated access forbidden | - | - | **never implemented**; browse by hand and import with `--csv` |
| 8 | GitHub API (orgs by location) | free token | acceptable-use policy forbids using GitHub user data for spam/unsolicited outreach *(unverified)* | 60/h unauthenticated | dev-oriented orgs; uncertain fit for lead generation | not implemented: terms-grey, needs your decision |
| 9 | Registries (MCA company master via data.gov.in, Udyam, Startup India) | free key | open data | - | **no website field and no niche**, so no domains by themselves | not implemented |
| 10 | Common Crawl | free | open | - | no niche/city search; very heavy | not implemented |
| 11 | OpenCorporates | key, quotas | - | - | no websites | not implemented |
| + | Manual CSV (`--csv`), listing pages you choose (`--seed-url`) | - | your responsibility | engine enforces robots | whatever you find | **implemented** (earlier stage) |

## The blocker, stated plainly
No source could be **verified** (from here) as both robots-permitted and aimed at *small AI-automation agencies*. The two implemented automatic
sources reach established IT-services/software firms (Wikidata) and mapped offices (OSM); small agencies are mostly outside them. Reaching 100
relevant prospects is therefore **not assured**; the report states the real count. The reliable top-ups are your own research via `--csv` and
public listing pages you have read the terms of (`--seed-url`).

## API keys and quotas for what is implemented
None. No key, no account, no payment. Requirements: `LE_USER_AGENT` identifying you with a contact (Wikimedia asks for it; the engine insists);
one Wikidata query per place (Delhi NCR = 6 queries, 8 in total for your three cities) with a 20 s hard deadline each; a local OSM file needs nothing.
Large-scale use of Wikidata should go through your own mirror/dump (`--wikidata-endpoint`), as Wikimedia's bot guidance recommends.

## Getting an OSM extract (one-time, by you)
Any of these is a bulk open-data download or an interactive tool, not a crawl by this engine; read the provider's terms first.
* **Overpass Turbo** (web UI, a person running a query and exporting GeoJSON) for one city at a time, e.g. offices with a website inside the city.
* **Geofabrik / BBBike** regional or city extract (`.osm.pbf`), then filter and convert with `osmium` (the engine does not read `.pbf` itself):
  ```
  osmium tags-filter india.osm.pbf nwr/office=it,company,consulting,software,advertising,research -o offices.osm.pbf
  osmium export offices.osm.pbf -o offices.geojson
  ```
Then: `--osm-file offices.geojson --location Lucknow --location "Delhi NCR" --location Bengaluru` (coarse bounding boxes, hints only).
