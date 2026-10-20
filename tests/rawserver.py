"""Tiny raw-socket HTTP server for tests that need behaviour the fixture app can't express
(slow-drip bodies, challenge pages, redirects to other hosts, odd robots.txt)."""
import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def serve(handler):
    """handler(host, path) -> (status_line, headers, body_bytes) or an async generator yielding body chunks
    after returning ("200 OK", headers, agen)."""
    hits = []

    async def h(r, w):
        try:
            req = (await r.readuntil(b"\r\n\r\n")).decode("latin-1")
            path = req.split()[1]
            host = next((l.split(":", 1)[1].strip().split(":")[0] for l in req.split("\r\n") if l.lower().startswith("host:")), "")
            hits.append((host, path))
            status, headers, body = handler(host, path)
            head = f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            if isinstance(body, (bytes, bytearray)):
                w.write(head.encode() + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await w.drain()
            else:
                w.write(head.encode() + b"Connection: close\r\n\r\n")
                async for chunk in body:
                    w.write(chunk)
                    await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    srv = await asyncio.start_server(h, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        yield port, hits
    finally:
        srv.close()
        await srv.wait_closed()
