from __future__ import annotations

import asyncio
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes
    content_type: str


class BoundedHttpClient:
    def __init__(self, timeout: float, max_bytes: int, concurrency: int = 3) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._slots = asyncio.Semaphore(max(1, concurrency))

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        async with self._slots:
            return await asyncio.to_thread(self._get_sync, url, headers or {})

    def _get_sync(self, url: str, headers: dict[str, str]) -> Response:
        request = urllib.request.Request(url, headers={"User-Agent": "New-Orayan-Market-Intelligence/0.1", **headers})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise ValueError(f"response exceeds {self.max_bytes} bytes")
            return Response(response.geturl(), int(response.status), body, response.headers.get_content_type())

