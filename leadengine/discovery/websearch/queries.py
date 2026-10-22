"""Query planning: industries x cities x phrasings, ordered so that EVERY (industry, city) combination is tried before any is tried a second time.
Nothing here is a claim about a company: a query is only what was asked of a search engine."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

DEFAULT_INDUSTRIES: dict[str, list[str]] = {
    "IT services": ["IT services company", "software development company"],
    "SaaS": ["SaaS company", "B2B software product company"],
    "Healthcare": ["healthcare services provider", "medical devices company"],
    "Education": ["edtech company", "corporate training company"],
    "Real estate": ["real estate developer", "property consultants"],
    "Marketing": ["digital marketing agency", "advertising agency"],
    "Business services": ["business consulting firm", "HR and staffing company"],
}
DEFAULT_CITIES = ["Bengaluru", "Mumbai", "Pune", "Hyderabad", "Chennai", "Delhi", "Gurugram", "Noida", "Kolkata", "Ahmedabad", "Jaipur",
                  "Lucknow", "Chandigarh", "Indore", "Kochi"]
# "contact us"/"about us" bias results towards a company's own pages rather than listicles; "official website" likewise.
TEMPLATES = ["{phrase} in {city}", "{phrase} {city} contact us", "{phrase} {city} about us", "{phrase} {city} official website"]


@dataclass(frozen=True)
class Query:
    text: str
    industry: str
    city: str
    phrase: str
    page: int = 1


def plan_queries(industries: dict[str, list[str]], cities: list[str], max_queries: int, max_pages: int = 2) -> list[Query]:
    """Round r = every (industry, city) once with its r-th phrasing (templates x phrases), then page 2 of the same, ... Deterministic; no repeats."""
    combos = list(product(industries, cities))
    variants = {ind: [(ph, tpl) for tpl in TEMPLATES for ph in phrases] for ind, phrases in industries.items()}
    out: list[Query] = []
    longest = max((len(v) for v in variants.values()), default=0)
    for page in range(1, max_pages + 1):
        for r in range(longest):
            for ind, city in combos:
                if r >= len(variants[ind]):
                    continue                                        # this industry has no further phrasing: never repeat a query
                ph, tpl = variants[ind][r]
                out.append(Query(tpl.format(phrase=ph, city=city), ind, city, ph, page))
                if len(out) >= max_queries:
                    return out
    return out
