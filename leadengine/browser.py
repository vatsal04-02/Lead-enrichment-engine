"""Tier 4: Playwright rendering, only when plain HTTP returned a JS shell.

The browser never touches the network itself: every request is intercepted and fulfilled by
the guarded Fetcher, so SSRF protection, size limits and per-domain rate limits still apply.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional

from .budget import Budget, RunMetrics
from .config import Settings
from .fetcher import Fetcher

MAX_SUBREQUESTS = 60
MAX_SUBREQUEST_BYTES = 5_000_000


@dataclass
class RenderResult:
    html: str = ""
    error: str = ""
    seconds: float = 0.0


class BrowserRenderer:
    def __init__(self, settings: Settings, fetcher: Fetcher):
        self.s = settings
        self.fetcher = fetcher
        self.sem = asyncio.Semaphore(settings.browser_concurrency)
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.available: Optional[bool] = None

    async def _ensure(self) -> bool:
        if self._browser:
            return True
        async with self._lock:
            if self._browser:
                return True
            try:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                args = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
                self._browser = await self._pw.chromium.launch(headless=True, args=args)
                self.available = True
            except Exception as e:  # not installed / no browser binary
                self.available = False
                self.last_error = f"{type(e).__name__}: {e}"[:200]
            return bool(self._browser)

    async def aclose(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def render(self, url: str, run: Optional[RunMetrics], budget: Optional[Budget]) -> RenderResult:
        if budget is not None and not budget.allows("browser_ops"):
            return RenderResult(error="budget")
        if self.available is False or not await self._ensure():
            return RenderResult(error="browser_unavailable")
        async with self.sem:
            t0 = time.monotonic()
            if budget is not None:
                budget.charge("browser_ops")
            if run is not None:
                run.browser_ops += 1
            ctx = await self._browser.new_context(user_agent=self.s.user_agent, service_workers="block",
                                                  java_script_enabled=True)
            count = {"n": 0, "bytes": 0}

            async def handler(route):
                req = route.request
                if req.method != "GET" or req.resource_type in ("image", "media", "font", "stylesheet", "websocket", "eventsource", "other") \
                        or count["n"] >= MAX_SUBREQUESTS or count["bytes"] >= MAX_SUBREQUEST_BYTES:
                    return await route.abort()
                count["n"] += 1
                status, headers, body = await self.fetcher.fetch_subresource(req.url, run)
                if status == 0:
                    return await route.abort()
                count["bytes"] += len(body)
                await route.fulfill(status=status, headers=headers, body=body)

            try:
                await ctx.route("**/*", handler)
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.s.browser_timeout * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(5000, self.s.browser_timeout * 500))
                except Exception:
                    pass
                html = await page.content()
                return RenderResult(html=html, seconds=time.monotonic() - t0)
            except Exception as e:
                return RenderResult(error=f"{type(e).__name__}: {e}"[:200], seconds=time.monotonic() - t0)
            finally:
                secs = time.monotonic() - t0
                if run is not None:
                    run.browser_seconds += secs
                    run.cost_browser_usd += secs * self.s.browser_cost_per_second_usd
                await ctx.close()
