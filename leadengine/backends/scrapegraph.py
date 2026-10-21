"""ScrapeGraphAI adapter (optional, off by default).

Integration boundary - what ScrapeGraphAI is and is NOT allowed to do here:
  * It never fetches anything. The engine's guarded Fetcher (SSRF checks, robots.txt, rate limits, budgets, audit log)
    retrieves each page; the adapter hands the library a compacted copy of that page as HTML *content*.
  * It runs in a separate worker process (own interpreter, so Python >= 3.12 and its ~400 MB of dependencies stay out of
    the engine) whose sockets are restricted to the configured model endpoint and which cannot spawn processes
    (no browser). Telemetry is off and its tokenizer download is replaced by an offline estimator.
  * Every value it returns is checked against the page (leadengine.backends.normalize); unsupported values are dropped.
Hosted ScrapeGraphAI services, its stealth browser loader, proxy rotation and web search are never used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ..config import Settings
from ..security import sanitize_for_log
from .base import BackendConfigError, BackendUnavailable, PageInput, PageOutcome, Usage
from .normalize import PROMPT_VERSION, build_prompt, make_digest, normalize_page

WORKER_PATH = Path(__file__).with_name("sg_worker.py")
PROVIDERS = ("ollama", "openai", "anthropic")
DEFAULT_ENDPOINT = {"ollama": ("http", "127.0.0.1", 11434), "openai": ("https", "api.openai.com", 443),
                    "anthropic": ("https", "api.anthropic.com", 443)}
DEFAULT_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
PASS_ENV = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL", "LD_LIBRARY_PATH"}


class ResolvedConfig:
    """Validated, explicit configuration. There are no defaults for provider or model."""

    def __init__(self, s: Settings):
        if s.extraction_backend != "scrapegraph":
            raise BackendConfigError("extraction backend is not 'scrapegraph'")
        if not s.scrapegraph_enabled:
            raise BackendConfigError("LE_EXTRACTION_BACKEND=scrapegraph also requires LE_SCRAPEGRAPH_ENABLED=true "
                                     "(AI-assisted extraction stays off until you switch it on explicitly)")
        prov, model = s.scrapegraph_provider.strip().lower(), s.scrapegraph_model.strip()
        if prov not in PROVIDERS:
            raise BackendConfigError(f"LE_SCRAPEGRAPH_PROVIDER must be one of {PROVIDERS} (got {prov!r}); there is no default "
                                     "and no fallback to another provider")
        if not model:
            raise BackendConfigError("LE_SCRAPEGRAPH_MODEL is required (no default model, no silent substitution)")
        if model.lower().startswith("scrapegraphai/"):
            raise BackendConfigError("hosted ScrapeGraphAI models ('scrapegraphai/...') are not supported or allowed")
        scheme0, host0, port0 = DEFAULT_ENDPOINT[prov]
        url = s.scrapegraph_base_url or f"{scheme0}://{host0}:{port0}"
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            raise BackendConfigError("LE_SCRAPEGRAPH_BASE_URL must be http(s)://host[:port] without credentials")
        host = u.hostname.lower()
        port = u.port or (443 if u.scheme == "https" else 80)
        if host not in LOOPBACK and u.scheme != "https":
            raise BackendConfigError("a non-loopback model endpoint must use https:// (page text and keys would travel in clear text)")
        self.provider, self.model, self.base_url, self.host, self.port = prov, model, url.rstrip("/"), host, port
        self.scope = "local" if host in LOOPBACK else "external"
        self.key_env = s.scrapegraph_api_key_env or DEFAULT_KEY_ENV.get(prov, "")
        if self.key_env and not self.key_env.replace("_", "").isalnum():
            raise BackendConfigError("LE_SCRAPEGRAPH_API_KEY_ENV must be the NAME of an environment variable")
        self.python = s.scrapegraph_python or sys.executable
        self.allowed = [f"{host}:{port}"]
        self.fingerprint = hashlib.sha256(f"{prov}|{model}|{self.base_url}|{s.scrapegraph_ollama_thinking}|{PROMPT_VERSION}|{s.scrapegraph_max_input_chars}".encode()).hexdigest()[:16]

    def describe(self) -> dict:
        return {"backend": "scrapegraph", "provider": self.provider, "model": self.model, "endpoint_host": self.host,
                "scope": self.scope, "python": self.python}


class _Worker:
    """One worker subprocess. Blocking I/O, used through asyncio.to_thread (portable to Windows' selector event loop)."""

    def __init__(self, cfg: ResolvedConfig, s: Settings):
        self.cfg, self.s = cfg, s
        self.proc: Optional[subprocess.Popen] = None
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.stderr: deque = deque(maxlen=30)
        self.home = tempfile.mkdtemp(prefix="le-sg-")
        self.ready: dict = {}

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k.upper() in PASS_ENV}
        env.update(HOME=self.home, USERPROFILE=self.home, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   SCRAPEGRAPHAI_TELEMETRY_ENABLED="false", PYTHONDONTWRITEBYTECODE="1", NO_PROXY="*")
        if self.cfg.key_env and os.environ.get(self.cfg.key_env):
            env[self.cfg.key_env] = os.environ[self.cfg.key_env]
        return env

    def start(self) -> dict:
        try:
            self.proc = subprocess.Popen([self.cfg.python, "-u", str(WORKER_PATH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=self._env(), cwd=self.home)
        except OSError as e:
            raise BackendUnavailable(f"cannot start the ScrapeGraphAI worker with interpreter {self.cfg.python!r}: {e}. "
                                     "Set LE_SCRAPEGRAPH_PYTHON to a Python >= 3.12 that has scrapegraphai installed.", "dependency_unavailable")
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        boot = {"allowed": self.cfg.allowed, "model": {"provider": self.cfg.provider, "model": self.cfg.model,
                                                       "base_url": self.cfg.base_url, "api_key_env": self.cfg.key_env,
                                                       "thinking": self.s.scrapegraph_ollama_thinking}}
        self._write(boot)
        ready = self._read(30.0, "worker start-up")
        if not ready.get("ready"):
            err = ready.get("error", {})
            self.kill()
            raise BackendUnavailable(err.get("message") or "ScrapeGraphAI worker failed to start", err.get("kind", "dependency_unavailable"))
        self.ready = ready
        return ready

    def _pump_stdout(self) -> None:
        limit = self.s.scrapegraph_max_worker_response_bytes
        try:
            while True:
                line = self.proc.stdout.readline(limit + 1)
                if not line:
                    break
                if len(line) > limit and not line.endswith(b"\n"):
                    self.lines.put(b"__OVERSIZE__")
                    break
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _pump_stderr(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            self.stderr.append(sanitize_for_log(raw.decode("utf-8", "replace").rstrip(), 300))

    def _write(self, obj: dict) -> None:
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise BackendUnavailable("ScrapeGraphAI worker process is not running", "worker_crash")

    def _read(self, timeout: float, what: str) -> dict:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.kill()
            raise BackendUnavailable(f"{what} timed out after {timeout:.0f}s (worker killed)", "model_timeout")
        if line is None:
            raise BackendUnavailable("ScrapeGraphAI worker exited unexpectedly: " + " | ".join(list(self.stderr)[-3:]), "worker_crash")
        if line == b"__OVERSIZE__":
            self.kill()
            raise BackendUnavailable("worker response exceeded LE_SCRAPEGRAPH_MAX_WORKER_RESPONSE_BYTES (worker killed)", "invalid_response")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            self.kill()
            raise BackendUnavailable("worker produced a malformed response (worker killed)", "invalid_response")

    def request(self, obj: dict, timeout: float) -> dict:
        self._write(obj)
        return self._read(timeout, f"model request ({obj.get('op')})")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass


class ScrapeGraphBackend:
    name = "scrapegraph"

    def __init__(self, settings: Settings):
        self.s = settings
        self.cfg = ResolvedConfig(settings)
        self.provider, self.model, self.scope = self.cfg.provider, self.cfg.model, self.cfg.scope
        self.fingerprint = self.cfg.fingerprint
        self._idle: "asyncio.Queue[_Worker]" = asyncio.Queue()
        self._sem = asyncio.Semaphore(settings.scrapegraph_workers)
        self._all: list[_Worker] = []
        self._fatal: Optional[BackendUnavailable] = None

    # ---- lifecycle
    async def _new_worker(self) -> _Worker:
        w = _Worker(self.cfg, self.s)
        await asyncio.to_thread(w.start)
        self._all.append(w)
        return w

    async def preflight(self) -> None:
        """Fail early and clearly: interpreter/library present, guards active, telemetry off, model installed.
        Sends no paid request (remote providers are only checked for credentials)."""
        w = await self._new_worker()
        try:
            chk = await asyncio.to_thread(w.request, {"id": "pre1", "op": "selfcheck"}, 30)
            bad = [k for k, v in chk.get("guards", {}).items() if v != "blocked"]
            if bad or chk.get("telemetry_enabled"):
                w.kill()
                raise BackendUnavailable(f"ScrapeGraphAI worker safety self-check FAILED (not blocked: {bad}, "
                                         f"telemetry_enabled={chk.get('telemetry_enabled')}); refusing to run", "unsafe_worker")
            m = await asyncio.to_thread(w.request, {"id": "pre2", "op": "model_check"}, 30)
            if not m.get("ok"):
                err = m.get("error", {})
                raise BackendUnavailable(err.get("message", "model not available"), err.get("kind", "model_unavailable"))
            self.library_version = chk.get("scrapegraphai")
            self.selfcheck = chk
        except BackendUnavailable as e:
            self._fatal = e
            await self.aclose()
            raise
        self._idle.put_nowait(w)

    async def aclose(self) -> None:
        for w in self._all:
            await asyncio.to_thread(w.kill)
        self._all.clear()
        while not self._idle.empty():
            self._idle.get_nowait()

    # ---- extraction
    async def _acquire(self) -> _Worker:
        while not self._idle.empty():
            w = self._idle.get_nowait()
            if w.alive:
                return w
        return await self._new_worker()

    async def _call(self, req: dict, timeout: float) -> dict:
        async with self._sem:
            w = await self._acquire()
            try:
                return await asyncio.to_thread(w.request, req, timeout)
            except BackendUnavailable:
                w.kill()
                raise
            finally:
                if w.alive:
                    self._idle.put_nowait(w)

    async def extract_page(self, page: PageInput, fields: list[str], max_calls: Optional[int] = None) -> PageOutcome:
        """max_calls = what is left of the per-company model-call allowance; it bounds this request AND its retries."""
        if self._fatal:
            raise self._fatal
        allowance = max_calls if max_calls is not None else self.s.scrapegraph_max_calls_per_company
        digest = make_digest(page.html, self.s.scrapegraph_max_input_chars)
        out = PageOutcome(digest_chars=len(digest))
        req = {"id": uuid.uuid4().hex[:8], "op": "extract", "html": digest, "fields": fields, "prompt": build_prompt(fields),
               "limits": {"max_calls": max(1, allowance), "timeout_s": self.s.scrapegraph_request_timeout,
                          "max_output_tokens": self.s.scrapegraph_max_output_tokens, "model_tokens": self.s.scrapegraph_model_tokens}}
        timeout = self.s.scrapegraph_request_timeout + 10
        for attempt in range(self.s.scrapegraph_retries + 1):
            if attempt:
                if out.usage.calls >= allowance:
                    out.error_kind, out.error = out.error_kind or "call_budget", out.error or "per-company model-call allowance used up"
                    return out
                req["limits"] = {**req["limits"], "max_calls": max(1, allowance - out.usage.calls)}
            t0 = time.perf_counter()
            try:
                resp = await self._call(req, timeout)
            except BackendUnavailable as e:
                out.usage.failures += 1
                if e.kind == "model_timeout":
                    out.usage.timeouts += 1
                if e.kind in ("worker_crash", "model_timeout") and attempt < self.s.scrapegraph_retries:
                    out.usage.retries += 1
                    continue
                if e.kind in ("worker_crash", "model_timeout", "invalid_response"):
                    out.error_kind, out.error = e.kind, str(e)[:300]
                    return out
                self._fatal = e
                raise
            out.usage.latency_ms += (time.perf_counter() - t0) * 1000
            out.worker_blocked = list(resp.get("blocked_attempts") or [])
            out.usage.calls += int(resp.get("calls", 0))
            out.usage.tokens_in += int(resp.get("tokens_in", 0))
            out.usage.tokens_out += int(resp.get("tokens_out", 0))
            if resp.get("ok"):
                out.fields, out.rejected = normalize_page(resp.get("values") or {}, fields, digest, page)
                out.error_kind = out.error = ""
                return out
            err = resp.get("error", {})
            kind = err.get("kind", "model_error")
            if kind == "model_unavailable":
                self._fatal = BackendUnavailable(err.get("message", "model unavailable"), kind)
                raise self._fatal
            out.usage.failures += 1
            out.usage.timeouts += kind == "model_timeout"
            out.error_kind, out.error = kind, sanitize_for_log(err.get("message", ""), 300)
            if kind in ("model_timeout", "model_error") and attempt < self.s.scrapegraph_retries:
                out.usage.retries += 1
                continue
            return out
        return out
