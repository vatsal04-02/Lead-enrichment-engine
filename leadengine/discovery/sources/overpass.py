"""OpenStreetMap open data through an Overpass API instance (free; ODbL licence, attribution required).

Finds mapped offices (office=it/company/consulting/software/advertising/research ...) that publish a `website` tag inside a named
city area, plus any mapped object with a `website` whose NAME matches the niche keywords. OSM coverage of small agencies is
partial, so this finds only what contributors mapped; it is a *candidate generator*, and niche relevance is judged later from
the company's own website. The query is read-only, bounded (timeout, result cap) and one request per location. The public
instance is shared infrastructure: keep locations few. Use --overpass-endpoint for your own instance.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..models import SRC_OSM, RawHit, now_iso
from . import SourceContext

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
OFFICES = "it|company|consulting|software|advertising|research|coworking|educational_institution"
INDIA_BBOX = (6.0, 68.0, 37.6, 97.5)           # south, west, north, east: coarse filter against same-named places abroad
NAME_RE = re.compile(r"^[^\W\d_][\w .'&()/-]{1,59}$", re.UNICODE)


def location_ok(loc: str) -> bool:
    return bool(NAME_RE.match(loc or ""))


def build_query(location: str, keywords: list[str], cap: int) -> str:
    """Overpass QL. `location` is validated by location_ok() and additionally escaped; keywords are regex-escaped."""
    loc = location.replace("\\", "\\\\").replace('"', '\\"')
    kws = [re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", k) for k in keywords if k and len(k) <= 40][:25]
    name_clause = ""
    if kws:
        rx = "|".join(kws).replace('"', "")
        name_clause = f'  nwr(area.a)["name"~"{rx}",i]["website"];\n  nwr(area.a)["name"~"{rx}",i]["contact:website"];\n'
    return (f'[out:json][timeout:60];\n'
            f'area["name"="{loc}"]["boundary"="administrative"]->.a;\n'
            f'(\n'
            f'  nwr(area.a)["office"~"^({OFFICES})$"]["website"];\n'
            f'  nwr(area.a)["office"~"^({OFFICES})$"]["contact:website"];\n'
            f'{name_clause}'
            f');\n'
            f'out tags center {int(cap)};')


def _in_india(el: dict) -> bool:
    c = el.get("center") or ({"lat": el.get("lat"), "lon": el.get("lon")} if "lat" in el else None)
    if not c or c.get("lat") is None:
        return True                      # no coordinates: cannot exclude; the later India-evidence column decides
    s, w, n, e = INDIA_BBOX
    return s <= float(c["lat"]) <= n and w <= float(c["lon"]) <= e


class OverpassSource:
    name = SRC_OSM
    needs_network = True

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = endpoint

    def _url(self, query: str) -> str:
        return f"{self.endpoint}?data={quote(query, safe='')}"

    def planned_requests(self, ctx: SourceContext) -> list[str]:
        return [f"GET {self.endpoint}?data=<Overpass query for area '{loc}'>  (+ GET robots.txt)" for loc in ctx.locations]

    async def discover(self, ctx: SourceContext) -> list[RawHit]:
        ctx.notes.append("OpenStreetMap data: (c) OpenStreetMap contributors, ODbL 1.0 - https://www.openstreetmap.org/copyright")
        hits: list[RawHit] = []
        if not ctx.locations:
            ctx.error(self.name, "-", "no_location", "the OpenStreetMap source needs at least one --location (a city name)")
            return hits
        cap = max(10, min(ctx.limit * 3, 500))
        for loc in ctx.locations:
            if not location_ok(loc):
                ctx.error(self.name, loc, "invalid_location", "location must be a plain place name (letters, spaces, . ' & ( ) - /)")
                continue
            q = build_query(loc, ctx.keywords, cap)
            url = self._url(q)
            res = await ctx.fetcher.fetch(url, accept_types=("application/json",))
            if not res.ok:
                ctx.error(self.name, loc, res.error_kind or f"http_{res.status}", res.error or f"HTTP {res.status}")
                continue
            try:
                data = json.loads(res.text)
                elements = data.get("elements", [])
            except (ValueError, AttributeError):
                ctx.error(self.name, loc, "bad_response", "Overpass did not return JSON")
                continue
            if not isinstance(elements, list):
                ctx.error(self.name, loc, "bad_response", "unexpected Overpass response shape")
                continue
            for el in elements:
                tags = el.get("tags") or {}
                site = tags.get("website") or tags.get("contact:website")
                if not site or not tags.get("name") or not _in_india(el):
                    continue
                addr = ", ".join(tags[k] for k in ("addr:street", "addr:city", "addr:state") if tags.get(k)) or loc
                otype, oid = el.get("type", "node"), el.get("id")
                hits.append(RawHit(
                    website=site, name=tags["name"], location=addr, industry=tags.get("office"), source=SRC_OSM,
                    source_url=f"https://www.openstreetmap.org/{otype}/{oid}", source_ref=f"overpass area '{loc}'",
                    evidence=("OSM tags: " + ", ".join(f"{k}={tags[k]}" for k in ("office", "description", "addr:city") if tags.get(k)))[:300],
                    niche_query="; ".join(ctx.niches), location_query=loc, discovered_at=now_iso()))
        return hits
