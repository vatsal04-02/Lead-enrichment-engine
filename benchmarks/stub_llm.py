"""A local STUB model server speaking the Ollama and OpenAI-compatible HTTP APIs.

!!! It is NOT a language model. It answers with simple rules over the page HTML it is sent (mode "heuristic"), or with
!!! deliberately fabricated values (mode "noisy"). Numbers produced with it measure the plumbing, the grounding filter and the
!!! cost accounting - they say NOTHING about ScrapeGraphAI's or any real model's extraction quality.
Used by tests and by `benchmarks/compare_backends.py --model-mode stub-*`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from typing import Callable, Optional

from bs4 import BeautifulSoup

SOCIAL = {"linkedin_url": "linkedin.com/company", "twitter_url": "twitter.com", "facebook_url": "facebook.com",
          "instagram_url": "instagram.com", "github_url": "github.com", "youtube_url": "youtube.com"}
GENERIC = {"home", "welcome", "homepage", "index", "contact", "about", "about us", "contact us"}
FIELD_LINE = re.compile(r"^- ([a-z_]+):", re.M)


def heuristic_answer(fields: list[str], text: str) -> dict:
    """Rule-based reader over whatever text the library forwards (markdown or HTML). NOT a language model."""
    out: dict = {f: None for f in fields}
    nl = r"(?:\\n|\n)"                                               # the repr'd list the library sends holds literal backslash-n
    text = re.sub(r"-" + nl + r"\s*", "-", text)                    # the library hard-wraps markdown at ~80 columns (also after hyphens): undo it
    text = re.sub(r"(?<!\\n)" + nl + r"(?!" + nl + ")", " ", text)   # a lone line break inside a paragraph is a space; blank lines stay
    g = lambda pat, flags=0: (re.search(pat, text, flags) or [None, None])[1]
    ld_name = g(r'"name"\s*:\s*"([^"]+)"')
    title = g(r"Page title:\s*([^\n<\[]+?)(?:\s*(?:\\n|\n|Meta |Structured|<|$))")
    segs = [x.strip() for x in re.split(r"\s*[|\u2013\u2014:]\s*|\s+-\s+", title or "") if x.strip()]
    keep = [x for x in segs if x.lower() not in GENERIC]
    for f in fields:
        if f == "company_name":
            out[f] = g(r"Meta og:site_name:\s*([^\n<]+?)\s*(?:Meta |Structured|\n|<|$)") or ld_name or (keep[0] if len(segs) > 1 and len(keep) == 1 else None)
        elif f == "description":
            md = g(r"Meta (?:og:)?description:\s*(.+?)\s*(?:Meta |Structured data|\\n\\n|\n\n|<|$)", re.S)
            md = re.sub(r"(?:\\n|\s)+", " ", md).strip() if md else md      # the library hard-wraps markdown lines
            out[f] = g(r'"description"\s*:\s*"([^"]+)"') or md
        elif f == "email":
            out[f] = g(r'"email"\s*:\s*"([^"]+)"') or g(r"mailto:([^)\s'\"<>?]+)") or g(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
        elif f == "phone":
            out[f] = (g(r'"telephone"\s*:\s*"([^"]+)"') or g(r"tel:([^)\s'\"<>]+(?: [\d-]+)*)")
                      or g(r"(\+\d[\d\s().-]{8,}\d)"))
        elif f == "address":
            a = re.search(r'"streetAddress"\s*:\s*"([^"]+)"[^}]*?"addressLocality"\s*:\s*"([^"]+)"(?:[^}]*?"addressRegion"\s*:\s*"([^"]+)")?', text)
            out[f] = ", ".join(x for x in a.groups() if x) if a else None
        elif f in SOCIAL:
            out[f] = g(r"(https?://(?:www\.)?" + re.escape(SOCIAL[f]) + r"[^\s)'\"<>]*)")
        elif f == "founded_year":
            out[f] = g(r"(?:founded|established|since)\s*(?:in\s*)?(\d{4})", re.I)
    return out


def noisy_answer(fields: list[str], digest_html: str, host: str, rate: float = 0.35) -> dict:
    """heuristic answer, but each non-empty value is replaced by an invented one with probability `rate` (deterministic per host+field)."""
    base = heuristic_answer(fields, digest_html)
    for f in fields:
        h = int(hashlib.sha256(f"{host}:{f}".encode()).hexdigest(), 16) % 1000 / 1000
        if h < rate:
            base[f] = {"email": "sales@fabricated-example.org", "phone": "+1 555 000 0000", "company_name": "Fabricated Holdings Ltd",
                       "address": "1 Imaginary Road, Nowhere", "description": "A leading provider of world-class solutions."}.get(f, base[f] or "Invented")
    return base


def method_is_post(m) -> bool:
    return m == "POST"


class StubLLMServer:
    """Threaded HTTP server. `requests` records every call so tests can assert on exactly what the model was sent."""

    def __init__(self, mode: str = "heuristic", models: Optional[list[str]] = None, answer: Optional[Callable] = None,
                 delay_s: float = 0.0, noise_rate: float = 0.35):
        self.mode, self.models, self.answer, self.delay_s, self.noise_rate = mode, models or ["stubmodel:latest"], answer, delay_s, noise_rate
        self.requests: list[dict] = []
        self.http_status = 200
        self.inflight = 0
        self.max_inflight = 0
        self.hang = False
        self.garbage: Optional[str] = None
        self.port = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None

    @property
    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "StubLLMServer":
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            self.port = self._server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(5)
        return self

    def __exit__(self, *a):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ protocol
    def _reply_text(self, body: dict) -> tuple[str, int, int]:
        msgs = body.get("messages", [])
        text = "\n".join(m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content")) for m in msgs)
        fields = FIELD_LINE.findall(text) or []
        i = text.find("WEBSITE CONTENT:")
        digest = text[i:] if i >= 0 else text
        host = (re.search(r'<title>[^<]*', digest) or re.search(r"content=\"([^\"]*)", digest) or [""])[0][:40]
        if self.garbage is not None:
            ans = self.garbage
        elif self.answer:
            ans = json.dumps(self.answer(fields, digest, text))
        elif self.mode == "noisy":
            ans = json.dumps(noisy_answer(fields, digest, host + str(len(digest)), self.noise_rate))
        else:
            ans = json.dumps(heuristic_answer(fields, digest))
        return ans, max(1, len(text) // 4), max(1, len(ans) // 4)

    async def _handle(self, r, w):
        try:
            head = (await r.readuntil(b"\r\n\r\n")).decode("latin-1")
            first = head.split("\r\n")[0].split()
            method, path = first[0], first[1]
            n = next((int(l.split(":")[1]) for l in head.split("\r\n") if l.lower().startswith("content-length")), 0)
            body = json.loads(await r.readexactly(n)) if n else {}
            hdrs = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in head.split("\r\n")[1:] if ":" in l}
            self.requests.append({"method": method, "path": path, "body": body, "t": time.time(), "headers": hdrs})
            if method == "POST":
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            if self.hang and method == "POST":
                await asyncio.sleep(3600)
            if self.delay_s and method == "POST":
                await asyncio.sleep(self.delay_s)
            if path.startswith("/api/tags"):
                payload, ctype = json.dumps({"models": [{"name": m, "model": m} for m in self.models]}).encode(), "application/json"
            elif self.http_status != 200 and method == "POST":
                w.write(f"HTTP/1.1 {self.http_status} Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode())
                await w.drain()
                return
            elif path.startswith("/api/chat"):
                ans, tin, tout = self._reply_text(body)
                payload = (json.dumps({"model": body.get("model"), "created_at": "2026-01-01T00:00:00Z",
                                       "message": {"role": "assistant", "content": ans}, "done": True, "done_reason": "stop",
                                       "prompt_eval_count": tin, "eval_count": tout}) + "\n").encode()
                ctype = "application/x-ndjson"
            elif path.startswith("/v1/chat/completions"):
                ans, tin, tout = self._reply_text(body)
                payload = json.dumps({"id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": body.get("model"),
                                      "choices": [{"index": 0, "message": {"role": "assistant", "content": ans}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout}}).encode()
                ctype = "application/json"
            else:
                payload, ctype = b"{}", "application/json"
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode() + b"\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(payload) + payload)
            await w.drain()
        except Exception:
            pass
        finally:
            if method_is_post(locals().get("method")):
                self.inflight -= 1
            try:
                w.close()
            except Exception:
                pass
