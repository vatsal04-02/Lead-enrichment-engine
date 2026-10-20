"""Measured per-tier and overall metrics from enrichment_runs (the only source of truth)."""
from __future__ import annotations

from typing import Optional

TIER_ORDER = ["tier1_existing", "tier2_http", "tier3_targeted", "tier4_browser", "tier5_ai"]


async def collect(db, job_id: Optional[str] = None, since_run_id: int = 0) -> dict:
    where, args = "WHERE id > %s", [since_run_id]
    if job_id:
        where += " AND job_id = %s"
        args.append(job_id)
    async with db.connection() as c:
        overall = await (await c.execute(
            f"""SELECT count(*) runs,
                   count(*) FILTER (WHERE outcome='cached') cached,
                   count(*) FILTER (WHERE outcome='enriched') enriched,
                   count(*) FILTER (WHERE outcome='partial') partial,
                   count(*) FILTER (WHERE outcome='no_data') no_data,
                   count(*) FILTER (WHERE outcome='unreachable') unreachable,
                   count(*) FILTER (WHERE outcome='failed') failed,
                   coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),0) p50_ms,
                   coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),0) p95_ms,
                   coalesce(avg(http_requests),0) http_requests_avg, coalesce(avg(http_cache_hits),0) cache_hits_avg,
                   coalesce(avg(ai_calls),0) ai_calls_avg, coalesce(avg(retries),0) retries_avg,
                   coalesce(avg(bytes_downloaded),0) bytes_avg,
                   count(*) FILTER (WHERE browser_ops>0) browser_runs,
                   count(*) FILTER (WHERE ai_calls>0) ai_runs,
                   coalesce(sum(cost_usd_est),0) cost_usd_est,
                   coalesce(sum(http_requests),0) http_requests, coalesce(sum(ai_calls),0) ai_calls,
                   coalesce(sum(browser_ops),0) browser_ops, coalesce(sum(retries),0) retries
               FROM enrichment_runs {where}""", args)).fetchone()
        tiers = await (await c.execute(
            f"""SELECT t.key tier,
                   count(*) FILTER (WHERE (t.value->>'attempted')::bool) attempted,
                   count(*) FILTER (WHERE (t.value->>'attempted')::bool AND (t.value->>'fields_found')::int > 0) productive,
                   coalesce(sum((t.value->>'fields_found')::int),0) fields_found,
                   coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY (t.value->>'ms')::float)
                       FILTER (WHERE (t.value->>'attempted')::bool),0) p50_ms,
                   coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY (t.value->>'ms')::float)
                       FILTER (WHERE (t.value->>'attempted')::bool),0) p95_ms,
                   coalesce(sum((t.value->>'requests')::int),0) requests,
                   coalesce(sum((t.value->>'cost_usd')::float),0) cost_usd_est
               FROM enrichment_runs, jsonb_each(tiers) t {where} GROUP BY t.key""", args)).fetchall()
        llm_rows = await (await c.execute(
            f"SELECT cost->'llm' AS llm FROM enrichment_runs {where} AND cost ? 'llm' AND cost->'llm' <> 'null'::jsonb", args)).fetchall()
    runs = overall["runs"] or 0
    by = {t["tier"]: t for t in tiers}
    tier_out = {}
    for name in TIER_ORDER:
        t = by.get(name)
        if not t:
            continue
        att = t["attempted"]
        tier_out[name] = {
            "attempted": att, "success_rate": round(t["productive"] / att, 3) if att else None,
            "success_def": "share of attempts that yielded >=1 field not already found by earlier tiers",
            "fields_found": t["fields_found"], "latency_ms_p50": round(t["p50_ms"], 1),
            "latency_ms_p95": round(t["p95_ms"], 1), "requests": t["requests"],
            "cost_usd_estimated": round(t["cost_usd_est"], 6)}
    f = lambda k: float(overall[k])
    return {
        "runs": runs,
        "outcomes": {k: overall[k] for k in ("cached", "enriched", "partial", "no_data", "unreachable", "failed")},
        "latency_ms": {"p50": round(f("p50_ms"), 1), "p95": round(f("p95_ms"), 1)},
        "per_company": {"http_requests": round(f("http_requests_avg"), 3), "cache_hits": round(f("cache_hits_avg"), 3),
                        "ai_calls": round(f("ai_calls_avg"), 4), "retries": round(f("retries_avg"), 4),
                        "bytes_downloaded": round(f("bytes_avg"))},
        "browser_rendering_frequency": round(overall["browser_runs"] / runs, 4) if runs else 0,
        "ai_call_frequency": round(overall["ai_runs"] / runs, 4) if runs else 0,
        "retry_rate": round(overall["retries"] / overall["http_requests"], 4) if overall["http_requests"] else 0,
        "cost_usd_estimated": round(f("cost_usd_est"), 6),
        "tiers": tier_out,
        "llm": llm_usage([r["llm"] for r in llm_rows]),
        "note": "USD figures are estimates from configured unit prices (LE_AI_PRICE_*, LE_COST_PER_HTTP_REQUEST_USD, "
                "LE_BROWSER_COST_PER_SECOND_USD); infrastructure cost is not included unless configured.",
    }


def llm_usage(per_run: list[dict]) -> Optional[dict]:
    """Aggregate the optional extraction backend's per-run usage. None when no run used a model backend (the default).
    Local and external usage are reported separately; local inference is NOT claimed to be free of cost."""
    per_run = [r for r in per_run if isinstance(r, dict)]
    if not per_run:
        return None
    out: dict = {"runs": len(per_run), "backend": per_run[0].get("backend"), "provider": per_run[0].get("provider"),
                 "model": per_run[0].get("model"), "scope": per_run[0].get("scope"), "scope_note": per_run[0].get("scope_note")}
    skipped: dict = {}
    for r in per_run:
        for reason, n in (r.get("pages_skipped") or {}).items():
            skipped[reason] = skipped.get(reason, 0) + int(n)
    rejected: dict = {}                                            # run stores {"kind:field": reason}; report counts per reason
    for r in per_run:
        for reason in (r.get("rejected") or {}).values():
            rejected[str(reason)] = rejected.get(str(reason), 0) + 1
    out["pages_skipped"], out["rejected"] = skipped, rejected
    out["rejected_total"] = sum(rejected.values())
    for k in ("calls", "tokens_in", "tokens_out", "failures", "timeouts", "retries", "pages_sent", "pages_from_cache"):
        out[k] = sum(int(r.get(k) or 0) for r in per_run)
    out["latency_ms_total"] = round(sum(float(r.get("latency_ms") or 0) for r in per_run), 1)
    out["external_usd_estimated"] = round(sum(float(r.get("external_usd_estimated") or 0) for r in per_run), 6)
    out["worker_blocked_attempts"] = sum(len(r.get("worker_blocked_attempts") or []) for r in per_run)
    return out
