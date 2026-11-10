import asyncio
import logging

import httpx

from .http import post_with_retry

log = logging.getLogger("ocr.protonx")


class ProtonXClient:
    """Client gọi service ProtonX."""

    def __init__(self, http: httpx.AsyncClient, url: str, timeout: float, concurrency: int):
        self._http = http
        self._url = url
        self._timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)

    async def correct(self, texts: list[str]) -> list[str] | None:
        """Trả text đã sửa; None nếu lỗi."""
        try:
            async with self._sem:
                resp = await post_with_retry(self._http, self._url, {"texts": texts}, "", self._timeout)
            fixed = resp.json()["texts"]
        except Exception as exc:
            log.warning("ProtonX request failed: %s", type(exc).__name__)
            return None
        if not isinstance(fixed, list) or len(fixed) != len(texts) or not all(isinstance(t, str) for t in fixed):
            log.warning("ProtonX returned %s texts for %d inputs", len(fixed) if isinstance(fixed, list) else "no", len(texts))
            return None
        return fixed
