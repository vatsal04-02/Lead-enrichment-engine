"""Common interface for alternative, model-backed extraction backends.

The default backend ("inhouse") is the existing deterministic pipeline and is NOT routed through this interface at runtime;
`InHouseBackend` implements it only so both engines can be driven identically by tests and benchmarks.

Contract for every backend:
  * it receives page CONTENT that the engine's guarded fetcher already retrieved (robots.txt, SSRF checks, rate limits,
    budgets and the fetch audit log all happened before it sees anything); it is never given a URL to fetch;
  * it returns per-field outcomes with an explicit state, never a silent default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

STATES = ("extracted", "missing", "invalid", "conflict", "skipped_budget")


class BackendConfigError(RuntimeError):
    """The backend is mis-configured (missing provider/model/switch...). Raised at startup, never silently ignored."""


class BackendUnavailable(RuntimeError):
    """Dependency, interpreter, model server or model is not available. Raised instead of switching provider."""

    def __init__(self, message: str, kind: str = "model_unavailable"):
        super().__init__(message)
        self.kind = kind


@dataclass
class PageInput:
    url: str                    # final URL the content was fetched from (the only source URL ever attributed)
    kind: str                   # home | about | contact | ...
    html: str                   # raw HTML as fetched (or browser-rendered) by the engine
    content_hash: str = ""


@dataclass
class FieldOutcome:
    field: str
    state: str                  # one of STATES
    value: Optional[str] = None
    source_url: Optional[str] = None
    method: Optional[str] = None
    evidence: str = ""          # short snippet of the PAGE around the value (never model text)
    reason: str = ""            # why missing/invalid/skipped
    alternates: list = field(default_factory=list)


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    failures: int = 0
    timeouts: int = 0
    retries: int = 0

    def add(self, o: "Usage") -> None:
        for k in ("calls", "tokens_in", "tokens_out", "latency_ms", "failures", "timeouts", "retries"):
            setattr(self, k, getattr(self, k) + getattr(o, k))


@dataclass
class PageOutcome:
    fields: dict[str, FieldOutcome] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    error_kind: str = ""        # "" | model_unavailable | model_timeout | model_error | worker_crash | call_budget | invalid_response
    error: str = ""
    digest_chars: int = 0
    rejected: dict = field(default_factory=dict)   # field -> reason for model values dropped as unsupported
    worker_blocked: list = field(default_factory=list)   # network/process attempts the worker's guards refused (should be [])


class ExtractionBackend(Protocol):
    name: str
    provider: str
    model: str
    scope: str                  # "local" | "external" | "none"

    async def preflight(self) -> None: ...
    async def extract_page(self, page: PageInput, fields: list[str], max_calls: Optional[int] = None) -> PageOutcome: ...
    async def aclose(self) -> None: ...
