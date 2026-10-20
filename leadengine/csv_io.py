"""CSV ingestion (streaming, bounded memory) and export (streaming).

Row identity: `row_index` is the 1-based number of the data record in the uploaded file and is
never reused or renumbered; invalid and duplicate rows keep their number and are reported.
"""
from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional

from charset_normalizer import from_bytes

from .domain import InvalidDomain, normalize

csv.field_size_limit(1_000_000)

ALIASES = {
    "domain": ["domain", "company domain", "root domain", "primary domain"],
    "website": ["website", "website url", "url", "site", "web", "homepage", "company website", "web site"],
    "name": ["company", "company name", "name", "organization", "organisation", "account", "account name"],
    "location": ["location", "city", "hq", "headquarters", "address", "country", "state"],
    "industry": ["industry", "sector", "vertical"],
}
MAPPING_KEYS = tuple(ALIASES)


class CSVError(ValueError):
    pass


def detect_encoding(sample: bytes) -> str:
    if b"\x00" in sample[:4096] and not sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise CSVError("file looks binary (NUL bytes); expected a CSV text file")
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as e:
        if e.start >= len(sample) - 4:        # sample cut a multi-byte char in half
            return "utf-8"
    best = from_bytes(sample).best()
    return best.encoding if best else "cp1252"


def detect_delimiter(text_sample: str) -> str:
    """Pick the delimiter giving the most columns consistently across the first lines (quote-aware)."""
    lines = [ln for ln in text_sample.splitlines()[:8] if ln.strip()]
    best, best_score = ",", (0, 0)
    for d in (",", ";", "\t", "|"):
        try:
            widths = [len(r) for r in csv.reader(lines, delimiter=d)]
        except csv.Error:
            continue
        if not widths or widths[0] < 2:
            continue
        consistent = sum(1 for w in widths if w == widths[0])
        score = (consistent, widths[0])
        if score > best_score:
            best, best_score = d, score
    return best


def auto_map(headers: list[str]) -> dict[str, str]:
    norm = {re.sub(r"[\s_\-]+", " ", h.strip().lower()): h for h in headers}
    mapping: dict[str, str] = {}
    for key, aliases in ALIASES.items():
        for a in aliases:
            if a in norm and norm[a] not in mapping.values():
                mapping[key] = norm[a]
                break
    return mapping


def validate_mapping(mapping: dict[str, str], headers: list[str]) -> dict[str, str]:
    bad = [f"{k}->{v!r}" for k, v in mapping.items() if v not in headers]
    if bad:
        raise CSVError(f"mapped column(s) not in file header: {', '.join(bad)}")
    unknown = [k for k in mapping if k not in MAPPING_KEYS]
    if unknown:
        raise CSVError(f"unknown mapping key(s): {unknown}; valid: {list(MAPPING_KEYS)}")
    if "domain" not in mapping and "website" not in mapping:
        raise CSVError("no website/domain column found; provide a mapping, e.g. {'website': 'Company URL'}")
    return mapping


@dataclass
class ParsedRow:
    row_index: int
    original: dict            # header -> raw cell, untouched
    website: Optional[str] = None
    name: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None
    domain: Optional[str] = None
    error: Optional[str] = None


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        h = h.strip() or "column"
        n = seen.get(h, 0)
        seen[h] = n + 1
        out.append(h if n == 0 else f"{h}_{n + 1}")
    return out


class CSVReader:
    """Opens a CSV file path, detects encoding/delimiter, exposes headers and a row iterator."""

    def __init__(self, path: str, mapping: Optional[dict[str, str]] = None):
        self.path = path
        with open(path, "rb") as f:
            sample = f.read(65536)
        if not sample.strip():
            raise CSVError("file is empty")
        self.encoding = detect_encoding(sample)
        text = sample.decode(self.encoding, errors="replace")
        self.delimiter = detect_delimiter(text[:8192])
        with open(path, newline="", encoding=self.encoding, errors="replace") as f:
            try:
                raw_headers = next(csv.reader(f, delimiter=self.delimiter))
            except StopIteration:
                raise CSVError("file has no header row")
        self.headers = _dedupe_headers(raw_headers)
        if len(set(h.lower() for h in self.headers)) != len(self.headers):
            raise CSVError("header row contains duplicate column names")
        self.mapping = validate_mapping(mapping or auto_map(self.headers), self.headers)

    def rows(self) -> Iterator[ParsedRow]:
        m = self.mapping
        with open(self.path, newline="", encoding=self.encoding, errors="replace") as f:
            rd = csv.reader(f, delimiter=self.delimiter)
            next(rd, None)
            idx = 0
            while True:
                try:
                    rec = next(rd)
                except StopIteration:
                    return
                except csv.Error as e:
                    idx += 1
                    yield ParsedRow(idx, {}, error=f"unparseable CSV record: {e}")
                    continue
                idx += 1
                if not rec or all(not c.strip() for c in rec):
                    yield ParsedRow(idx, {h: "" for h in self.headers}, error="blank row")
                    continue
                if len(rec) > len(self.headers):
                    yield ParsedRow(idx, dict(zip(self.headers, rec)),
                                    error=f"row has {len(rec)} columns but header has {len(self.headers)}")
                    continue
                rec = rec + [""] * (len(self.headers) - len(rec))
                orig = dict(zip(self.headers, rec))
                get = lambda k: (orig.get(m[k], "").strip() or None) if k in m else None
                row = ParsedRow(idx, orig, website=get("website"), name=get("name"), location=get("location"),
                                industry=get("industry"))
                src = get("domain") or row.website
                if not src:
                    row.error = ("no website or domain value; name-only rows cannot be enriched "
                                 "(no company search provider is configured)")
                else:
                    try:
                        row.domain = normalize(src).key
                    except InvalidDomain as e:
                        row.error = f"invalid website/domain {src[:80]!r}: {e}"
                yield row


# ------------------------------------------------------------------ export
_LEAD_WS = re.compile(r"^[\s\x00-\x1f]*")


def spreadsheet_safe(v: Optional[str]) -> str:
    """Neutralise CSV/formula injection in untrusted text: if the first meaningful character could start a
    formula, prefix an apostrophe. Plain phone-like numbers (+1 512-555-0100) are left alone."""
    if not v:
        return ""
    body = v[_LEAD_WS.match(v).end():]
    if not body:
        return v
    first = body[0]
    if first in "=@" or v[0] in "\t\r\n" or (first in "+-" and not re.fullmatch(r"[+\-]?[\d\s().\-]+", body)):
        return "'" + v
    return v


def status_string(st: dict) -> str:
    if not st["present"]:
        return "missing"
    parts = ["present"]
    for k in ("source_attributed", "format_valid", "verified", "stale"):
        if st[k]:
            parts.append(k)
    return "+".join(parts)
