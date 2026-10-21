"""Wikidata (SPARQL) - a free, documented, openly licensed (CC0) knowledge base. Finds organisations whose headquarters is in a named
Indian city and that publish an *official website* statement (P856).

Honest scope: Wikidata is community-edited and lists NOTABLE organisations. It is a good candidate generator for established IT-services
and software companies in a city, and a poor one for small AI-automation agencies (few have an item). Niche relevance is therefore only a
hint here (industry label / description keywords) and is judged properly later from the company's own website.

Compliance: read-only SELECT, one bounded query per place, sent through the engine's guarded Fetcher - so robots.txt of the endpoint is
ENFORCED (a disallow is reported as an error and never bypassed), requests identify you via LE_USER_AGENT (Wikimedia requires an identifying
User-Agent), and the query is capped (LIMIT, 20 s hard deadline). Wikimedia's query budget is about 60 s of query time per minute per client;
this source issues one query per place. No API key. Prefer your own Wikidata dump/mirror for large scale (--wikidata-endpoint).
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..models import SRC_WIKIDATA, RawHit, now_iso
from ..places import expand_locations, place_ok
from . import SourceContext

DEFAULT_ENDPOINT = "https://query.wikidata.org/sparql"
LICENCE_NOTE = ("Wikidata: structured data is CC0 1.0 (public domain). Requests identify the client via the User-Agent; Wikimedia's "
                "query budget is roughly 60 s of query time per minute per client. Coverage is limited to notable organisations.")
QID = re.compile(r"^Q\d+$")
JSON_TYPES = ("application/sparql-results+json", "application/json")


def build_query(place: str, cap: int) -> str:
    """SPARQL for one place. `place` must satisfy places.place_ok(); it is also escaped. P159 headquarters location, P856 official website,
    P452 industry, P131 located-in (transitive), P17 country, Q668 India. The city must itself be in India (no same-named towns abroad)."""
    label = place.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "SELECT ?item ?itemLabel ?website ?industryLabel ?desc WHERE {\n"
        f'  ?city rdfs:label|skos:altLabel "{label}"@en .\n'
        "  ?city wdt:P17 wd:Q668 .\n"
        "  ?hq wdt:P131* ?city .\n"
        "  ?item wdt:P159 ?hq ;\n"
        "        wdt:P856 ?website .\n"
        "  OPTIONAL { ?item wdt:P452 ?industry . }\n"
        '  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }\n'
        '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }\n'
        f"}} LIMIT {int(cap)}")


def _val(b: dict, k: str):
    v = (b.get(k) or {}).get("value")
    return v.strip() if isinstance(v, str) and v.strip() else None


class WikidataSource:
    name = SRC_WIKIDATA
    needs_network = True

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = endpoint

    def _url(self, query: str) -> str:
        return f"{self.endpoint}?format=json&query={quote(query, safe='')}"

    def planned_requests(self, ctx: SourceContext) -> list[str]:
        return [f"GET {self.endpoint}?query=<SPARQL for places in '{requested}': {place}>  (+ GET robots.txt)"
                for place, requested in expand_locations(ctx.locations)]

    async def discover(self, ctx: SourceContext) -> list[RawHit]:
        ctx.notes.append(LICENCE_NOTE)
        hits: list[RawHit] = []
        if not ctx.locations:
            ctx.error(self.name, "-", "no_location", "the Wikidata source needs at least one --location (an Indian city name)")
            return hits
        cap = max(20, min(ctx.limit * 3, 500))
        for place, requested in expand_locations(ctx.locations):
            if not place_ok(place):
                ctx.error(self.name, place, "invalid_location", "location must be a plain place name (letters, spaces, . ' & ( ) - /)")
                continue
            res = await ctx.fetcher.fetch(self._url(build_query(place, cap)), accept_types=JSON_TYPES)
            if not res.ok:
                msg = res.error or f"HTTP {res.status}"
                if res.error_kind == "robots":
                    msg += (" - the engine does not bypass robots.txt. Options: use your own SPARQL mirror (--wikidata-endpoint), "
                            "an offline OpenStreetMap extract (--osm-file), or a manual CSV (--csv).")
                ctx.error(self.name, place, res.error_kind or f"http_{res.status}", msg)
                continue
            try:
                bindings = json.loads(res.text)["results"]["bindings"]
                if not isinstance(bindings, list):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                ctx.error(self.name, place, "bad_response", "the endpoint did not return SPARQL JSON results")
                continue
            merged: dict[tuple, dict] = {}
            for b in bindings:
                item, site = _val(b, "item"), _val(b, "website")
                if not item or not site:
                    continue                                    # a result without an entity or a website states nothing we can use
                m = merged.setdefault((item, site), {"label": _val(b, "itemLabel"), "desc": _val(b, "desc"), "industries": []})
                ind = _val(b, "industryLabel")
                if ind and not QID.match(ind) and ind not in m["industries"]:
                    m["industries"].append(ind)
            for (item, site), m in merged.items():
                qid = item.rsplit("/", 1)[-1]
                label = m["label"] if m["label"] and not QID.match(m["label"]) else None          # never present a bare Q-id as a name
                ev = "; ".join(x for x in (f"Wikidata {qid}", f"industry: {', '.join(m['industries'])}" if m["industries"] else "",
                                           m["desc"] or "") if x)
                hits.append(RawHit(
                    website=site, name=label, location=place, industry=", ".join(m["industries"]) or None, source=SRC_WIKIDATA,
                    source_url=f"https://www.wikidata.org/wiki/{qid}", source_ref=f"sparql place '{place}'" + (f" (asked: {requested})" if requested != place else ""),
                    evidence=ev[:300], niche_query="; ".join(ctx.niches), location_query=requested, discovered_at=now_iso()))
        return hits
