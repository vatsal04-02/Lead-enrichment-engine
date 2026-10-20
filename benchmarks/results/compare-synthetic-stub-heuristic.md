# Backend comparison report

Run: 2026-10-20T21:38:20+00:00 · code `fced40e` · Linux-6.18.44-fc-v139-x86_64-with-glibc2.39, Intel(R) Xeon(R) Processor @ 2.10GHz x4, 15.7 GiB, Python 3.13.16

> **Everything below the SYNTHETIC heading is measured on generated local sites with known ground truth. It is NOT evidence of accuracy on real websites.**

> **Model: SIMULATED rule-based responder (stub-heuristic); NOT a language model.** The scrapegraph column therefore measures the integration (grounding, budgets, provenance, cost accounting), not language-model quality.

## 1. SYNTHETIC (fixture sites, generator ground truth)

Sites: 60 · mix: `{"injection": 2, "slow": 2, "not_found": 2, "jsonld": 6, "plain": 15, "flaky": 6, "etag": 3, "contact_only": 11, "prose_only": 3, "broken_html": 4, "js_shell": 4, "ratelimited": 2}` · concurrency 4

| metric | inhouse | scrapegraph |
|---|---|---|
| companies | 60 | 60 |
| usable_correct_rate | 0.6 _SYNTHETIC_ | 0.6 _SYNTHETIC_ |
| usable_correct_count | 36 | 36 |
| fields_stored | 212 | 212 |
| fields_correct | 212 | 212 |
| fields_wrong_or_spurious | 0 | 0 |
| overall_precision | 1.0 _SYNTHETIC_ | 1.0 _SYNTHETIC_ |
| unsupported_claims_total | 0 _SYNTHETIC_ | 0 _SYNTHETIC_ |
| source_attribution_complete | 1.0 _SYNTHETIC_ | 1.0 _SYNTHETIC_ |
| email_mailbox_verified | 0 _MEASURED_ | 0 _MEASURED_ |
| failure_rate | 0.033 _SYNTHETIC_ | 0.033 _SYNTHETIC_ |
| outcomes | {"unreachable": 2, "enriched": 21, "partial": 30, "no_data": 7} | {"unreachable": 2, "enriched": 21, "partial": 30, "no_data": 7} |
| failure_reasons | {"http": 2} | {"http": 2} |
| latency_ms_p50 | 29.3 _MEASURED_ | 140.7 _MEASURED_ |
| latency_ms_p95 | 1040.0 _MEASURED_ | 1177.1 _MEASURED_ |
| http_requests_per_company | 3.1 _MEASURED_ | 3.1 _MEASURED_ |
| pages_per_company | 2.93 _MEASURED_ | 2.93 _MEASURED_ |
| http_retry_rate | 0.043 _MEASURED_ | 0.043 _MEASURED_ |
| wall_seconds | 3.77 _MEASURED_ | 5.58 _MEASURED_ |
| model_calls_per_company | 0 _MEASURED_ | 1.45 _MEASURED_ |
| cost_usd_per_usable | 0.0 _MEASURED_ | None _UNKNOWN_ |
| tokens_in_per_company | - | 1271 _MEASURED_ |
| tokens_out_per_company | - | 62 _MEASURED_ |
| model_retry_rate | - | 0.0 _MEASURED_ |
| model_timeout_rate | - | 0.0 _MEASURED_ |
| model_failures | - | 0 _MEASURED_ |
| unsupported_values_dropped_before_storage | - | 0 _MEASURED_ |
| worker_blocked_network_attempts | - | 0 _MEASURED_ |
| tokens_per_usable | - | 2223 _MEASURED_ |
| model_scope | - | local |
| model | - | stubmodel |
| provider | - | ollama |

### Per field (precision / recall / unsupported claims)

| field | inhouse | scrapegraph |
|---|---|---|
| company_name | P=1.0 R=0.9 cov=0.6 unsup=0 | P=1.0 R=0.9 cov=0.6 unsup=0 |
| description | P=1.0 R=1.0 cov=0.6 unsup=0 | P=1.0 R=1.0 cov=0.6 unsup=0 |
| email | P=1.0 R=0.927 cov=0.85 unsup=0 | P=1.0 R=0.927 cov=0.85 unsup=0 |
| phone | P=1.0 R=0.922 cov=0.783 unsup=0 | P=1.0 R=0.922 cov=0.783 unsup=0 |
| address | P=1.0 R=1.0 cov=0.35 unsup=0 | P=1.0 R=1.0 cov=0.35 unsup=0 |
| linkedin_url | P=1.0 R=1.0 cov=0.35 unsup=0 | P=1.0 R=1.0 cov=0.35 unsup=0 |

## 2. HUMAN-REVIEWED LIVE

**UNKNOWN - none supplied.** No live website was contacted and no human-audited live sample exists for either engine. Run a separately reviewed, permitted pilot (docs/STAGE5_PILOT_GUIDE.md) per backend, audit it, then pass the `.scored.json` files with `--live-scored`.

## 3. UNKNOWN (not measured here)

- Accuracy of either engine on REAL websites (no live site was contacted).
- Quality of any real language model unless `--model ollama` was used - and even then only on synthetic pages.
- Behaviour on JavaScript-heavy, anti-bot, multilingual or very large real pages.
- Cost of local inference (hardware, electricity, GPU time): reported as tokens/latency, deliberately not converted to money.
- Mailbox existence of any e-mail address: never verified by this project.
- Latency under real network conditions and politeness delays: fixtures answer in milliseconds on localhost.
