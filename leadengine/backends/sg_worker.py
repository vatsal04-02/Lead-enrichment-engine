"""ScrapeGraphAI worker process. Standalone: stdlib + scrapegraphai only (no leadengine imports), so it can run in a
different interpreter / virtualenv than the engine (ScrapeGraphAI 2.x needs Python >= 3.12 and ~400 MB of dependencies).

Protocol: one JSON object per line on stdin -> one JSON object per line on stdout. Everything else (library prints, logs)
goes to stderr. The parent never sends URLs, only page digests.

Safety model (defence in depth; the engine's guarded fetcher already did all page fetching):
  1. network allowlist: socket connect/DNS are refused for everything except the configured model endpoint, and
     subprocess creation is refused (no browser can be launched);
  2. telemetry disabled before import (the library posts page content to a vendor host by default);
  3. the library's tokenizer would download a vocabulary from the internet on first use -> replaced by an offline estimator;
  4. modules for hosted/stealth/proxy features are made unimportable; URL-mode sources and hosted model names are refused;
  5. a callback hard-caps model calls and counts tokens; retries are disabled (the parent decides about retries).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

PROTO_OUT = sys.stdout            # keep the real stdout for the protocol ...
sys.stdout = sys.stderr           # ... and make every stray print() harmless

os.environ["SCRAPEGRAPHAI_TELEMETRY_ENABLED"] = "false"
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
           "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
    os.environ.pop(_v, None)

BLOCKED_ATTEMPTS: list[str] = []
ALLOWED_HOSTS: set[str] = set()      # host names (lowercase) the model client may resolve
ALLOWED_IPS: set[tuple[str, int]] = set()   # (ip, port) learned from guarded resolution of allowed hosts
ALLOWED_PORTS: dict[str, set[int]] = {}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_gai = socket.getaddrinfo


def _record(kind: str, what) -> None:
    BLOCKED_ATTEMPTS.append(f"{kind}:{what}")
    del BLOCKED_ATTEMPTS[:-50]


def _guarded_gai(host, port, *a, **k):
    h = host.decode() if isinstance(host, bytes) else host
    if h is not None and str(h).lower() in ALLOWED_HOSTS:
        res = _real_gai(host, port, *a, **k)
        for fam, _t, _p, _c, sa in res:
            ALLOWED_IPS.add((sa[0], sa[1] if len(sa) > 1 else 0))
            ALLOWED_IPS.add((sa[0], int(port) if isinstance(port, (int, str)) and str(port).isdigit() else 0))
        return res
    if h in LOOPBACK and str(h).lower() in ALLOWED_HOSTS:
        return _real_gai(host, port, *a, **k)
    _record("dns", h)
    raise OSError(f"network access to {h!r} is blocked by the extraction worker")


def _addr_allowed(addr) -> bool:
    if not isinstance(addr, tuple) or len(addr) < 2:
        return False
    ip, port = addr[0], int(addr[1])
    if (ip, port) in ALLOWED_IPS:
        return True
    return ip in LOOPBACK and any(ip in LOOPBACK and port in ports for h, ports in ALLOWED_PORTS.items() if h in LOOPBACK)


def _guarded_connect(self, addr):
    if _addr_allowed(addr):
        return _real_connect(self, addr)
    _record("connect", addr)
    raise OSError(f"connection to {addr!r} is blocked by the extraction worker")


def _guarded_connect_ex(self, addr):
    if _addr_allowed(addr):
        return _real_connect_ex(self, addr)
    _record("connect_ex", addr)
    return 1


def _blocked(*_a, **_k):
    _record("blocked-call", "subprocess/exec")
    raise PermissionError("spawning processes is blocked in the extraction worker")


def install_guards(allowed: list[str]) -> None:
    """allowed: ["host:port", ...]. Called once at start-up; requests cannot widen it."""
    for entry in allowed:
        host, _, port = entry.rpartition(":")
        host = host.strip("[]").lower()
        ALLOWED_HOSTS.add(host)
        ALLOWED_PORTS.setdefault(host, set()).add(int(port))
        if host in LOOPBACK:
            ALLOWED_IPS.update({("127.0.0.1", int(port)), ("::1", int(port))})
            ALLOWED_HOSTS.update({"localhost", "127.0.0.1", "::1"})
            ALLOWED_PORTS.setdefault("localhost", set()).add(int(port))
            ALLOWED_PORTS.setdefault("127.0.0.1", set()).add(int(port))
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_gai
    socket.gethostbyname = lambda h: (_ for _ in ()).throw(OSError("blocked")) if str(h).lower() not in ALLOWED_HOSTS else _real_gai(h, None)[0][4][0]
    import subprocess
    subprocess.Popen.__init__ = _blocked           # no browser, no helper programs
    os.system = _blocked
    for n in ("execv", "execve", "execvp", "spawnv", "spawnl", "startfile", "popen"):
        if hasattr(os, n):
            setattr(os, n, _blocked)
    # hosted / stealth / proxy / search integrations of the library must never load
    # (`fp` and `playwright` are imported by the library at load time and cannot be blocked; the process-spawn and socket
    #  guards above make them unusable instead - see selfcheck())
    for mod in ("undetected_playwright", "undetected_chromedriver", "free_proxy", "ddgs", "scrapegraph_py", "browserbase", "selenium"):
        sys.modules[mod] = None  # type: ignore[assignment]


def offline_tokens(text, *_a, **_k) -> int:
    return max(1, len(str(text)) // 4)


def patch_library() -> dict:
    import scrapegraphai  # noqa: F401
    import scrapegraphai.telemetry.telemetry as tel
    tel.disable_telemetry()
    import scrapegraphai.utils.tokenizer as t1
    import scrapegraphai.utils.tokenizers.tokenizer_openai as t2
    t1.num_tokens_calculus = offline_tokens
    t2.num_tokens_openai = offline_tokens
    for name, mod in list(sys.modules.items()):
        if name and name.startswith("scrapegraphai") and mod is not None:
            for attr in ("num_tokens_calculus", "num_tokens_openai"):
                if hasattr(mod, attr):
                    setattr(mod, attr, offline_tokens)
    from importlib.metadata import version
    return {"scrapegraphai": version("scrapegraphai"), "telemetry_enabled": bool(tel.g_telemetry_enabled)}


class CallBudgetExceeded(Exception):
    pass


def make_usage_guard(max_calls: int):
    from langchain_core.callbacks import BaseCallbackHandler

    class UsageGuard(BaseCallbackHandler):
        raise_error = True            # propagate the exception: this is an enforcement point, not logging

        def __init__(self):
            self.calls = 0
            self.tokens_in = 0
            self.tokens_out = 0

        def on_chat_model_start(self, serialized, messages, **kw):
            self._start()

        def on_llm_start(self, serialized, prompts, **kw):
            self._start()

        def _start(self):
            self.calls += 1
            if self.calls > max_calls:
                raise CallBudgetExceeded(f"model call cap of {max_calls} per request reached")

        def on_llm_end(self, response, **kw):
            try:
                for gen in response.generations:
                    for g in gen:
                        um = getattr(getattr(g, "message", None), "usage_metadata", None)
                        if um:
                            self.tokens_in += int(um.get("input_tokens", 0))
                            self.tokens_out += int(um.get("output_tokens", 0))
                            return
                tu = (response.llm_output or {}).get("token_usage") or {}
                self.tokens_in += int(tu.get("prompt_tokens", 0))
                self.tokens_out += int(tu.get("completion_tokens", 0))
            except Exception:
                pass
    return UsageGuard()


def build_model(cfg: dict, guard):
    prov, name = cfg["provider"], cfg["model"]
    timeout = float(cfg.get("timeout_s", 60))
    if prov == "ollama":
        from langchain_ollama import ChatOllama
        extra = {"reasoning": False} if cfg.get("thinking") == "off" else {}      # think=false: reasoning models must answer, not deliberate
        if cfg.get("model_tokens"):
            extra["num_ctx"] = int(cfg["model_tokens"])          # else Ollama's small default window silently truncates the prompt
        return ChatOllama(model=name, base_url=cfg["base_url"], temperature=0, num_predict=int(cfg["max_output_tokens"]),
                          format="json", callbacks=[guard], client_kwargs={"timeout": timeout}, **extra)
    if prov == "openai":
        from langchain_openai import ChatOpenAI
        key = os.environ.get(cfg.get("api_key_env") or "OPENAI_API_KEY")
        if not key:
            raise _Err("model_unavailable", f"environment variable {cfg.get('api_key_env') or 'OPENAI_API_KEY'} is not set")
        return ChatOpenAI(model=name, base_url=cfg.get("base_url") or None, api_key=key, temperature=0,
                          max_tokens=int(cfg["max_output_tokens"]), timeout=timeout, max_retries=0, callbacks=[guard])
    if prov == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise _Err("model_unavailable", "provider 'anthropic' needs the langchain-anthropic package in the worker interpreter")
        key = os.environ.get(cfg.get("api_key_env") or "ANTHROPIC_API_KEY")
        if not key:
            raise _Err("model_unavailable", f"environment variable {cfg.get('api_key_env') or 'ANTHROPIC_API_KEY'} is not set")
        return ChatAnthropic(model=name, api_key=key, temperature=0, max_tokens=int(cfg["max_output_tokens"]), timeout=timeout,
                             max_retries=0, callbacks=[guard])
    raise _Err("invalid_request", f"unsupported provider {prov!r}")


class _Err(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def classify(e: BaseException) -> tuple[str, str]:
    name, msg = type(e).__name__, str(e)
    low = (name + " " + msg).lower()
    if isinstance(e, CallBudgetExceeded) or "model call cap" in low:
        return "call_budget", msg
    if "timeout" in low or "timed out" in low:
        return "model_timeout", msg
    if "connect" in low or "connection refused" in low or "blocked by the extraction worker" in low or "nodename" in low:
        return "model_unavailable", f"cannot reach the model endpoint: {msg}"
    if "not found" in low and "model" in low or "404" in low and "model" in low:
        return "model_unavailable", f"model not available: {msg}"
    if "401" in low or "403" in low or "authentication" in low or "api key" in low:
        return "model_unavailable", f"model authentication failed: {name}"
    return "model_error", f"{name}: {msg}"[:400]


def make_schema(fields: list[str], spec: dict | None = None):
    """Flat pydantic model. spec maps field -> "str" (default) | "list" (list of strings); nothing else is accepted."""
    from typing import Optional
    from pydantic import BaseModel, create_model
    spec = spec or {}
    for f, kind in spec.items():
        if kind not in ("str", "list"):
            raise _Err("invalid_request", f"unsupported schema type {kind!r} for {f!r}")
    return create_model("CompanyFacts", __base__=BaseModel,
                        **{f: ((Optional[list[str]], None) if spec.get(f) == "list" else (Optional[str], None)) for f in fields})


def do_extract(req: dict, defaults: dict) -> dict:
    cfg = {**defaults, **req.get("model", {})}
    cfg["thinking"] = defaults.get("thinking")                 # never changeable per request
    if cfg["provider"] != defaults["provider"] or cfg["model"] != defaults["model"]:
        raise _Err("invalid_request", "model/provider cannot be changed per request")
    if str(cfg["model"]).startswith("scrapegraphai/"):
        raise _Err("invalid_request", "hosted ScrapeGraphAI models are not allowed")
    lim = req["limits"]
    from scrapegraphai.graphs import SmartScraperGraph
    guard = make_usage_guard(int(lim["max_calls"]))
    model = build_model({**cfg, "timeout_s": lim["timeout_s"], "max_output_tokens": lim["max_output_tokens"],
                         "model_tokens": lim.get("model_tokens")}, guard)
    out = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0.0, "values": None}
    html = req["html"]
    if not html.lstrip().startswith("<"):
        raise _Err("invalid_request", "source must be HTML content, never a URL or path")
    t0 = time.perf_counter()
    try:
        graph = SmartScraperGraph(
            prompt=req["prompt"], source=html,
            config={"llm": {"model_instance": model, "model_tokens": int(lim["model_tokens"])}, "verbose": False,
                    "headless": True, "timeout": float(lim["timeout_s"]), "force": False, "cut": True},
            schema=make_schema(req["fields"], req.get("schema")))
        res = graph.run()
        out["values"] = res.model_dump() if hasattr(res, "model_dump") else res
    finally:
        out["calls"], out["tokens_in"], out["tokens_out"] = guard.calls, guard.tokens_in, guard.tokens_out
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not isinstance(out["values"], dict):
        raise _Err("model_error", "model output was not a JSON object")
    # library answer shape: either the fields directly or {"content": ...} on failure
    return out


def selfcheck() -> dict:
    """Evidence for the preflight/tests that the guards are really active inside this process."""
    probes = {}
    for label, fn in (("public_ip_connect", lambda: socket.create_connection(("93.184.216.34", 80), timeout=1)),
                      ("metadata_ip_connect", lambda: socket.create_connection(("169.254.169.254", 80), timeout=1)),
                      ("dns_lookup", lambda: socket.getaddrinfo("example.com", 80)),
                      ("spawn_process", lambda: __import__("subprocess").Popen(["echo", "x"])),
                      ("launch_browser", lambda: __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright().start()),
                      ("import_stealth_patches", lambda: __import__("undetected_playwright")),
                      ("import_hosted_sdk", lambda: __import__("scrapegraph_py"))):
        try:
            fn()
            probes[label] = "ALLOWED"
        except BaseException as e:  # noqa: BLE001
            probes[label] = "blocked"
    BLOCKED_ATTEMPTS.clear()          # the probes above were deliberate; only unexpected attempts should remain visible
    return {"guards": probes, "allowed_hosts": sorted(ALLOWED_HOSTS)}


def model_check(cfg: dict) -> dict:
    """Is the configured model actually available? Never sends a paid request."""
    if cfg["provider"] != "ollama":
        key_env = cfg.get("api_key_env") or {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(cfg["provider"], "")
        if cfg["provider"] == "anthropic":
            try:
                import langchain_anthropic  # noqa: F401
            except ImportError:
                raise _Err("model_unavailable", "provider 'anthropic' needs the langchain-anthropic package in the worker interpreter")
        if not os.environ.get(key_env):
            raise _Err("model_unavailable", f"environment variable {key_env} is not set")
        return {"checked": "credentials-present-only (no request was sent)"}
    import urllib.request
    try:
        with urllib.request.urlopen(cfg["base_url"].rstrip("/") + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise _Err("model_unavailable", f"cannot reach Ollama at {cfg['base_url']}: {type(e).__name__}: {e}"[:300])
    names = {m.get("name", "") for m in tags.get("models", [])} | {m.get("model", "") for m in tags.get("models", [])}
    want = cfg["model"]
    if want not in names and f"{want}:latest" not in names:
        raise _Err("model_unavailable", f"model {want!r} is not installed in Ollama (installed: {sorted(n for n in names if n)[:10]}); "
                                        f"run `ollama pull {want}`")
    return {"checked": "ollama /api/tags", "installed": True}


def main() -> None:
    cfg_line = sys.stdin.readline()
    boot = json.loads(cfg_line)
    install_guards(boot["allowed"])
    try:
        info = patch_library()
    except BaseException as e:  # noqa: BLE001
        PROTO_OUT.write(json.dumps({"ready": False, "error": {"kind": "model_unavailable",
                                    "message": f"scrapegraphai is not importable in {sys.executable}: {type(e).__name__}: {e}"[:400]}}) + "\n")
        PROTO_OUT.flush()
        return
    defaults = boot["model"]
    PROTO_OUT.write(json.dumps({"ready": True, "python": sys.version.split()[0], **info}) + "\n")
    PROTO_OUT.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        resp = {"id": None}
        try:
            req = json.loads(line)
            resp["id"] = req.get("id")
            op = req.get("op")
            if op == "selfcheck":
                resp.update(ok=True, **selfcheck(), **info, blocked_attempts=list(BLOCKED_ATTEMPTS))
            elif op == "model_check":
                resp.update(ok=True, **model_check(defaults))
            elif op == "extract":
                resp.update(ok=True, **do_extract(req, defaults))
            elif op == "exit":
                break
            else:
                raise _Err("invalid_request", f"unknown op {op!r}")
        except _Err as e:
            resp.update(ok=False, error={"kind": e.kind, "message": e.message})
        except BaseException as e:  # noqa: BLE001
            kind, msg = classify(e)
            resp.update(ok=False, error={"kind": kind, "message": msg})
        resp["blocked_attempts"] = list(BLOCKED_ATTEMPTS)
        PROTO_OUT.write(json.dumps(resp, default=str) + "\n")
        PROTO_OUT.flush()


if __name__ == "__main__":
    main()
