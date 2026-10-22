"""Search providers: a small interface (`SearchProvider.search`) so providers can be added without touching the engine.

What each provider needs (checked 2026-10; re-check before relying on it - these terms change):
  searxng  FREE, no key. A metasearch engine YOU run (docker, localhost) or an instance you are allowed to use. Needs `json` in `search.formats` of its
           settings.yml. It queries upstream engines from YOUR address: their terms/rate limits apply to your instance, so keep the rate low.
  brave    OPTIONAL and METERED: needs LE_BRAVE_API_KEY. Brave removed its free tier (Feb 2026) for ~USD 5/month of credit; a card is required and
           overage is billed. Its terms restrict storing/training on results - read them first. Not used unless you select it.
  static   FREE, offline: replays a JSON file of saved results (e.g. results you collected by hand, or a previous run's search_log). No network.
Never implemented, on purpose: scraping Google/Bing/DuckDuckGo result pages (their terms forbid it and they answer automation with challenges),
Google Programmable Search (closed to new customers), Bing Search API (retired Aug 2025).

All network providers share: timeouts, retries with exponential backoff + jitter (honouring Retry-After), a minimum interval between requests, an on-disk
query cache, and error kinds the engine logs and counts. API keys are read from the environment, never logged or written to any output."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urlsplit

import httpx


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str
    query: str
    rank: int                     # 1-based position within the page of results
    page: int = 1


class SearchError(Exception):
    """kind: rate_limited | quota | auth | timeout | network | http | bad_response | not_configured | format_disabled"""

    def __init__(self, kind: str, message: str, retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind, self.retryable, self.retry_after = kind, retryable, retry_after


class SearchProvider(Protocol):
    name: str
    needs_network: bool

    def describe(self) -> dict: ...
    async def search(self, query: str, *, page: int = 1, count: int = 10) -> list[SearchResult]: ...
    async def aclose(self) -> None: ...


def _clip(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


class HttpJsonClient:
    """GET -> JSON with timeouts, retries + backoff and a minimum interval between requests. `sleep` is injectable for tests."""

    def __init__(self, *, timeout: float = 15.0, retries: int = 2, backoff: float = 2.0, min_interval: float = 2.0, max_retry_after: float = 30.0,
                 sleep=asyncio.sleep, client: Optional[httpx.AsyncClient] = None, user_agent: str = ""):
        self.timeout, self.retries, self.backoff, self.min_interval, self.max_retry_after = timeout, retries, backoff, min_interval, max_retry_after
        self._sleep, self._last, self._lock = sleep, 0.0, asyncio.Lock()
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": user_agent or "leadengine-search/1.0"})
        self.requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._last = time.monotonic()

    async def get_json(self, url: str, params: dict, headers: Optional[dict] = None) -> dict:
        last: Optional[SearchError] = None
        for attempt in range(self.retries + 1):
            if attempt:
                delay = last.retry_after if last and last.retry_after is not None else self.backoff * (2 ** (attempt - 1)) * (0.75 + random.random() / 2)
                await self._sleep(min(delay, self.max_retry_after))
            await self._throttle()
            self.requests += 1
            try:
                r = await self._client.get(url, params=params, headers=headers or {})
            except httpx.TimeoutException:
                last = SearchError("timeout", f"timeout after {self.timeout:.0f}s", retryable=True)
                continue
            except httpx.HTTPError as e:
                last = SearchError("network", f"{type(e).__name__}", retryable=True)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    raise SearchError("bad_response", "the endpoint did not return JSON")
                if not isinstance(data, dict):
                    raise SearchError("bad_response", "unexpected JSON shape")
                return data
            ra = r.headers.get("retry-after", "")
            retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None
            if r.status_code == 429:
                last = SearchError("rate_limited", "HTTP 429 (rate limited)", retryable=True, retry_after=retry_after)
                continue
            if r.status_code in (401, 403):
                raise SearchError("auth", f"HTTP {r.status_code} (key missing/invalid, or the JSON format is disabled on this instance)")
            if r.status_code in (402,):
                raise SearchError("quota", "HTTP 402 (plan/credit exhausted)")
            if r.status_code >= 500:
                last = SearchError("http", f"HTTP {r.status_code}", retryable=True, retry_after=retry_after)
                continue
            raise SearchError("http", f"HTTP {r.status_code}")
        assert last is not None
        raise last


class SearxngProvider:
    name, needs_network = "searxng", True

    def __init__(self, base_url: str = "http://localhost:8080", *, engines: str = "", language: str = "en-IN", http: Optional[HttpJsonClient] = None):
        u = urlsplit(base_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            raise SearchError("not_configured", "LE_SEARXNG_URL must be http(s)://host[:port] without credentials")
        self.base = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"
        self.engines, self.language, self.http = engines, language, http or HttpJsonClient()

    def describe(self) -> dict:
        return {"provider": self.name, "endpoint": self.base, "api_key_required": False, "cost": "free (self-hosted)",
                "requirements": "a SearXNG instance with `json` in search.formats; its upstream engines' terms apply to your instance"}

    async def search(self, query: str, *, page: int = 1, count: int = 10) -> list[SearchResult]:
        params = {"q": query, "format": "json", "pageno": page, "language": self.language, "safesearch": 0}
        if self.engines:
            params["engines"] = self.engines
        try:
            data = await self.http.get_json(f"{self.base}/search", params)
        except SearchError as e:
            if e.kind == "auth":
                raise SearchError("format_disabled", "HTTP 403: enable `json` under search.formats in the instance's settings.yml (and check its limiter)")
            raise
        rows = data.get("results")
        if not isinstance(rows, list):
            raise SearchError("bad_response", "no `results` list in the SearXNG answer")
        out = []
        for i, r in enumerate(rows, 1):
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                out.append(SearchResult(_clip(r.get("title"), 300), r["url"][:2048], _clip(r.get("content"), 600), self.name, query, i, page))
        return out[:count] if count else out

    async def aclose(self) -> None:
        await self.http.aclose()


class BraveProvider:
    name, needs_network = "brave", True
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str = "", *, http: Optional[HttpJsonClient] = None, endpoint: str = ENDPOINT):
        if not api_key:
            raise SearchError("not_configured", "the brave provider needs LE_BRAVE_API_KEY (metered, card required - see providers.py)")
        self._key, self.endpoint, self.http = api_key, endpoint, http or HttpJsonClient()

    def describe(self) -> dict:
        return {"provider": self.name, "endpoint": self.endpoint, "api_key_required": True, "cost": "metered: ~USD 5 per 1,000 requests after the monthly credit",
                "requirements": "LE_BRAVE_API_KEY; read Brave's terms on storing results first"}

    async def search(self, query: str, *, page: int = 1, count: int = 10) -> list[SearchResult]:
        params = {"q": query, "count": min(max(count, 1), 20), "offset": max(page - 1, 0), "country": "IN", "search_lang": "en"}
        data = await self.http.get_json(self.endpoint, params, {"X-Subscription-Token": self._key, "Accept": "application/json"})
        rows = (data.get("web") or {}).get("results")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise SearchError("bad_response", "unexpected Brave answer")
        return [SearchResult(_clip(r.get("title"), 300), r["url"][:2048], _clip(r.get("description"), 600), self.name, query, i, page)
                for i, r in enumerate(rows, 1) if isinstance(r, dict) and isinstance(r.get("url"), str)]

    async def aclose(self) -> None:
        await self.http.aclose()


class StaticProvider:
    """Replays saved results. File: {"results": {"<query text>": [{"title","url","snippet"}, ...]}} (queries matched case-insensitively; page 1 only
    unless the value is {"1": [...], "2": [...]}), or a flat list of {"query","title","url","snippet"}. Offline: needs no network."""
    name, needs_network = "static", False

    def __init__(self, path: str):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.table: dict[str, dict[int, list[dict]]] = {}
        if isinstance(raw, dict):
            for q, v in (raw.get("results") or {}).items():
                self.table[q.casefold()] = {1: v} if isinstance(v, list) else {int(k): x for k, x in v.items()}
        elif isinstance(raw, list):
            for r in raw:
                self.table.setdefault(str(r.get("query", "")).casefold(), {1: []})[1].append(r)
        else:
            raise SearchError("bad_response", "unrecognised static results file")
        self.path = path

    def describe(self) -> dict:
        return {"provider": self.name, "endpoint": str(self.path), "api_key_required": False, "cost": "free (offline replay)", "requirements": "a JSON file"}

    async def search(self, query: str, *, page: int = 1, count: int = 10) -> list[SearchResult]:
        rows = self.table.get(query.casefold(), {}).get(page, [])
        return [SearchResult(_clip(r.get("title"), 300), str(r.get("url"))[:2048], _clip(r.get("snippet") or r.get("content"), 600), self.name, query, i, page)
                for i, r in enumerate(rows, 1) if r.get("url")][:count or None]

    async def aclose(self) -> None: ...


class QueryCache:
    """On-disk cache of search answers, one JSON file per (provider, query, page, count). A hit costs no request and no quota."""

    def __init__(self, directory: str | Path, ttl_seconds: float = 7 * 86400):
        self.dir, self.ttl = Path(directory), ttl_seconds

    def _path(self, provider: str, query: str, page: int, count: int) -> Path:
        return self.dir / (hashlib.sha256(f"{provider}\0{query}\0{page}\0{count}".encode()).hexdigest()[:40] + ".json")

    def get(self, provider: str, query: str, page: int, count: int) -> Optional[list[SearchResult]]:
        p = self._path(provider, query, page, count)
        try:
            if time.time() - p.stat().st_mtime > self.ttl:
                return None
            return [SearchResult(**r) for r in json.loads(p.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return None

    def put(self, provider: str, query: str, page: int, count: int, results: list[SearchResult]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(provider, query, page, count).with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(r) for r in results]), encoding="utf-8")
            os.replace(tmp, self._path(provider, query, page, count))
        except OSError:
            pass                                                    # a cache that cannot be written is only a missed optimisation


def make_provider(name: str, env=None, *, min_interval: float = 2.0, timeout: float = 15.0, retries: int = 2, user_agent: str = "",
                  static_path: str = "") -> SearchProvider:
    env = os.environ if env is None else env
    http = lambda: HttpJsonClient(min_interval=min_interval, timeout=timeout, retries=retries, user_agent=user_agent)
    if name == "searxng":
        return SearxngProvider(env.get("LE_SEARXNG_URL", "http://localhost:8080"), engines=env.get("LE_SEARXNG_ENGINES", ""), http=http())
    if name == "brave":
        return BraveProvider(env.get("LE_BRAVE_API_KEY", ""), http=http())
    if name == "static":
        if not static_path:
            raise SearchError("not_configured", "--static-results FILE is required for the static provider")
        return StaticProvider(static_path)
    raise SearchError("not_configured", f"unknown search provider {name!r} (searxng | brave | static)")
