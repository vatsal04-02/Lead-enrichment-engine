"""Plain data types shared by discovery sources, validation, enrichment and the output writers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# validation_status values (candidate level)
VALID = "valid"
INVALID_URL = "invalid_url"
RESERVED = "reserved_domain"          # .test/.example/localhost-style names: never a real business
BLOCKED = "blocked_domain"            # social networks, directories, marketplaces, shared hosting platforms
DUPLICATE = "duplicate"

# source names (also the value of `discovery_source` in the CSVs)
SRC_CSV = "manual_csv"
SRC_SEED = "seed_page"
SRC_OSM = "osm_overpass"
SRC_OSM_FILE = "osm_extract"
SRC_WIKIDATA = "wikidata"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RawHit:
    """What a source reported, before validation. Nothing here is verified."""
    website: str
    name: Optional[str] = None
    location: Optional[str] = None          # as stated by the source
    industry: Optional[str] = None
    source: str = ""
    source_url: str = ""                    # the real page / object the hit came from ('' only for manual rows without one)
    source_ref: str = ""                    # e.g. "candidates.csv#row 12" or the API query
    evidence: str = ""                      # short snippet shown to the reviewer (anchor text, tags)
    niche_query: str = ""
    location_query: str = ""
    discovered_at: str = ""
    carried: dict = field(default_factory=dict)   # review columns carried over from a previous discovery_candidates.csv


@dataclass
class Candidate:
    website: str                            # normalised start URL
    domain: str                             # registrable key (the dedupe key)
    name: Optional[str]
    location: Optional[str]
    industry: Optional[str]
    niche_query: str
    location_query: str
    discovery_source: str
    source_url: str
    source_ref: str
    evidence: str
    niche_signal: str                       # keyword terms found in name/evidence, '' if none (a hint, not a classification)
    discovered_at: str
    validation_status: str = VALID
    validation_notes: str = ""
    duplicate_sources: list = field(default_factory=list)    # [(source, source_url)] for merged duplicates
    review_status: str = "unreviewed"
    reviewed_by: str = ""
    synthetic: bool = False


@dataclass
class Rejected:
    raw: str
    status: str
    reason: str
    source: str
    source_url: str
    source_ref: str
    name: Optional[str] = None


@dataclass
class SourceError:
    source: str
    where: str
    kind: str
    message: str
    at: str = field(default_factory=now_iso)
