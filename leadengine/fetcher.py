"""Async HTTP fetching: pooled client, caching, robots.txt, rate limits, retries.

Everything that touches the network on behalf of extraction goes through Fetcher.fetch(),
so SSRF checks, size limits, per-domain politeness, caching and accounting live in one place.
"""
from __future__ import annotations

import asyncio
import contextvars
import email.utils
import hashlib
import random
import re
import ssl
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from bs4 import UnicodeDammit

from .budget import Budget, RunMetrics
from .config import Settings
from .db import Database
from .security import BlockedDestination, Resolver, build_http_client, sanitize_for_log, validate_url

HTML_TYPES = ("text/html", "application/xhtml+xml")
# Extra response types a caller explicitly opts into for ONE fetch (e.g. discovery reading an open-data JSON API). Everything else
# in the guarded path (SSRF checks, robots, size caps, challenge detection, audit) is identical. Task-local, so it never leaks.
_EXTRA_TYPES: contextvars.ContextVar[tuple] = contextvars.ContextVar("fetch_extra_types", default=())
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Strong, page-level anti-bot interstitial phrases only. A reCAPTCHA widget on a normal contact form is NOT a challenge.
CHALLENGE_MARKERS = ("just a moment...", "/cdn-cgi/challenge-platform", "attention required! | cloudflare",
                     "checking your browser before accessing", "px-captcha", "captcha-delivery.com",
                     "please verify you are a human", "enable javascript and cookies to continue")


def looks_like_challenge(headers, raw_len: int, text: str) -> bool:
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    if raw_len > 100_000:
        return False
    low = text.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""
    content_type: str = ""
    content_hash: str = ""
    from_cache: bool = False
    bytes: int = 0
    elapsed_ms: float = 0.0
    error_kind: str = ""     # "" | blocked | robots | budget | timeout | network | http | too_large | content_type | rate_limited
    error: str = ""
    retries: int = 0
    retry_after: Optional[float] = None

    @property
    def ok(self) -> bool:
        return not self.error_kind and self.status == 200

    @property
    def transient(self) -> bool:
        return self.error_kind in ("timeout", "network", "rate_limited") or (
            self.error_kind == "http" and self.status in RETRYABLE_STATUS)


