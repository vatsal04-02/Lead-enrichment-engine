"""Wires settings, DB, fetcher, optional AI/browser tiers into one Enricher."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from typing import Optional

from .ai import AIExtractor, LLMProvider, make_provider
from .browser import BrowserRenderer
from .config import Settings, get_settings
from .audit import FetchAudit
from .db import Database
from .fetcher import Fetcher
from .pipeline import Enricher


class ConfigError(RuntimeError):
    pass


class Engine:
    def __init__(self, settings: Optional[Settings] = None, provider: Optional[LLMProvider] = None):
        self.settings = settings or get_settings()
        self._provider = provider
        self.db: Database = None  # type: ignore[assignment]
        self.fetcher: Fetcher = None  # type: ignore[assignment]
        self.browser: Optional[BrowserRenderer] = None
        self.enricher: Enricher = None  # type: ignore[assignment]
        self._pool: Optional[ProcessPoolExecutor] = None
        self.audit: Optional[FetchAudit] = None

    async def start(self, init_db: bool = True) -> "Engine":
        s = self.settings
        if "example.invalid" in s.user_agent and not s.host_overrides:
            raise ConfigError("LE_USER_AGENT is still the placeholder. Sites must be able to identify and contact you: "
                              'set e.g. LE_USER_AGENT="YourBot/1.0 (+https://your.site/bot; ops@your.site)"')
        self.db = await Database(s).open(init=init_db)
        self.audit = FetchAudit(self.db) if s.fetch_audit_log else None
        if self.audit:
            self.audit.start()
        self.fetcher = Fetcher(s, self.db, audit=self.audit)
        provider = self._provider or make_provider(s)
        ai = AIExtractor(s, provider) if provider else None
        self.browser = BrowserRenderer(s, self.fetcher) if s.browser_enabled else None
        self._pool = ProcessPoolExecutor(s.parse_workers) if s.parse_workers > 0 else None
        self.enricher = Enricher(s, self.db, self.fetcher, ai, self.browser, self._pool)
        return self

    async def stop(self) -> None:
        if self.browser:
            await self.browser.aclose()
        if self.fetcher:
            await self.fetcher.aclose()
        if getattr(self, "audit", None):
            await self.audit.aclose()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self.db:
            await self.db.close()

    async def __aenter__(self) -> "Engine":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()
