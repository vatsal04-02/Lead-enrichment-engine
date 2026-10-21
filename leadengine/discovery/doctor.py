"""Read-only environment check for the local-model path. It never downloads or pulls a model and never contacts anything except a
loopback Ollama endpoint and the configured interpreter (a local subprocess).  `smoke=True` additionally asks the *already
installed* model one question about a tiny built-in synthetic page (local inference only)."""
from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_MODEL = "qwen3:4b"
DEFAULT_URL = "http://127.0.0.1:11434"


def _item(name: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"check": name, "ok": ok, "detail": detail, "fix": fix}


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_interpreter(python: str) -> list[dict]:
    out = []
    try:
        r = subprocess.run([python, "-c", "import sys,importlib.metadata as m;print(sys.version_info[0],sys.version_info[1],m.version('scrapegraphai'))"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [_item("scrapegraphai interpreter", False, f"cannot run {python!r}: {e}",
                      "set LE_SCRAPEGRAPH_PYTHON (or --sg-python) to a Python >= 3.12 that has scrapegraphai installed")]
    if r.returncode != 0:
        return [_item("scrapegraphai library", False, f"not importable by {python!r}", f'{python} -m pip install "scrapegraphai>=2.3.1,<2.4"')]
    major, minor, ver = r.stdout.split()
    out.append(_item("python >= 3.12 (library interpreter)", (int(major), int(minor)) >= (3, 12), f"{major}.{minor}"))
    ok_ver = ver.startswith("2.3.")
    out.append(_item("scrapegraphai version", ok_ver, f"{ver} (audited range: 2.3.x)", "" if ok_ver else 'pip install "scrapegraphai>=2.3.1,<2.4"'))
    return out


def check_ollama(base_url: str, model: str, timeout: float = 3.0) -> list[dict]:
    if not is_loopback(base_url):
        return [_item("ollama endpoint", False, f"{base_url} is not a loopback address; doctor only checks a local Ollama", "use http://127.0.0.1:11434")]
    items = [_item("ollama binary on PATH", bool(shutil.which("ollama")), shutil.which("ollama") or "not found (not required if the server runs elsewhere on this machine)")]
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout) as r:      # list installed models: read-only
            names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError) as e:
        items.append(_item("ollama server reachable", False, f"{base_url}: {type(e).__name__}", "start Ollama (`ollama serve`)"))
        return items
    items.append(_item("ollama server reachable", True, f"{base_url} ({len(names)} model(s) installed)"))
    have = model in names or (":" not in model and f"{model}:latest" in names)
    items.append(_item(f"model {model} installed", have, "installed" if have else f"not installed; installed: {names}",
                       "" if have else f"ollama pull {model}   (not run automatically)"))
    return items


def smoke_test(python: str, model: str, base_url: str) -> dict:
    from ..backends import make_llm_backend
    from ..backends.base import PageInput
    from ..config import Settings
    import asyncio

    async def go():
        s = Settings(extraction_backend="scrapegraph", scrapegraph_enabled=True, scrapegraph_provider="ollama", scrapegraph_model=model,
                     scrapegraph_base_url=base_url, scrapegraph_python=python, scrapegraph_request_timeout=180,
                     scrapegraph_ollama_thinking="off" if model.startswith("qwen3") else "default", user_agent="doctor/1 (+local; local@localhost)")
        b = make_llm_backend(s)
        try:
            await b.preflight()
            html = ("<html><head><title>Acme Automation | Workflow automation agency</title></head><body><h1>Acme Automation</h1>"
                    "<p>We build workflow automations for small businesses.</p><p>Email hello@acme-automation.test</p></body></html>")
            out = await b.extract_page(PageInput("https://acme-automation.test/", "home", html, "smoke"), ["company_name", "email"])
            return {"states": {f: o.state for f, o in out.fields.items()}, "values": {f: o.value for f, o in out.fields.items()},
                    "tokens": [out.usage.tokens_in, out.usage.tokens_out], "latency_ms": out.usage.latency_ms, "error": out.error}
        finally:
            await b.aclose()
    try:
        r = asyncio.run(go())
        ok = not r["error"] and any(s == "extracted" for s in r["states"].values())
        return _item("smoke extraction (synthetic page, local model)", ok, json.dumps(r), "" if ok else "the model did not return grounded values; see detail")
    except Exception as e:
        return _item("smoke extraction (synthetic page, local model)", False, f"{type(e).__name__}: {e}")


def run_doctor(*, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_URL, python: str = "", smoke: bool = False) -> dict:
    python = python or sys.executable
    items = [_item("host python", True, sys.version.split()[0])]
    items += check_interpreter(python)
    items += check_ollama(base_url, model)
    if smoke:
        items.append(smoke_test(python, model, base_url) if all(i["ok"] for i in items if i["check"] != "ollama binary on PATH")
                     else _item("smoke extraction", False, "skipped: fix the failing checks above first"))
    ready = all(i["ok"] for i in items if i["check"] != "ollama binary on PATH")
    return {"ready_for_scrapegraph_ollama": ready, "model": model, "base_url": base_url, "interpreter": python, "checks": items,
            "if_not_ready": "the run falls back to the in-house engine and says so in the report" if not ready else ""}
