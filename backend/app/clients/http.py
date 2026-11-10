import asyncio
import logging

import httpx

log = logging.getLogger("ocr.llm")


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def post_with_retry(
    http: httpx.AsyncClient,
    url: str,
    payload: dict,
    api_key: str,
    timeout: float,
    retries: int = 2,
) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await http.post(url, json=payload, headers=_headers(api_key), timeout=timeout)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise httpx.HTTPStatusError("upstream error", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            retriable = isinstance(exc, httpx.TransportError) or exc.response.status_code >= 500 or exc.response.status_code == 429
            if not retriable or attempt == retries:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]
