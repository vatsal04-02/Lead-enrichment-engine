"""Place names for discovery sources: aliases (e.g. "Delhi NCR" -> its member cities) and coarse bounding boxes for filtering local
OpenStreetMap extracts. Boxes are deliberately approximate hints (neighbouring towns can leak in); they never decide India-ness -
that is judged later from the company's own data (see niche.india_evidence)."""
from __future__ import annotations

import re
from typing import Optional

# "Delhi NCR" is a region, not one OSM/Wikidata place: expand it to the cities a query can actually match.
ALIASES: dict[str, list[str]] = {
    "delhi ncr": ["Delhi", "New Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad"],
    "ncr": ["Delhi", "New Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad"],
    "national capital region": ["Delhi", "New Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad"],
}

# (south, west, north, east) in degrees; approximate.
CITY_BBOX: dict[str, tuple[float, float, float, float]] = {
    "bengaluru": (12.80, 77.40, 13.20, 77.80), "bangalore": (12.80, 77.40, 13.20, 77.80),
    "lucknow": (26.70, 80.80, 27.00, 81.10),
    "delhi": (28.40, 76.84, 28.90, 77.35), "new delhi": (28.48, 77.05, 28.70, 77.35),
    "gurugram": (28.35, 76.85, 28.55, 77.15), "gurgaon": (28.35, 76.85, 28.55, 77.15),
    "noida": (28.45, 77.30, 28.65, 77.60), "ghaziabad": (28.60, 77.35, 28.75, 77.55), "faridabad": (28.30, 77.20, 28.50, 77.40),
}
INDIA_BBOX = (6.0, 68.0, 37.6, 97.5)
NAME_RE = re.compile(r"^[^\W\d_][\w .'&()/-]{1,59}$", re.UNICODE)


def place_ok(name: str) -> bool:
    """A plain place name: safe to put (escaped) into a query."""
    return bool(NAME_RE.match(name or ""))


def key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def expand_locations(locations: list[str]) -> list[tuple[str, str]]:
    """-> [(concrete place, the location the user asked for)], de-duplicated, order kept. 'Delhi NCR' -> six cities."""
    out, seen = [], set()
    for requested in locations:
        for place in ALIASES.get(key(requested), [requested]):
            if (key(place), key(requested)) not in seen:
                seen.add((key(place), key(requested)))
                out.append((place, requested))
    return out


def bbox_for(place: str) -> Optional[tuple[float, float, float, float]]:
    return CITY_BBOX.get(key(place))


def in_box(lat: float, lon: float, box: tuple[float, float, float, float]) -> bool:
    s, w, n, e = box
    return s <= lat <= n and w <= lon <= e
