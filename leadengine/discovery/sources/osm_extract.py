"""OpenStreetMap data from a LOCAL FILE you obtained yourself (bulk open-data download, not a crawl).

Why this exists: a public API (Overpass) can refuse automated clients via robots.txt - and this engine never bypasses that. OSM data is
openly licensed (ODbL 1.0, attribution required) and offered as bulk extracts precisely for this kind of use, so you download an extract
once (Geofabrik regional extract, a BBBike city extract, or an Overpass Turbo / `osmium export` GeoJSON for your cities) and this source
reads it with NO network access.

Supported files: GeoJSON (`.geojson`/`.json`; tags either flat in `properties` or under `properties.tags`), Overpass-style OSM XML
(`.osm`, `.xml`, optionally `.gz`/`.bz2`; nodes, and ways/relations carrying a <center> as written by `out center`).
Raw `.osm.pbf` is NOT read directly (no unverified parser is bundled): convert it first, e.g.
    osmium tags-filter india.osm.pbf nwr/office=it,company,consulting,software,advertising,research -o offices.osm.pbf
    osmium export offices.osm.pbf -o offices.geojson

Each kept hit is a mapped office (office=* tag) with a name and a website tag, inside the box of a requested city (coarse approximate
boxes in places.py) or, for an unknown place, matching its addr:city tag. Provenance is the OSM object URL plus the file name.
"""
from __future__ import annotations

import bz2
import gzip
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Optional

from ..models import SRC_OSM_FILE, RawHit, now_iso
from ..places import INDIA_BBOX, bbox_for, expand_locations, in_box, key
from . import SourceContext

ATTRIBUTION = "OpenStreetMap data: (c) OpenStreetMap contributors, ODbL 1.0 - https://www.openstreetmap.org/copyright"
OFFICES = {"it", "company", "consulting", "software", "advertising", "research", "coworking", "educational_institution", "telecommunication", "financial"}
MAX_BYTES = 2_000_000_000
SITE_TAGS = ("website", "contact:website", "url")


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    if path.suffix == ".bz2":
        return bz2.open(path, "rb")
    return path.open("rb")


def _centroid(geom: dict) -> Optional[tuple[float, float]]:
    """(lat, lon) of a GeoJSON geometry: the point itself, or the mean of all its vertices (a hint, not a survey)."""
    pts: list = []

    def walk(c):
        if isinstance(c, (list, tuple)) and len(c) >= 2 and all(isinstance(x, (int, float)) for x in c[:2]):
            pts.append(c)
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)
    walk((geom or {}).get("coordinates", []))
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def iter_geojson(path: Path) -> Iterator[tuple[str, dict, Optional[tuple[float, float]]]]:
    with _open(path) as f:
        data = json.load(f)
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise ValueError("not a GeoJSON FeatureCollection")
    for ft in feats:
        props = (ft or {}).get("properties") or {}
        tags = props.get("tags") if isinstance(props.get("tags"), dict) else props
        oid = str(props.get("@id") or props.get("id") or (ft or {}).get("id") or "")
        yield oid, {k: v for k, v in tags.items() if isinstance(v, (str, int, float))}, _centroid((ft or {}).get("geometry"))


def iter_osm_xml(path: Path) -> Iterator[tuple[str, dict, Optional[tuple[float, float]]]]:
    with _open(path) as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag in ("node", "way", "relation"):
                tags = {t.get("k"): t.get("v") for t in el.findall("tag") if t.get("k")}
                if tags:
                    c = el.find("center")
                    lat, lon = ((c.get("lat"), c.get("lon")) if c is not None else (el.get("lat"), el.get("lon")))
                    pos = (float(lat), float(lon)) if lat is not None and lon is not None else None
                    yield f"{el.tag}/{el.get('id')}", tags, pos
                el.clear()


