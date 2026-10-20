import asyncio

import pytest

from leadengine.budget import RunMetrics
from leadengine.browser import BrowserRenderer
from leadengine.fetcher import Fetcher
from tests.conftest import make_settings
from tests.rawserver import serve

PAGE = b"""<html><head><script>
 try { new WebSocket('ws://127.0.0.1:%PORT%/ws'); } catch(e) {}
 fetch('http://127.0.0.1:%PORT%/fetch', {mode:'no-cors'}).catch(()=>{});
 new Image().src = 'http://127.0.0.1:%PORT%/img.png';
 const x = new XMLHttpRequest(); x.open('GET','http://localhost:%PORT%/xhr'); try { x.send(); } catch(e) {}
</script></head><body><div id=root></div><script src='/a.js' defer></script><script src='/b.js' defer></script>
<h1>Hi</h1></body></html>"""


async def test_browser_page_cannot_reach_internal_services_by_any_channel():
    """A malicious page tries WebSocket, fetch, <img> and XHR against a 'private' listener.
    Nothing may connect: all browser traffic must go through the guarded fetcher."""
    internal = []

    async def internal_srv(r, w):
        internal.append(1)
        w.close()
    isrv = await asyncio.start_server(internal_srv, "127.0.0.1", 0)
    iport = isrv.sockets[0].getsockname()[1]

    def handler(host, path):
        body = PAGE.replace(b"%PORT%", str(iport).encode()) if path == "/" else b"//js"
        return "200 OK", {"Content-Type": "text/html" if path == "/" else "application/javascript"}, body
    async with serve(handler) as (port, _):
        s = make_settings(host_overrides="evil.fixture.test=127.0.0.1", respect_robots=False, browser_enabled=True)
        f = Fetcher(s)
        br = BrowserRenderer(s, f)
        try:
            r = await br.render(f"http://evil.fixture.test:{port}/", RunMetrics(), None)
            if br.available is False:
                pytest.skip(f"chromium unavailable: {br.last_error}")
            await asyncio.sleep(1.0)
            assert r.html and "Hi" in r.html                 # the legitimate page still renders
            assert internal == [], f"{len(internal)} connection(s) reached the internal listener"
        finally:
            await br.aclose(); await f.aclose(); isrv.close()


async def test_browser_recovers_after_chromium_dies():
    def handler(host, path):
        return "200 OK", {"Content-Type": "text/html"}, b"<html><body><h1>ok</h1></body></html>"
    async with serve(handler) as (port, _):
        s = make_settings(host_overrides="b.fixture.test=127.0.0.1", respect_robots=False, browser_enabled=True)
        f = Fetcher(s)
        br = BrowserRenderer(s, f)
        try:
            r = await br.render(f"http://b.fixture.test:{port}/", RunMetrics(), None)
            if br.available is False:
                pytest.skip(f"chromium unavailable: {br.last_error}")
            await br._browser.close()                          # simulate a crashed/closed browser
            r2 = await br.render(f"http://b.fixture.test:{port}/", RunMetrics(), None)
            assert "ok" in r2.html and not r2.error
        finally:
            await br.aclose(); await f.aclose()
