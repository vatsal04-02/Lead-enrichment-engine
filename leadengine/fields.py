"""Field registry: what we can enrich, where it is likely to live, how fresh it must be."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str                 # text | email | phone | url | year
    pages: tuple[str, ...]    # page kinds most likely to carry it, best first
    freshness_days: int       # default refresh interval
    weight: float             # weight in completeness / quality scores
    ai_eligible: bool         # may the AI tier be asked for it?
    social_host: str = ""     # for *_url social fields


PAGE_KINDS = ("home", "about", "contact", "team", "services")

FIELDS: dict[str, FieldSpec] = {f.name: f for f in [
    FieldSpec("company_name", "text", ("home", "about"), 365, 1.0, True),
    FieldSpec("description", "text", ("home", "about"), 180, 1.0, True),
    FieldSpec("email", "email", ("contact", "home", "about"), 90, 1.0, False),
    FieldSpec("phone", "phone", ("contact", "home"), 90, 1.0, False),
    FieldSpec("address", "text", ("contact", "about", "home"), 180, 0.75, True),
    FieldSpec("industry", "text", ("about", "home", "services"), 365, 0.5, True),
    FieldSpec("founded_year", "year", ("about", "home"), 365, 0.25, True),
    FieldSpec("linkedin_url", "url", ("home", "contact", "about"), 180, 0.5, False, "linkedin.com"),
    FieldSpec("twitter_url", "url", ("home", "contact", "about"), 180, 0.25, False, "twitter.com"),
    FieldSpec("facebook_url", "url", ("home", "contact", "about"), 180, 0.25, False, "facebook.com"),
    FieldSpec("instagram_url", "url", ("home", "contact", "about"), 180, 0.25, False, "instagram.com"),
    FieldSpec("github_url", "url", ("home", "about"), 180, 0.25, False, "github.com"),
    FieldSpec("youtube_url", "url", ("home", "about"), 180, 0.25, False, "youtube.com"),
    FieldSpec("logo_url", "url", ("home",), 365, 0.25, False),
    # Not requestable: always recorded from input; verified by a reachability + domain-match check.
    FieldSpec("website", "url", ("home",), 90, 0.0, False),
]}

DEFAULT_REQUESTED = tuple(f for f in FIELDS if f != "website")


def resolve_requested(requested: list[str] | None) -> list[str]:
    if not requested:
        return list(DEFAULT_REQUESTED)
    unknown = [f for f in requested if f not in FIELDS or f == "website"]
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}; valid: {', '.join(FIELDS)}")
    seen, out = set(), []
    for f in requested:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out
