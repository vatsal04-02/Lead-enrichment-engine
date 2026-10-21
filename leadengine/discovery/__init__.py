"""Live business discovery + enrichment orchestration for FlowHQ.

Discovery finds *candidate* company websites from sources a person has chosen and reviewed (manual CSV, public listing pages,
OpenStreetMap open data). Enrichment reuses the existing engine: ScrapeGraphAI (local Ollama) first, the in-house HTTP/HTML
scraper as the explicit fallback, and a manual-review record when both fail. Nothing here fabricates data, bypasses access
controls, or calls a paid API. See docs/DISCOVERY_ENGINE.md.
"""
