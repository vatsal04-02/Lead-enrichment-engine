"""Platform compatibility helpers."""
from __future__ import annotations

import asyncio
import sys
import warnings


def configure_event_loop() -> None:
    """psycopg's async API refuses Windows' default ProactorEventLoop, so use the selector loop there.

    Consequence (Windows only): the selector loop cannot start asyncio subprocesses, which Playwright needs, so the
    browser tier reports itself unavailable (it degrades safely; see BrowserRenderer.last_error)."""
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
