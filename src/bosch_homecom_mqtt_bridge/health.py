"""Minimal HTTP health endpoints on asyncio streams (no extra dependency).

``/healthz``: the process answers (always 200). ``/readyz``: 200 only in state ``ready``, else 503.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)
READ_TIMEOUT = 5.0
MAX_HEADER_BYTES = 8192
_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


class HealthServer:
    def __init__(self, port: int, state: Callable[[], str], host: str = "0.0.0.0") -> None:
        self._host = host
        self._port = port
        self._state = state
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port, limit=MAX_HEADER_BYTES)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def respond(self, method: str, path: str) -> tuple[int, str]:
        if method not in ("GET", "HEAD"):
            return 405, "method not allowed"
        if path == "/healthz":
            return 200, "alive"
        if path == "/readyz":
            state = self._state()
            return (200 if state == "ready" else 503), state
        return 404, "not found"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                request_line = await reader.readline()
                while (await reader.readline()).strip():  # skip headers
                    pass
            parts = request_line.decode("latin-1").split()
            status, text = self.respond(parts[0], parts[1].split("?")[0]) if len(parts) >= 2 else (404, "not found")
            body = json.dumps({"status": text}).encode()
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head + (b"" if parts[:1] == ["HEAD"] else body))
            await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass  # a broken or slow client must not disturb the bridge
        finally:
            writer.close()
