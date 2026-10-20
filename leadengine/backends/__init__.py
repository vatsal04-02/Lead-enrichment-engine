"""Optional alternative extraction backends. The default ('inhouse') never imports or starts anything here."""
from __future__ import annotations

from typing import Optional

from ..config import Settings
from .base import BackendConfigError, BackendUnavailable, ExtractionBackend, FieldOutcome, PageInput, PageOutcome, Usage  # noqa: F401


def make_llm_backend(settings: Settings) -> Optional["ExtractionBackend"]:
    """None for the default in-house engine. Raises BackendConfigError for any incomplete scrapegraph configuration."""
    if settings.extraction_backend == "inhouse":
        return None
    from .scrapegraph import ScrapeGraphBackend
    return ScrapeGraphBackend(settings)