def osm_url(oid: str) -> str:
    """https://www.openstreetmap.org/<type>/<id> for 'node/12', 'way/5' (Overpass/GeoJSON) or osmium's 'n12'/'w5'/'r7'; '' if unknown."""
    m = re.fullmatch(r"(node|way|relation)/(\d+)", oid or "")
    if m:
        return f"https://www.openstreetmap.org/{m.group(1)}/{m.group(2)}"
    m = re.fullmatch(r"([nwr])(\d+)", oid or "")
    return f"https://www.openstreetmap.org/{ {'n': 'node', 'w': 'way', 'r': 'relation'}[m.group(1)] }/{m.group(2)}" if m else ""


def read_file(path: Path) -> Iterator[tuple[str, dict, Optional[tuple[float, float]]]]:
    name = path.name.lower()
    if name.endswith(".pbf"):
        raise ValueError("raw .pbf is not read directly; convert it first with osmium (see the docs/module header): "
                         "`osmium export file.pbf -o file.geojson`")
    if re.search(r"\.(geojson|json)(\.gz|\.bz2)?$", name):
        return iter_geojson(path)
    if re.search(r"\.(osm|xml)(\.gz|\.bz2)?$", name):
        return iter_osm_xml(path)
    raise ValueError("unsupported file type (use .geojson/.json or Overpass-style .osm/.xml, optionally .gz/.bz2)")


class OsmExtractSource:
    name = SRC_OSM_FILE
    needs_network = False

    def __init__(self, paths: list[str]):
        self.paths = [Path(p) for p in paths]

    def planned_requests(self, ctx: SourceContext) -> list[str]:
        return []                                   # reads local files only

    async def discover(self, ctx: SourceContext) -> list[RawHit]:
        ctx.notes.append(ATTRIBUTION)
        places = expand_locations(ctx.locations)
        boxes = [(p, req, bbox_for(p)) for p, req in places]
        hits: list[RawHit] = []
        for path in self.paths:
            if not path.is_file():
                ctx.error(self.name, str(path), "file_missing", f"{path} does not exist or is not a file")
                continue
            if path.stat().st_size > MAX_BYTES:
                ctx.error(self.name, path.name, "file_too_large", "file is larger than 2 GB; filter it first with osmium")
                continue
            try:
                it = read_file(path)
                seen = 0
                for oid, tags, pos in it:
                    seen += 1
                    office = (tags.get("office") or "").lower()
                    site = next((tags[k] for k in SITE_TAGS if tags.get(k)), None)
                    name = (tags.get("name") or "").strip()
                    if office not in OFFICES or not site or not name:
                        continue
                    where = self._match(tags, pos, boxes)
                    if where is None:
                        continue
                    place, requested = where
                    url = osm_url(oid)
                    addr = ", ".join(str(tags[k]) for k in ("addr:street", "addr:city", "addr:state") if tags.get(k)) or place
                    hits.append(RawHit(
                        website=str(site), name=name, location=addr, industry=office, source=SRC_OSM_FILE, source_url=url,
                        source_ref=f"{path.name}" + (f" ({oid})" if oid else ""),
                        evidence=("OSM tags: " + ", ".join(f"{k}={tags[k]}" for k in ("office", "description", "addr:city") if tags.get(k)))[:300],
                        niche_query="; ".join(ctx.niches), location_query=requested, discovered_at=now_iso()))
                if seen == 0:
                    ctx.error(self.name, path.name, "empty_file", "no tagged objects were found in the file")
            except (ValueError, OSError, ET.ParseError, json.JSONDecodeError, EOFError) as e:
                ctx.error(self.name, path.name, "unreadable_file", e)
        return hits

    @staticmethod
    def _match(tags: dict, pos, boxes) -> Optional[tuple[str, str]]:
        """Which requested place does this object belong to? None = drop. No requested locations: keep anything inside India's box."""
        if not boxes:
            return ("India", "") if (pos is None or in_box(pos[0], pos[1], INDIA_BBOX)) else None
        city = key(tags.get("addr:city") or "")
        for place, requested, box in boxes:
            if pos is not None and box is not None and in_box(pos[0], pos[1], box):
                return place, requested
            if city and city == key(place):                       # unknown place / no coordinates: trust the object's own addr:city
                return place, requested
        return None
