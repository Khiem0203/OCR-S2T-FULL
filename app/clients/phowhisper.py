"""Client PhoWhisper chạy trên vLLM (/v1/audio/transcriptions)."""
import asyncio
import logging

import httpx

log = logging.getLogger("ocr.phowhisper")


class PhoWhisperError(Exception):
    pass


class PhoWhisperClient:
    def __init__(self, http: httpx.AsyncClient, url: str, model: str, api_key: str, language: str, timeout: float,
                 concurrency: int, retries: int = 2):
        self._http = http
        self._url = url
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._language = language
        self._timeout = timeout
        self._retries = retries
        self._sem = asyncio.Semaphore(concurrency)

    async def transcribe(self, wav: bytes) -> str:
        """Chép lời một đoạn wav ≤ 30 giây; lỗi mạng / 5xx thì thử lại, 4xx báo luôn."""
        data = {"model": self._model, "language": self._language, "temperature": "0", "response_format": "json"}
        last = "error"
        for attempt in range(self._retries + 1):
            try:
                async with self._sem:
                    resp = await self._http.post(
                        self._url, files={"file": ("doan.wav", wav, "audio/wav")}, data=data,
                        headers=self._headers, timeout=self._timeout,
                    )
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise PhoWhisperError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return (resp.json().get("text") or "").strip()
            except PhoWhisperError:
                raise
            except Exception as exc:
                last = type(exc).__name__
                if attempt < self._retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise PhoWhisperError(last)