class DomainLimiter:
    """Per-host concurrency cap + minimum spacing between request starts + 429 cool-down."""

    class _State:
        __slots__ = ("sem", "next_at", "cooldown_until", "lock", "waiters", "min_delay")

        def __init__(self, n: int):
            self.sem = asyncio.Semaphore(n)
            self.next_at = 0.0
            self.cooldown_until = 0.0
            self.lock = asyncio.Lock()
            self.waiters = 0
            self.min_delay = 0.0     # raised by robots.txt Crawl-delay

    def __init__(self, settings: Settings):
        self.s = settings
        self.states: dict[str, "DomainLimiter._State"] = {}

    def _state(self, host: str) -> "DomainLimiter._State":
        st = self.states.get(host)
        if st is None:
            if len(self.states) >= self.s.max_tracked_domains:
                self._evict()
            st = self.states[host] = self._State(self.s.per_domain_concurrency)
        return st

    def _evict(self) -> None:
        now = time.monotonic()
        for h in [h for h, st in self.states.items()
                  if st.waiters == 0 and st.next_at < now and st.cooldown_until < now
                  and not st.sem.locked()][: max(1, len(self.states) // 2)]:
            del self.states[h]

    def set_min_delay(self, host: str, seconds: float) -> None:
        self._state(host).min_delay = seconds

    def cooldown(self, host: str, seconds: float) -> None:
        st = self._state(host)
        st.cooldown_until = max(st.cooldown_until, time.monotonic() + seconds)

    def slot(self, host: str):
        return _Slot(self, host)


class _Slot:
    def __init__(self, lim: DomainLimiter, host: str):
        self.lim, self.host = lim, host

    async def __aenter__(self):
        st = self.lim._state(self.host)
        st.waiters += 1
        try:
            await st.sem.acquire()
        finally:
            st.waiters -= 1
        try:
            async with st.lock:
                now = time.monotonic()
                start = max(now, st.next_at, st.cooldown_until)
                st.next_at = start + max(self.lim.s.per_domain_min_delay, st.min_delay)
            if start > now:
                await asyncio.sleep(start - now)
        except BaseException:
            st.sem.release()
            raise
        return self

    async def __aexit__(self, *exc):
        self.lim._state(self.host).sem.release()


def _parse_cache_control(value: str) -> dict:
    out = {}
    for part in (value or "").lower().split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip().strip('"')
    return out


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _tls_kind(exc: BaseException) -> str:
    """'tls_cert' for certificate validation failures (never retried or downgraded), 'tls' for other handshake
    failures (e.g. a plain-HTTP server on port 443), '' if the error is not TLS-related."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError):
            return "tls_cert"
        if isinstance(exc, ssl.SSLError):
            return "tls"
        exc = exc.__cause__ or exc.__context__
    return ""


def decode_body(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        try:
            return raw.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    return UnicodeDammit(raw[:2_000_000], is_html=True).unicode_markup or raw.decode("utf-8", "replace")


class Fetcher:
    def __init__(self, settings: Settings, db: Optional[Database] = None,
                 client: Optional[httpx.AsyncClient] = None, resolver: Optional[Resolver] = None, audit=None):
        self.s = settings
        self.db = db
        self.audit = audit
        if client is None:
            client, resolver = build_http_client(settings)
        self.client = client
        self.resolver = resolver or Resolver(settings)
        self.limiter = DomainLimiter(settings)
        self.global_sem = asyncio.Semaphore(settings.http_max_connections)
        self._robots: dict[str, tuple[float, Optional[RobotFileParser], str]] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, asyncio.Future] = {}   # single-flight per URL

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---------- public ----------
    async def fetch(self, url: str, run: Optional[RunMetrics] = None, budget: Optional[Budget] = None,
                    *, use_robots: bool = True, ttl_override: Optional[int] = None,
                    accept_types: tuple = ()) -> FetchResult:
        """Fetch one page. Concurrent calls for the same URL share one network fetch.
        `accept_types`: additional exact content-types to accept for this call (default: HTML and plain text only)."""
        if accept_types:
            token = _EXTRA_TYPES.set(tuple(x.lower() for x in accept_types))
            try:
                return await self.fetch(url, run, budget, use_robots=use_robots, ttl_override=ttl_override)
            finally:
                _EXTRA_TYPES.reset(token)
        key = url
        fut = self._inflight.get(key)
        if fut is not None:
            res = await asyncio.shield(fut)
            if run is not None:
                run.http_cache_hits += 1
            return res
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            res = await self._fetch_uncoalesced(url, run, budget, use_robots, ttl_override)
            if self.audit is not None:
                await self.audit.record(job_id=getattr(run, "job_id", None), kind="page" if use_robots else "robots",
                                        url=url, res=res)
            fut.set_result(res)
            return res
        except Exception as e:      # never leave waiters hanging; surface as a failed fetch
            res = FetchResult(url=url, error_kind="network", error=sanitize_for_log(f"{type(e).__name__}: {e}"))
            fut.set_result(res)
            return res
        finally:
            if not fut.done():      # cancelled: release waiters with a transient failure
                fut.set_result(FetchResult(url=url, error_kind="network", error="cancelled"))
            self._inflight.pop(key, None)

    async def fetch_subresource(self, url: str, run: Optional[RunMetrics] = None,
                                max_bytes: int = 2_000_000) -> tuple[int, dict, bytes]:
        """Uncached GET for browser sub-requests (JS/XHR/HTML). (0, {}, b"") means refused/failed.

        Same SSRF guard, redirect re-validation and per-domain limits as page fetches; robots.txt is
        not re-checked (the page that loads these resources was already allowed)."""
        current = url
        for _ in range(self.s.max_redirects + 1):
            try:
                cu = validate_url(current, self.s)
                async with self.global_sem, self.limiter.slot(cu.host):
                    if run is not None:
                        run.http_requests += 1
                        run.cost_http_usd += self.s.cost_per_http_request_usd
                    async with asyncio.timeout(self.s.total_timeout), self.client.stream("GET", current) as resp:
                        loc = resp.headers.get("location")
                        if resp.status_code in (301, 302, 303, 307, 308) and loc:
                            current = urljoin(current, loc)
                            continue
                        body, total = [], 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                return 0, {}, b""
                            body.append(chunk)
                        if run is not None:
                            run.bytes_downloaded += total
                        hdrs = {"content-type": resp.headers.get("content-type", "application/octet-stream")}
                        if self.audit is not None:
                            await self.audit.record(job_id=getattr(run, "job_id", None), kind="subresource", url=current,
                                                    res=FetchResult(url=current, status=resp.status_code, bytes=total))
                        return resp.status_code, hdrs, b"".join(body)
            except (BlockedDestination, httpx.HTTPError, TimeoutError):
                return 0, {}, b""
        return 0, {}, b""

    # ---------- internals ----------
    async def _fetch_uncoalesced(self, url, run, budget, use_robots, ttl_override) -> FetchResult:
        t0 = time.monotonic()
        try:
            cu = validate_url(url, self.s)
        except BlockedDestination as e:
            return FetchResult(url=url, error_kind="blocked", error=sanitize_for_log(str(e)))

        cached = await self._cache_get(url)
        now = datetime.now(timezone.utc)
        if cached and cached["expires_at"] > now:
            if run is not None:
                run.http_cache_hits += 1
            return self._from_cache(url, cached, t0)

        if use_robots and self.s.respect_robots:
            allowed, kind, why = await self._robots_allowed(cu.scheme, cu.host, cu.port, url, run, budget)
            if not allowed:
                return FetchResult(url=url, error_kind=kind, error=why,
                                   elapsed_ms=(time.monotonic() - t0) * 1000)

        if budget is not None and use_robots and not budget.allows("pages"):
            return FetchResult(url=url, error_kind="budget", error="page budget exhausted")
        if run is not None and use_robots:        # robots.txt itself does not count as a page
            host = cu.host
            if run.pages_by_host.get(host, 0) >= self.s.max_pages_per_domain:
                return FetchResult(url=url, error_kind="budget", error="per-domain page limit reached")
            run.pages_by_host[host] = run.pages_by_host.get(host, 0) + 1
        if budget is not None and use_robots:
            budget.charge("pages")

        res = await self._network_fetch(url, cached, run, ttl_override)
        res.elapsed_ms = (time.monotonic() - t0) * 1000
        return res

    def _from_cache(self, url, row, t0) -> FetchResult:
        body = zlib.decompress(row["body"]) if row["body"] else b""
        return FetchResult(url=url, final_url=row["final_url"], status=row["status"],
                           text=decode_body(body, row["content_type"] or ""),
                           content_type=row["content_type"] or "", content_hash=row["body_hash"] or "",
                           from_cache=True, bytes=0, elapsed_ms=(time.monotonic() - t0) * 1000)

    async def _network_fetch(self, url, cached, run, ttl_override) -> FetchResult:
        attempt = 0
        res = FetchResult(url=url)
        while True:
            res = await self._one_attempt(url, cached, run, ttl_override)
            res.retries = attempt
            if not res.transient or attempt >= self.s.http_max_retries:
                return res
            delay = res.retry_after
            if delay is None:
                delay = min(self.s.backoff_max, self.s.backoff_base * (2 ** attempt))
                delay = random.uniform(delay / 2, delay)     # equal jitter
            elif delay > self.s.max_retry_after:
                res.error_kind = "rate_limited"
                res.error = f"Retry-After {delay:.0f}s exceeds limit"
                return res
            attempt += 1
            if run is not None:
                run.retries += 1
            await asyncio.sleep(delay)

    async def _one_attempt(self, url, cached, run, ttl_override) -> FetchResult:
        current = url
        cond = {}
        if cached and cached.get("etag"):
            cond["If-None-Match"] = cached["etag"]
        if cached and cached.get("last_modified"):
            cond["If-Modified-Since"] = cached["last_modified"]
        for _hop in range(self.s.max_redirects + 1):
            try:
                cu = validate_url(current, self.s)
            except BlockedDestination as e:
                return FetchResult(url=url, final_url=current, error_kind="blocked",
                                   error=sanitize_for_log(str(e)))
            if _hop > 0 and self.s.respect_robots:
                ok, kind, why = await self._robots_allowed(cu.scheme, cu.host, cu.port, current, run, None)
                if not ok:
                    return FetchResult(url=url, final_url=current, error_kind=kind, error=why)
            try:
                if self.s.egress_proxy:      # proxy connects, so we can only pre-check what the name resolves to
                    await self._precheck_via_proxy(cu)
                async with self.global_sem, self.limiter.slot(cu.host):
                    if run is not None:
                        run.http_requests += 1
                        run.cost_http_usd += self.s.cost_per_http_request_usd
                    # read_timeout resets on every received chunk, so a slow-drip server could hold a connection
                    # forever; total_timeout is a hard deadline for the whole request (headers + body).
                    async with asyncio.timeout(self.s.total_timeout), \
                            self.client.stream("GET", current, headers=cond if _hop == 0 else {}) as resp:
                        loc = resp.headers.get("location")
                        if resp.status_code in (301, 302, 303, 307, 308) and loc:
                            current = urljoin(current, loc)
                            continue
                        return await self._handle_response(url, current, resp, cached, run, ttl_override, cu.host)
            except (httpx.TimeoutException, TimeoutError) as e:
                return FetchResult(url=url, final_url=current, error_kind="timeout",
                                   error=type(e).__name__)
            except httpx.HTTPError as e:
                if isinstance(e.__cause__, BlockedDestination):
                    return FetchResult(url=url, final_url=current, error_kind="blocked",
                                       error=sanitize_for_log(str(e.__cause__)))
                return FetchResult(url=url, final_url=current, error_kind=_tls_kind(e) or "network",
                                   error=sanitize_for_log(f"{type(e).__name__}: {e}"))
            except BlockedDestination as e:
                return FetchResult(url=url, final_url=current, error_kind="blocked",
                                   error=sanitize_for_log(str(e)))
        return FetchResult(url=url, error_kind="redirect_loop", error=f"more than {self.s.max_redirects} redirects")

    async def _precheck_via_proxy(self, cu) -> None:
        try:
            await self.resolver.resolve_public(cu.host, cu.port)
        except BlockedDestination:
            raise
        except Exception:
            pass        # local DNS may be unavailable when a proxy resolves names; only definite hits block

    async def _handle_response(self, url, current, resp, cached, run, ttl_override, host) -> FetchResult:
        status = resp.status_code
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        cc = _parse_cache_control(resp.headers.get("cache-control", ""))
        if (resp.headers.get("cf-mitigated") or "").lower() == "challenge":     # never retry or work around
            return FetchResult(url=url, final_url=current, status=status, error_kind="bot_challenge",
                               error="site served an anti-bot challenge; not attempting to bypass")

        if status == 304 and cached:
            await self._cache_touch(url, self._expiry(cc, resp.headers, ttl_override))
            body = zlib.decompress(cached["body"]) if cached["body"] else b""
            return FetchResult(url=url, final_url=cached["final_url"], status=200,
                               text=decode_body(body, cached["content_type"] or ""),
                               content_type=cached["content_type"] or "",
                               content_hash=cached["body_hash"] or "", from_cache=True)

        if status in RETRYABLE_STATUS:
            ra = _retry_after_seconds(resp.headers.get("retry-after"))
            if status in (429, 503) and ra:
                self.limiter.cooldown(host, min(ra, self.s.max_retry_after))
            r = FetchResult(url=url, final_url=current, status=status, error_kind="http",
                            error=f"HTTP {status}")
            r.retry_after = ra
            return r
        if status >= 400:
            return FetchResult(url=url, final_url=current, status=status, error_kind="http",
                               error=f"HTTP {status}")
        if status != 200:
            return FetchResult(url=url, final_url=current, status=status, error_kind="http",
                               error=f"unexpected HTTP {status}")

        if not (ctype in HTML_TYPES or ctype == "text/plain" or ctype == "" or ctype in _EXTRA_TYPES.get()):
            return FetchResult(url=url, final_url=current, status=status, content_type=ctype,
                               error_kind="content_type", error=f"unsupported content-type {ctype}")
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > self.s.max_response_bytes:
            return FetchResult(url=url, final_url=current, status=status, error_kind="too_large",
                               error=f"content-length {clen} exceeds limit")
        chunks, total = [], 0
        async for chunk in resp.aiter_bytes():     # decoded bytes: bounds gzip bombs too
            total += len(chunk)
            if total > self.s.max_response_bytes:
                return FetchResult(url=url, final_url=current, status=status, error_kind="too_large",
                                   error="response exceeds size limit")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if run is not None:
            run.bytes_downloaded += total
            run.pages_fetched += 1
        digest = hashlib.sha256(raw).hexdigest()
        decoded = decode_body(raw, resp.headers.get("content-type", ""))
        if looks_like_challenge(resp.headers, total, decoded):
            return FetchResult(url=url, final_url=current, status=status, error_kind="bot_challenge",
                               error="site served an anti-bot challenge; not attempting to bypass")
        res = FetchResult(url=url, final_url=current, status=200, text=decoded,
                          content_type=ctype, content_hash=digest, bytes=total)
        if "no-store" not in cc and "private" not in cc:
            await self._cache_put(url, res, raw, resp.headers.get("etag"), resp.headers.get("last-modified"),
                                  self._expiry(cc, resp.headers, ttl_override))
        return res

    def _expiry(self, cc: dict, headers, ttl_override: Optional[int]) -> datetime:
        now = datetime.now(timezone.utc)
        if ttl_override is not None:
            return now + timedelta(seconds=ttl_override)
        if "no-cache" in cc:
            return now
        if "max-age" in cc and cc["max-age"].isdigit():
            return now + timedelta(seconds=int(cc["max-age"]))
        exp = headers.get("expires")
        if exp:
            try:
                return max(now, email.utils.parsedate_to_datetime(exp))
            except (TypeError, ValueError):
                pass
        return now + timedelta(seconds=self.s.http_cache_default_ttl)

    # ---------- cache ----------
    async def _cache_get(self, url):
        if not self.db:
            return None
        async with self.db.connection() as c:
            return await (await c.execute("SELECT * FROM http_cache WHERE url=%s", (url,))).fetchone()

    async def _cache_touch(self, url, expires):
        if self.db:
            async with self.db.connection() as c:
                await c.execute("UPDATE http_cache SET expires_at=%s, fetched_at=now() WHERE url=%s", (expires, url))

    async def _cache_put(self, url, res: FetchResult, raw: bytes, etag, last_modified, expires):
        if not self.db or len(raw) > self.s.http_cache_max_body_bytes:
            return
        async with self.db.connection() as c:
            await c.execute(
                """INSERT INTO http_cache (url, final_url, status, content_type, etag, last_modified,
                       fetched_at, expires_at, body, body_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,now(),%s,%s,%s)
                   ON CONFLICT (url) DO UPDATE SET final_url=EXCLUDED.final_url, status=EXCLUDED.status,
                       content_type=EXCLUDED.content_type, etag=EXCLUDED.etag,
                       last_modified=EXCLUDED.last_modified, fetched_at=now(),
                       expires_at=EXCLUDED.expires_at, body=EXCLUDED.body, body_hash=EXCLUDED.body_hash""",
                (url, res.final_url, res.status, res.content_type, etag, last_modified, expires,
                 zlib.compress(raw, 3), res.content_hash))

    # ---------- robots.txt ----------
    async def _robots_allowed(self, scheme, host, port, url, run, budget) -> tuple[bool, str, str]:
        origin = f"{scheme}://{host}" + (f":{port}" if port not in (80, 443) else "")
        entry = self._robots.get(origin)
        if entry is None or entry[0] < time.monotonic():
            lock = self._robots_locks.setdefault(origin, asyncio.Lock())
            async with lock:
                entry = self._robots.get(origin)
                if entry is None or entry[0] < time.monotonic():
                    entry = await self._load_robots(origin, run)
                    if len(self._robots) > 20000:
                        self._robots.clear()
                    self._robots[origin] = entry
                    if entry[2] == "rules" and entry[3]:
                        self.limiter.set_min_delay(host, float(entry[3]))
            self._robots_locks.pop(origin, None)
        _, parser, mode, delay = entry
        if mode == "disallow_all":     # RFC 9309: unreachable robots.txt => assume disallowed, but retryable
            return False, "network", "robots.txt unreachable (5xx/timeout) — retry later"
        if mode == "forbidden":        # 401/403: access control on robots.txt itself — we do not push past it
            return False, "robots", "robots.txt access denied (HTTP 401/403) — treated as disallow"
        if mode == "blocked":
            return False, "blocked", "robots.txt destination blocked"
        if mode in ("tls", "tls_cert"):
            return False, mode, f"TLS failure while fetching robots.txt ({mode})"
        if delay and delay > self.s.max_crawl_delay:
            return False, "robots", f"robots.txt Crawl-delay {delay}s exceeds LE_MAX_CRAWL_DELAY={self.s.max_crawl_delay}s"
        if parser is None or parser.can_fetch(self.s.user_agent, url):
            return True, "", ""
        return False, "robots", "disallowed by robots.txt"

    async def robots_verdict(self, url: str) -> tuple[bool, str, str]:
        """Would fetching `url` be allowed by robots.txt? -> (allowed, error kind, reason). Fetches robots.txt only (never the URL itself);
        nothing is cached beyond the normal robots cache. Used by the discovery `check-sources` preflight."""
        try:
            cu = validate_url(url, self.s)
        except BlockedDestination as e:
            return False, "blocked", sanitize_for_log(str(e))
        return await self._robots_allowed(cu.scheme, cu.host, cu.port, url, None, None)

    async def _load_robots(self, origin: str, run):
        ttl = 3600.0
        res = await self.fetch(origin + "/robots.txt", run, None, use_robots=False, ttl_override=86400)
        horizon = time.monotonic() + ttl
        if res.ok and res.status == 200:
            p = RobotFileParser()
            p.parse(res.text.splitlines())
            return horizon, p, "rules", p.crawl_delay(self.s.user_agent)
        if res.error_kind == "http" and res.status in (401, 403):
            return horizon, None, "forbidden", None
        if res.error_kind == "http" and 400 <= res.status < 500:
            return horizon, None, "allow_all", None          # RFC 9309: other 4xx => no restrictions
        if res.error_kind == "blocked":
            return horizon, None, "blocked", None
        if res.error_kind in ("tls", "tls_cert"):
            return horizon, None, res.error_kind, None
        if res.error_kind in ("content_type", "too_large"):
            return horizon, None, "allow_all", None
        return time.monotonic() + 120, None, "disallow_all", None   # 5xx/timeouts: be conservative, retry soon
