"""Company profile enrichment: read companies (name + website) from a CSV, fetch their public Home/About/Services/Contact pages through the
engine's guarded fetcher (robots.txt, rate limits, SSRF guard, no bypass), and have a LOCAL model (ScrapeGraphAI + Ollama, e.g. qwen3:4b)
extract a structured profile. Writes a NEW CSV; the input is never modified. No database, no paid or cloud API.

Truthfulness rule: the model is a reader, not a source of truth. Every stored statement must be tied to a passage that verifiably appears on
a fetched page (or, for the three hypothesis fields, cite such a passage and is labelled 'inferred' for human review). Anything else is dropped.
"""
