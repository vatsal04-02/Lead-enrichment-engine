"""Persistence for companies, field-level provenance and audit history."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg.types.json import Jsonb

from .domain import Identity
from .fields import FIELDS
from .scoring import score_company


async def upsert_company(conn, ident: Identity, name=None, location=None, industry=None) -> int:
    row = await (await conn.execute(
        """INSERT INTO companies (domain, website_url, input_name, input_location, input_industry)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (domain) DO UPDATE SET
               updated_at = now(),
               input_name = COALESCE(companies.input_name, EXCLUDED.input_name),
               input_location = COALESCE(companies.input_location, EXCLUDED.input_location),
               input_industry = COALESCE(companies.input_industry, EXCLUDED.input_industry)
           RETURNING id""",
        (ident.key, ident.start_url, name, location, industry))).fetchone()
    return row["id"]


async def load_fields(conn, company_id: int) -> dict[str, dict]:
    cur = await conn.execute("SELECT * FROM company_fields WHERE company_id=%s", (company_id,))
    return {r["field"]: r for r in await cur.fetchall()}


async def save_field(conn, company_id: int, field: str, *, status: str, value: Optional[str],
                     source_url: Optional[str], method: Optional[str], tier: Optional[str],
                     prior: Optional[float], verification_status: str, verification_method: Optional[str],
                     checks: dict, source_hash: Optional[str], alternates: list, refresh_after: datetime,
                     existing: Optional[dict], note: Optional[str] = None) -> None:
    if existing and existing["status"] == "found" and existing["value"] != value and status == "found":
        await conn.execute(
            """INSERT INTO company_field_history (company_id, field, old_value, new_value, old_source_url,
                   new_source_url, method) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (company_id, field, existing["value"], value, existing["source_url"], source_url, method))
    await conn.execute(
        """INSERT INTO company_fields (company_id, field, value, status, source_url, method, tier, method_prior,
               extracted_at, refresh_after, verification_status, verification_method, checks, source_hash,
               alternates, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id, field) DO UPDATE SET
               value=EXCLUDED.value, status=EXCLUDED.status, source_url=EXCLUDED.source_url,
               method=EXCLUDED.method, tier=EXCLUDED.tier, method_prior=EXCLUDED.method_prior,
               extracted_at=EXCLUDED.extracted_at, refresh_after=EXCLUDED.refresh_after,
               verification_status=EXCLUDED.verification_status,
               verification_method=EXCLUDED.verification_method, checks=EXCLUDED.checks,
               source_hash=EXCLUDED.source_hash, alternates=EXCLUDED.alternates, note=EXCLUDED.note""",
        (company_id, field, value, status, source_url, method, tier, prior, refresh_after,
         verification_status, verification_method, Jsonb(checks), source_hash, Jsonb(alternates), note))


async def defer_refresh(conn, company_id: int, field: str, refresh_after: datetime, note: str) -> None:
    """Keep an existing (stale) value but stop re-trying until refresh_after."""
    await conn.execute("UPDATE company_fields SET refresh_after=%s, note=%s WHERE company_id=%s AND field=%s",
                       (refresh_after, note, company_id, field))


def field_json(row: dict) -> dict:
    return {"value": row["value"], "status": row["status"], "source_url": row["source_url"],
            "method": row["method"], "tier": row["tier"], "method_prior": row["method_prior"],
            "extracted_at": row["extracted_at"].isoformat(), "refresh_after": row["refresh_after"].isoformat(),
            "verification_status": row["verification_status"],
            "verification_method": row["verification_method"], "checks": row["checks"],
            "alternates": row["alternates"], "note": row["note"]}


async def company_view(conn, domain: str, requested: Optional[list[str]] = None,
                       include_history: bool = False) -> Optional[dict]:
    comp = await (await conn.execute("SELECT * FROM companies WHERE domain=%s", (domain,))).fetchone()
    if not comp:
        return None
    rows = await load_fields(conn, comp["id"])
    req = requested or [f for f in FIELDS if f != "website"]
    score = score_company(rows, req)
    view = {
        "domain": comp["domain"], "website_url": comp["website_url"], "last_outcome": comp["last_outcome"],
        "last_enriched_at": comp["last_enriched_at"].isoformat() if comp["last_enriched_at"] else None,
        "completeness": score["completeness"], "quality": score["quality"],
        "verified_share": score["verified_share"],
        "fields": {f: {**score["fields"][f], **(field_json(rows[f]) if f in rows else {"value": None})}
                   for f in req},
    }
    if "website" in rows:
        view["website"] = field_json(rows["website"])
    if include_history:
        cur = await conn.execute(
            "SELECT field, old_value, new_value, old_source_url, new_source_url, method, changed_at "
            "FROM company_field_history WHERE company_id=%s ORDER BY changed_at DESC LIMIT 200", (comp["id"],))
        view["history"] = [{**r, "changed_at": r["changed_at"].isoformat()} for r in await cur.fetchall()]
    return view


async def existing_domains(conn, domains: list[str]) -> set[str]:
    if not domains:
        return set()
    cur = await conn.execute("SELECT domain FROM companies WHERE domain = ANY(%s)", (domains,))
    return {r["domain"] for r in await cur.fetchall()}
